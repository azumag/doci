"""YouTube攻略Chの主題ガードとGitHub Issue確認フロー。

自動公開は厳格な企画項目だけで判定し、時間経過は一切参照しない。
限定公開動画はfsync済みoutboxへ登録してからGitHub Issueへ結び、明示ラベルが
付いた場合だけ処理する。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from .channel import ChannelSpec, YouTubeReviewSpec

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_GH_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_CYCLE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_LOCK_WAIT_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.25
_MAX_ISSUE_LIST_PAGES = 10
_MAX_RETRY_PLAN_FILES = 64
_OUTBOX_COMPACT_MIN_EVENTS = 100
_MARKER_RE = re.compile(
    r"<!--\s*doci-youtube-review\s+video_id=([A-Za-z0-9_-]{6,20})\s*-->"
)
_SECRET_RE = re.compile(
    r"(?:github_pat_|gh[opsu]_|sk-ant-|ya29\.)[A-Za-z0-9_.-]{12,}"
    r"|Bearer\s+\S+",
    re.IGNORECASE,
)
_SUBJECT_REJECTION_MARKERS = (
    "youtubeとは関係ない",
    "youtubeと関係ない",
    "youtubeとは無関係",
    "youtubeと無関係",
    "youtubeが主題ではない",
    "youtubeは主題ではない",
    "youtube制作者向けではない",
    "youtube向けではない",
    "youtube運用向けではない",
    "youtube制作者は対象外",
    "youtube制作者を対象としない",
    "youtubeが対象ではない",
    "youtubeショートとは関係ない",
    "youtubeショートと関係ない",
    "youtube動画とは関係ない",
    "youtube動画と関係ない",
    "youtube動画とは無関係",
)
_YOUTUBE_CONTEXT_MARKERS = (
    "youtube",
    "ショート",
    "shorts",
)
_YOUTUBE_OPERATION_MARKERS = (
    "ctr",
    "サムネ",
    "クリック率",
    "視聴維持",
    "維持率",
    "離脱",
    "平均視聴時間",
    "再生数",
    "登録者",
    "インプレッション",
    "youtube studio",
    "アナリティクス",
    "チャンネル登録",
    "関連動画",
    "流入元",
    "タイトル",
    "冒頭",
)
_PROBLEM_SIGNAL_MARKERS = (
    "ctr",
    "クリック率",
    "視聴維持",
    "維持率",
    "離脱",
    "平均視聴時間",
    "再生数",
    "登録者",
    "インプレッション",
    "流入元",
    "関連動画",
    "低い",
    "下が",
    "伸びない",
    "増えない",
    "減る",
    "不足",
    "届かない",
    "クリックされない",
    "視聴されない",
    "できない",
    "わからない",
    "迷う",
    "失敗",
)
_ACTION_TARGET_MARKERS = (
    "youtube studio",
    "次の動画",
    "次の一本",
    "次のショート",
    "タイトル",
    "サムネ",
    "冒頭",
    "説明欄",
    "視聴維持",
    "アナリティクス",
)
_ACTION_MARKERS = (
    "確認",
    "変更",
    "比較",
    "記録",
    "設定",
    "編集",
    "作成",
    "試す",
    "測る",
    "調整",
    "開く",
    "選ぶ",
    "削る",
    "追加",
)
_TERMINAL_OUTBOX_STATUSES = {"published", "keep_unlisted"}


@dataclass(frozen=True)
class ThemeAssessment:
    audience: str
    problem: str
    viewer_action: str
    theme_fit: str
    theme_fit_reason: str
    subject_clear: bool
    eligible_for_public: bool
    reasons: tuple[str, ...]

    @property
    def privacy(self) -> str:
        return "public" if self.eligible_for_public else "unlisted"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["privacy"] = self.privacy
        return data


@dataclass(frozen=True)
class TrackingIssue:
    number: int
    video_id: str
    title: str
    body: str
    labels: tuple[str, ...]
    url: str
    state: str
    author: str


@dataclass(frozen=True)
class ReviewRecord:
    video_id: str
    title: str
    assessment: ThemeAssessment
    status: str = "pending"
    issue_number: int | None = None
    issue_url: str | None = None
    issue_author: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    events: tuple[str, ...]
    failed_count: int = 0
    failed_video_ids: tuple[str, ...] = ()


def _text(value: object, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _rejects_youtube_subject(*values: str) -> bool:
    folded = " ".join(values).casefold()
    return any(marker in folded for marker in _SUBJECT_REJECTION_MARKERS)


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in markers)


def _matched_markers(value: str, markers: tuple[str, ...]) -> set[str]:
    folded = value.casefold()
    return {marker for marker in markers if marker in folded}


def assess(script: dict) -> ThemeAssessment:
    """3明示項目と主題適合が全て厳格に確認できる場合だけ自動公開可とする。"""
    research = script.get("_research")
    research = research if isinstance(research, dict) else {}
    audience = _text(research.get("youtube_creator_audience"))
    problem = _text(research.get("youtube_creator_problem"))
    viewer_action = _text(research.get("viewer_action"))
    theme_fit = _text(research.get("theme_fit"), limit=40).casefold()
    theme_fit_reason = _text(research.get("theme_fit_reason"))
    title = _text(script.get("title"))
    topic = _text(research.get("topic"))
    angle = _text(research.get("angle"))
    description = _text(script.get("description"))
    narration = _text(script.get("narration"), limit=5000)

    audience_clear = audience.casefold() == "youtube制作者".casefold()
    problem_markers = _matched_markers(problem, _YOUTUBE_OPERATION_MARKERS)
    problem_clear = (
        len(problem) >= 8
        and bool(problem_markers)
        and _contains_any(problem, _PROBLEM_SIGNAL_MARKERS)
        and not _rejects_youtube_subject(problem)
    )
    action_clear = (
        len(viewer_action) >= 8
        and _contains_any(viewer_action, _ACTION_TARGET_MARKERS)
        and _contains_any(viewer_action, _ACTION_MARKERS)
        and not _rejects_youtube_subject(viewer_action)
    )
    planned_subject = " ".join((topic, angle))
    generated_subject = " ".join((title, description, narration))
    context_clear = all(
        _contains_any(value, _YOUTUBE_CONTEXT_MARKERS)
        for value in (planned_subject, generated_subject)
    )
    focus_consistent = bool(problem_markers) and all(
        bool(problem_markers.intersection(_matched_markers(value, _YOUTUBE_OPERATION_MARKERS)))
        for value in (
            planned_subject,
            title,
            " ".join((description, narration)),
            theme_fit_reason,
        )
    )
    subject_clear = (
        context_clear
        and focus_consistent
        and not _rejects_youtube_subject(
            audience,
            problem,
            viewer_action,
            topic,
            angle,
            title,
            description,
            narration,
            theme_fit_reason,
        )
    )

    reasons: list[str] = []
    if not audience_clear:
        reasons.append("対象者がYouTube制作者と厳密に明記されていない")
    if not problem_clear:
        reasons.append("解決する具体的なYouTube上の課題または指標がない")
    if not action_clear:
        reasons.append("視聴後に取れる具体的なYouTube操作がない")
    if theme_fit != "clear":
        reasons.append("主題適合がclearではない")
    if not theme_fit_reason:
        reasons.append("主題適合の理由がない")
    if not subject_clear:
        reasons.append("企画・タイトルからYouTube主題を明確に確認できない")

    return ThemeAssessment(
        audience=audience,
        problem=problem,
        viewer_action=viewer_action,
        theme_fit=theme_fit or "missing",
        theme_fit_reason=theme_fit_reason,
        subject_clear=subject_clear,
        eligible_for_public=not reasons,
        reasons=tuple(reasons),
    )


def choose_privacy(
    spec: ChannelSpec,
    script: dict,
) -> tuple[str, ThemeAssessment | None]:
    """確認運用が有効なチャンネルだけ、安全側の公開判定を適用する。"""
    if not spec.publish.youtube.review.enabled:
        return spec.publish.youtube.privacy, None
    assessment = assess(script)
    return assessment.privacy, assessment


def _redact(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", value)


def _run_gh(
    args: list[str],
    *,
    stdin: str | None = None,
    timeout: int = 60,
) -> str:
    """ghの既存認証だけを使う。トークンを引数・ログ・ファイルへ渡さない。"""
    proc = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = _redact((proc.stderr or proc.stdout).strip())[:600]
        raise RuntimeError(f"GitHub操作に失敗しました (rc={proc.returncode}): {detail}")
    return proc.stdout.strip()


def _parse_issue(row: dict) -> TrackingIssue | None:
    body = str(row.get("body") or "")
    match = _MARKER_RE.search(body)
    if not match:
        return None
    labels = row.get("labels") or []
    label_names = tuple(
        str(label.get("name") or "")
        for label in labels
        if isinstance(label, dict) and label.get("name")
    )
    author = row.get("author")
    if not isinstance(author, dict):
        author = row.get("user")
    return TrackingIssue(
        number=int(row["number"]),
        video_id=match.group(1),
        title=str(row.get("title") or ""),
        body=body,
        labels=label_names,
        url=str(row.get("html_url") or row.get("url") or ""),
        state=str(row.get("state") or "").upper(),
        author=str(author.get("login") or "") if isinstance(author, dict) else "",
    )


def _issue_json_fields() -> str:
    return "number,title,body,labels,url,state,author"


@lru_cache(maxsize=1)
def _current_gh_login() -> str:
    """secretを読まず、ghが認証済みの現在ユーザー名だけを取得する。"""
    login = _run_gh(["api", "user", "--jq", ".login"]).strip()
    if not _GH_LOGIN_RE.fullmatch(login):
        raise RuntimeError("gh認証ユーザー名を安全に確認できません")
    return login


def _find_issue(
    review: YouTubeReviewSpec,
    video_id: str,
    expected_author: str | None = None,
) -> TrackingIssue | None:
    """検索indexを使わず、指定actor作成IssueをREST一覧からmarkerで確定する。"""
    expected_author = expected_author or _current_gh_login()
    if not _GH_LOGIN_RE.fullmatch(expected_author):
        raise RuntimeError("outboxのIssue作成者が不正なため検索を拒否します")
    for page in range(1, _MAX_ISSUE_LIST_PAGES + 1):
        raw = _run_gh(
            [
                "api",
                f"repos/{review.repository}/issues",
                "--method",
                "GET",
                "-f",
                "state=all",
                "-f",
                f"creator={expected_author}",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
                "-f",
                "sort=created",
                "-f",
                "direction=desc",
            ]
        )
        rows = json.loads(raw or "[]")
        if not isinstance(rows, list):
            raise RuntimeError("GitHub Issue検索結果の形式が不正です")
        for row in rows:
            if isinstance(row, dict) and row.get("pull_request"):
                continue
            issue = _parse_issue(row) if isinstance(row, dict) else None
            if (
                issue is not None
                and issue.video_id == video_id
                and issue.author.casefold() == expected_author.casefold()
            ):
                return issue
        if len(rows) < 100:
            return None
    raise RuntimeError(
        "GitHub Issue検索が安全なページ上限に達しました。"
        "重複作成を避けるため自動作成を停止します"
    )


def _list_open_tracking_issues(
    review: YouTubeReviewSpec,
    expected_author: str,
) -> dict[int, TrackingIssue]:
    """Openな確認Issueをまとめて取得し、未決件数に比例するAPI呼出しを避ける。"""
    if not _GH_LOGIN_RE.fullmatch(expected_author):
        raise RuntimeError("Issue一覧の作成者が不正なため取得を拒否します")
    issues: dict[int, TrackingIssue] = {}
    for page in range(1, _MAX_ISSUE_LIST_PAGES + 1):
        raw = _run_gh(
            [
                "api",
                f"repos/{review.repository}/issues",
                "--method",
                "GET",
                "-f",
                "state=open",
                "-f",
                f"creator={expected_author}",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
                "-f",
                "sort=updated",
                "-f",
                "direction=desc",
            ]
        )
        rows = json.loads(raw or "[]")
        if not isinstance(rows, list):
            raise RuntimeError("GitHub Issue一覧の形式が不正です")
        for row in rows:
            if not isinstance(row, dict) or row.get("pull_request"):
                continue
            issue = _parse_issue(row)
            if (
                issue is not None
                and issue.author.casefold() == expected_author.casefold()
            ):
                issues[issue.number] = issue
        if len(rows) < 100:
            return issues
    raise RuntimeError(
        "OpenなGitHub Issue一覧が安全なページ上限に達しました。"
        "一部だけを処理せず確認処理を停止します"
    )


def _get_issue(
    review: YouTubeReviewSpec,
    number: int,
) -> TrackingIssue | None:
    """処理直前にIssue状態とラベルを再取得する。"""
    raw = _run_gh(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            review.repository,
            "--json",
            _issue_json_fields(),
        ]
    )
    row = json.loads(raw or "{}")
    return _parse_issue(row) if isinstance(row, dict) else None


def _assessment_from_dict(value: object) -> ThemeAssessment:
    data = value if isinstance(value, dict) else {}
    reasons = data.get("reasons")
    return ThemeAssessment(
        audience=_text(data.get("audience")),
        problem=_text(data.get("problem")),
        viewer_action=_text(data.get("viewer_action")),
        theme_fit=_text(data.get("theme_fit"), limit=40) or "missing",
        theme_fit_reason=_text(data.get("theme_fit_reason")),
        subject_clear=bool(data.get("subject_clear")),
        eligible_for_public=False,
        reasons=tuple(
            _text(reason, limit=200)
            for reason in reasons
            if _text(reason, limit=200)
        )
        if isinstance(reasons, list)
        else ("確認待ち",),
    )


def _issue_body(
    video_id: str,
    title: str,
    assessment: ThemeAssessment,
    review: YouTubeReviewSpec,
) -> str:
    reason = " / ".join(assessment.reasons) or "主題適合の最終確認"
    return f"""\
