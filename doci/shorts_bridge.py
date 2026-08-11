"""Shortsから次の動画への橋渡しを手動検証する記録CLI（issue #138）。

YouTubeへの書込みは行わない。dociの投稿履歴にあるShortと同一チャンネルの
遷移先動画を固定し、Short終盤に実在する橋渡し文と、YouTube Studioで手動設定した
関連動画を記録する。観測完了後はYouTube Analytics APIをread-onlyで照会し、同一
期間の元Short視聴数と、RELATED_VIDEOの参照元が元Shortと確認できた遷移先視聴数を
保存する。

公式資料はShortsの関連動画リンクがRELATED_VIDEOへ必ず分類されるとは明記して
いない。そのため、該当する参照元行が返らない場合を0件とせず判定材料不足にする。
5%などの万能な合格ラインも置かず、同じcorner・source/target tier・観測日数を持つ
3件以上の観測だけを相対比較する。
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
OFFICIAL_HELP_URL = "https://support.google.com/youtube/answer/14075157?hl=ja"
ANALYTICS_DIMENSIONS_URL = (
    "https://developers.google.com/youtube/analytics/dimensions"
)
DECISION_METRIC = "related_video_attributed_views_per_source_short_view"
OBSERVATION_TIME_ZONE = "America/Los_Angeles"
DEFAULT_OBSERVATION_DAYS = 7
MIN_OBSERVATION_DAYS = 1
MAX_OBSERVATION_DAYS = 28
MIN_COMPARABLE_EXPERIMENTS = 3
ANALYTICS_UNVERIFIABLE_WAIT_DAYS = 7
ACTIVE_STATUSES = frozenset({"planned", "running"})
VALID_STATUSES = frozenset({"planned", "running", "completed", "invalidated"})
VALID_RESULT_STATUSES = frozenset(
    {"observed", "insufficient_data", "stopped_changed_setup"}
)
SHORT_TIERS = frozenset({"short", "long_short"})
VALID_TIERS = frozenset({"short", "long_short", "longform"})
_EXPERIMENT_ID_RE = re.compile(r"sbr-[0-9a-f]{16}")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_TIMESTAMP_FIELD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_FIELD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAN_FIELDS = (
    "schema_version",
    "experiment_id",
    "channel",
    "created_at",
    "official_help_url",
    "analytics_dimensions_url",
    "decision_metric",
    "observation_days",
    "bridge_setup",
    "source",
    "target",
    "warnings",
)


class ShortsBridgeError(ValueError):
    """Shorts橋渡し計画または状態遷移を安全に実行できない。"""


def _root(spec: ChannelSpec) -> Path:
    return spec.output_dir / "shorts_bridge_tests"


def _ensure_root_dir(path: Path) -> Path:
    if path.is_symlink():
        raise ShortsBridgeError(
            f"shorts bridge test root must not be a symlink: {path}"
        )
    if path.exists() and not path.is_dir():
        raise ShortsBridgeError(
            f"shorts bridge test root is not a directory: {path}"
        )
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ShortsBridgeError(
            f"shorts bridge test root must not be a symlink: {path}"
        )
    return path


def _validate_root_readable(path: Path) -> None:
    if path.is_symlink():
        raise ShortsBridgeError(
            f"shorts bridge test root must not be a symlink: {path}"
        )
    if not path.is_dir():
        raise ShortsBridgeError(
            f"shorts bridge test root is not a directory: {path}"
        )


def _manifest_path(spec: ChannelSpec, experiment_id: str) -> Path:
    if not _EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise ShortsBridgeError(f"invalid experiment id: {experiment_id!r}")
    directory = _root(spec) / experiment_id
    if directory.is_symlink():
        raise ShortsBridgeError(
            f"shorts bridge test directory must not be a symlink: {directory}"
        )
    if directory.name != experiment_id:
        raise ShortsBridgeError(
            f"shorts bridge test directory name mismatch: {directory.name!r}"
        )
    return directory / "manifest.json"


def _now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ShortsBridgeError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _now_iso(now: datetime | None) -> str:
    return _now(now).isoformat()


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    root = _ensure_root_dir(_root(spec))
    path = root / ".shorts_bridge.lock"
    if path.is_symlink():
        raise ShortsBridgeError(
            f"shorts bridge lock must not be a symlink: {path}"
        )
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_manifest(path: Path, manifest: dict) -> None:
    _write_text_atomic(
        path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )


def _plan_checksum(manifest: dict) -> str:
    stable = {key: manifest[key] for key in _PLAN_FIELDS if key in manifest}
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _validate_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_FIELD_RE.fullmatch(value):
        raise ShortsBridgeError(f"{label} must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShortsBridgeError(
            f"{label} is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShortsBridgeError(f"{label} must include a UTC offset")
    return value


def _validate_date(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DATE_FIELD_RE.fullmatch(value):
        raise ShortsBridgeError(f"{label} must be a YYYY-MM-DD string")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ShortsBridgeError(f"{label} is not a valid date: {value!r}") from exc
    return value


def _validate_video_record(record: object, role: str) -> dict:
    if not isinstance(record, dict):
        raise ShortsBridgeError(f"{role} must be an object")
    video_id = record.get("video_id")
    if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
        raise ShortsBridgeError(f"{role}.video_id is invalid")
    tier = record.get("tier")
    if tier not in VALID_TIERS:
        raise ShortsBridgeError(f"{role}.tier is invalid")
    if role == "source" and tier not in SHORT_TIERS:
        raise ShortsBridgeError("source tier must be a YouTube Short tier")
    if role == "target" and tier != "longform":
        raise ShortsBridgeError("target tier must be longform")
    if record.get("youtube_privacy") not in {"public", "unlisted"}:
        raise ShortsBridgeError(
            f"{role}.youtube_privacy must be public or unlisted"
        )
    corner = record.get("corner")
    if not isinstance(corner, str) or not corner.strip():
        raise ShortsBridgeError(f"{role}.corner must be a non-empty string")
    return record


def _validate_manifest_plan(data: dict, path: Path) -> None:
    if not isinstance(data, dict):
        raise ShortsBridgeError(f"invalid manifest: {path}")
    if path.name != "manifest.json":
        raise ShortsBridgeError(f"manifest file name mismatch: {path.name!r}")
    experiment_id = str(data.get("experiment_id") or "")
    if experiment_id != path.parent.name:
        raise ShortsBridgeError(
            "experiment_id/directory mismatch: "
            f"{experiment_id!r} != {path.parent.name!r}"
        )
    missing = [key for key in _PLAN_FIELDS if key not in data]
    if missing:
        raise ShortsBridgeError(
            f"shorts bridge manifest is missing fields: {', '.join(missing)}"
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ShortsBridgeError(
            f"unsupported schema version: {data.get('schema_version')}"
        )
    if data.get("status") not in VALID_STATUSES:
        raise ShortsBridgeError(f"invalid status: {data.get('status')!r}")
    _validate_timestamp(data.get("created_at"), "created_at")
    if data.get("decision_metric") != DECISION_METRIC:
        raise ShortsBridgeError("decision_metric is invalid")
    if data.get("official_help_url") != OFFICIAL_HELP_URL:
        raise ShortsBridgeError("official_help_url is invalid")
    if data.get("analytics_dimensions_url") != ANALYTICS_DIMENSIONS_URL:
        raise ShortsBridgeError("analytics_dimensions_url is invalid")
    observation_days = data.get("observation_days")
    if (
        isinstance(observation_days, bool)
        or not isinstance(observation_days, int)
        or not MIN_OBSERVATION_DAYS <= observation_days <= MAX_OBSERVATION_DAYS
    ):
        raise ShortsBridgeError("observation_days is out of range")
    setup = data.get("bridge_setup")
    if not isinstance(setup, dict):
        raise ShortsBridgeError("bridge_setup must be an object")
    if setup.get("content_direct_confirmed") is not True:
        raise ShortsBridgeError("content-direct confirmation is missing")
    if setup.get("youtube_write_performed") is not False:
        raise ShortsBridgeError("youtube_write_performed must be false")
    bridge_text = setup.get("bridge_text")
    if not isinstance(bridge_text, str) or not 4 <= len(bridge_text) <= 300:
        raise ShortsBridgeError("bridge_text length is invalid")
    source = _validate_video_record(data.get("source"), "source")
    target = _validate_video_record(data.get("target"), "target")
    if setup.get("source_video_id") != source["video_id"]:
        raise ShortsBridgeError("bridge_setup.source_video_id is inconsistent")
    if setup.get("target_video_id") != target["video_id"]:
        raise ShortsBridgeError("bridge_setup.target_video_id is inconsistent")
    if source["video_id"] == target["video_id"]:
        raise ShortsBridgeError("source and target video ids must differ")
    narration_length = setup.get("narration_char_count")
    final_start = setup.get("final_section_start_char")
    bridge_start = setup.get("bridge_start_char")
    bridge_end = setup.get("bridge_end_char")
    if (
        isinstance(narration_length, bool)
        or not isinstance(narration_length, int)
        or narration_length <= 0
        or setup.get("final_section") != "last_third"
    ):
        raise ShortsBridgeError("bridge narration boundary is invalid")
    expected_final_start = narration_length * 2 // 3
    if final_start != expected_final_start:
        raise ShortsBridgeError("final section start is inconsistent")
    if (
        isinstance(bridge_start, bool)
        or not isinstance(bridge_start, int)
        or isinstance(bridge_end, bool)
        or not isinstance(bridge_end, int)
        or bridge_start < expected_final_start
        or bridge_end != bridge_start + len(bridge_text)
        or bridge_end > narration_length
    ):
        raise ShortsBridgeError("bridge text is outside the final third")
    narration_sha256 = source.get("narration_sha256")
    if not isinstance(narration_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", narration_sha256
    ):
        raise ShortsBridgeError("source.narration_sha256 is invalid")
    warnings = data.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        raise ShortsBridgeError("warnings must be a list of strings")
    expected = _plan_checksum(data)
    actual = str(data.get("plan_sha256") or "")
    if not hmac.compare_digest(actual, expected):
        raise ShortsBridgeError("shorts bridge plan checksum mismatch")


def _validate_manifest_status(data: dict) -> None:
    status = str(data.get("status") or "")
    if status == "planned":
        for field in (
            "started_at",
            "observation_start_date",
            "observation_end_date",
            "result",
        ):
            if field in data:
                raise ShortsBridgeError(f"planned manifest must not have {field}")
        return
    if status == "running":
        _validate_timestamp(data.get("started_at"), "started_at")
        start = _validate_date(
            data.get("observation_start_date"), "observation_start_date"
        )
        end = _validate_date(data.get("observation_end_date"), "observation_end_date")
        expected_end = (
            date.fromisoformat(start) + timedelta(days=data["observation_days"] - 1)
        ).isoformat()
        if end != expected_end:
            raise ShortsBridgeError("observation window length is inconsistent")
        if "result" in data or "completed_at" in data:
            raise ShortsBridgeError("running manifest must not have terminal fields")
        return
    if status in {"completed", "invalidated"}:
        _validate_timestamp(data.get("started_at"), "started_at")
        _validate_timestamp(data.get("completed_at"), "completed_at")
        _validate_date(data.get("observation_start_date"), "observation_start_date")
        _validate_date(data.get("observation_end_date"), "observation_end_date")
        if not isinstance(data.get("result"), dict):
            raise ShortsBridgeError("terminal manifest must have result")
        return
    raise ShortsBridgeError(f"invalid status: {status!r}")


def _validate_optional_count(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShortsBridgeError(f"{label} must be a non-negative integer or null")
    return value


def _validate_manifest_result(data: dict, path: Path) -> None:
    _validate_manifest_plan(data, path)
    _validate_manifest_status(data)
    result = data.get("result")
    if not isinstance(result, dict):
        raise ShortsBridgeError("result must be an object")
    result_status = result.get("status")
    if result_status not in VALID_RESULT_STATUSES:
        raise ShortsBridgeError(f"invalid result status: {result_status!r}")
    _validate_timestamp(result.get("recorded_at"), "result.recorded_at")
    source_views = _validate_optional_count(result.get("source_views"), "source_views")
    attributed_views = _validate_optional_count(
        result.get("attributed_target_views"), "attributed_target_views"
    )
    ratio = result.get("transition_ratio_percent")
    if result.get("universal_threshold_applied") is not False:
        raise ShortsBridgeError("universal_threshold_applied must be false")
    if result_status != "stopped_changed_setup":
        if result.get("requested_start_date") != data.get(
            "observation_start_date"
        ) or result.get("requested_end_date") != data.get("observation_end_date"):
            raise ShortsBridgeError("result requested period is inconsistent")
        requested_start = str(data["observation_start_date"])
        requested_end = str(data["observation_end_date"])
        probe_end = _validate_date(
            result.get("availability_probe_end_date"),
            "result.availability_probe_end_date",
        )
        if probe_end < requested_end:
            raise ShortsBridgeError("availability probe ends before observation")
        data_through_raw = result.get("views_data_through_date")
        data_through = (
            _validate_date(data_through_raw, "result.views_data_through_date")
            if data_through_raw is not None
            else None
        )
        if data_through is not None and not requested_start <= data_through <= probe_end:
            raise ShortsBridgeError("views data-through date is outside the probe period")
        period_confirmed = result.get("analytics_period_confirmed")
        if not isinstance(period_confirmed, bool):
            raise ShortsBridgeError("analytics_period_confirmed must be boolean")
        if period_confirmed:
            if data_through is None or data_through < requested_end:
                raise ShortsBridgeError(
                    "result does not cover the full observation period"
                )
        else:
            settled_date = (
                date.fromisoformat(requested_end)
                + timedelta(days=ANALYTICS_UNVERIFIABLE_WAIT_DAYS)
            ).isoformat()
            if result_status != "insufficient_data" or probe_end < settled_date:
                raise ShortsBridgeError(
                    "unconfirmed Analytics period must wait and be insufficient_data"
                )
            if any(
                value is not None for value in (source_views, attributed_views, ratio)
            ):
                raise ShortsBridgeError(
                    "unconfirmed Analytics period must not contain metrics"
                )
        if result.get("attribution_source_type") != "RELATED_VIDEO":
            raise ShortsBridgeError("result attribution source type is invalid")
        if result.get("attribution_detail_limit") != 25:
            raise ShortsBridgeError("result attribution detail limit is invalid")
    if result_status == "observed":
        if data.get("status") != "completed":
            raise ShortsBridgeError("observed result must be completed")
        if source_views is None or source_views <= 0 or attributed_views is None:
            raise ShortsBridgeError("observed result requires both view counts")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise ShortsBridgeError("observed result requires a numeric ratio")
        expected = attributed_views * 100.0 / source_views
        if not math.isclose(float(ratio), expected, abs_tol=0.0001):
            raise ShortsBridgeError("transition ratio is inconsistent with view counts")
        if result.get("setup_unchanged_confirmed") is not True:
            raise ShortsBridgeError("observed result requires unchanged setup confirmation")
    elif result_status == "insufficient_data":
        if data.get("status") != "completed":
            raise ShortsBridgeError("insufficient_data result must be completed")
        if ratio is not None:
            raise ShortsBridgeError("insufficient_data result must not have a ratio")
        if not str(result.get("reason") or "").strip():
            raise ShortsBridgeError("insufficient_data result requires a reason")
        if result.get("setup_unchanged_confirmed") is not True:
            raise ShortsBridgeError(
                "insufficient_data result requires unchanged setup confirmation"
            )
    else:
        if data.get("status") != "invalidated":
            raise ShortsBridgeError("changed setup result must be invalidated")
        if any(value is not None for value in (source_views, attributed_views, ratio)):
            raise ShortsBridgeError("invalidated result must not contain metrics")
        if result.get("setup_unchanged_confirmed") is not False:
            raise ShortsBridgeError(
                "changed setup result must not confirm unchanged setup"
            )
    comparison = result.get("comparison")
    if not isinstance(comparison, dict):
        raise ShortsBridgeError("result.comparison must be an object")


def _load_manifest(path: Path, *, expected_channel: str | None = None) -> dict:
    if path.is_symlink():
        raise ShortsBridgeError(f"manifest must not be a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShortsBridgeError(f"cannot read shorts bridge manifest: {path}") from exc
    _validate_manifest_plan(data, path)
    _validate_manifest_status(data)
    if data.get("status") in {"completed", "invalidated"}:
        _validate_manifest_result(data, path)
    if expected_channel is not None and data.get("channel") != expected_channel:
        raise ShortsBridgeError(
            f"manifest channel mismatch: {data.get('channel')!r} != {expected_channel!r}"
        )
    return data


def _all_manifests(spec: ChannelSpec) -> list[dict]:
    root = _root(spec)
    if not root.exists():
        return []
    _validate_root_readable(root)
    manifests: list[dict] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            raise ShortsBridgeError(
                f"shorts bridge test directory must not be a symlink: {child}"
            )
        if not _EXPERIMENT_ID_RE.fullmatch(child.name):
            continue
        path = child / "manifest.json"
        if not path.is_file():
            raise ShortsBridgeError(f"shorts bridge manifest missing: {path}")
        manifests.append(_load_manifest(path, expected_channel=spec.id))
    return manifests


def _history_video(spec: ChannelSpec, video_id: str, *, source: bool) -> dict:
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise ShortsBridgeError(f"invalid YouTube video id: {video_id!r}")
    if not spec.history_file.exists():
        raise ShortsBridgeError(f"doci history is missing: {spec.history_file}")
    found: dict | None = None
    for line in spec.history_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("video_id") or "") == video_id:
            found = row
    if found is None:
        raise ShortsBridgeError(f"video is not present in doci history: {video_id}")
    if str(found.get("status") or "") != "published":
        raise ShortsBridgeError("video is not recorded as published")
    if found.get("youtube_privacy") not in {"public", "unlisted"}:
        raise ShortsBridgeError(
            "video privacy must be recorded as public or unlisted"
        )
    tier = str(found.get("tier") or "")
    if tier not in VALID_TIERS:
        raise ShortsBridgeError(f"video tier is invalid: {tier!r}")
    if source and tier not in SHORT_TIERS:
        raise ShortsBridgeError("source video must be a YouTube Short")
    if not source and tier != "longform":
        raise ShortsBridgeError("target video must be a regular longform video")
    corner = str(found.get("corner") or "").strip()
    if not corner:
        raise ShortsBridgeError("video history is missing corner")
    return found


def _normalise_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _bridge_narration_evidence(
    recorded: dict, bridge_text: str
) -> tuple[str, str, dict[str, int]]:
    workdir_raw = str(recorded.get("workdir") or "").strip()
    if not workdir_raw:
        raise ShortsBridgeError("source history is missing workdir")
    workdir = Path(workdir_raw)
    if workdir.is_symlink() or not workdir.is_dir():
        raise ShortsBridgeError("source workdir must be a real directory")
    script_path = workdir / "script.json"
    if script_path.is_symlink() or not script_path.is_file():
        raise ShortsBridgeError("source script.json is missing or unsafe")
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShortsBridgeError("source script.json cannot be read") from exc
    if not isinstance(script, dict):
        raise ShortsBridgeError("source script.json must contain an object")
    narration = _normalise_text(str(script.get("narration") or ""))
    bridge = _normalise_text(bridge_text)
    if not 4 <= len(bridge) <= 300:
        raise ShortsBridgeError("bridge text must be 4 to 300 characters")
    bridge_start = narration.rfind(bridge)
    final_start = len(narration) * 2 // 3
    if bridge_start < final_start:
        raise ShortsBridgeError(
            "bridge text must start in the final third of source narration"
        )
    digest = hashlib.sha256(narration.encode("utf-8")).hexdigest()
    return bridge, digest, {
        "narration_char_count": len(narration),
        "final_section_start_char": final_start,
        "bridge_start_char": bridge_start,
        "bridge_end_char": bridge_start + len(bridge),
    }


def _video_record(recorded: dict, *, narration_sha256: str | None = None) -> dict:
    result = {
        "video_id": str(recorded.get("video_id") or ""),
        "title": str(recorded.get("title") or ""),
        "history_ts": str(recorded.get("ts") or ""),
        "workdir": str(recorded.get("workdir") or ""),
        "corner": str(recorded.get("corner") or ""),
        "tier": str(recorded.get("tier") or ""),
        "youtube_privacy": recorded.get("youtube_privacy"),
    }
    if narration_sha256 is not None:
        result["narration_sha256"] = narration_sha256
    return result


def _safe_cell(value: object) -> str:
    return str(value or "").replace("|", "｜").replace("\n", " ")


def _plan_markdown(manifest: dict) -> str:
    setup = manifest["bridge_setup"]
    source = manifest["source"]
    target = manifest["target"]
    warnings = "\n".join(f"- {item}" for item in manifest["warnings"])
    return f"""# Shorts関連動画への橋渡し検証計画

