"""コメントステッカー返信Shortを手動運用する記録CLI（issue #105）。

YouTubeへの書込みは行わない。質問・要望コメントをローカル計画へ固定し、
YouTubeアプリでコメントステッカー付きShortを公開した事実だけを明示確認する。
公開後は返信Shortと運用者が選んだ直近同系統Shortを、各動画の公開翌日から同じ
完了日数でYouTube Analytics APIからread-only取得する。

コメント数、動画watch pageへ帰属した登録者増減、再生1,000回当たりの参考値を
記述比較するが、3本未満のbaselineではmedianを出さず、勝者・因果・万能閾値を
自動決定しない。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

from . import channel, youtube
from .channel import ChannelSpec


SCHEMA_VERSION = 1
OFFICIAL_HELP_URL = "https://support.google.com/youtube/answer/12816427?hl=ja"
ANALYTICS_METRICS_URL = "https://developers.google.com/youtube/analytics/metrics"
REPORTS_QUERY_URL = (
    "https://developers.google.com/youtube/analytics/reference/reports/query"
)
OBSERVATION_TIME_ZONE = "America/Los_Angeles"
DEFAULT_OBSERVATION_DAYS = 7
MIN_OBSERVATION_DAYS = 1
MAX_OBSERVATION_DAYS = 28
MIN_BASELINES = 1
MAX_BASELINES = 5
MIN_COMPARABLE_BASELINES = 3
ANALYTICS_UNVERIFIABLE_WAIT_DAYS = 7
MAX_SHORT_DURATION_SECONDS = 180.0
ACTIVE_STATUSES = frozenset({"planned", "running"})
VALID_STATUSES = frozenset({"planned", "running", "completed", "invalidated"})
VALID_RESULT_STATUSES = frozenset(
    {"observed", "insufficient_data", "stopped_changed_setup"}
)
_EXPERIMENT_ID_RE = re.compile(r"crs-[0-9a-f]{16}")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_COMMENT_ID_RE = re.compile(r"\S{1,256}")
_TIMESTAMP_FIELD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_FIELD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?$"
)
_PLAN_FIELDS = (
    "schema_version",
    "experiment_id",
    "channel",
    "created_at",
    "official_help_url",
    "analytics_metrics_url",
    "reports_query_url",
    "decision_metrics",
    "observation_days",
    "reply_corner",
    "comparison_key",
    "source_comment",
    "manual_workflow",
    "warnings",
)
_SETUP_FIELDS = (
    "owned_channel_id",
    "source_video",
    "reply",
    "baselines",
    "manual_confirmation",
)
_RUNNING_FIELDS = (
    "status",
    "started_at",
    "plan_sha256",
    "setup_sha256",
)
_TERMINAL_FIELDS = (
    "status",
    "started_at",
    "completed_at",
    "plan_sha256",
    "setup_sha256",
    "running_sha256",
    "result",
)
_COUNT_FIELDS = (
    "views",
    "comments",
    "subscribers_gained",
    "subscribers_lost",
    "net_subscribers",
)


class CommentReplyShortError(ValueError):
    """コメント返信Short計画または状態遷移を安全に実行できない。"""


def _root(spec: ChannelSpec) -> Path:
    return spec.output_dir / "comment_reply_short_tests"


def _ensure_root_dir(path: Path) -> Path:
    if path.is_symlink():
        raise CommentReplyShortError(
            f"comment reply Short root must not be a symlink: {path}"
        )
    if path.exists() and not path.is_dir():
        raise CommentReplyShortError(
            f"comment reply Short root is not a directory: {path}"
        )
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CommentReplyShortError(
            f"comment reply Short root must not be a symlink: {path}"
        )
    return path


def _validate_root_readable(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise CommentReplyShortError(
            f"comment reply Short root is not a real directory: {path}"
        )


def _manifest_path(spec: ChannelSpec, experiment_id: str) -> Path:
    if not _EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise CommentReplyShortError(f"invalid experiment id: {experiment_id!r}")
    directory = _root(spec) / experiment_id
    if directory.is_symlink():
        raise CommentReplyShortError(
            f"comment reply Short directory must not be a symlink: {directory}"
        )
    return directory / "manifest.json"


def _now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise CommentReplyShortError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _now_iso(now: datetime | None) -> str:
    return _now(now).isoformat()


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    root = _ensure_root_dir(_root(spec))
    path = root / ".comment_reply_short.lock"
    if path.is_symlink():
        raise CommentReplyShortError(f"lock must not be a symlink: {path}")
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_manifest(path: Path, manifest: dict) -> None:
    _write_text_atomic(
        path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )


def _checksum(data: dict, fields: Sequence[str]) -> str:
    stable = {key: data[key] for key in fields if key in data}
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_checksum(data: dict) -> str:
    return _checksum(data, _PLAN_FIELDS)


def _setup_checksum(data: dict) -> str:
    return _checksum(data, _SETUP_FIELDS)


def _running_checksum(data: dict) -> str:
    # terminal stateでも開始時のrunning projectionを検証し、正規の状態遷移だけ許す。
    running_state = {**data, "status": "running"}
    return _checksum(running_state, _RUNNING_FIELDS)


def _terminal_checksum(data: dict) -> str:
    return _checksum(data, _TERMINAL_FIELDS)


def _normalise_text(value: object, *, max_length: int, label: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise CommentReplyShortError(f"{label} must not be empty")
    if len(text) > max_length:
        raise CommentReplyShortError(
            f"{label} must be at most {max_length} characters"
        )
    return text


def _validate_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_FIELD_RE.fullmatch(value):
        raise CommentReplyShortError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommentReplyShortError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommentReplyShortError(f"{label} must include a UTC offset")
    return value


def _parse_timestamp(value: object, label: str) -> datetime:
    return datetime.fromisoformat(
        _validate_timestamp(value, label).replace("Z", "+00:00")
    )


def _validate_date(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DATE_FIELD_RE.fullmatch(value):
        raise CommentReplyShortError(f"{label} must be a YYYY-MM-DD string")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise CommentReplyShortError(f"{label} is not a valid date") from exc
    return value


def _duration_seconds(value: object) -> float:
    if not isinstance(value, str):
        raise CommentReplyShortError("video duration must be an ISO-8601 string")
    match = _DURATION_RE.fullmatch(value)
    if not match or not any(match.groupdict().values()):
        raise CommentReplyShortError(f"unsupported video duration: {value!r}")
    seconds = (
        int(match.group("hours") or 0) * 3600
        + int(match.group("minutes") or 0) * 60
        + float(match.group("seconds") or 0)
    )
    if seconds <= 0:
        raise CommentReplyShortError("video duration must be positive")
    return seconds


def _window_for(published_at: str, observation_days: int) -> tuple[str, str]:
    published = _parse_timestamp(published_at, "published_at")
    first_full_day = (
        published.astimezone(ZoneInfo(OBSERVATION_TIME_ZONE)).date()
        + timedelta(days=1)
    )
    last_day = first_full_day + timedelta(days=observation_days - 1)
    return first_full_day.isoformat(), last_day.isoformat()


def _validate_plan(data: dict, path: Path) -> None:
    if not isinstance(data, dict):
        raise CommentReplyShortError(f"invalid manifest object: {path}")
    if path.name != "manifest.json" or data.get("experiment_id") != path.parent.name:
        raise CommentReplyShortError("manifest path and experiment id do not match")
    missing = [field for field in _PLAN_FIELDS if field not in data]
    if missing:
        raise CommentReplyShortError(
            f"manifest is missing plan fields: {', '.join(missing)}"
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise CommentReplyShortError("unsupported manifest schema")
    if data.get("status") not in VALID_STATUSES:
        raise CommentReplyShortError(f"invalid status: {data.get('status')!r}")
    _validate_timestamp(data.get("created_at"), "created_at")
    if data.get("official_help_url") != OFFICIAL_HELP_URL:
        raise CommentReplyShortError("official help URL is invalid")
    if data.get("analytics_metrics_url") != ANALYTICS_METRICS_URL:
        raise CommentReplyShortError("Analytics metrics URL is invalid")
    if data.get("reports_query_url") != REPORTS_QUERY_URL:
        raise CommentReplyShortError("reports query URL is invalid")
    if data.get("decision_metrics") != [
        "comments",
        "subscribers_gained",
        "subscribers_lost",
        "net_subscribers",
    ]:
        raise CommentReplyShortError("decision metrics are invalid")
    observation_days = data.get("observation_days")
    if (
        isinstance(observation_days, bool)
        or not isinstance(observation_days, int)
        or not MIN_OBSERVATION_DAYS <= observation_days <= MAX_OBSERVATION_DAYS
    ):
        raise CommentReplyShortError("observation_days is out of range")
    for field, limit in (("reply_corner", 80), ("comparison_key", 120)):
        value = data.get(field)
        if not isinstance(value, str) or _normalise_text(
            value, max_length=limit, label=field
        ) != value:
            raise CommentReplyShortError(f"{field} is not normalized")
    source = data.get("source_comment")
    if not isinstance(source, dict):
        raise CommentReplyShortError("source_comment must be an object")
    if set(source) != {
        "source_video_id",
        "comment_id",
        "request_summary",
        "question_or_request_confirmed",
        "commenter_identity_stored",
        "raw_comment_stored",
    }:
        raise CommentReplyShortError("source_comment fields are invalid")
    if not isinstance(source.get("source_video_id"), str) or not _VIDEO_ID_RE.fullmatch(
        source["source_video_id"]
    ):
        raise CommentReplyShortError("source video id is invalid")
    if not isinstance(source.get("comment_id"), str) or not _COMMENT_ID_RE.fullmatch(
        source["comment_id"]
    ):
        raise CommentReplyShortError("source comment id is invalid")
    if not isinstance(source.get("request_summary"), str) or _normalise_text(
        source["request_summary"], max_length=500, label="request_summary"
    ) != source["request_summary"]:
        raise CommentReplyShortError("request summary is not normalized")
    if source.get("question_or_request_confirmed") is not True:
        raise CommentReplyShortError("question/request confirmation is missing")
    if source.get("commenter_identity_stored") is not False:
        raise CommentReplyShortError("commenter identity must not be stored")
    if source.get("raw_comment_stored") is not False:
        raise CommentReplyShortError("raw comment must not be stored")
    workflow = data.get("manual_workflow")
    if not isinstance(workflow, dict):
        raise CommentReplyShortError("manual_workflow must be an object")
    if workflow != {
        "comment_sticker_required": True,
        "youtube_app_publish_required": True,
        "youtube_write_performed": False,
    }:
        raise CommentReplyShortError("manual_workflow is invalid")
    warnings = data.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        raise CommentReplyShortError("warnings must be a list of strings")
    expected = _plan_checksum(data)
    actual = str(data.get("plan_sha256") or "")
    if not hmac.compare_digest(expected, actual):
        raise CommentReplyShortError("plan checksum mismatch")


def _validate_video_record(
    record: object,
    *,
    role: str,
    channel_id: str,
    observation_days: int,
    reply_corner: str,
) -> dict:
    if not isinstance(record, dict):
        raise CommentReplyShortError(f"{role} video must be an object")
    expected_fields = {
        "video_id",
        "channel_id",
        "title",
        "published_at",
        "duration",
        "privacy_status",
    }
    if role != "source":
        expected_fields.update(
            {
                "corner",
                "short_confirmed",
                "observation_start_date",
                "observation_end_date",
            }
        )
    if set(record) != expected_fields:
        raise CommentReplyShortError(f"{role} video fields are invalid")
    video_id = record.get("video_id")
    if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
        raise CommentReplyShortError(f"{role} video id is invalid")
    if record.get("channel_id") != channel_id:
        raise CommentReplyShortError(f"{role} video channel is inconsistent")
    if record.get("privacy_status") not in {"public", "unlisted"}:
        raise CommentReplyShortError(f"{role} video must be public or unlisted")
    if not isinstance(record.get("title"), str):
        raise CommentReplyShortError(f"{role} title must be a string")
    published_at = _validate_timestamp(record.get("published_at"), "published_at")
    duration = record.get("duration")
    seconds = _duration_seconds(duration)
    if role == "source":
        if any(
            field in record
            for field in (
                "corner",
                "short_confirmed",
                "observation_start_date",
                "observation_end_date",
            )
        ):
            raise CommentReplyShortError("source video has reply-only fields")
        return record
    if seconds > MAX_SHORT_DURATION_SECONDS:
        raise CommentReplyShortError(f"{role} video exceeds 180 seconds")
    if record.get("short_confirmed") is not True:
        raise CommentReplyShortError(f"{role} Short confirmation is missing")
    if record.get("corner") != reply_corner:
        raise CommentReplyShortError(f"{role} corner is inconsistent")
    start, end = _window_for(published_at, observation_days)
    if record.get("observation_start_date") != start:
        raise CommentReplyShortError(f"{role} observation start is inconsistent")
    if record.get("observation_end_date") != end:
        raise CommentReplyShortError(f"{role} observation end is inconsistent")
    return record


def _validate_setup(data: dict) -> None:
    channel_id = data.get("owned_channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise CommentReplyShortError("owned channel id is missing")
    source = _validate_video_record(
        data.get("source_video"),
        role="source",
        channel_id=channel_id,
        observation_days=data["observation_days"],
        reply_corner=data["reply_corner"],
    )
    if source["video_id"] != data["source_comment"]["source_video_id"]:
        raise CommentReplyShortError("source comment video is inconsistent")
    reply = _validate_video_record(
        data.get("reply"),
        role="reply",
        channel_id=channel_id,
        observation_days=data["observation_days"],
        reply_corner=data["reply_corner"],
    )
    if reply["video_id"] == source["video_id"]:
        raise CommentReplyShortError("reply Short must differ from source video")
    baselines = data.get("baselines")
    if not isinstance(baselines, list) or not MIN_BASELINES <= len(
        baselines
    ) <= MAX_BASELINES:
        raise CommentReplyShortError("baseline count is out of range")
    seen: set[str] = set()
    reply_published = _parse_timestamp(reply["published_at"], "reply.published_at")
    for baseline in baselines:
        baseline = _validate_video_record(
            baseline,
            role="baseline",
            channel_id=channel_id,
            observation_days=data["observation_days"],
            reply_corner=data["reply_corner"],
        )
        video_id = baseline["video_id"]
        if video_id == reply["video_id"] or video_id in seen:
            raise CommentReplyShortError("baseline video ids must be unique")
        seen.add(video_id)
        if _parse_timestamp(baseline["published_at"], "baseline.published_at") >= reply_published:
            raise CommentReplyShortError("baseline videos must predate the reply Short")
    confirmation = data.get("manual_confirmation")
    if confirmation != {
        "comment_sticker_confirmed": True,
        "youtube_app_published_confirmed": True,
        "recent_same_type_baselines_confirmed": True,
        "youtube_write_performed": False,
    }:
        raise CommentReplyShortError("manual confirmation is invalid")
    expected = _setup_checksum(data)
    actual = str(data.get("setup_sha256") or "")
    if not hmac.compare_digest(expected, actual):
        raise CommentReplyShortError("started setup checksum mismatch")


def _validate_running_checksum(data: dict) -> None:
    actual = data.get("running_sha256")
    if (
        not isinstance(actual, str)
        or not re.fullmatch(r"[0-9a-f]{64}", actual)
        or not hmac.compare_digest(_running_checksum(data), actual)
    ):
        raise CommentReplyShortError("running checksum mismatch")


def _blank_observations(data: dict) -> list[dict]:
    videos = [("reply", data["reply"])] + [
        ("baseline", item) for item in data["baselines"]
    ]
    return [
        {
            "role": role,
            "video_id": video["video_id"],
            "start_date": video["observation_start_date"],
            "end_date": video["observation_end_date"],
            "views": None,
            "comments": None,
            "subscribers_gained": None,
            "subscribers_lost": None,
            "net_subscribers": None,
            "comments_per_1000_views": None,
            "net_subscribers_per_1000_views": None,
            "valid_for_comparison": False,
            "reason": "Analytics期間を確認できませんでした",
        }
        for role, video in videos
    ]


def _optional_count(value: object, label: str, *, signed: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommentReplyShortError(f"{label} must be an integer or null")
    if not signed and value < 0:
        raise CommentReplyShortError(f"{label} must not be negative")
    return value


def _validate_observation(observation: object) -> dict:
    if not isinstance(observation, dict):
        raise CommentReplyShortError("observation must be an object")
    if observation.get("role") not in {"reply", "baseline"}:
        raise CommentReplyShortError("observation role is invalid")
    if not isinstance(observation.get("video_id"), str) or not _VIDEO_ID_RE.fullmatch(
        observation["video_id"]
    ):
        raise CommentReplyShortError("observation video id is invalid")
    _validate_date(observation.get("start_date"), "observation.start_date")
    _validate_date(observation.get("end_date"), "observation.end_date")
    views = _optional_count(observation.get("views"), "views")
    comments = _optional_count(observation.get("comments"), "comments")
    gained = _optional_count(
        observation.get("subscribers_gained"), "subscribers_gained"
    )
    lost = _optional_count(
        observation.get("subscribers_lost"), "subscribers_lost"
    )
    net = _optional_count(
        observation.get("net_subscribers"), "net_subscribers", signed=True
    )
    if (gained is None) != (lost is None):
        raise CommentReplyShortError("subscriber gained/lost availability differs")
    if gained is not None and net != gained - lost:
        raise CommentReplyShortError("net subscribers are inconsistent")
    comments_rate = observation.get("comments_per_1000_views")
    subscriber_rate = observation.get("net_subscribers_per_1000_views")
    for value, label in (
        (comments_rate, "comments_per_1000_views"),
        (subscriber_rate, "net_subscribers_per_1000_views"),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise CommentReplyShortError(f"{label} must be numeric or null")
    valid = observation.get("valid_for_comparison")
    expected_valid = (
        views is not None
        and views > 0
        and comments is not None
        and gained is not None
        and lost is not None
    )
    if valid is not expected_valid:
        raise CommentReplyShortError("observation validity is inconsistent")
    if expected_valid:
        expected_comments_rate = round(comments * 1000.0 / views, 4)
        expected_subscriber_rate = round(net * 1000.0 / views, 4)
        if not math.isclose(float(comments_rate), expected_comments_rate, abs_tol=0.0001):
            raise CommentReplyShortError("comment rate is inconsistent")
        if not math.isclose(
            float(subscriber_rate), expected_subscriber_rate, abs_tol=0.0001
        ):
            raise CommentReplyShortError("subscriber rate is inconsistent")
        if observation.get("reason"):
            raise CommentReplyShortError("valid observation must not have a reason")
    else:
        if comments_rate is not None or subscriber_rate is not None:
            raise CommentReplyShortError("invalid observation must not have rates")
        if not str(observation.get("reason") or "").strip():
            raise CommentReplyShortError("invalid observation requires a reason")
    return observation


def _median(values: list[float | int]) -> float:
    return round(float(statistics.median(values)), 4)


def _comparison(observations: list[dict]) -> dict:
    reply = next(
        (item for item in observations if item.get("role") == "reply"), None
    )
    baselines = [
        item
        for item in observations
        if item.get("role") == "baseline" and item.get("valid_for_comparison")
    ]
    ready = bool(
        reply
        and reply.get("valid_for_comparison")
        and len(baselines) >= MIN_COMPARABLE_BASELINES
    )
    metric_names = (
        "views",
        "comments",
        "net_subscribers",
        "comments_per_1000_views",
        "net_subscribers_per_1000_views",
    )
    medians = {
        metric: _median([item[metric] for item in baselines]) if ready else None
        for metric in metric_names
    }
    deltas = {
        metric: round(float(reply[metric]) - float(medians[metric]), 4)
        if ready
        else None
        for metric in metric_names
    }
    if not reply or not reply.get("valid_for_comparison"):
        status = "reply_metrics_unavailable"
    elif not baselines:
        status = "no_comparable_baseline_metrics"
    elif not ready:
        status = "insufficient_comparable_baselines"
    else:
        status = "ready"
    return {
        "status": status,
        "valid_baseline_count": len(baselines),
        "required_baseline_count": MIN_COMPARABLE_BASELINES,
        "baseline_video_ids": [item["video_id"] for item in baselines],
        "baseline_medians": medians,
        "reply_minus_baseline_median": deltas,
        "universal_threshold_applied": False,
        "winner": None,
        "causal_conclusion": None,
    }


def _validate_result(data: dict, path: Path) -> None:
    result = data.get("result")
    if not isinstance(result, dict):
        raise CommentReplyShortError(f"terminal manifest lacks result: {path}")
    actual_checksum = data.get("terminal_sha256")
    if (
        not isinstance(actual_checksum, str)
        or not re.fullmatch(r"[0-9a-f]{64}", actual_checksum)
        or not hmac.compare_digest(_terminal_checksum(data), actual_checksum)
    ):
        raise CommentReplyShortError("terminal checksum mismatch")
    result_status = result.get("status")
    if result_status not in VALID_RESULT_STATUSES:
        raise CommentReplyShortError("result status is invalid")
    _validate_timestamp(data.get("completed_at"), "completed_at")
    _validate_timestamp(result.get("recorded_at"), "result.recorded_at")
    if result.get("universal_threshold_applied") is not False:
        raise CommentReplyShortError("universal threshold must not be applied")
    if result.get("winner") is not None or result.get("causal_conclusion") is not None:
        raise CommentReplyShortError("result must not claim a winner or causality")
    observations = result.get("observations")
    comparison = result.get("comparison")
    if result_status == "stopped_changed_setup":
        if data.get("status") != "invalidated":
            raise CommentReplyShortError("changed setup must invalidate the experiment")
        if result.get("setup_unchanged_confirmed") is not False:
            raise CommentReplyShortError("changed setup cannot be confirmed unchanged")
        if observations != [] or comparison != {
            "status": "invalidated",
            "valid_baseline_count": 0,
            "required_baseline_count": MIN_COMPARABLE_BASELINES,
            "baseline_video_ids": [],
            "baseline_medians": {},
            "reply_minus_baseline_median": {},
            "universal_threshold_applied": False,
            "winner": None,
            "causal_conclusion": None,
        }:
            raise CommentReplyShortError("invalidated comparison is inconsistent")
        return
    if data.get("status") != "completed":
        raise CommentReplyShortError("measured result must be completed")
    if result.get("setup_unchanged_confirmed") is not True:
        raise CommentReplyShortError("measured result requires unchanged setup")
    reply_end = data["reply"]["observation_end_date"]
    if result.get("requested_reply_end_date") != reply_end:
        raise CommentReplyShortError("requested reply period is inconsistent")
    probe_end = _validate_date(
        result.get("availability_probe_end_date"), "availability_probe_end_date"
    )
    if probe_end < reply_end:
        raise CommentReplyShortError("availability probe ends before reply observation")
    data_through_raw = result.get("analytics_data_through_date")
    data_through = (
        _validate_date(data_through_raw, "analytics_data_through_date")
        if data_through_raw is not None
        else None
    )
    period_confirmed = result.get("analytics_period_confirmed")
    if not isinstance(period_confirmed, bool):
        raise CommentReplyShortError("analytics_period_confirmed must be boolean")
    if period_confirmed:
        if data_through is None or data_through < reply_end:
            raise CommentReplyShortError("Analytics period is not fully covered")
    else:
        settled = (
            date.fromisoformat(reply_end)
            + timedelta(days=ANALYTICS_UNVERIFIABLE_WAIT_DAYS)
        ).isoformat()
        if result_status != "insufficient_data" or probe_end < settled:
            raise CommentReplyShortError(
                "unconfirmed Analytics must wait and remain insufficient"
            )
    if not isinstance(observations, list):
        raise CommentReplyShortError("observations must be a list")
    expected_videos = [("reply", data["reply"])] + [
        ("baseline", item) for item in data["baselines"]
    ]
    if len(observations) != len(expected_videos):
        raise CommentReplyShortError("observation count is inconsistent")
    for observation, (role, video) in zip(observations, expected_videos):
        observation = _validate_observation(observation)
        if (
            observation["role"] != role
            or observation["video_id"] != video["video_id"]
            or observation["start_date"] != video["observation_start_date"]
            or observation["end_date"] != video["observation_end_date"]
        ):
            raise CommentReplyShortError("observation provenance is inconsistent")
    expected_comparison = _comparison(observations)
    if comparison != expected_comparison:
        raise CommentReplyShortError("comparison is inconsistent with observations")
    valid_reply = observations[0]["valid_for_comparison"]
    valid_baselines = sum(
        bool(item["valid_for_comparison"]) for item in observations[1:]
    )
    expected_status = (
        "observed" if valid_reply and valid_baselines >= 1 else "insufficient_data"
    )
    if result_status != expected_status:
        raise CommentReplyShortError("result status is inconsistent with observations")
    if result_status == "insufficient_data" and not str(
        result.get("reason") or ""
    ).strip():
        raise CommentReplyShortError("insufficient result requires a reason")


def _validate_status(data: dict, path: Path) -> None:
    status = data["status"]
    if status == "planned":
        forbidden = (
            "started_at",
            "owned_channel_id",
            "source_video",
            "reply",
            "baselines",
            "manual_confirmation",
            "setup_sha256",
            "running_sha256",
            "completed_at",
            "result",
            "terminal_sha256",
        )
        if any(field in data for field in forbidden):
            raise CommentReplyShortError("planned manifest has later state")
        return
    _validate_timestamp(data.get("started_at"), "started_at")
    _validate_setup(data)
    _validate_running_checksum(data)
    if status == "running":
        if any(
            field in data for field in ("completed_at", "result", "terminal_sha256")
        ):
            raise CommentReplyShortError("running manifest has terminal state")
        return
    _validate_result(data, path)


def _load_manifest(path: Path, *, expected_channel: str | None = None) -> dict:
    if path.is_symlink():
        raise CommentReplyShortError(f"manifest must not be a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommentReplyShortError(f"cannot read manifest: {path}") from exc
    _validate_plan(data, path)
    _validate_status(data, path)
    if expected_channel is not None and data.get("channel") != expected_channel:
        raise CommentReplyShortError("manifest channel does not match requested channel")
    return data


def _all_manifests(spec: ChannelSpec) -> list[dict]:
    root = _root(spec)
    if not root.exists():
        return []
    _validate_root_readable(root)
    manifests: list[dict] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            raise CommentReplyShortError(f"unsafe experiment directory: {child}")
        if not _EXPERIMENT_ID_RE.fullmatch(child.name):
            continue
        path = child / "manifest.json"
        if not path.is_file():
            raise CommentReplyShortError(f"manifest is missing: {path}")
        manifests.append(_load_manifest(path, expected_channel=spec.id))
    return manifests


def _safe_cell(value: object) -> str:
    return str(value or "").replace("|", "｜").replace("\n", " ")


def _plan_markdown(data: dict) -> str:
    source = data["source_comment"]
    warnings = "\n".join(f"- {item}" for item in data["warnings"])
    return f"""# コメントステッカー返信Short検証計画