<!-- doci-youtube-review video_id={video_id} -->
## 確認対象

- 動画: https://youtu.be/{video_id}
- タイトル: {_text(title, limit=200)}
- 現在の公開設定: `unlisted`
- 自動公開しなかった理由: {reason}

## 企画の明示項目

- 対象者: {assessment.audience or "未記載"}
- 課題・指標: {assessment.problem or "未記載"}
- 視聴後の操作: {assessment.viewer_action or "未記載"}
- 主題適合: `{assessment.theme_fit}`
- 主題適合の理由: {assessment.theme_fit_reason or "未記載"}

## 決定方法

次のラベルを1つだけ付けてください。

- `{review.publish_label}`: YouTubeを公開へ変更し、完了URLを記録してこのIssueを閉じる
- `{review.hold_label}`: 現状の限定公開から変更しない
- `{review.keep_unlisted_label}`: 削除せず限定公開のまま維持する

複数の決定ラベルが付いた場合は曖昧として何も変更しません。
ラベルが無い動画や限定公開の経過時間だけを理由に、自動公開することはありません。
"""


def _create_issue(
    review: YouTubeReviewSpec,
    video_id: str,
    title: str,
    assessment: ThemeAssessment,
) -> TrackingIssue:
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError(f"invalid YouTube video id: {video_id!r}")
    issue_title = f"[YouTube確認] {_text(title, limit=140)} ({video_id})"
    body = _issue_body(video_id, title, assessment, review)
    url = _run_gh(
        [
            "issue",
            "create",
            "--repo",
            review.repository,
            "--title",
            issue_title,
            "--body-file",
            "-",
        ],
        stdin=body,
    )
    try:
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError as exc:
        raise RuntimeError(f"作成Issue番号を取得できませんでした: {url[:200]}") from exc
    return TrackingIssue(
        number=number,
        video_id=video_id,
        title=issue_title,
        body=body,
        labels=(),
        url=url,
        state="OPEN",
        author=_current_gh_login(),
    )


def _outbox_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / "youtube_review_outbox.jsonl"


def _lock_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / ".youtube_review.lock"


def _retry_plan_path(spec: ChannelSpec, cycle_id: str) -> Path:
    """Concurrent cron cycles cannot overwrite each other's retry state."""
    digest = hashlib.sha256(cycle_id.encode("utf-8")).hexdigest()
    return spec.output_dir / ".youtube_review_retry" / f"{digest}.json"


