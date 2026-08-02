"""AnalyticsのdecisionからfeedbackラベルのGitHub issueを安全に生成する(issue #39)。

既定はdry-run（候補JSONと予定タイトル・本文の表示のみ、外部状態を変更しない）。
`--apply` を明示した場合だけ `gh issue create` を呼ぶ。判定は
`doci.performance.build_decision()` が既に保存した `performance_decision.json` を
読むだけで、このモジュール自身は `build_decision()` を呼ばない（呼ぶと
performance_decision.json の上書きや history.jsonl への追記という副作用が
発生し、dry-runの無副作用性が壊れるため）。

定期実行する場合は週1回程度を推奨する。作成件数は
FEEDBACK_ISSUES_MAX_PER_RUN（既定1）・FEEDBACK_ISSUES_MAX_PER_WEEK（既定3）で
環境変数から上書き可能な上限を持つ。
"""
from __future__ import annotations

import argparse
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

from . import channel, config, performance
from .channel import ChannelSpec
from .youtube_review import _run_gh

_FEEDBACK_MARKER_RE = re.compile(r"<!--\s*doci-feedback:([0-9a-f]{16})\s*-->")
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


# --- 読み取り専用入力（performance.build_decision は呼ばない） ---


def _load_decision(spec: ChannelSpec) -> dict | None:
    path = performance._decision_path(spec)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _latest_snapshot_at(spec: ChannelSpec) -> str | None:
    rows = performance._read_jsonl(performance._snapshot_path(spec))
    if not rows:
        return None
    return rows[-1].get("collected_at")


def _read_records(spec: ChannelSpec) -> list[dict]:
    return performance._read_jsonl(_history_path(spec))


# --- 純関数（副作用なし） ---


