"""実績フィードバックissue化の共通基盤(issue #39起点。issue #92でcycle単位に一般化)。

このモジュール自身は「何を仮説にするか」を一切知らない。`doci.performance_report`が
1チャンネル1サイクル分の候補（fingerprint・corner毎のhypothesis_keys・issue本文）を
組み立て、このモジュールが持つ重複防止・週次レート制御・排他ロック・GitHub I/Oだけを
再利用する。

既定はdry-run（候補の外部状態を一切変更しない）。`submit_candidate(..., apply=True)`
を明示した場合だけ`gh issue create`を呼ぶ。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import config
from .channel import ChannelSpec
from .gh_cli import run_gh as _run_gh

_FEEDBACK_MARKER_RE = re.compile(r"<!--\s*doci-feedback:([0-9a-f]{16})\s*-->")
_CHANNEL_MARKER_RE = re.compile(
    r"<!--\s*doci-feedback-channel:([A-Za-z0-9_-]+)\s*-->"
)
_HYPOTHESIS_MARKER_RE = re.compile(
    r"<!--\s*doci-feedback-hypothesis:([0-9a-f]{16})\s*-->"
)
_LOCK_WAIT_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.25
_ISSUE_LABELS = ("enhancement", "feedback")
# gh issue list の --json body は /search/issues と異なり結果整合ラグがないREST/
# GraphQL経由。件数がこの上限に達したら安全側で停止する(継続週3件上限なら数年分)。
_ISSUE_LIST_LIMIT = 1000


# --- パス ---


def _history_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / "feedback_issues.jsonl"


def _lock_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / ".feedback_issues.lock"


def _read_records(spec: ChannelSpec) -> list[dict]:
    from . import performance

    return performance._read_jsonl(_history_path(spec))


# --- 純関数（副作用なし） ---


def hypothesis_hash(hypothesis_key: str) -> str:
    """hypothesis_keyをissue本文の不可視マーカー用にハッシュする(生の内容を漏らさないため)。"""
    return hashlib.sha256(hypothesis_key.encode("utf-8")).hexdigest()[:16]


def channel_marker(channel_id: str) -> str:
    return f"<!-- doci-feedback-channel:{channel_id} -->"


def feedback_marker(fp: str) -> str:
    return f"<!-- doci-feedback:{fp} -->"


def hypothesis_marker(hypothesis_key: str) -> str:
    return f"<!-- doci-feedback-hypothesis:{hypothesis_hash(hypothesis_key)} -->"


# --- 上限・重複判定（ローカルJSONLのみ参照） ---


def _weekly_created_count(records: list[dict], now: datetime) -> int:
    threshold = now - timedelta(days=7)
    count = 0
    for row in records:
        if row.get("status") != "created":
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("ts")))
            is_recent = ts >= threshold
        except (ValueError, TypeError):
            continue
        if is_recent:
            count += 1
    return count


def _recent_hypothesis_keys_from_records(
    records: list[dict], now: datetime
) -> set[str]:
    threshold = now - timedelta(days=config.FEEDBACK_ISSUES_HYPOTHESIS_COOLDOWN_DAYS)
    keys: set[str] = set()
    for row in records:
        if row.get("status") != "created":
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("ts")))
            is_recent = ts >= threshold
        except (ValueError, TypeError):
            continue
        if is_recent:
            keys.update(row.get("hypothesis_keys") or [])
    return keys


def recent_hypothesis_keys(spec: ChannelSpec, *, now: datetime | None = None) -> set[str]:
    """直近{cooldown}日以内に'created'状態のissueへ使われたhypothesis_keyの集合を返す。

    呼び出し側(performance_report.py)は、corner毎の候補hypothesis_keyがこの
    集合に含まれる場合、そのcornerの「新仮説」節をissue本文から省く
    （cooldown中に同じ仮説を繰り返し提案しないため）。`submit_candidate`も
    同じ判定をローカルJSONLだけで即座に行う防御的チェックとして内部で使う。
    """
    reference = now or datetime.now(timezone.utc)
    return _recent_hypothesis_keys_from_records(_read_records(spec), reference)


def _local_terminal_record(records: list[dict], fp: str) -> dict | None:
    for row in reversed(records):
        if row.get("fingerprint") == fp and row.get("status") in (
            "created",
            "duplicate",
        ):
            return row
    return None


# --- GitHub I/O（--apply 経路のみ到達） ---


def _issue_summary(row: dict) -> dict:
    return {
        "number": int(row["number"]),
        "url": str(row.get("url") or ""),
        "state": str(row.get("state") or "").upper(),
    }


def _find_duplicate(
    repository: str,
    fp: str,
    hypothesis_hashes: set[str],
    *,
    channel_id: str,
    cooldown_days: int,
    now: datetime,
) -> tuple[str | None, dict | None, int]:
    """open/closed両方を対象に、本文中のfingerprint/hypothesisマーカーで
    既存issueを検索する。("kind", issue, remote_weekly_count) を返す。kindは
    "duplicate_remote"（fingerprint完全一致）/
    "duplicate_hypothesis_remote"（同一仮説がcooldown内に作成済み）/
    None（重複なし）。remote_weekly_countは直近7日にこのチャンネルがこの
    ツールで作成した（＝doci-feedback-channelマーカーがchannel_idと一致する）
    issue件数。

    複数チャンネルが同一repositoryを共有する構成を想定し、週次上限は
    チャンネル単位で数える（他チャンネルの発行がこのチャンネルの枠を
    食い潰さないようにするため）。

    `gh api search/issues` (Search API) は結果整合で数秒〜数分のインデックス
    遅延があり、直前に作成が成功していても未検出になり得るため使わない。
    `gh issue list` は通常のissue一覧取得APIを叩くため即時反映される。
    """
    raw = _run_gh(
        [
            "issue",
            "list",
            "--repo",
            repository,
            "--label",
            "feedback",
            "--state",
            "all",
            "--limit",
            str(_ISSUE_LIST_LIMIT),
            "--json",
            "number,url,state,body,createdAt",
        ]
    )
    rows = json.loads(raw or "[]")
    if not isinstance(rows, list):
        raise RuntimeError("GitHub Issue一覧の形式が不正です")
    if len(rows) >= _ISSUE_LIST_LIMIT:
        raise RuntimeError(
            f"feedbackラベルのGitHub Issueが{_ISSUE_LIST_LIMIT}件以上あり、"
            "一覧が切り詰められた可能性があります。重複作成を避けるため自動作成を停止します"
        )

    weekly_threshold = now - timedelta(days=7)
    remote_weekly_count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = str(row.get("body") or "")
        if not _FEEDBACK_MARKER_RE.search(body):
            continue
        channel_match = _CHANNEL_MARKER_RE.search(body)
        if not channel_match or channel_match.group(1) != channel_id:
            continue
        try:
            created_at = datetime.fromisoformat(
                str(row.get("createdAt")).replace("Z", "+00:00")
            )
            is_recent = created_at >= weekly_threshold
        except (ValueError, TypeError):
            continue
        if is_recent:
            remote_weekly_count += 1

    for row in rows:
        if not isinstance(row, dict):
            continue
        body = str(row.get("body") or "")
        match = _FEEDBACK_MARKER_RE.search(body)
        if match and match.group(1) == fp:
            return "duplicate_remote", _issue_summary(row), remote_weekly_count

    if hypothesis_hashes:
        threshold = now - timedelta(days=cooldown_days)
        for row in rows:
            if not isinstance(row, dict):
                continue
            body = str(row.get("body") or "")
            matched = {m.group(1) for m in _HYPOTHESIS_MARKER_RE.finditer(body)}
            if not matched & hypothesis_hashes:
                continue
            try:
                created_at = datetime.fromisoformat(
                    str(row.get("createdAt")).replace("Z", "+00:00")
                )
                is_recent = created_at >= threshold
            except (ValueError, TypeError):
                continue
            if is_recent:
                return (
                    "duplicate_hypothesis_remote",
                    _issue_summary(row),
                    remote_weekly_count,
                )
    return None, None, remote_weekly_count


def _create_issue(repository: str, title: str, body: str) -> tuple[int, str]:
    args = ["issue", "create", "--repo", repository, "--title", title]
    for label in _ISSUE_LABELS:
        args += ["--label", label]
    args += ["--body-file", "-"]
    url = _run_gh(args, stdin=body)
    try:
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError as exc:
        raise RuntimeError(f"作成Issue番号を取得できませんでした: {url[:200]}") from exc
    return number, url


# --- 永続化・排他 ---


def _append_record(spec: ChannelSpec, row: dict) -> None:
    path = _history_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    path = _lock_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_TIMEOUT_SECONDS
    with path.open("a+", encoding="utf-8") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"feedback issue処理lockを"
                        f"{_LOCK_WAIT_TIMEOUT_SECONDS:g}秒以内に取得できません"
                    )
                time.sleep(_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _record_row(
    spec: ChannelSpec,
    candidate: dict,
    *,
    status: str,
    reason: str = "",
    issue: dict | None = None,
) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "schema_version": 2,
        "feedback_id": f"fb-{candidate['fingerprint']}",
        "fingerprint": candidate["fingerprint"],
        "channel": spec.id,
        "hypothesis_keys": list(candidate.get("hypothesis_keys") or []),
        "issue_number": (issue or {}).get("number"),
        "issue_url": (issue or {}).get("url"),
        "status": status,
        "reason": reason,
    }


# --- オーケストレーション ---


def submit_candidate(
    spec: ChannelSpec,
    candidate: dict,
    *,
    apply: bool = False,
    max_issues: int | None = None,
) -> dict:
    """候補(candidate)を重複防止・週次上限・ロック・GitHub I/Oを通してissue化する。

    `candidate`は呼び出し側が組み立てた
    `{fingerprint, hypothesis_keys, title, body}`を持つ辞書。このモジュールは
    その中身（何がissueの対象か）を一切解釈せず、機構だけを提供する。
    既定はdry-run（`candidate`をそのまま返すだけで外部状態を変更しない）。
    """
    if not apply:
        return {
            "mode": "dry-run",
            "channel": spec.id,
            "candidate": candidate,
            "skip_reason": "",
        }

    repository = spec.pipeline_get("feedback_repository", "")
    if not repository:
        return {
            "mode": "apply",
            "channel": spec.id,
            "candidate": candidate,
            "created": None,
            "skip_reason": "no_repository",
        }

    limit = config.FEEDBACK_ISSUES_MAX_PER_RUN if max_issues is None else max_issues
    if limit <= 0:
        return {
            "mode": "apply",
            "channel": spec.id,
            "candidate": candidate,
            "created": None,
            "skip_reason": "run_limit_reached",
        }

    with _operation_lock(spec):
        records = _read_records(spec)
        now = datetime.now(timezone.utc)

        local_hit = _local_terminal_record(records, candidate["fingerprint"])
        if local_hit is not None:
            return {
                "mode": "apply",
                "channel": spec.id,
                "candidate": candidate,
                "created": None,
                "skip_reason": f"local_{local_hit['status']}",
            }

        local_weekly_count = _weekly_created_count(records, now)
        if local_weekly_count >= config.FEEDBACK_ISSUES_MAX_PER_WEEK:
            return {
                "mode": "apply",
                "channel": spec.id,
                "candidate": candidate,
                "created": None,
                "skip_reason": "weekly_limit_reached",
            }

        candidate_hypothesis_keys = set(candidate.get("hypothesis_keys") or [])
        if candidate_hypothesis_keys & _recent_hypothesis_keys_from_records(
            records, now
        ):
            # ローカルJSONLだけで即座に分かる防御的チェック。呼び出し側が
            # cooldown中のhypothesis_keyを候補へ含めてしまった場合の保険
            # （本来はrecent_hypothesis_keys()で候補構築時に除外される想定）。
            return {
                "mode": "apply",
                "channel": spec.id,
                "candidate": candidate,
                "created": None,
                "skip_reason": "duplicate_hypothesis",
            }

        hypothesis_hashes = {
            hypothesis_hash(key) for key in candidate.get("hypothesis_keys") or []
        }
        kind, existing, remote_weekly_count = _find_duplicate(
            repository,
            candidate["fingerprint"],
            hypothesis_hashes,
            channel_id=spec.id,
            cooldown_days=config.FEEDBACK_ISSUES_HYPOTHESIS_COOLDOWN_DAYS,
            now=now,
        )
        if kind is not None:
            # duplicate_remote(fingerprint完全一致)は恒久的に同じ結論になるため
            # ローカルでも永続ブロックしてよいが、duplicate_hypothesis_remoteは
            # cooldown経過後に許可されるべきなので _local_terminal_record の
            # 対象("created"/"duplicate")に含めない別statusで記録する。
            record_status = "duplicate" if kind == "duplicate_remote" else "duplicate_hypothesis"
            _append_record(
                spec,
                _record_row(
                    spec,
                    candidate,
                    status=record_status,
                    reason=f"{kind}: existing issue #{existing['number']}",
                    issue=existing,
                ),
            )
            return {
                "mode": "apply",
                "channel": spec.id,
                "candidate": candidate,
                "created": None,
                "skip_reason": kind,
                "existing_issue": existing,
            }

        if max(local_weekly_count, remote_weekly_count) >= config.FEEDBACK_ISSUES_MAX_PER_WEEK:
            return {
                "mode": "apply",
                "channel": spec.id,
                "candidate": candidate,
                "created": None,
                "skip_reason": "weekly_limit_reached",
            }

        _append_record(spec, _record_row(spec, candidate, status="creating"))
        number, url = _create_issue(repository, candidate["title"], candidate["body"])
        issue = {"number": number, "url": url}
        _append_record(
            spec, _record_row(spec, candidate, status="created", issue=issue)
        )
        return {
            "mode": "apply",
            "channel": spec.id,
            "candidate": candidate,
            "created": issue,
            "skip_reason": "",
        }