def _prune_retry_plans(directory: Path, keep: Path) -> None:
    """Overlapping cyclesを残しつつ、補助planの総数を一定以下に保つ。"""
    def modified_at(candidate: Path) -> int:
        try:
            return candidate.stat().st_mtime_ns
        except FileNotFoundError:
            return -1

    plans = sorted(
        (
            candidate
            for candidate in directory.glob("*.json")
            if candidate != keep
        ),
        key=modified_at,
        reverse=True,
    )
    retained_others = max(0, _MAX_RETRY_PLAN_FILES - 1)
    for stale in plans[retained_others:]:
        try:
            stale.unlink()
        except FileNotFoundError:
            pass


def save_retry_plan(
    spec: ChannelSpec,
    cycle_id: str,
    failed_video_ids: tuple[str, ...],
) -> None:
    """同一cron cycleの後続runへ、失敗動画だけをローカルに引き渡す。"""
    if not cycle_id:
        return
    if not _CYCLE_ID_RE.fullmatch(cycle_id):
        raise ValueError("invalid YouTube review cycle id")
    video_ids = tuple(dict.fromkeys(failed_video_ids))
    if any(not _VIDEO_ID_RE.fullmatch(video_id) for video_id in video_ids):
        raise ValueError("invalid YouTube review retry video id")
    path = _retry_plan_path(spec, cycle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "cycle_id": cycle_id,
                    "failed_video_ids": list(video_ids),
                },
                file,
                ensure_ascii=False,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        _prune_retry_plans(path.parent, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def load_retry_plan(
    spec: ChannelSpec,
    cycle_id: str,
) -> tuple[str, ...] | None:
    """cycleが一致するplanだけを返す。手動runや次cycleは全件確認へ戻す。"""
    if not cycle_id or not _CYCLE_ID_RE.fullmatch(cycle_id):
        return None
    try:
        row = json.loads(
            _retry_plan_path(spec, cycle_id).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict) or row.get("cycle_id") != cycle_id:
        return None
    video_ids = row.get("failed_video_ids")
    if not isinstance(video_ids, list):
        return None
    if any(
        not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id)
        for video_id in video_ids
    ):
        return None
    return tuple(dict.fromkeys(video_ids))