- experiment_id: `{manifest['experiment_id']}`
- 元Short: `{source['video_id']}`（corner: `{_safe_cell(source['corner'])}`）
- 遷移先: `{target['video_id']}`（tier: `{target['tier']}`）
- 終盤の橋渡し文: `{_safe_cell(setup['bridge_text'])}`
- 観測期間: 太平洋時間の完了日 {manifest['observation_days']}日分
- 判定材料: 元Short視聴数と、RELATED_VIDEOで参照元Shortを確認できた遷移先視聴数
- 公式設定手順: {OFFICIAL_HELP_URL}
- Analyticsディメンション: {ANALYTICS_DIMENSIONS_URL}

## 実施手順

1. 元Shortの終盤に上記の橋渡し文が入っていることを確認します。
2. YouTube Studioで元Shortを開き、内容が直結する遷移先を関連動画として手動設定します。
3. 設定完了後に`start`を記録します。dociはYouTubeへ書き込みません。
4. 観測中は橋渡し文と関連動画の組を変更しません。変更した場合は
   `--setup-changed`で実験を無効化します。
5. 観測期間が終わった後、`complete --confirm-setup-unchanged`で同一期間の
   Analyticsをread-only取得します。

## 判定上の注意

{warnings}
"""


def _comparison_key(manifest: dict) -> tuple[str, str, str, int]:
    return (
        str((manifest.get("source") or {}).get("corner") or ""),
        str((manifest.get("source") or {}).get("tier") or ""),
        str((manifest.get("target") or {}).get("tier") or ""),
        int(manifest.get("observation_days") or 0),
    )


def _observed_entry(manifest: dict) -> dict | None:
    result = manifest.get("result")
    if not isinstance(result, dict) or result.get("status") != "observed":
        return None
    ratio = result.get("transition_ratio_percent")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return None
    return {
        "experiment_id": manifest.get("experiment_id"),
        "source_video_id": (manifest.get("source") or {}).get("video_id"),
        "target_video_id": (manifest.get("target") or {}).get("video_id"),
        "transition_ratio_percent": float(ratio),
    }


def _group_summary(key: tuple[str, str, str, int], entries: list[dict]) -> dict:
    ratios = [entry["transition_ratio_percent"] for entry in entries]
    ready = len(entries) >= MIN_COMPARABLE_EXPERIMENTS
    return {
        "comparison_key": {
            "source_corner": key[0],
            "source_tier": key[1],
            "target_tier": key[2],
            "observation_days": key[3],
        },
        "status": "ready" if ready else "insufficient_comparable_experiments",
        "comparable_count": len(entries),
        "required_count": MIN_COMPARABLE_EXPERIMENTS,
        "median_transition_ratio_percent": (
            round(float(statistics.median(ratios)), 4) if ready else None
        ),
        "experiments": sorted(entries, key=lambda item: item["experiment_id"]),
        "universal_threshold_applied": False,
    }


def summarize_experiments(spec: ChannelSpec) -> dict:
    """比較可能な観測をgroup化し、3件以上だけmedianを返す。"""
    groups: dict[tuple[str, str, str, int], list[dict]] = {}
    for manifest in _all_manifests(spec):
        entry = _observed_entry(manifest)
        if entry is None:
            continue
        groups.setdefault(_comparison_key(manifest), []).append(entry)
    return {
        "channel": spec.id,
        "groups": [
            _group_summary(key, groups[key])
            for key in sorted(groups)
        ],
    }


def _comparison_with_current(spec: ChannelSpec, manifest: dict) -> dict:
    current_entry = _observed_entry(manifest)
    entries: list[dict] = []
    for existing in _all_manifests(spec):
        if existing.get("experiment_id") == manifest.get("experiment_id"):
            continue
        if _comparison_key(existing) != _comparison_key(manifest):
            continue
        entry = _observed_entry(existing)
        if entry is not None:
            entries.append(entry)
    if current_entry is None:
        return {
            **_group_summary(_comparison_key(manifest), entries),
            "status": "current_experiment_not_observed",
        }
    entries.append(current_entry)
    return _group_summary(_comparison_key(manifest), entries)


def _result_memo(manifest: dict) -> str:
    result = manifest["result"]
    comparison = result["comparison"]
    ratio = result.get("transition_ratio_percent")
    ratio_text = f"{float(ratio):.4f}%" if ratio is not None else "算出なし"
    source_views = result.get("source_views")
    source_text = str(source_views) if source_views is not None else "取得不可"
    attributed = result.get("attributed_target_views")
    attributed_text = str(attributed) if attributed is not None else "取得不可"
    median = comparison.get("median_transition_ratio_percent")
    median_text = f"{float(median):.4f}%" if median is not None else "算出なし"
    period_confirmed = (
        "確認済み" if result.get("analytics_period_confirmed") else "確認不可"
    )
    data_through = str(result.get("views_data_through_date") or "取得不可")
    notes = str(result.get("notes") or "").strip() or "記載なし"
    return f"""# 次回企画メモ: Shorts関連動画への橋渡し