def fingerprint(decision: dict) -> str:
    """仮説の安定内容だけをハッシュする。decision_id は snapshot_at を含み
    指標が僅かに動くだけで変わるため使わない（同一仮説の再検出に使えなくなる）。"""
    seed = {
        "channel": decision.get("channel"),
        "corner": decision.get("corner"),
        "metric": decision.get("metric"),
        "format_cohort": decision.get("format_cohort"),
        "positive_traits": sorted(decision.get("positive_traits") or []),
        "negative_traits": sorted(decision.get("negative_traits") or []),
        "top_video_ids": sorted(decision.get("top_video_ids") or []),
        "bottom_video_ids": sorted(decision.get("bottom_video_ids") or []),
    }
    return hashlib.sha256(
        json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _hypothesis_key(decision: dict) -> str:
    traits = sorted(
        (decision.get("positive_traits") or [])
        + (decision.get("negative_traits") or [])
    )
    return f"{decision.get('corner')}|{decision.get('metric')}|{','.join(traits)}"


def _hypothesis_hash(hypothesis_key: str) -> str:
    return hashlib.sha256(hypothesis_key.encode("utf-8")).hexdigest()[:16]


def _primary_trait(decision: dict) -> str:
    positive = decision.get("positive_traits") or []
    negative = decision.get("negative_traits") or []
    return (positive or negative or [""])[0]


def _issue_title(decision: dict, fp: str) -> str:
    corner = decision.get("corner") or "unknown"
    trait = _primary_trait(decision) or "形式仮説"
    return f"[feedback] {corner}: {trait} の形式仮説 ({fp[:8]})"


def _issue_body(decision: dict, fp: str) -> str:
    top = ", ".join(f"`{v}`" for v in decision.get("top_video_ids") or [])
    bottom = ", ".join(f"`{v}`" for v in decision.get("bottom_video_ids") or [])
    positive = ", ".join(decision.get("positive_traits") or []) or "なし"
    negative = ", ".join(decision.get("negative_traits") or []) or "なし"
    trait = _primary_trait(decision) or "(不明)"
    eligible = decision.get("eligible_video_ids") or []
    hypothesis_hash = _hypothesis_hash(_hypothesis_key(decision))
    return f"""\
<!-- doci-feedback:{fp} -->
<!-- doci-feedback-hypothesis:{hypothesis_hash} -->
## 観測

{decision.get('corner')} cornerの同一cohort（{decision.get('format_cohort')}）内で、\
指標 {decision.get('metric')} の相対上位群と相対下位群を単一の形式特性で分離できた。

## 根拠

- fingerprint: `{fp}`
- decision: `{decision.get('decision_id')}`
- 実績snapshot: {decision.get('snapshot_at')}
- 指標: {decision.get('metric')}（対象{len(eligible)}本）
- 比較cohort: {decision.get('corner')} / {decision.get('format_cohort')}
- 相対上位群: {top or 'なし'}
- 相対下位群: {bottom or 'なし'}

## 改善仮説（因果と断定しない）

- 相対上位群に固有の形式特性: {positive}
- 相対下位群に固有の形式特性: {negative}
- これは相関にもとづく実験仮説であり、因果の証明ではない。

## 1回に変更する形式変数

- `{trait}` のみ。他の形式変数は変えない。
- 上位動画の題材・タイトルは再利用せず、topic cooldownを常に優先して新しい題材を選ぶ。

## 完了条件・再評価条件

- この仮説を適用した新規動画1本が評価閾値（Analytics viewsが十分な水準に到達）へ到達する。
- 到達後の新しいsnapshotで同一指標 {decision.get('metric')} を再評価し、結果をこのissueへ記録して閉じる。
- 適用前にdecisionが `{decision.get('decision_id')}` から変わった場合は、このissueの仮説を見直す。
"""


def build_candidate(
    spec: ChannelSpec,
    decision: dict | None,
    *,
    corner_key: str | None = None,
) -> tuple[dict | None, str]:
    """作成条件を順に検証し (candidate, skip_reason) を返す。

    candidate は None でない場合 fingerprint/decision_id/source_snapshot_at/
    title/body/hypothesis_key を持つ。"""
    if decision is None:
        return None, "no_decision"
    if corner_key and decision.get("corner") != corner_key:
        return None, "corner_mismatch"
    latest_snapshot_at = _latest_snapshot_at(spec)
    if latest_snapshot_at != decision.get("snapshot_at"):
        return None, "stale_decision"
    if decision.get("status") != "active":
        return None, str(decision.get("status") or "unknown_status")
    analytics = (decision.get("source_status") or {}).get("analytics") or {}
    if analytics.get("available") is not True:
        return None, "analytics_unavailable"
    fp = fingerprint(decision)
    return (
        {
            "fingerprint": fp,
            "decision_id": decision.get("decision_id"),
            "source_snapshot_at": decision.get("snapshot_at"),
            "hypothesis_key": _hypothesis_key(decision),
            "title": _issue_title(decision, fp),
            "body": _issue_body(decision, fp),
            "top_video_ids": decision.get("top_video_ids") or [],
            "bottom_video_ids": decision.get("bottom_video_ids") or [],
        },
        "",
    )


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


def _recent_same_hypothesis(
    records: list[dict], hypothesis_key: str, now: datetime
) -> bool:
    threshold = now - timedelta(days=config.FEEDBACK_ISSUES_HYPOTHESIS_COOLDOWN_DAYS)
    for row in records:
        if row.get("status") != "created":
            continue
        if row.get("hypothesis_key") != hypothesis_key:
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("ts")))
            is_recent = ts >= threshold
        except (ValueError, TypeError):
            continue
        if is_recent:
            return True
    return False


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
    hypothesis_hash: str,
    *,
    cooldown_days: int,
    now: datetime,
) -> tuple[str | None, dict | None, int]:
    """open/closed両方を対象に、本文中のfingerprint/hypothesisマーカーで
    既存issueを検索する。("kind", issue, remote_weekly_count) を返す。kindは
    "duplicate_remote"（fingerprint完全一致）/
    "duplicate_hypothesis_remote"（同一仮説がcooldown内に作成済み）/
    None（重複なし）。remote_weekly_countは直近7日にこのツールが作成した
    （＝doci-feedbackマーカーを持つ）issue件数。

    `gh api search/issues` (Search API) は結果整合で数秒〜数分のインデックス
    遅延があり、直前に作成が成功していても未検出になり得るため使わない。
    `gh issue list` は通常のissue一覧取得APIを叩くため即時反映される。
    fingerprintマーカー自体が一意な識別子なので作成者(author)では絞り込まない
    （ローカル実行とCI/botなど実行アカウントが異なると誤って見逃すため）。

    ローカルJSONL履歴（feedback_issues.jsonl）が永続しない実行環境（ephemeral
    なCI等）では週次上限・仮説cooldownのローカル判定が常に無効になるため、
    このリモート一覧取得を週次上限・仮説cooldown両方の正本として使う。
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
        # feedbackラベルは人手作成issue(#36-#38等)にも付くため、このツール由来
        # (fingerprintマーカーを持つ)issueだけを週次カウント対象にする。
        if not _FEEDBACK_MARKER_RE.search(body):
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

    threshold = now - timedelta(days=cooldown_days)
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = str(row.get("body") or "")
        match = _HYPOTHESIS_MARKER_RE.search(body)
        if not match or match.group(1) != hypothesis_hash:
            continue
        try:
            created_at = datetime.fromisoformat(
                str(row.get("createdAt")).replace("Z", "+00:00")
            )
            is_recent = created_at >= threshold
        except (ValueError, TypeError):
            continue
        if is_recent:
            return "duplicate_hypothesis_remote", _issue_summary(row), remote_weekly_count
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
    candidate: dict, *, status: str, reason: str = "", issue: dict | None = None
) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "feedback_id": f"fb-{candidate['fingerprint']}",
        "fingerprint": candidate["fingerprint"],
        "decision_id": candidate["decision_id"],
        "source_snapshot_at": candidate["source_snapshot_at"],
        "hypothesis_key": candidate["hypothesis_key"],
        "issue_number": (issue or {}).get("number"),
        "issue_url": (issue or {}).get("url"),
        "status": status,
        "reason": reason,
    }


# --- オーケストレーション ---


def run(
    spec: ChannelSpec,
    *,
    corner_key: str | None = None,
    apply: bool = False,
    max_issues: int | None = None,
) -> dict:
    decision = _load_decision(spec)
    candidate, skip_reason = build_candidate(spec, decision, corner_key=corner_key)
    if candidate is None:
        return {
            "mode": "apply" if apply else "dry-run",
            "channel": spec.id,
            "candidate": None,
            "skip_reason": skip_reason,
        }

    if not apply:
        return {
            "mode": "dry-run",
            "channel": spec.id,
            "candidate": candidate,
            "skip_reason": "",
        }

    repository = spec.publish.youtube.review.repository
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

        if _recent_same_hypothesis(records, candidate["hypothesis_key"], now):
            return {
                "mode": "apply",
                "channel": spec.id,
                "candidate": candidate,
                "created": None,
                "skip_reason": "duplicate_hypothesis",
            }

        kind, existing, remote_weekly_count = _find_duplicate(
            repository,
            candidate["fingerprint"],
            _hypothesis_hash(candidate["hypothesis_key"]),
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

        _append_record(
            spec, _record_row(candidate, status="creating")
        )
        number, url = _create_issue(repository, candidate["title"], candidate["body"])
        issue = {"number": number, "url": url}
        _append_record(
            spec, _record_row(candidate, status="created", issue=issue)
        )
        return {
            "mode": "apply",
            "channel": spec.id,
            "candidate": candidate,
            "created": issue,
            "skip_reason": "",
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analytics decisionからfeedback issueを生成（既定はdry-run）"
    )
    parser.add_argument("--channel", required=True)
    parser.add_argument("--corner")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="GitHub issueを実際に作成する（未指定時はdry-runで候補表示のみ）",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help="1回の実行で作成する最大件数（既定はFEEDBACK_ISSUES_MAX_PER_RUN）",
    )
    args = parser.parse_args()
    spec = channel.load(args.channel)
    result = run(
        spec,
        corner_key=args.corner,
        apply=args.apply,
        max_issues=args.max_issues,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