@contextmanager
def _operation_lock(
    spec: ChannelSpec,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """ネットワーク停止中の別processへ無期限追従しない、上限付き排他lock。"""
    path = _lock_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    wait_limit = (
        _LOCK_WAIT_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(0.0, timeout_seconds)
    )
    deadline = time.monotonic() + wait_limit
    with path.open("a+", encoding="utf-8") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"YouTube確認処理lockを{wait_limit:g}秒以内に取得できません"
                    )
                time.sleep(_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _record_from_row(row: dict) -> ReviewRecord | None:
    video_id = str(row.get("video_id") or "")
    if not _VIDEO_ID_RE.fullmatch(video_id):
        return None
    issue_number = row.get("issue_number")
    return ReviewRecord(
        video_id=video_id,
        title=_text(row.get("title"), limit=200) or video_id,
        assessment=_assessment_from_dict(row.get("assessment")),
        status=str(row.get("status") or "pending"),
        issue_number=issue_number if isinstance(issue_number, int) else None,
        issue_url=_text(row.get("issue_url")) or None,
        issue_author=_text(row.get("issue_author"), limit=40) or None,
    )


def _latest_records(spec: ChannelSpec) -> dict[str, ReviewRecord]:
    path = _outbox_path(spec)
    if not path.is_file():
        return {}
    latest: dict[str, ReviewRecord] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        record = _record_from_row(row) if isinstance(row, dict) else None
        if record is not None:
            latest[record.video_id] = record
    return latest


def _record_row(record: ReviewRecord) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "video_id": record.video_id,
        "title": record.title,
        "assessment": record.assessment.to_dict(),
        "status": record.status,
        "issue_number": record.issue_number,
        "issue_url": record.issue_url,
        "issue_author": record.issue_author,
    }