- experiment_id: `{data['experiment_id']}`
- source_video_id: `{source['source_video_id']}`
- source_comment_id: `{_safe_cell(source['comment_id'])}`
- 質問・要望の要約: {_safe_cell(source['request_summary'])}
- 返信corner: `{_safe_cell(data['reply_corner'])}`
- 比較cohort: `{_safe_cell(data['comparison_key'])}`
- 観測期間: 各動画の公開翌日から太平洋時間で{data['observation_days']}完了日
- 公式手順: {OFFICIAL_HELP_URL}

## 実施手順

1. YouTubeアプリで上記コメントを選び、コメントステッカー付きShortを作成します。
2. アプリから公開した後、返信Shortと直近同系統Short 1〜5本を指定して`start`します。
3. dociは所有チャンネル・公開日時・180秒以内だけをread-only確認します。Shorts分類と
   同系統性はAPIから断定できないため、人が確認します。
4. 返信Shortの観測期間終了後、`complete --confirm-setup-unchanged`で、各動画の
   公開翌日から同じ日数のAnalyticsを取得します。

## 判定上の注意

{warnings}
"""


def _video_record(raw: dict, *, corner: str | None, observation_days: int) -> dict:
    record = {
        "video_id": str(raw.get("video_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "title": str(raw.get("title") or ""),
        "published_at": str(raw.get("published_at") or ""),
        "duration": str(raw.get("duration") or ""),
        "privacy_status": str(raw.get("privacy_status") or ""),
    }
    if corner is not None:
        start, end = _window_for(record["published_at"], observation_days)
        record.update(
            {
                "corner": corner,
                "short_confirmed": True,
                "observation_start_date": start,
                "observation_end_date": end,
            }
        )
    return record


def plan_experiment(
    spec: ChannelSpec,
    *,
    source_video_id: str,
    source_comment_id: str,
    request_summary: str,
    reply_corner: str,
    comparison_key: str,
    observation_days: int = DEFAULT_OBSERVATION_DAYS,
    question_or_request_confirmed: bool = False,
    now: datetime | None = None,
    experiment_id: str | None = None,
) -> dict:
    """質問・要望コメントと比較条件を固定する（YouTube書込みなし）。"""
    if not question_or_request_confirmed:
        raise CommentReplyShortError(
            "confirm that the selected comment is a question or request"
        )
    if not _VIDEO_ID_RE.fullmatch(source_video_id):
        raise CommentReplyShortError("source video id is invalid")
    if not _COMMENT_ID_RE.fullmatch(source_comment_id):
        raise CommentReplyShortError("source comment id is invalid")
    if reply_corner not in spec.corners:
        raise CommentReplyShortError(
            f"reply corner is not configured for channel {spec.id}: {reply_corner}"
        )
    if (
        isinstance(observation_days, bool)
        or not isinstance(observation_days, int)
        or not MIN_OBSERVATION_DAYS <= observation_days <= MAX_OBSERVATION_DAYS
    ):
        raise CommentReplyShortError(
            f"observation_days must be {MIN_OBSERVATION_DAYS} to {MAX_OBSERVATION_DAYS}"
        )
    summary = _normalise_text(
        request_summary, max_length=500, label="request_summary"
    )
    cohort = _normalise_text(
        comparison_key, max_length=120, label="comparison_key"
    )
    corner = _normalise_text(reply_corner, max_length=80, label="reply_corner")
    created_at = _now_iso(now)

    with _operation_lock(spec):
        for existing in _all_manifests(spec):
            source = existing.get("source_comment") or {}
            if (
                source.get("source_video_id") == source_video_id
                and source.get("comment_id") == source_comment_id
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise CommentReplyShortError(
                    "active experiment already exists for this source comment: "
                    f"{existing.get('experiment_id')}"
                )
        candidate_id = experiment_id or f"crs-{uuid.uuid4().hex[:16]}"
        destination = _manifest_path(spec, candidate_id).parent
        if destination.exists():
            raise CommentReplyShortError(f"experiment already exists: {candidate_id}")
        root = _root(spec)
        staging = Path(tempfile.mkdtemp(prefix=".plan-", dir=root))
        try:
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "experiment_id": candidate_id,
                "channel": spec.id,
                "status": "planned",
                "created_at": created_at,
                "official_help_url": OFFICIAL_HELP_URL,
                "analytics_metrics_url": ANALYTICS_METRICS_URL,
                "reports_query_url": REPORTS_QUERY_URL,
                "decision_metrics": [
                    "comments",
                    "subscribers_gained",
                    "subscribers_lost",
                    "net_subscribers",
                ],
                "observation_days": observation_days,
                "reply_corner": corner,
                "comparison_key": cohort,
                "source_comment": {
                    "source_video_id": source_video_id,
                    "comment_id": source_comment_id,
                    "request_summary": summary,
                    "question_or_request_confirmed": True,
                    "commenter_identity_stored": False,
                    "raw_comment_stored": False,
                },
                "manual_workflow": {
                    "comment_sticker_required": True,
                    "youtube_app_publish_required": True,
                    "youtube_write_performed": False,
                },
                "warnings": [
                    "コメント選択、ステッカー付与、公開はYouTubeアプリで手動実施します。",
                    "コメント投稿者名とコメント原文は保存しません。",
                    "登録者増減は指定動画のwatch pageに帰属した値だけです。",
                    "比較baselineは直近同系統かを人が確認し、3本未満ではmedianを出しません。",
                    "勝者、因果、万能な合格ラインは自動判定しません。",
                ],
            }
            manifest["plan_sha256"] = _plan_checksum(manifest)
            path = staging / "manifest.json"
            _write_manifest(path, manifest)
            _validate_plan(manifest, destination / "manifest.json")
            _validate_status(manifest, destination / "manifest.json")
            _write_text_atomic(staging / "plan.md", _plan_markdown(manifest))
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return manifest


def _validate_video_readback(payload: object, video_ids: list[str]) -> tuple[str, dict[str, dict]]:
    if not isinstance(payload, dict):
        raise CommentReplyShortError("video readback returned an invalid payload")
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise CommentReplyShortError("video readback lacks owned channel id")
    videos = payload.get("videos")
    if not isinstance(videos, list):
        raise CommentReplyShortError("video readback lacks videos")
    by_id: dict[str, dict] = {}
    for item in videos:
        if not isinstance(item, dict):
            raise CommentReplyShortError("video readback item is invalid")
        video_id = str(item.get("video_id") or "")
        if video_id in by_id:
            raise CommentReplyShortError("video readback contains duplicate ids")
        by_id[video_id] = item
    missing = [video_id for video_id in video_ids if video_id not in by_id]
    if missing:
        raise CommentReplyShortError(
            "videos were not found on the authenticated channel: " + ", ".join(missing)
        )
    if set(by_id) != set(video_ids):
        raise CommentReplyShortError("video readback returned unexpected ids")
    return channel_id, by_id


def start_experiment(
    spec: ChannelSpec,
    experiment_id: str,
    *,
    reply_video_id: str,
    baseline_video_ids: Sequence[str],
    comment_sticker_confirmed: bool = False,
    youtube_app_published_confirmed: bool = False,
    recent_same_type_baselines_confirmed: bool = False,
    now: datetime | None = None,
) -> dict:
    """アプリ公開済み返信Shortと直近同系統baselineをread-only確認する。"""
    if not all(
        (
            comment_sticker_confirmed,
            youtube_app_published_confirmed,
            recent_same_type_baselines_confirmed,
        )
    ):
        raise CommentReplyShortError(
            "confirm the comment sticker, YouTube app publication, and recent same-type baselines"
        )
    if not _VIDEO_ID_RE.fullmatch(reply_video_id):
        raise CommentReplyShortError("reply video id is invalid")
    baselines = list(baseline_video_ids)
    if not MIN_BASELINES <= len(baselines) <= MAX_BASELINES:
        raise CommentReplyShortError(
            f"provide {MIN_BASELINES} to {MAX_BASELINES} baseline video ids"
        )
    if any(not _VIDEO_ID_RE.fullmatch(video_id) for video_id in baselines):
        raise CommentReplyShortError("baseline video id is invalid")
    if len(set(baselines)) != len(baselines) or reply_video_id in baselines:
        raise CommentReplyShortError("reply and baseline video ids must be distinct")
    current = _now(now)

    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest["status"] != "planned":
            raise CommentReplyShortError("only a planned experiment can be started")
        identity = (manifest["plan_sha256"], manifest["created_at"])
        source_video_id = manifest["source_comment"]["source_video_id"]
        requested_ids = list(
            dict.fromkeys([source_video_id, reply_video_id, *baselines])
        )

    try:
        payload = youtube.owned_video_details_readonly(
            requested_ids,
            token_file=spec.publish.youtube.analytics_token,
            client_secret_file=spec.publish.youtube.client_secret,
        )
        channel_id, by_id = _validate_video_readback(payload, requested_ids)
    except Exception as exc:
        raise CommentReplyShortError(
            f"read-only video verification failed; experiment remains planned: {exc}"
        ) from exc

    observation_days = manifest["observation_days"]
    reply_corner = manifest["reply_corner"]
    source = _video_record(
        by_id[source_video_id], corner=None, observation_days=observation_days
    )
    reply = _video_record(
        by_id[reply_video_id], corner=reply_corner, observation_days=observation_days
    )
    baseline_records = [
        _video_record(by_id[video_id], corner=reply_corner, observation_days=observation_days)
        for video_id in baselines
    ]
    if _parse_timestamp(reply["published_at"], "reply.published_at") > current:
        raise CommentReplyShortError("reply Short publication time is in the future")

    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest["status"] != "planned" or (
            manifest["plan_sha256"],
            manifest["created_at"],
        ) != identity:
            raise CommentReplyShortError(
                "experiment changed while video details were being read"
            )
        for existing in _all_manifests(spec):
            if (
                existing.get("experiment_id") != experiment_id
                and (existing.get("reply") or {}).get("video_id") == reply_video_id
                and existing.get("status") != "invalidated"
            ):
                raise CommentReplyShortError(
                    "experiment already exists for reply Short: "
                    f"{existing.get('experiment_id')}"
                )
        started = {
            **manifest,
            "status": "running",
            "started_at": current.isoformat(),
            "owned_channel_id": channel_id,
            "source_video": source,
            "reply": reply,
            "baselines": baseline_records,
            "manual_confirmation": {
                "comment_sticker_confirmed": True,
                "youtube_app_published_confirmed": True,
                "recent_same_type_baselines_confirmed": True,
                "youtube_write_performed": False,
            },
        }
        started["setup_sha256"] = _setup_checksum(started)
        started["running_sha256"] = _running_checksum(started)
        _validate_plan(started, path)
        _validate_status(started, path)
        _write_manifest(path, started)
    return started


def _validate_metrics_readback(payload: object, data: dict, probe_end: str) -> dict:
    if not isinstance(payload, dict):
        raise CommentReplyShortError("Analytics returned an invalid payload")
    if payload.get("source") != "youtube_analytics_api_v2":
        raise CommentReplyShortError("Analytics source is invalid")
    expected_metrics = [
        "views",
        "comments",
        "subscribersGained",
        "subscribersLost",
    ]
    if payload.get("metrics") != expected_metrics:
        raise CommentReplyShortError("Analytics metric provenance is invalid")
    if payload.get("availability_start_date") != data["reply"][
        "observation_start_date"
    ] or payload.get("availability_probe_end_date") != probe_end:
        raise CommentReplyShortError("Analytics availability period is inconsistent")
    rows = payload.get("videos")
    if not isinstance(rows, list):
        raise CommentReplyShortError("Analytics rows are missing")
    expected = [data["reply"], *data["baselines"]]
    if len(rows) != len(expected):
        raise CommentReplyShortError("Analytics row count is inconsistent")
    for row, video in zip(rows, expected):
        if not isinstance(row, dict):
            raise CommentReplyShortError("Analytics row is invalid")
        if (
            row.get("video_id") != video["video_id"]
            or row.get("start_date") != video["observation_start_date"]
            or row.get("end_date") != video["observation_end_date"]
        ):
            raise CommentReplyShortError("Analytics row provenance is inconsistent")
        for field in _COUNT_FIELDS:
            if field == "net_subscribers":
                _optional_count(row.get(field), field, signed=True)
            else:
                _optional_count(row.get(field), field)
        gained = row.get("subscribers_gained")
        lost = row.get("subscribers_lost")
        net = row.get("net_subscribers")
        if gained is not None and lost is not None and net != gained - lost:
            raise CommentReplyShortError("Analytics subscriber net is inconsistent")
    data_through = payload.get("data_through_date")
    if data_through is not None:
        _validate_date(data_through, "data_through_date")
        if not data["reply"]["observation_start_date"] <= data_through <= probe_end:
            raise CommentReplyShortError("Analytics data-through date is out of range")
    return payload


def _observation_from_row(role: str, row: dict) -> dict:
    views = row.get("views")
    comments = row.get("comments")
    gained = row.get("subscribers_gained")
    lost = row.get("subscribers_lost")
    net = row.get("net_subscribers")
    reasons: list[str] = []
    if views is None:
        reasons.append("viewsを取得できませんでした")
    elif views <= 0:
        reasons.append("viewsが0以下です")
    if comments is None:
        reasons.append("commentsを取得できませんでした")
    if gained is None or lost is None:
        reasons.append("登録者増減を取得できませんでした")
    valid = not reasons
    return {
        "role": role,
        "video_id": row["video_id"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "views": views,
        "comments": comments,
        "subscribers_gained": gained,
        "subscribers_lost": lost,
        "net_subscribers": net,
        "comments_per_1000_views": (
            round(comments * 1000.0 / views, 4) if valid else None
        ),
        "net_subscribers_per_1000_views": (
            round(net * 1000.0 / views, 4) if valid else None
        ),
        "valid_for_comparison": valid,
        "reason": "。".join(reasons),
    }


def _insufficient_reason(observations: list[dict], period_confirmed: bool) -> str:
    if not period_confirmed:
        return (
            "観測終了後7完了日を待ってもAnalyticsの利用可能期間を確認できませんでした。"
            "値は0とせず取得不可として記録します"
        )
    if not observations[0]["valid_for_comparison"]:
        return "返信Shortの指標が揃いませんでした: " + observations[0]["reason"]
    if not any(item["valid_for_comparison"] for item in observations[1:]):
        return "比較可能なbaseline指標がありませんでした"
    return ""


def _invalidated_comparison() -> dict:
    return {
        "status": "invalidated",
        "valid_baseline_count": 0,
        "required_baseline_count": MIN_COMPARABLE_BASELINES,
        "baseline_video_ids": [],
        "baseline_medians": {},
        "reply_minus_baseline_median": {},
        "universal_threshold_applied": False,
        "winner": None,
        "causal_conclusion": None,
    }


def _result_memo(data: dict) -> str:
    result = data["result"]
    comparison = result["comparison"]
    observations = result.get("observations") or []
    rows = []
    for item in observations:
        rows.append(
            "| {role} | `{video}` | {views} | {comments} | {net} | {comments_rate} | {net_rate} | {valid} |".format(
                role=item["role"],
                video=item["video_id"],
                views=item.get("views") if item.get("views") is not None else "取得不可",
                comments=item.get("comments") if item.get("comments") is not None else "取得不可",
                net=item.get("net_subscribers") if item.get("net_subscribers") is not None else "取得不可",
                comments_rate=item.get("comments_per_1000_views") if item.get("comments_per_1000_views") is not None else "算出なし",
                net_rate=item.get("net_subscribers_per_1000_views") if item.get("net_subscribers_per_1000_views") is not None else "算出なし",
                valid="比較可" if item.get("valid_for_comparison") else "保留",
            )
        )
    notes = str(result.get("notes") or "").strip() or "記載なし"
    return f"""# 次回企画メモ: コメントステッカー返信Short