- experiment_id: `{manifest['experiment_id']}`
- 元Short: `{manifest['source']['video_id']}`
- 遷移先: `{manifest['target']['video_id']}`
- outcome: `{result['status']}`
- 観測期間: `{manifest['observation_start_date']}`〜`{manifest['observation_end_date']}`
- Analytics期間: `{period_confirmed}`（日次行: `{data_through}`まで）
- availability確認日: `{result.get('availability_probe_end_date') or '取得不可'}`
- 元Short視聴数: `{source_text}`
- 参照元Shortを確認できた遷移先視聴数: `{attributed_text}`
- 視聴遷移比: `{ratio_text}`
- 比較可能な観測数: `{comparison['comparable_count']}` / 必要数 `{comparison['required_count']}`
- 比較groupのmedian: `{median_text}`
- 記録日時: `{result['recorded_at']}`

## 運用メモ

{notes}

視聴遷移比はクリック率ではなく、同一期間の「RELATED_VIDEOで参照元Shortを確認できた
遷移先視聴数 ÷ 元Short視聴数」です。該当行が返らない場合は0件と断定しません。
5%などの万能な合格ラインは使わず、同じcorner・source/target tier・観測日数の観測が
3件以上揃った場合だけ相対的なmedianを参考表示します。因果や勝者は自動判定しません。
"""


def plan_experiment(
    spec: ChannelSpec,
    *,
    source_video_id: str,
    target_video_id: str,
    bridge_text: str,
    observation_days: int = DEFAULT_OBSERVATION_DAYS,
    content_direct_confirmed: bool = False,
    now: datetime | None = None,
    experiment_id: str | None = None,
) -> dict:
    """公開済みShortと遷移先、実在する終盤の橋渡し文を固定する。"""
    if not content_direct_confirmed:
        raise ShortsBridgeError(
            "confirm that the target directly continues the source Short's content"
        )
    if (
        isinstance(observation_days, bool)
        or not isinstance(observation_days, int)
        or not MIN_OBSERVATION_DAYS <= observation_days <= MAX_OBSERVATION_DAYS
    ):
        raise ShortsBridgeError(
            f"observation_days must be {MIN_OBSERVATION_DAYS} to {MAX_OBSERVATION_DAYS}"
        )
    if source_video_id == target_video_id:
        raise ShortsBridgeError("source and target video ids must differ")
    source = _history_video(spec, source_video_id, source=True)
    target_record = _history_video(spec, target_video_id, source=False)
    normalised_bridge, narration_sha256, narration_evidence = _bridge_narration_evidence(
        source, bridge_text
    )
    created_at = _now_iso(now)

    with _operation_lock(spec):
        for existing in _all_manifests(spec):
            if (
                (existing.get("source") or {}).get("video_id") == source_video_id
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise ShortsBridgeError(
                    "active shorts bridge test already exists for source video "
                    f"{source_video_id}: {existing.get('experiment_id')}"
                )
        candidate_id = experiment_id or f"sbr-{uuid.uuid4().hex[:16]}"
        destination = _manifest_path(spec, candidate_id).parent
        if destination.exists():
            raise ShortsBridgeError(f"experiment already exists: {candidate_id}")
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
                "analytics_dimensions_url": ANALYTICS_DIMENSIONS_URL,
                "decision_metric": DECISION_METRIC,
                "observation_days": observation_days,
                "bridge_setup": {
                    "source_video_id": source_video_id,
                    "target_video_id": target_video_id,
                    "bridge_text": normalised_bridge,
                    "final_section": "last_third",
                    **narration_evidence,
                    "content_direct_confirmed": True,
                    "youtube_write_performed": False,
                },
                "source": _video_record(
                    source, narration_sha256=narration_sha256
                ),
                "target": _video_record(target_record),
                "warnings": [
                    "Shortsの関連動画設定はYouTube Studioで手動実施します。",
                    "RELATED_VIDEOに元Shortの行が無ければ0件とせず判定材料不足にします。",
                    "Analytics期間を確認できなければ再試行し、7完了日後は取得不可で閉じます。",
                    "視聴遷移比はクリック率ではなく、5%等の万能基準を使いません。",
                ],
            }
            manifest["plan_sha256"] = _plan_checksum(manifest)
            path = staging / "manifest.json"
            _write_manifest(path, manifest)
            _validate_manifest_plan(manifest, destination / "manifest.json")
            _validate_manifest_status(manifest)
            _write_text_atomic(staging / "plan.md", _plan_markdown(manifest))
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return manifest


def start_experiment(
    spec: ChannelSpec,
    experiment_id: str,
    *,
    studio_setup_confirmed: bool = False,
    now: datetime | None = None,
) -> dict:
    """Studioの関連動画設定を確認し、次の太平洋時間完了日から観測する。"""
    if not studio_setup_confirmed:
        raise ShortsBridgeError(
            "confirm that the related video was set in YouTube Studio"
        )
    current = _now(now)
    first_full_day = (
        current.astimezone(ZoneInfo(OBSERVATION_TIME_ZONE)).date()
        + timedelta(days=1)
    )
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "planned":
            raise ShortsBridgeError("only a planned shorts bridge test can be started")
        for existing in _all_manifests(spec):
            if (
                existing.get("experiment_id") != experiment_id
                and (existing.get("source") or {}).get("video_id")
                == manifest["source"]["video_id"]
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise ShortsBridgeError(
                    "another active shorts bridge test exists for this source video: "
                    f"{existing.get('experiment_id')}"
                )
        last_day = first_full_day + timedelta(
            days=int(manifest["observation_days"]) - 1
        )
        manifest = {
            **manifest,
            "status": "running",
            "started_at": current.isoformat(),
            "observation_start_date": first_full_day.isoformat(),
            "observation_end_date": last_day.isoformat(),
        }
        _validate_manifest_plan(manifest, path)
        _validate_manifest_status(manifest)
        _write_manifest(path, manifest)
    return manifest


def _insufficient_reason(metrics: dict) -> str:
    reasons: list[str] = []
    source_views = metrics.get("source_views")
    attributed = metrics.get("attributed_target_views")
    if source_views is None:
        reasons.append("元Shortのviews行を取得できませんでした")
    elif isinstance(source_views, bool) or not isinstance(source_views, int):
        reasons.append("元Shortのviewsが不正です")
    elif source_views <= 0:
        reasons.append("元Shortのviewsが0以下です")
    if attributed is None:
        reasons.append(
            "RELATED_VIDEOの上位25参照元に元Shortを確認できませんでした"
        )
    elif isinstance(attributed, bool) or not isinstance(attributed, int):
        reasons.append("遷移先viewsが不正です")
    elif attributed < 0:
        reasons.append("遷移先viewsが負数です")
    return "。".join(reasons)


def _validate_metric_provenance(metrics: object, query: dict) -> dict:
    if not isinstance(metrics, dict):
        raise ShortsBridgeError("Analytics readback returned an invalid payload")
    expected = {
        "source_video_id": query["source_video_id"],
        "target_video_id": query["target_video_id"],
        "start_date": query["start_date"],
        "end_date": query["end_date"],
        "availability_probe_end_date": query["availability_probe_end_date"],
        "attribution_source_type": "RELATED_VIDEO",
        "attribution_detail_limit": 25,
    }
    for key, value in expected.items():
        if metrics.get(key) != value:
            raise ShortsBridgeError(
                f"Analytics readback provenance mismatch for {key}"
            )
    data_through = metrics.get("views_data_through_date")
    if data_through is not None:
        _validate_date(data_through, "views_data_through_date")
        if not query["start_date"] <= data_through <= query[
            "availability_probe_end_date"
        ]:
            raise ShortsBridgeError(
                "Analytics readback data-through date is outside the requested period"
            )
    return metrics


def complete_experiment(
    spec: ChannelSpec,
    experiment_id: str,
    *,
    setup_unchanged_confirmed: bool = False,
    setup_changed: bool = False,
    notes: str = "",
    now: datetime | None = None,
) -> dict:
    """同一期間のAnalyticsを読み、観測または判定材料不足として記録する。"""
    if setup_unchanged_confirmed == setup_changed:
        raise ShortsBridgeError(
            "choose exactly one of unchanged setup confirmation or setup changed"
        )
    current = _now(now)
    recorded_at = current.isoformat()
    clean_notes = _normalise_text(notes)[:1000]

    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "running":
            raise ShortsBridgeError("only a running shorts bridge test can be completed")
        running_identity = (
            manifest.get("started_at"),
            manifest.get("plan_sha256"),
        )
        if setup_changed:
            result = {
                "status": "stopped_changed_setup",
                "source_views": None,
                "attributed_target_views": None,
                "transition_ratio_percent": None,
                "reason": "橋渡し文または関連動画設定が観測中に変更されました",
                "recorded_at": recorded_at,
                "notes": clean_notes,
                "setup_unchanged_confirmed": False,
                "universal_threshold_applied": False,
            }
            invalidated = {
                **manifest,
                "status": "invalidated",
                "completed_at": recorded_at,
                "result": result,
            }
            result["comparison"] = _comparison_with_current(spec, invalidated)
            _validate_manifest_result(invalidated, path)
            _write_text_atomic(path.parent / "next_idea_memo.md", _result_memo(invalidated))
            _write_manifest(path, invalidated)
            return invalidated

        end_date = date.fromisoformat(manifest["observation_end_date"])
        current_pt_date = current.astimezone(
            ZoneInfo(OBSERVATION_TIME_ZONE)
        ).date()
        if current_pt_date <= end_date:
            raise ShortsBridgeError(
                "observation window is not complete; complete it after "
                f"{end_date.isoformat()} in {OBSERVATION_TIME_ZONE}"
            )
        query = {
            "source_video_id": manifest["source"]["video_id"],
            "target_video_id": manifest["target"]["video_id"],
            "start_date": manifest["observation_start_date"],
            "end_date": manifest["observation_end_date"],
            "availability_probe_end_date": (
                current_pt_date - timedelta(days=1)
            ).isoformat(),
        }

    # ネットワーク待機中にチャンネルの記録lockを保持しない。取得失敗時はrunningの
    # manifestを変更せず、再試行できる状態を残す。
    try:
        metrics = _validate_metric_provenance(
            youtube.shorts_bridge_metrics(
                query["source_video_id"],
                query["target_video_id"],
                start_date=query["start_date"],
                end_date=query["end_date"],
                availability_end_date=query["availability_probe_end_date"],
                token_file=spec.publish.youtube.token,
                client_secret_file=spec.publish.youtube.client_secret,
            ),
            query,
        )
    except Exception as exc:
        raise ShortsBridgeError(
            f"Analytics readback failed; experiment remains running: {exc}"
        ) from exc
    data_through = metrics.get("views_data_through_date")
    period_confirmed = (
        isinstance(data_through, str) and data_through >= query["end_date"]
    )
    settled_date = (
        date.fromisoformat(query["end_date"])
        + timedelta(days=ANALYTICS_UNVERIFIABLE_WAIT_DAYS)
    ).isoformat()
    if (
        not period_confirmed
        and query["availability_probe_end_date"] < settled_date
    ):
        available = str(data_through or "取得不可")
        raise ShortsBridgeError(
            "Analytics views data is not available through the observation end "
            f"date (available through: {available}); experiment remains running"
        )
    reason = (
        _insufficient_reason(metrics)
        if period_confirmed
        else (
            "観測終了後7完了日を待ってもAnalyticsの利用可能期間を確認できませんでした。"
            "再生数は0とせず取得不可として記録します"
        )
    )
    source_views = metrics.get("source_views")
    attributed = metrics.get("attributed_target_views")
    observed = not reason
    ratio = (
        round(attributed * 100.0 / source_views, 4)
        if observed
        else None
    )

    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "running" or (
            manifest.get("started_at"),
            manifest.get("plan_sha256"),
        ) != running_identity:
            raise ShortsBridgeError(
                "shorts bridge manifest changed while Analytics was being read"
            )
        result = {
            "status": "observed" if observed else "insufficient_data",
            "source_views": (
                source_views
                if isinstance(source_views, int) and not isinstance(source_views, bool)
                else None
            ),
            "attributed_target_views": (
                attributed
                if isinstance(attributed, int) and not isinstance(attributed, bool)
                else None
            ),
            "transition_ratio_percent": ratio,
            "reason": reason,
            "requested_start_date": query["start_date"],
            "requested_end_date": query["end_date"],
            "availability_probe_end_date": query["availability_probe_end_date"],
            "views_data_through_date": data_through,
            "analytics_period_confirmed": period_confirmed,
            "attribution_source_type": "RELATED_VIDEO",
            "attribution_detail_limit": 25,
            "recorded_at": recorded_at,
            "notes": clean_notes,
            "setup_unchanged_confirmed": True,
            "universal_threshold_applied": False,
        }
        completed = {
            **manifest,
            "status": "completed",
            "completed_at": recorded_at,
            "result": result,
        }
        result["comparison"] = _comparison_with_current(spec, completed)
        _validate_manifest_result(completed, path)
        _write_text_atomic(path.parent / "next_idea_memo.md", _result_memo(completed))
        _write_manifest(path, completed)
    return completed


def show_experiment(spec: ChannelSpec, experiment_id: str) -> dict:
    _validate_root_readable(_root(spec))
    return _load_manifest(
        _manifest_path(spec, experiment_id), expected_channel=spec.id
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Shorts関連動画への橋渡しをローカル記録・read-only分析"
            "（YouTube書込みなし）"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--channel", required=True)
    plan.add_argument("--source-video-id", required=True)
    plan.add_argument("--target-video-id", required=True)
    plan.add_argument("--bridge-text", required=True)
    plan.add_argument(
        "--observation-days", type=int, default=DEFAULT_OBSERVATION_DAYS
    )
    plan.add_argument("--confirm-content-direct", action="store_true")

    start = subparsers.add_parser("start")
    start.add_argument("--channel", required=True)
    start.add_argument("--experiment-id", required=True)
    start.add_argument("--confirm-studio-setup", action="store_true")

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
                target_video_id=args.target_video_id,
                bridge_text=args.bridge_text,
                observation_days=args.observation_days,
                content_direct_confirmed=args.confirm_content_direct,
            )
        elif args.command == "start":
            result = start_experiment(
                spec,
                args.experiment_id,
                studio_setup_confirmed=args.confirm_studio_setup,
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
            raise ShortsBridgeError(f"unknown command: {args.command}")
    except (ShortsBridgeError, OSError, RuntimeError) as exc:
        print(f"[doci] Shorts橋渡し: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