def _append_record(spec: ChannelSpec, record: ReviewRecord) -> None:
    """callerがoperation lockを保持した状態で、outboxイベントを耐久追記する。"""
    path = _outbox_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = _record_row(record)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


def _compact_outbox_locked(spec: ChannelSpec) -> None:
    """イベント増幅分だけをatomicに畳み、各動画の最新状態は永久保持する。"""
    path = _outbox_path(spec)
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < _OUTBOX_COMPACT_MIN_EVENTS:
        return
    latest = _latest_records(spec)
    if len(lines) <= max(_OUTBOX_COMPACT_MIN_EVENTS, len(latest) * 2):
        return
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as file:
            for record in latest.values():
                file.write(
                    json.dumps(_record_row(record), ensure_ascii=False) + "\n"
                )
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def queue_pending(
    spec: ChannelSpec,
    video_id: str,
    title: str,
    assessment: ThemeAssessment,
) -> ReviewRecord:
    """限定公開アップロード直後に、Issue処理より先にfsyncして再起動可能にする。"""
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError(f"invalid YouTube video id: {video_id!r}")
    with _operation_lock(spec):
        existing = _latest_records(spec).get(video_id)
        if existing is not None:
            return existing
        record = ReviewRecord(
            video_id=video_id,
            title=_text(title, limit=200) or video_id,
            assessment=assessment,
        )
        _append_record(spec, record)
        return record