- experiment_id: `{data['experiment_id']}`
- source_comment_id: `{_safe_cell(data['source_comment']['comment_id'])}`
- reply_video_id: `{data['reply']['video_id']}`
- outcome: `{result['status']}`
- 比較status: `{comparison['status']}`
- 観測期間: 各動画の公開翌日から{data['observation_days']}完了日
- Analytics期間確認: `{'confirmed' if result.get('analytics_period_confirmed') else 'unavailable'}`
- Analytics data through: `{result.get('analytics_data_through_date') or '取得不可'}`
- 記録日時: `{result['recorded_at']}`

| role | video_id | views | comments | net subscribers | comments/1,000 views | net subscribers/1,000 views | status |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
{chr(10).join(rows) if rows else '| invalidated | - | - | - | - | - | - | 設定変更 |'}

## 運用メモ

{notes}

コメント数と登録者増減は各動画の同じ公開後日数を記述比較したものです。登録者増減は
指定動画のwatch pageへ帰属した値だけで、チャンネル全体の増減ではありません。
3本未満のbaselineではmedianを表示せず、3本以上でも勝者や因果は断定しません。
"""


def complete_experiment(
    spec: ChannelSpec,
    experiment_id: str,
    *,
    setup_unchanged_confirmed: bool = False,
    setup_changed: bool = False,
    notes: str = "",
    now: datetime | None = None,
) -> dict:
    """同じ公開後日数のコメント・登録者指標をread-onlyで比較する。"""
    if setup_unchanged_confirmed == setup_changed:
        raise CommentReplyShortError(
            "choose exactly one of unchanged setup confirmation or setup changed"
        )
    current = _now(now)
    recorded_at = current.isoformat()
    clean_notes = " ".join(str(notes or "").split())[:1000]

    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest["status"] != "running":
            raise CommentReplyShortError("only a running experiment can be completed")
        running_identity = (
            manifest["started_at"],
            manifest["plan_sha256"],
            manifest["setup_sha256"],
            manifest["running_sha256"],
        )
        if setup_changed:
            result = {
                "status": "stopped_changed_setup",
                "reason": "コメントステッカー返信Shortまたは比較条件が変更されました",
                "observations": [],
                "comparison": _invalidated_comparison(),
                "recorded_at": recorded_at,
                "notes": clean_notes,
                "setup_unchanged_confirmed": False,
                "universal_threshold_applied": False,
                "winner": None,
                "causal_conclusion": None,
            }
            invalidated = {
                **manifest,
                "status": "invalidated",
                "completed_at": recorded_at,
                "result": result,
            }
            invalidated["terminal_sha256"] = _terminal_checksum(invalidated)
            _validate_result(invalidated, path)
            _write_text_atomic(
                path.parent / "next_idea_memo.md", _result_memo(invalidated)
            )
            _write_manifest(path, invalidated)
            return invalidated

        reply_end = date.fromisoformat(manifest["reply"]["observation_end_date"])
        current_pt = current.astimezone(ZoneInfo(OBSERVATION_TIME_ZONE)).date()
        if current_pt <= reply_end:
            raise CommentReplyShortError(
                "reply observation window is not complete; retry after "
                f"{reply_end.isoformat()} in {OBSERVATION_TIME_ZONE}"
            )
        probe_end = (current_pt - timedelta(days=1)).isoformat()
        windows = [
            {
                "video_id": item["video_id"],
                "start_date": item["observation_start_date"],
                "end_date": item["observation_end_date"],
            }
            for item in [manifest["reply"], *manifest["baselines"]]
        ]

    try:
        metrics = _validate_metrics_readback(
            youtube.comment_reply_short_metrics(
                windows,
                availability_start_date=manifest["reply"]["observation_start_date"],
                availability_end_date=probe_end,
                token_file=spec.publish.youtube.analytics_token,
                client_secret_file=spec.publish.youtube.client_secret,
            ),
            manifest,
            probe_end,
        )
    except Exception as exc:
        raise CommentReplyShortError(
            f"Analytics readback failed; experiment remains running: {exc}"
        ) from exc

    data_through = metrics.get("data_through_date")
    period_confirmed = isinstance(data_through, str) and data_through >= manifest[
        "reply"
    ]["observation_end_date"]
    settled = reply_end + timedelta(days=ANALYTICS_UNVERIFIABLE_WAIT_DAYS)
    if not period_confirmed and date.fromisoformat(probe_end) < settled:
        raise CommentReplyShortError(
            "Analytics data is not available through the reply observation end; "
            "experiment remains running"
        )
    if period_confirmed:
        observations = [
            _observation_from_row("reply" if index == 0 else "baseline", row)
            for index, row in enumerate(metrics["videos"])
        ]
    else:
        observations = _blank_observations(manifest)
    reason = _insufficient_reason(observations, period_confirmed)
    result_status = "observed" if not reason else "insufficient_data"
    result = {
        "status": result_status,
        "reason": reason,
        "requested_reply_end_date": manifest["reply"]["observation_end_date"],
        "availability_probe_end_date": probe_end,
        "analytics_data_through_date": data_through,
        "analytics_period_confirmed": period_confirmed,
        "observations": observations,
        "comparison": _comparison(observations),
        "recorded_at": recorded_at,
        "notes": clean_notes,
        "setup_unchanged_confirmed": True,
        "universal_threshold_applied": False,
        "winner": None,
        "causal_conclusion": None,
    }

    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest["status"] != "running" or (
            manifest["started_at"],
            manifest["plan_sha256"],
            manifest["setup_sha256"],
            manifest["running_sha256"],
        ) != running_identity:
            raise CommentReplyShortError(
                "experiment changed while Analytics was being read"
            )
        completed = {
            **manifest,
            "status": "completed",
            "completed_at": recorded_at,
            "result": result,
        }
        completed["terminal_sha256"] = _terminal_checksum(completed)
        _validate_result(completed, path)
        _write_text_atomic(path.parent / "next_idea_memo.md", _result_memo(completed))
        _write_manifest(path, completed)
    return completed


def show_experiment(spec: ChannelSpec, experiment_id: str) -> dict:
    _validate_root_readable(_root(spec))
    return _load_manifest(
        _manifest_path(spec, experiment_id), expected_channel=spec.id
    )


def _summary_group(key: tuple[str, str, int], entries: list[dict]) -> dict:
    ready = len(entries) >= MIN_COMPARABLE_BASELINES
    return {
        "reply_corner": key[0],
        "comparison_key": key[1],
        "observation_days": key[2],
        "status": "ready" if ready else "insufficient_observed_experiments",
        "observed_count": len(entries),
        "required_count": MIN_COMPARABLE_BASELINES,
        "median_reply_comments_per_1000_views": (
            _median([item["comments_per_1000_views"] for item in entries])
            if ready
            else None
        ),
        "median_reply_net_subscribers_per_1000_views": (
            _median([item["net_subscribers_per_1000_views"] for item in entries])
            if ready
            else None
        ),
        "experiments": [
            {
                "experiment_id": item["experiment_id"],
                "reply_video_id": item["video_id"],
                "comments_per_1000_views": item["comments_per_1000_views"],
                "net_subscribers_per_1000_views": item[
                    "net_subscribers_per_1000_views"
                ],
            }
            for item in entries
        ],
        "universal_threshold_applied": False,
        "winner": None,
        "causal_conclusion": None,
    }


def summarize_experiments(spec: ChannelSpec) -> dict:
    groups: dict[tuple[str, str, int], dict[str, dict]] = {}
    for manifest in _all_manifests(spec):
        result = manifest.get("result")
        if not isinstance(result, dict) or result.get("status") != "observed":
            continue
        reply = next(
            (
                item
                for item in result.get("observations", [])
                if item.get("role") == "reply" and item.get("valid_for_comparison")
            ),
            None,
        )
        if reply is None:
            continue
        entry = {
            **reply,
            "experiment_id": manifest["experiment_id"],
            "completed_at": manifest["completed_at"],
        }
        key = (
            manifest["reply_corner"],
            manifest["comparison_key"],
            manifest["observation_days"],
        )
        existing = groups.setdefault(key, {}).get(reply["video_id"])
        if existing is None or entry["completed_at"] > existing["completed_at"]:
            groups[key][reply["video_id"]] = entry
    return {
        "channel": spec.id,
        "groups": [
            _summary_group(
                key,
                sorted(groups[key].values(), key=lambda item: item["experiment_id"]),
            )
            for key in sorted(groups)
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "コメントステッカー返信Shortをローカル記録・read-only分析"
            "（YouTube書込みなし）"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--channel", required=True)
    plan.add_argument("--source-video-id", required=True)
    plan.add_argument("--source-comment-id", required=True)
    plan.add_argument("--request-summary", required=True)
    plan.add_argument("--reply-corner", required=True)
    plan.add_argument("--comparison-key", required=True)
    plan.add_argument("--observation-days", type=int, default=DEFAULT_OBSERVATION_DAYS)
    plan.add_argument("--confirm-question-or-request", action="store_true")

    start = subparsers.add_parser("start")
    start.add_argument("--channel", required=True)
    start.add_argument("--experiment-id", required=True)
    start.add_argument("--reply-video-id", required=True)
    start.add_argument("--baseline-video-id", action="append", default=[])
    start.add_argument("--confirm-comment-sticker", action="store_true")
    start.add_argument("--confirm-youtube-app-published", action="store_true")
    start.add_argument("--confirm-recent-same-type", action="store_true")

    complete = subparsers.add_parser("complete")
    complete.add_argument("--channel", required=True)
    complete.add_argument("--experiment-id", required=True)
    state = complete.add_mutually_exclusive_group(required=True)
    state.add_argument("--confirm-setup-unchanged", action="store_true")
    state.add_argument("--setup-changed", action="store_true")
    complete.add_argument("--notes", default="")

    show = subparsers.add_parser("show")
    show.add_argument("--channel", required=True)
    show.add_argument("--experiment-id", required=True)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--channel", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    spec = channel.load(args.channel)
    try:
        if args.command == "plan":
            result = plan_experiment(
                spec,
                source_video_id=args.source_video_id,
                source_comment_id=args.source_comment_id,
                request_summary=args.request_summary,
                reply_corner=args.reply_corner,
                comparison_key=args.comparison_key,
                observation_days=args.observation_days,
                question_or_request_confirmed=args.confirm_question_or_request,
            )
        elif args.command == "start":
            result = start_experiment(
                spec,
                args.experiment_id,
                reply_video_id=args.reply_video_id,
                baseline_video_ids=args.baseline_video_id,
                comment_sticker_confirmed=args.confirm_comment_sticker,
                youtube_app_published_confirmed=args.confirm_youtube_app_published,
                recent_same_type_baselines_confirmed=args.confirm_recent_same_type,
            )
        elif args.command == "complete":
            result = complete_experiment(
                spec,
                args.experiment_id,
                setup_unchanged_confirmed=args.confirm_setup_unchanged,
                setup_changed=args.setup_changed,
                notes=args.notes,
            )
        elif args.command == "show":
            result = show_experiment(spec, args.experiment_id)
        elif args.command == "summary":
            result = summarize_experiments(spec)
        else:
            raise CommentReplyShortError(f"unknown command: {args.command}")
    except (CommentReplyShortError, OSError, RuntimeError) as exc:
        print(f"[doci] コメント返信Short: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