def _with_issue(record: ReviewRecord, issue: TrackingIssue) -> ReviewRecord:
    return ReviewRecord(
        video_id=record.video_id,
        title=record.title,
        assessment=record.assessment,
        status="pending" if record.status == "issue_creating" else record.status,
        issue_number=issue.number,
        issue_url=issue.url,
        issue_author=issue.author,
    )


def _with_status(record: ReviewRecord, status: str) -> ReviewRecord:
    return ReviewRecord(
        video_id=record.video_id,
        title=record.title,
        assessment=record.assessment,
        status=status,
        issue_number=record.issue_number,
        issue_url=record.issue_url,
        issue_author=record.issue_author,
    )


def _ensure_issue_locked(
    spec: ChannelSpec,
    record: ReviewRecord,
    *,
    prefetched_issues: dict[int, TrackingIssue] | None = None,
) -> tuple[ReviewRecord, TrackingIssue]:
    review = spec.publish.youtube.review
    current_author = _current_gh_login()
    expected_author = record.issue_author or current_author
    if not _GH_LOGIN_RE.fullmatch(expected_author):
        raise RuntimeError("outboxのIssue作成者が不正なため処理を拒否します")
    if record.issue_number is not None:
        issue = (
            prefetched_issues.get(record.issue_number)
            if prefetched_issues is not None
            else None
        )
        if issue is None:
            # Closed・marker欠損など、open一覧に無い記録だけ個別取得して確定する。
            issue = _get_issue(review, record.issue_number)
        if issue is None:
            raise RuntimeError(
                "記録済みの確認Issueから追跡markerを確認できません。"
                "重複作成を避けるため自動再作成しません"
            )
    else:
        issue = _find_issue(review, record.video_id, expected_author)
    if issue is None and record.status == "issue_creating":
        if record.issue_author is None:
            raise RuntimeError(
                "作成者未記録の確認Issue作成中レコードです。"
                "重複作成を避けるため自動再作成しません"
            )
        if current_author.casefold() != expected_author.casefold():
            raise RuntimeError(
                "確認Issue作成後にgh認証ユーザーが変更されました。"
                "元の作成者へ戻すまで自動再作成しません"
            )
        # direct REST一覧で作成済みIssueが無いことを1run後に確認できたため、
        # pendingへ戻す。次の3時間runでだけ再試行し、直後の重複作成を避ける。
        _append_record(spec, _with_status(record, "pending"))
        raise RuntimeError(
            "確認Issueの作成結果を確認できませんでした。"
            "重複作成を避け、次回runで作成を再試行します"
        )
    if issue is None:
        if (
            record.issue_author is not None
            and current_author.casefold() != expected_author.casefold()
        ):
            raise RuntimeError(
                "outbox記録後にgh認証ユーザーが変更されました。"
                "元の作成者へ戻すまで確認Issueを作成しません"
            )
        # operation lock内で検索→作成するため、同一ホストの並行runは重複作成しない。
        intent = ReviewRecord(
            video_id=record.video_id,
            title=record.title,
            assessment=record.assessment,
            status="issue_creating",
            issue_number=record.issue_number,
            issue_url=record.issue_url,
            issue_author=current_author,
        )
        _append_record(spec, intent)
        record = intent
        created = _create_issue(
            review,
            record.video_id,
            record.title,
            record.assessment,
        )
        issue = _get_issue(review, created.number)
        if issue is None:
            raise RuntimeError(
                "作成した確認IssueをAPIで検証できません。"
                "outboxの作成中状態から次回再確認します"
            )
    if issue.video_id != record.video_id:
        raise RuntimeError("確認Issueの動画IDがoutboxと一致しません")
    if issue.author.casefold() != expected_author.casefold():
        raise RuntimeError("確認Issueの作成者がoutboxに記録したactorと一致しません")
    updated = _with_issue(record, issue)
    if updated != record:
        _append_record(spec, updated)
    return updated, issue


def ensure_issue(
    spec: ChannelSpec,
    video_id: str,
) -> TrackingIssue:
    """outboxへ登録済みの動画だけ、動画ID単位でIssueを冪等作成する。"""
    review = spec.publish.youtube.review
    if not review.enabled:
        raise ValueError("YouTube review is not enabled")
    with _operation_lock(spec):
        record = _latest_records(spec).get(video_id)
        if record is None:
            raise ValueError("YouTube video is not registered in review outbox")
        _, issue = _ensure_issue_locked(spec, record)
        return issue


def _history_candidates(spec: ChannelSpec) -> list[ReviewRecord]:
    """旧history書込みまで成功した未移行行をoutboxへ一度だけ移す。"""
    path = Path(spec.history_file)
    if not path.is_file():
        return []
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or "youtube_privacy" not in row:
            continue
        video_id = str(row.get("video_id") or "")
        if _VIDEO_ID_RE.fullmatch(video_id):
            latest[video_id] = row
    return [
        ReviewRecord(
            video_id=video_id,
            title=_text(row.get("title"), limit=200) or video_id,
            assessment=_assessment_from_dict(row.get("youtube_theme_review")),
        )
        for video_id, row in latest.items()
        if row.get("youtube_privacy") == "unlisted"
    ]


def _close_published_issue(
    review: YouTubeReviewSpec,
    issue: TrackingIssue,
) -> None:
    _run_gh(
        [
            "issue",
            "close",
            str(issue.number),
            "--repo",
            review.repository,
            "--comment",
            f"公開へ変更しました: https://youtu.be/{issue.video_id}",
        ]
    )


def _decision(
    issue: TrackingIssue,
    review: YouTubeReviewSpec,
) -> tuple[str | None, str | None]:
    decision_labels = {
        review.publish_label,
        review.hold_label,
        review.keep_unlisted_label,
    }
    decisions = decision_labels.intersection(issue.labels)
    if len(decisions) > 1:
        return None, "決定ラベル競合のため変更なし"
    return (next(iter(decisions)), None) if decisions else (None, None)


def reconcile_result(
    spec: ChannelSpec,
    *,
    only_video_ids: set[str] | None = None,
) -> ReconcileResult:
    """pending動画を処理し、イベントと個別失敗数を構造化して返す。"""
    review = spec.publish.youtube.review
    if not review.enabled:
        return ReconcileResult(())

    events: list[str] = []
    failed_count = 0
    failed_video_ids: list[str] = []
    with _operation_lock(spec):
        records = _latest_records(spec)
        for candidate in _history_candidates(spec):
            if candidate.video_id not in records:
                _append_record(spec, candidate)
                records[candidate.video_id] = candidate

        active = [
            record
            for record in records.values()
            if record.status not in _TERMINAL_OUTBOX_STATUSES
            and (
                only_video_ids is None
                or record.video_id in only_video_ids
            )
        ]
        current_author = _current_gh_login() if active else ""
        expected_authors = {
            record.issue_author or current_author
            for record in active
        }
        # 全actorの一覧取得が成功してから動画単位処理へ進み、ページ上限時に
        # 一部だけ公開変更されることを防ぐ。
        prefetched_by_author = {
            author: _list_open_tracking_issues(review, author)
            for author in expected_authors
        }
        for original in active:
            try:
                expected_author = original.issue_author or current_author
                record, issue = _ensure_issue_locked(
                    spec,
                    original,
                    prefetched_issues=prefetched_by_author[expected_author],
                )
                # _ensure_issue_lockedの取得結果を通常確認に再利用する。公開変更または
                # terminal化の直前だけ再取得し、安全境界を維持しつつgh呼出しを半減する。
                fresh = issue
                if record.status == "public_confirmed":
                    # 承認と公開状態はcloseより前に耐久記録済み。以後のラベル
                    # 撤回は公開済み事実を戻せないため、完了記録だけを冪等再試行する。
                    if fresh.state == "OPEN":
                        _close_published_issue(review, fresh)
                    terminal = _with_status(record, "published")
                    _append_record(spec, terminal)
                    events.append(
                        f"確認Issue #{fresh.number}: 公開完了状態を復旧 "
                        f"https://youtu.be/{record.video_id}"
                    )
                    continue
                if fresh.state != "OPEN":
                    events.append(
                        f"確認Issue #{fresh.number}: openな追跡Issueではないため変更なし"
                    )
                    continue
                decision, conflict = _decision(fresh, review)
                if conflict:
                    events.append(f"確認Issue #{fresh.number}: {conflict}")
                    continue
                if decision is None:
                    continue
                if decision in {
                    review.publish_label,
                    review.keep_unlisted_label,
                }:
                    refreshed = _get_issue(review, issue.number)
                    if (
                        refreshed is None
                        or refreshed.video_id != record.video_id
                    ):
                        failed_count += 1
                        failed_video_ids.append(record.video_id)
                        events.append(
                            f"確認Issue #{issue.number}: "
                            "追跡markerが一致しないため変更なし"
                        )
                        continue
                    fresh = refreshed
                    if fresh.state != "OPEN":
                        events.append(
                            f"確認Issue #{fresh.number}: "
                            "openな追跡Issueではないため変更なし"
                        )
                        continue
                    decision, conflict = _decision(fresh, review)
                    if conflict:
                        events.append(f"確認Issue #{fresh.number}: {conflict}")
                        continue
                    if decision is None:
                        continue
                    if decision == review.hold_label:
                        events.append(
                            f"確認Issue #{fresh.number}: 保留（変更なし）"
                        )
                        continue
                if decision == review.publish_label:
                    from . import youtube

                    current = youtube.privacy_status(
                        record.video_id,
                        token_file=spec.publish.youtube.token,
                        client_secret_file=spec.publish.youtube.client_secret,
                    )
                    if current == "unlisted":
                        youtube.set_privacy(
                            record.video_id,
                            "public",
                            expected_privacy="unlisted",
                            token_file=spec.publish.youtube.token,
                            client_secret_file=spec.publish.youtube.client_secret,
                        )
                    elif current != "public":
                        events.append(
                            f"確認Issue #{fresh.number}: 現在{current}のため公開変更を拒否"
                        )
                        continue
                    confirmed = _with_status(record, "public_confirmed")
                    if confirmed != record:
                        # Issue closeより先に公開済み状態をfsyncし、close後の書込み失敗を
                        # 次回runで安全にterminal化できるようにする。
                        _append_record(spec, confirmed)
                        record = confirmed
                    _close_published_issue(review, fresh)
                    terminal = _with_status(record, "published")
                    _append_record(spec, terminal)
                    events.append(
                        f"確認Issue #{fresh.number}: 公開完了 "
                        f"https://youtu.be/{record.video_id}"
                    )
                elif decision == review.hold_label:
                    events.append(f"確認Issue #{fresh.number}: 保留（変更なし）")
                else:
                    terminal = _with_status(record, "keep_unlisted")
                    _append_record(spec, terminal)
                    events.append(
                        f"確認Issue #{fresh.number}: 限定公開で保持（変更なし）"
                    )
            except Exception as exc:  # 1件の不調で他のpending動画を止めない
                failed_count += 1
                failed_video_ids.append(original.video_id)
                events.append(
                    f"動画 {original.video_id}: 確認処理失敗 "
                    f"{type(exc).__name__}: {_redact(str(exc))[:180]}"
                )
        _compact_outbox_locked(spec)
    return ReconcileResult(
        tuple(events),
        failed_count,
        tuple(dict.fromkeys(failed_video_ids)),
    )


def reconcile(spec: ChannelSpec) -> list[str]:
    """後方互換のイベント一覧API。失敗数が必要な呼出元はreconcile_resultを使う。"""
    return list(reconcile_result(spec).events)
