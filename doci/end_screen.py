"""YouTube終了画面の比較実験をローカル管理するCLI（issues #165/#171）。

YouTubeへの書込みは行わない。公開済み通常動画の終了画面について、内容が直結する
video要素1枠と複数要素baselineを、同じcohort・観測期間で比較できる形で記録する。
判定材料は対象video要素のクリック率と、遷移先動画に入った終了画面トラフィックの
視聴回数を分けて保存する。どちらも単発結果から勝者や因果を断定しない。

schema v1（issue #165）の1枠記録は読み取りと進行中記録の完了を継続サポートする。
新規計画はschema v2のみを作成する。
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
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import channel
from .channel import ChannelSpec


LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({LEGACY_SCHEMA_VERSION, SCHEMA_VERSION})
OFFICIAL_HELP_URL = "https://support.google.com/youtube/answer/6388789?hl=ja"
ANALYTICS_DIMENSIONS_URL = (
    "https://developers.google.com/youtube/analytics/dimensions"
)
ANALYTICS_CHANNEL_REPORTS_URL = (
    "https://developers.google.com/youtube/analytics/channel_reports"
)
PACIFIC = ZoneInfo("America/Los_Angeles")

SINGLE_VARIANT = "single_related_video"
MULTI_VARIANT = "multi_element_baseline"
VALID_VARIANTS = frozenset({SINGLE_VARIANT, MULTI_VARIANT})
VALID_ELEMENT_TYPES = frozenset({"video", "playlist", "subscribe", "channel", "link"})
VALID_MISSING_METRICS = frozenset({"click_rate", "end_screen_traffic_views"})
VALID_OBSERVATION_DAYS = frozenset({7, 28})
MIN_DISTINCT_VIDEOS_PER_VARIANT = 2
DEFAULT_TARGET_TIMING = "last_20_seconds_to_end"
DEFAULT_TARGET_POSITION = "center"

LEGACY_VALID_OUTCOMES = frozenset(
    {
        "clicked",
        "not_clicked",
        "insufficient_views",
        "stopped_changed_setup",
    }
)
VALID_OUTCOMES = frozenset(
    {"observed", "insufficient_views", "stopped_changed_setup"}
)
VALID_INSUFFICIENT_REASONS = frozenset({"low_views", "analytics_unavailable"})
ACTIVE_STATUSES = frozenset({"planned", "running"})
VALID_STATUSES = frozenset({"planned", "running", "completed", "invalidated"})

_EXPERIMENT_ID_RE = re.compile(r"esc-[0-9a-f]{16}")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_COMPARISON_KEY_RE = re.compile(r"[^\r\n]{1,80}")
_SETUP_LABEL_RE = re.compile(r"[^\r\n]{1,80}")
_REFERENCE_RE = re.compile(r"[^\r\n]{1,300}")
_TIMESTAMP_FIELD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_COMMON_PLAN_FIELDS = (
    "schema_version",
    "experiment_id",
    "channel",
    "video_id",
    "created_at",
    "official_help_url",
    "decision_metric",
    "end_screen_setup",
    "source",
    "warnings",
)
_V2_PLAN_FIELDS = _COMMON_PLAN_FIELDS + (
    "comparison_key",
    "observation_days",
    "target",
    "measurement",
)


class EndScreenError(ValueError):
    """終了画面計画または状態遷移が安全に実行できない。"""


def _root(spec: ChannelSpec) -> Path:
    return spec.output_dir / "end_screen_tests"


def _ensure_root_dir(path: Path) -> Path:
    if path.is_symlink():
        raise EndScreenError(f"end screen test root must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise EndScreenError(f"end screen test root is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise EndScreenError(f"end screen test root must not be a symlink: {path}")
    return path


def _validate_root_readable(path: Path) -> None:
    if path.is_symlink():
        raise EndScreenError(f"end screen test root must not be a symlink: {path}")
    if not path.is_dir():
        raise EndScreenError(f"end screen test root is not a directory: {path}")


def _manifest_path(spec: ChannelSpec, experiment_id: str) -> Path:
    if not _EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise EndScreenError(f"invalid experiment id: {experiment_id!r}")
    directory = _root(spec) / experiment_id
    if directory.is_symlink():
        raise EndScreenError(f"end screen test directory must not be a symlink: {directory}")
    if directory.name != experiment_id:
        raise EndScreenError(
            f"end screen test directory name mismatch: {directory.name!r}"
        )
    return directory / "manifest.json"


def _now_utc(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise EndScreenError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _now_iso(now: datetime | None) -> str:
    return _now_utc(now).isoformat()


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    root = _ensure_root_dir(_root(spec))
    path = root / ".end_screen.lock"
    if path.is_symlink():
        raise EndScreenError(f"end screen lock must not be a symlink: {path}")
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


def _plan_fields(manifest: dict) -> tuple[str, ...]:
    if manifest.get("schema_version") == LEGACY_SCHEMA_VERSION:
        return _COMMON_PLAN_FIELDS
    return _V2_PLAN_FIELDS


def _plan_checksum(manifest: dict) -> str:
    stable = {key: manifest[key] for key in _plan_fields(manifest) if key in manifest}
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _validate_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_FIELD_RE.fullmatch(value):
        raise EndScreenError(f"{label} must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EndScreenError(f"{label} is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EndScreenError(f"{label} must include a UTC offset")
    return value


def _validate_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise EndScreenError(f"{label} must be a YYYY-MM-DD date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise EndScreenError(f"{label} is not a valid date: {value!r}") from exc


def _validate_video_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EndScreenError(f"{label} must be a string")
    if not _VIDEO_ID_RE.fullmatch(value):
        raise EndScreenError(f"invalid {label}: {value!r}")
    return value


def _validate_comparison_key(value: object) -> str:
    if not isinstance(value, str):
        raise EndScreenError("comparison_key must be a string")
    normalized = value.strip()
    if normalized != value or not _COMPARISON_KEY_RE.fullmatch(normalized):
        raise EndScreenError("comparison_key must be 1 to 80 characters without newlines")
    return normalized


def _validate_setup_label(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EndScreenError(f"{label} must be a string")
    normalized = value.strip()
    if normalized != value or not _SETUP_LABEL_RE.fullmatch(normalized):
        raise EndScreenError(f"{label} must be 1 to 80 characters without newlines")
    return normalized


def _validate_extra_element(data: object, label: str) -> dict:
    if not isinstance(data, dict):
        raise EndScreenError(f"{label} must be an object")
    expected_fields = {"type", "selection", "reference", "timing", "position"}
    if set(data) != expected_fields:
        raise EndScreenError(
            f"{label} fields must be type, selection, reference, timing, and position"
        )
    element_type = data.get("type")
    selection = data.get("selection")
    reference = data.get("reference")
    if element_type not in VALID_ELEMENT_TYPES:
        raise EndScreenError(f"{label} has an invalid element type")
    if not isinstance(selection, str):
        raise EndScreenError(f"{label} selection must be a string")

    if element_type == "video":
        if selection == "specific":
            _validate_video_id(reference, f"{label} reference")
        elif selection in {"best_for_viewer", "latest_upload"}:
            if reference is not None:
                raise EndScreenError(
                    f"{label} {selection} selection must have a null reference"
                )
        else:
            raise EndScreenError(f"{label} has an invalid video selection")
    elif element_type == "subscribe":
        if selection != "current_channel" or reference is not None:
            raise EndScreenError(
                f"{label} subscribe element requires current_channel and a null reference"
            )
    else:
        if selection != "specific":
            raise EndScreenError(f"{label} {element_type} selection must be specific")
        if not isinstance(reference, str):
            raise EndScreenError(f"{label} reference must be a string")
        normalized_reference = reference.strip()
        if normalized_reference != reference or not _REFERENCE_RE.fullmatch(reference):
            raise EndScreenError(
                f"{label} reference must be 1 to 300 characters without newlines"
            )
        if element_type == "link":
            parsed = urlsplit(reference)
            if parsed.scheme != "https" or not parsed.netloc:
                raise EndScreenError(f"{label} link reference must be an HTTPS URL")

    _validate_setup_label(data.get("timing"), f"{label} timing")
    _validate_setup_label(data.get("position"), f"{label} position")
    return data


def _comparison_setup_profile(setup: dict) -> dict:
    extras = [
        {
            "type": item["type"],
            "selection": item["selection"],
            "reference_scope": (
                "content_specific" if item["reference"] is not None else "none"
            ),
            "timing": item["timing"],
            "position": item["position"],
        }
        for item in setup["extra_elements"]
    ]
    extras.sort(
        key=lambda item: (
            item["type"],
            item["selection"],
            item["reference_scope"],
            item["timing"],
            item["position"],
        )
    )
    return {
        "variant": setup["variant"],
        "element_count": setup["element_count"],
        "target": {
            "type": "video",
            "selection": "specific",
            "reference_scope": "content_direct_target",
            "timing": setup["target_timing"],
            "position": setup["target_position"],
        },
        "extras": extras,
        "reference_normalization": "specific_ids_and_urls_excluded",
    }


def _setup_signature(profile: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_recorded_video(data: object, label: str) -> dict:
    if not isinstance(data, dict):
        raise EndScreenError(f"{label} must be an object")
    if str(data.get("tier") or "") != "longform":
        raise EndScreenError(f"{label} tier must be longform")
    if str(data.get("youtube_privacy") or "") not in {"public", "unlisted"}:
        raise EndScreenError(f"{label} youtube_privacy must be public or unlisted")
    if label == "target":
        _validate_video_id(data.get("video_id"), "target video_id")
    corner = data.get("corner")
    if corner is not None and not isinstance(corner, str):
        raise EndScreenError(f"{label} corner must be a string")
    return data


def _validate_legacy_plan(data: dict) -> None:
    setup = data.get("end_screen_setup")
    if not isinstance(setup, dict):
        raise EndScreenError("end_screen_setup must be an object")
    if str(setup.get("element") or "") != "video":
        raise EndScreenError("end screen element must be a single video element")
    target_id = _validate_video_id(setup.get("link_video_id"), "link_video_id")
    source_id = _validate_video_id(data.get("video_id"), "video_id")
    if target_id == source_id:
        raise EndScreenError("end screen link_video_id must differ from the video itself")
    if setup.get("single_slot_only") is not True:
        raise EndScreenError("end screen must be a single video slot")
    if setup.get("subscription_button_prohibited") is not True:
        raise EndScreenError("end screen must prohibit the subscription button")
    if setup.get("playlist_element_prohibited") is not True:
        raise EndScreenError("end screen must prohibit playlist elements")
    if setup.get("content_direct_confirmed") is not True:
        raise EndScreenError(
            "end screen setup is missing the content-direct confirmation"
        )
    if data.get("decision_metric") != "youtube_studio.end_screen_click_rate":
        raise EndScreenError("decision_metric must be end_screen_click_rate")
    _validate_recorded_video(data.get("source"), "source")


def _validate_v2_plan(data: dict) -> None:
    source_id = _validate_video_id(data.get("video_id"), "video_id")
    _validate_comparison_key(data.get("comparison_key"))
    observation_days = data.get("observation_days")
    if (
        isinstance(observation_days, bool)
        or not isinstance(observation_days, int)
        or observation_days not in VALID_OBSERVATION_DAYS
    ):
        raise EndScreenError("observation_days must be 7 or 28")

    setup = data.get("end_screen_setup")
    if not isinstance(setup, dict):
        raise EndScreenError("end_screen_setup must be an object")
    variant = setup.get("variant")
    if variant not in VALID_VARIANTS:
        raise EndScreenError(f"invalid end screen variant: {variant!r}")
    if setup.get("target_element_type") != "video":
        raise EndScreenError("target end screen element must be video")
    target_id = _validate_video_id(setup.get("target_video_id"), "target_video_id")
    if target_id == source_id:
        raise EndScreenError("target_video_id must differ from the source video")
    if setup.get("content_direct_confirmed") is not True:
        raise EndScreenError(
            "end screen setup is missing the content-direct confirmation"
        )
    target_timing = _validate_setup_label(
        setup.get("target_timing"), "target_timing"
    )
    target_position = _validate_setup_label(
        setup.get("target_position"), "target_position"
    )
    extras = setup.get("extra_elements")
    if not isinstance(extras, list):
        raise EndScreenError("extra_elements must be a list")
    for index, item in enumerate(extras):
        _validate_extra_element(item, f"extra_elements[{index}]")
    extra_types = setup.get("extra_element_types")
    if extra_types != [item["type"] for item in extras]:
        raise EndScreenError("extra_element_types must match extra_elements")
    element_count = setup.get("element_count")
    if (
        isinstance(element_count, bool)
        or not isinstance(element_count, int)
        or element_count != 1 + len(extras)
        or not 1 <= element_count <= 4
    ):
        raise EndScreenError("element_count must equal target plus 0 to 3 extra elements")
    if variant == SINGLE_VARIANT and extras:
        raise EndScreenError("single_related_video must contain exactly one video element")
    if variant == MULTI_VARIANT and not extras:
        raise EndScreenError("multi_element_baseline requires at least one extra element")
    positions = [target_position, *(item["position"] for item in extras)]
    if len(positions) != len(set(positions)):
        raise EndScreenError("end screen element positions must be unique")
    subscribe_count = sum(item["type"] == "subscribe" for item in extras)
    if subscribe_count > 1:
        raise EndScreenError("end screen setup may contain only one subscribe element")
    fingerprints = [
        (item["type"], item["selection"], item["reference"]) for item in extras
    ]
    target_fingerprint = ("video", "specific", target_id)
    if target_fingerprint in fingerprints or len(fingerprints) != len(set(fingerprints)):
        raise EndScreenError("end screen setup contains a duplicate element")
    expected_profile = _comparison_setup_profile(setup)
    if setup.get("comparison_profile") != expected_profile:
        raise EndScreenError("end screen comparison_profile is invalid")
    if setup.get("setup_signature") != _setup_signature(expected_profile):
        raise EndScreenError("end screen setup_signature is invalid")
    if data.get("decision_metric") != "two_stage_end_screen_transition":
        raise EndScreenError("decision_metric must be two_stage_end_screen_transition")
    source = _validate_recorded_video(data.get("source"), "source")
    target = _validate_recorded_video(data.get("target"), "target")
    if target.get("video_id") != target_id:
        raise EndScreenError("target metadata/video id mismatch")
    if source.get("video_id") not in (None, source_id):
        raise EndScreenError("source metadata/video id mismatch")

    measurement = data.get("measurement")
    expected_measurement = {
        "click_rate_scope": "designated_target_video_element",
        "traffic_scope": "target_video_all_end_screens",
        "traffic_role": "context_only_not_variant_attributed",
        "provenance": "youtube_studio_manual",
        "source_specific_attribution": False,
    }
    if measurement != expected_measurement:
        raise EndScreenError("measurement contract is invalid")


def _validate_manifest_plan(data: dict, path: Path) -> None:
    if not isinstance(data, dict):
        raise EndScreenError(f"invalid manifest: {path}")
    if path.name != "manifest.json":
        raise EndScreenError(f"manifest file name mismatch: {path.name!r}")
    experiment_id = str(data.get("experiment_id") or "")
    if experiment_id != path.parent.name:
        raise EndScreenError(
            f"experiment_id/directory mismatch: {experiment_id!r} != {path.parent.name!r}"
        )
    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise EndScreenError(f"unsupported schema version: {schema_version}")
    fields = _COMMON_PLAN_FIELDS if schema_version == 1 else _V2_PLAN_FIELDS
    missing = [key for key in fields if key not in data]
    if missing:
        raise EndScreenError(
            f"end screen manifest is missing fields: {', '.join(missing)}"
        )
    if data.get("status") not in VALID_STATUSES:
        raise EndScreenError(f"invalid status: {data.get('status')!r}")
    _validate_timestamp(data.get("created_at"), "created_at")
    if data.get("official_help_url") != OFFICIAL_HELP_URL:
        raise EndScreenError("official_help_url is invalid")
    if not isinstance(data.get("warnings"), list) or any(
        not isinstance(item, str) for item in data["warnings"]
    ):
        raise EndScreenError("warnings must be a list of strings")
    if schema_version == LEGACY_SCHEMA_VERSION:
        _validate_legacy_plan(data)
    else:
        _validate_v2_plan(data)
    expected = _plan_checksum(data)
    actual = data.get("plan_sha256")
    if not isinstance(actual, str) or not hmac.compare_digest(actual, expected):
        raise EndScreenError("end screen plan checksum mismatch")


def _validate_manifest_status(data: dict) -> None:
    status = str(data.get("status") or "")
    schema_version = data.get("schema_version")
    if status == "planned":
        forbidden = ("started_at", "completed_at", "result")
        if schema_version == SCHEMA_VERSION:
            forbidden += ("observation_start_date", "observation_end_date")
        for field in forbidden:
            if field in data:
                raise EndScreenError(f"planned manifest must not have {field}")
        return
    if status == "running":
        started_at = _validate_timestamp(data.get("started_at"), "started_at")
        for field in ("completed_at", "result"):
            if field in data:
                raise EndScreenError(f"running manifest must not have {field}")
    elif status in ("completed", "invalidated"):
        started_at = _validate_timestamp(data.get("started_at"), "started_at")
        _validate_timestamp(data.get("completed_at"), "completed_at")
    else:
        raise EndScreenError(f"invalid status: {status!r}")

    if schema_version == SCHEMA_VERSION:
        start = _validate_date(
            data.get("observation_start_date"), "observation_start_date"
        )
        end = _validate_date(data.get("observation_end_date"), "observation_end_date")
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        expected_start = started.astimezone(PACIFIC).date() + timedelta(days=1)
        if start != expected_start:
            raise EndScreenError("observation_start_date must follow the start Pacific day")
        expected_end = start + timedelta(days=data["observation_days"] - 1)
        if end != expected_end:
            raise EndScreenError("observation window length is inconsistent")


def _validate_rate(value: object, *, required: bool) -> float | None:
    if value is None and not required:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EndScreenError("click_rate must be a finite number")
    rate = float(value)
    if not math.isfinite(rate) or not 0.0 <= rate <= 100.0:
        raise EndScreenError("click_rate must be between 0 and 100")
    return rate


def _validate_traffic_views(value: object, *, required: bool) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EndScreenError("end_screen_traffic_views must be a non-negative integer")
    return value


def _validate_missing_metrics(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item not in VALID_MISSING_METRICS
        for item in value
    ):
        raise EndScreenError("missing_metrics must contain known metric names")
    normalized = sorted(set(value))
    if value != normalized:
        raise EndScreenError("missing_metrics must be unique and sorted")
    return normalized


def _validate_legacy_result(data: dict) -> None:
    result = data.get("result")
    if not isinstance(result, dict):
        raise EndScreenError("result must be an object")
    outcome = str(result.get("outcome") or "")
    if outcome not in LEGACY_VALID_OUTCOMES:
        raise EndScreenError(f"invalid outcome: {outcome!r}")
    status = str(data.get("status") or "")
    if outcome == "stopped_changed_setup":
        if status != "invalidated":
            raise EndScreenError("stopped_changed_setup requires invalidated status")
        if result.get("setup_unchanged_confirmed") is not False:
            raise EndScreenError(
                "stopped_changed_setup requires setup_unchanged_confirmed=false"
            )
    else:
        if status != "completed":
            raise EndScreenError("non-stopped outcomes require completed status")
        if result.get("setup_unchanged_confirmed") is not True:
            raise EndScreenError("setup_unchanged_confirmed must be true")
    _validate_timestamp(result.get("recorded_at"), "recorded_at")
    click_rate = result.get("click_rate")
    if outcome in ("insufficient_views", "stopped_changed_setup"):
        if click_rate is not None:
            raise EndScreenError(f"click_rate must be null for outcome {outcome!r}")
    else:
        rate = _validate_rate(click_rate, required=True)
        if outcome == "clicked" and rate == 0.0:
            raise EndScreenError("clicked outcome requires a positive click_rate")
        if outcome == "not_clicked" and rate and rate > 0.0:
            raise EndScreenError("not_clicked outcome requires a zero click_rate")


def _validate_v2_result(data: dict) -> None:
    result = data.get("result")
    if not isinstance(result, dict):
        raise EndScreenError("result must be an object")
    outcome = str(result.get("outcome") or "")
    if outcome not in VALID_OUTCOMES:
        raise EndScreenError(f"invalid outcome: {outcome!r}")
    status = str(data.get("status") or "")
    if outcome == "stopped_changed_setup":
        if status != "invalidated":
            raise EndScreenError("stopped_changed_setup requires invalidated status")
        if result.get("setup_unchanged_confirmed") is not False:
            raise EndScreenError(
                "stopped_changed_setup requires setup_unchanged_confirmed=false"
            )
    else:
        if status != "completed":
            raise EndScreenError("observed outcomes require completed status")
        if result.get("setup_unchanged_confirmed") is not True:
            raise EndScreenError("setup_unchanged_confirmed must be true")
    recorded_at = _validate_timestamp(result.get("recorded_at"), "recorded_at")
    if recorded_at != data.get("completed_at"):
        raise EndScreenError("recorded_at must equal completed_at")
    if result.get("observation_start_date") != data.get("observation_start_date"):
        raise EndScreenError("result observation_start_date mismatch")
    if result.get("observation_end_date") != data.get("observation_end_date"):
        raise EndScreenError("result observation_end_date mismatch")

    sample_sufficient = result.get("sample_sufficient")
    click_rate = result.get("click_rate")
    traffic_views = result.get("end_screen_traffic_views")
    missing_metrics = _validate_missing_metrics(result.get("missing_metrics"))
    insufficient_reason = result.get("insufficient_reason")
    period_complete = result.get("period_data_complete_confirmed")
    if outcome != "stopped_changed_setup":
        recorded = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        latest_completed_pt_day = recorded.astimezone(PACIFIC).date() - timedelta(days=1)
        if latest_completed_pt_day < date.fromisoformat(result["observation_end_date"]):
            raise EndScreenError("recorded_at is before the observation window is complete")
    if outcome == "observed":
        if sample_sufficient is not True:
            raise EndScreenError("observed result requires sample_sufficient=true")
        if period_complete is not True:
            raise EndScreenError(
                "observed result requires period_data_complete_confirmed=true"
            )
        if insufficient_reason is not None:
            raise EndScreenError("observed result must not have insufficient_reason")
        if missing_metrics:
            raise EndScreenError("observed result must not have missing_metrics")
        _validate_rate(click_rate, required=True)
        _validate_traffic_views(traffic_views, required=True)
    elif outcome == "insufficient_views":
        if sample_sufficient is not False:
            raise EndScreenError(
                "insufficient_views requires sample_sufficient=false"
            )
        if insufficient_reason not in VALID_INSUFFICIENT_REASONS:
            raise EndScreenError("insufficient_views requires a valid reason")
        if insufficient_reason == "low_views" and period_complete is not True:
            raise EndScreenError("low_views requires complete-period confirmation")
        if insufficient_reason == "low_views":
            if missing_metrics:
                raise EndScreenError("low_views must not have missing_metrics")
            _validate_rate(click_rate, required=True)
            _validate_traffic_views(traffic_views, required=True)
        else:
            if not missing_metrics:
                raise EndScreenError(
                    "analytics_unavailable requires at least one missing metric"
                )
            available_metrics = VALID_MISSING_METRICS.difference(missing_metrics)
            if available_metrics and period_complete is not True:
                raise EndScreenError(
                    "available metrics require complete-period confirmation"
                )
            if not available_metrics and period_complete is not False:
                raise EndScreenError(
                    "all metrics unavailable must not confirm complete period data"
                )
            _validate_rate(
                click_rate, required="click_rate" not in missing_metrics
            )
            _validate_traffic_views(
                traffic_views,
                required="end_screen_traffic_views" not in missing_metrics,
            )
            if "click_rate" in missing_metrics and click_rate is not None:
                raise EndScreenError("missing click_rate must be null")
            if (
                "end_screen_traffic_views" in missing_metrics
                and traffic_views is not None
            ):
                raise EndScreenError("missing end_screen_traffic_views must be null")
    else:
        if sample_sufficient is not None:
            raise EndScreenError("stopped result must not have sample_sufficient")
        if insufficient_reason is not None:
            raise EndScreenError("stopped result must not have insufficient_reason")
        if missing_metrics:
            raise EndScreenError("stopped result must not have missing_metrics")
        if period_complete is not False:
            raise EndScreenError("stopped result must not confirm complete period data")
        if click_rate is not None or traffic_views is not None:
            raise EndScreenError("stopped result must not contain measured metrics")


def _validate_manifest_result(data: dict, path: Path) -> None:
    _validate_manifest_plan(data, path)
    _validate_manifest_status(data)
    if data.get("schema_version") == LEGACY_SCHEMA_VERSION:
        _validate_legacy_result(data)
    else:
        _validate_v2_result(data)


def _load_manifest(path: Path, *, expected_channel: str | None = None) -> dict:
    if path.is_symlink():
        raise EndScreenError(f"end screen manifest must not be a symlink: {path}")
    if not path.is_file():
        raise EndScreenError(f"end screen manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EndScreenError(f"cannot read manifest: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EndScreenError(f"invalid manifest: {path}")
    if expected_channel is not None and data.get("channel") != expected_channel:
        raise EndScreenError(
            f"channel mismatch: expected {expected_channel}, got {data.get('channel')!r}"
        )
    if data.get("status") in ("completed", "invalidated"):
        # show/summary must never receive terminal records with unvalidated metrics.
        _validate_manifest_result(data, path)
    else:
        _validate_manifest_plan(data, path)
        _validate_manifest_status(data)
    return data


def _all_manifests(spec: ChannelSpec) -> list[dict]:
    root = _root(spec)
    if root.is_symlink():
        raise EndScreenError(f"end screen test root must not be a symlink: {root}")
    if not root.exists():
        return []
    _validate_root_readable(root)
    manifests: list[dict] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            raise EndScreenError(
                f"end screen test directory must not be a symlink: {child}"
            )
        if not _EXPERIMENT_ID_RE.fullmatch(child.name):
            continue
        path = child / "manifest.json"
        if not path.is_file():
            raise EndScreenError(f"end screen manifest missing: {path}")
        manifests.append(_load_manifest(path, expected_channel=spec.id))
    return manifests


def _history_video(spec: ChannelSpec, video_id: str, *, role: str) -> dict:
    _validate_video_id(video_id, f"{role} video_id")
    if not spec.history_file.exists():
        raise EndScreenError(f"doci history is missing: {spec.history_file}")
    found: dict | None = None
    for line in spec.history_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("video_id") == video_id:
            found = row
    if found is None:
        raise EndScreenError(f"{role} video is not present in doci history: {video_id}")
    if found.get("tier") != "longform":
        raise EndScreenError("end screens are not available for Shorts")
    if found.get("status") != "published":
        raise EndScreenError(f"{role} video is not recorded as published")
    if found.get("youtube_privacy") not in {"public", "unlisted"}:
        raise EndScreenError(
            f"{role} video privacy must be recorded as public or unlisted"
        )
    return found


def _recorded_video(row: dict, *, include_id: bool) -> dict:
    recorded = {
        "title": str(row.get("title") or ""),
        "history_ts": str(row.get("ts") or ""),
        "workdir": str(row.get("workdir") or ""),
        "corner": str(row.get("corner") or ""),
        "tier": "longform",
        "youtube_privacy": row.get("youtube_privacy"),
    }
    if include_id:
        recorded["video_id"] = str(row.get("video_id") or "")
    return recorded


def _safe_cell(value: object) -> str:
    return str(value or "").replace("|", "｜").replace("\n", " ")


def _plan_markdown(manifest: dict) -> str:
    setup = manifest["end_screen_setup"]
    variant_text = (
        "内容直結のvideo要素1枠"
        if setup["variant"] == SINGLE_VARIANT
        else f"video要素を含む複数要素baseline（{setup['element_count']}要素）"
    )
    extras = (
        json.dumps(setup["extra_elements"], ensure_ascii=False, sort_keys=True)
        if setup["extra_elements"]
        else "なし"
    )
    warnings = "\n".join(f"- {item}" for item in manifest["warnings"]) or "- なし"
    return f"""# 終了画面の比較実験計画

- experiment_id: `{manifest['experiment_id']}`
- source video_id: `{manifest['video_id']}`
- target video_id: `{setup['target_video_id']}`
- variant: `{setup['variant']}`（{variant_text}）
- target timing: `{_safe_cell(setup['target_timing'])}`
- target position: `{_safe_cell(setup['target_position'])}`
- 追加要素: `{_safe_cell(extras)}`
- setup signature: `{setup['setup_signature']}`
- comparison_key: `{_safe_cell(manifest['comparison_key'])}`
- 観測期間: 太平洋時間の完了日 {manifest['observation_days']}日分
- 公式仕様: {OFFICIAL_HELP_URL}

## 実施手順

1. パソコン版YouTube Studioで対象の通常動画を開きます。
2. 上記variantどおりに終了画面を設定します。1枠は最適解ではなく実験条件です。
3. `start`を記録します。翌日の太平洋時間から観測期間が始まります。
4. テスト中は終了画面構成を変えません。変更時は`--setup-changed`で無効化します。
5. 指定期間で、対象video要素のクリック率と遷移先動画のトラフィックソース
   `END_SCREEN`の視聴回数をStudioから記録します。

## 測定上の注意

- クリック率は対象video要素の操作、終了画面流入は遷移先で発生した視聴です。
- 流入数は遷移先動画に対する全終了画面の集計で、元動画だけの帰属とは断定しません。
- 公式API文書は`END_SCREEN`のsource detail可否に不整合があるため、source別の値を
  自動補完しません: {ANALYTICS_DIMENSIONS_URL} / {ANALYTICS_CHANNEL_REPORTS_URL}
- 各variantが複数本揃うまで勝者を決めず、取得不能値を0へ置換しません。
- 比較signatureは実ID/URLをcontent-specific参照として正規化しますが、要素種別・選択方式・
  タイミング・位置が異なる構成は同じ標本へ混ぜません。

## 品質上の注意

{warnings}
"""


def _result_memo(manifest: dict) -> str:
    result = manifest["result"]
    setup = manifest["end_screen_setup"]
    click_rate = result.get("click_rate")
    traffic_views = result.get("end_screen_traffic_views")
    rate_text = f"{float(click_rate):.2f}%" if click_rate is not None else "取得不可"
    views_text = str(traffic_views) if traffic_views is not None else "取得不可"
    notes = str(result.get("notes") or "").strip() or "記載なし"
    return f"""# 次回企画メモ: 終了画面の比較実験

- experiment_id: `{manifest['experiment_id']}`
- variant: `{setup['variant']}`
- setup_signature: `{setup['setup_signature']}`
- comparison_key: `{_safe_cell(manifest['comparison_key'])}`
- source video_id: `{manifest['video_id']}`
- target video_id: `{setup['target_video_id']}`
- outcome: `{result['outcome']}`
- 対象video要素クリック率: `{rate_text}`
- 遷移先の終了画面流入視聴: `{views_text}`
- sample_sufficient: `{result.get('sample_sufficient')}`
- missing_metrics: `{', '.join(result.get('missing_metrics') or []) or 'なし'}`
- 観測期間: `{result['observation_start_date']}`〜`{result['observation_end_date']}`
- 記録日時: `{result['recorded_at']}`

## 運用メモ

{notes}

この結果だけで1枠または複数要素を普遍的な勝者としません。流入視聴は遷移先動画に
対する全終了画面の集計であり、元動画やvariantへ帰属させません。同じ
comparison_key・観測期間の各variantが複数本揃ってから、`summary`の記述統計を
次の仮説に使います。
"""


def _legacy_result_memo(manifest: dict) -> str:
    result = manifest["result"]
    click_rate = result.get("click_rate")
    click_rate_text = (
        f"{float(click_rate):.2f}%" if click_rate is not None else "記録なし"
    )
    notes = str(result.get("notes") or "").strip() or "記載なし"
    return f"""# 次回企画メモ: 終了画面1枠の旧検証

- experiment_id: `{manifest['experiment_id']}`
- video_id: `{manifest['video_id']}`
- リンク先: `{_safe_cell(manifest['end_screen_setup'].get('link_video_id'))}`
- outcome: `{result['outcome']}`
- 終了画面要素クリック率: `{click_rate_text}`
- 記録日時: `{result['recorded_at']}`

## 運用メモ

{notes}

これはschema v1の旧記録です。1枠を普遍的な勝者とみなさず、新しい比較にはschema v2の
二段階KPIを使用します。
"""


def plan_experiment(
    spec: ChannelSpec,
    *,
    video_id: str,
    link_video_id: str,
    variant: str = SINGLE_VARIANT,
    extra_elements: Sequence[dict] = (),
    target_timing: str = DEFAULT_TARGET_TIMING,
    target_position: str = DEFAULT_TARGET_POSITION,
    comparison_key: str = "default",
    observation_days: int = 7,
    content_direct_confirmed: bool = False,
    now: datetime | None = None,
    experiment_id: str | None = None,
) -> dict:
    """終了画面variantの比較計画をschema v2で保存する。"""
    if not content_direct_confirmed:
        raise EndScreenError(
            "confirm that the target video directly continues the source video's content"
        )
    if variant not in VALID_VARIANTS:
        raise EndScreenError(f"invalid end screen variant: {variant!r}")
    target_timing = _validate_setup_label(target_timing, "target_timing")
    target_position = _validate_setup_label(target_position, "target_position")
    extras = [dict(item) if isinstance(item, dict) else item for item in extra_elements]
    for index, item in enumerate(extras):
        _validate_extra_element(item, f"extra_elements[{index}]")
    if variant == SINGLE_VARIANT and extras:
        raise EndScreenError("single_related_video must not have extra elements")
    if variant == MULTI_VARIANT and not 1 <= len(extras) <= 3:
        raise EndScreenError("multi_element_baseline requires 1 to 3 extra elements")
    comparison_key = _validate_comparison_key(comparison_key)
    if (
        isinstance(observation_days, bool)
        or not isinstance(observation_days, int)
        or observation_days not in VALID_OBSERVATION_DAYS
    ):
        raise EndScreenError("observation_days must be 7 or 28")
    if video_id == link_video_id:
        raise EndScreenError("target_video_id must differ from the source video")
    source_row = _history_video(spec, video_id, role="source")
    target_row = _history_video(spec, link_video_id, role="target")

    with _operation_lock(spec):
        for existing in _all_manifests(spec):
            if (
                existing.get("video_id") == video_id
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise EndScreenError(
                    f"active end screen test already exists for video {video_id}: "
                    f"{existing.get('experiment_id')}"
                )
            existing_result = existing.get("result")
            if (
                existing.get("schema_version") == SCHEMA_VERSION
                and existing.get("video_id") == video_id
                and existing.get("comparison_key") == comparison_key
                and existing.get("observation_days") == observation_days
                and (existing.get("end_screen_setup") or {}).get("variant") == variant
                and (existing.get("source") or {}).get("corner")
                == str(source_row.get("corner") or "")
                and isinstance(existing_result, dict)
                and existing_result.get("outcome") == "observed"
            ):
                raise EndScreenError(
                    "an observed result already exists for this source video, variant, "
                    "comparison key, corner, and observation length"
                )
        candidate_id = experiment_id or f"esc-{uuid.uuid4().hex[:16]}"
        target = _manifest_path(spec, candidate_id).parent
        if target.exists():
            raise EndScreenError(f"experiment already exists: {candidate_id}")
        staging = Path(tempfile.mkdtemp(prefix=".plan-", dir=_root(spec)))
        try:
            setup = {
                "variant": variant,
                "target_element_type": "video",
                "target_video_id": link_video_id,
                "target_timing": target_timing,
                "target_position": target_position,
                "extra_elements": extras,
                "extra_element_types": [item["type"] for item in extras],
                "element_count": 1 + len(extras),
                "content_direct_confirmed": True,
            }
            comparison_profile = _comparison_setup_profile(setup)
            setup["comparison_profile"] = comparison_profile
            setup["setup_signature"] = _setup_signature(comparison_profile)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "experiment_id": candidate_id,
                "channel": spec.id,
                "video_id": video_id,
                "status": "planned",
                "created_at": _now_iso(now),
                "official_help_url": OFFICIAL_HELP_URL,
                "decision_metric": "two_stage_end_screen_transition",
                "comparison_key": comparison_key,
                "observation_days": observation_days,
                "end_screen_setup": setup,
                "source": _recorded_video(source_row, include_id=False),
                "target": _recorded_video(target_row, include_id=True),
                "measurement": {
                    "click_rate_scope": "designated_target_video_element",
                    "traffic_scope": "target_video_all_end_screens",
                    "traffic_role": "context_only_not_variant_attributed",
                    "provenance": "youtube_studio_manual",
                    "source_specific_attribution": False,
                },
                "warnings": [
                    "1枠は普遍的な最適解ではなく比較variantです。",
                    "終了画面流入は遷移先動画の全終了画面を含みます。",
                ],
            }
            manifest["plan_sha256"] = _plan_checksum(manifest)
            _validate_manifest_plan(manifest, target / "manifest.json")
            _write_manifest(staging / "manifest.json", manifest)
            _write_text_atomic(staging / "plan.md", _plan_markdown(manifest))
            os.replace(staging, target)
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
    if not studio_setup_confirmed:
        raise EndScreenError(
            "confirm that the planned end screen variant was set up in YouTube Studio"
        )
    current = _now_utc(now)
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "planned":
            raise EndScreenError("only a planned end screen test can be started")
        for existing in _all_manifests(spec):
            if (
                existing.get("experiment_id") != experiment_id
                and existing.get("video_id") == manifest.get("video_id")
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise EndScreenError(
                    "another active end screen test exists for this video: "
                    f"{existing.get('experiment_id')}"
                )
        updated = {**manifest, "status": "running", "started_at": current.isoformat()}
        if manifest.get("schema_version") == SCHEMA_VERSION:
            first_full_day = current.astimezone(PACIFIC).date() + timedelta(days=1)
            updated["observation_start_date"] = first_full_day.isoformat()
            updated["observation_end_date"] = (
                first_full_day + timedelta(days=manifest["observation_days"] - 1)
            ).isoformat()
        _validate_manifest_plan(updated, path)
        _validate_manifest_status(updated)
        _write_manifest(path, updated)
    return updated


def _complete_legacy(
    manifest: dict,
    *,
    outcome: str | None,
    click_rate: float | None,
    notes: str,
    setup_unchanged_confirmed: bool,
    recorded_at: str,
) -> tuple[dict, str]:
    if outcome not in LEGACY_VALID_OUTCOMES:
        raise EndScreenError("legacy schema requires a valid --outcome")
    if outcome == "stopped_changed_setup":
        if setup_unchanged_confirmed:
            raise EndScreenError(
                "stopped_changed_setup conflicts with setup-unchanged confirmation"
            )
    elif not setup_unchanged_confirmed:
        raise EndScreenError(
            "confirm that the end screen setup was not manually changed during the test"
        )
    if outcome in ("insufficient_views", "stopped_changed_setup"):
        if click_rate is not None:
            raise EndScreenError(f"click_rate must not be recorded for outcome {outcome!r}")
    else:
        rate = _validate_rate(click_rate, required=True)
        if outcome == "clicked" and rate == 0.0:
            raise EndScreenError("clicked outcome requires a positive click_rate")
        if outcome == "not_clicked" and rate and rate > 0.0:
            raise EndScreenError("not_clicked outcome requires a zero click_rate")
    status = "invalidated" if outcome == "stopped_changed_setup" else "completed"
    updated = {
        **manifest,
        "status": status,
        "completed_at": recorded_at,
        "result": {
            "outcome": outcome,
            "click_rate": click_rate,
            "notes": str(notes).strip(),
            "recorded_at": recorded_at,
            "setup_unchanged_confirmed": setup_unchanged_confirmed,
        },
    }
    return updated, status


def _complete_v2(
    manifest: dict,
    *,
    outcome: str | None,
    click_rate: float | None,
    end_screen_traffic_views: int | None,
    sample_sufficient: bool | None,
    insufficient_reason: str | None,
    missing_metrics: Sequence[str],
    period_data_complete_confirmed: bool,
    setup_unchanged_confirmed: bool,
    setup_changed: bool,
    notes: str,
    current: datetime,
) -> dict:
    if outcome is not None:
        raise EndScreenError("schema v2 uses sample/setup flags instead of --outcome")
    recorded_at = current.isoformat()
    raw_missing = list(missing_metrics)
    if any(
        not isinstance(item, str) or item not in VALID_MISSING_METRICS
        for item in raw_missing
    ):
        raise EndScreenError("missing_metrics must contain known metric names")
    normalized_missing = sorted(set(raw_missing))
    if len(normalized_missing) != len(raw_missing):
        raise EndScreenError("missing_metrics must not contain duplicates")
    _validate_missing_metrics(normalized_missing)
    if setup_changed:
        if setup_unchanged_confirmed:
            raise EndScreenError("setup-changed conflicts with setup-unchanged confirmation")
        if (
            sample_sufficient is not None
            or click_rate is not None
            or end_screen_traffic_views is not None
            or insufficient_reason is not None
            or normalized_missing
            or period_data_complete_confirmed
        ):
            raise EndScreenError(
                "setup-changed must not include sample, reason, period, or metric values"
            )
        result = {
            "outcome": "stopped_changed_setup",
            "click_rate": None,
            "end_screen_traffic_views": None,
            "sample_sufficient": None,
            "insufficient_reason": None,
            "missing_metrics": [],
            "period_data_complete_confirmed": False,
            "observation_start_date": manifest["observation_start_date"],
            "observation_end_date": manifest["observation_end_date"],
            "notes": str(notes).strip(),
            "recorded_at": recorded_at,
            "setup_unchanged_confirmed": False,
        }
        return {
            **manifest,
            "status": "invalidated",
            "completed_at": recorded_at,
            "result": result,
        }

    if not setup_unchanged_confirmed:
        raise EndScreenError(
            "confirm that the end screen setup was not manually changed during the test"
        )
    if sample_sufficient is None:
        raise EndScreenError("choose either sample-sufficient or insufficient-views")
    observation_end = date.fromisoformat(manifest["observation_end_date"])
    latest_completed_pt_day = current.astimezone(PACIFIC).date() - timedelta(days=1)
    if latest_completed_pt_day < observation_end:
        raise EndScreenError(
            "observation window is not complete; complete it after "
            f"{observation_end.isoformat()} Pacific time"
        )

    if sample_sufficient:
        _validate_rate(click_rate, required=True)
        _validate_traffic_views(end_screen_traffic_views, required=True)
        if not period_data_complete_confirmed:
            raise EndScreenError(
                "observed result requires --confirm-period-data-complete"
            )
        if insufficient_reason is not None:
            raise EndScreenError("sample-sufficient conflicts with insufficient-reason")
        if normalized_missing:
            raise EndScreenError("sample-sufficient conflicts with missing-metric")
        outcome_value = "observed"
    else:
        if insufficient_reason not in VALID_INSUFFICIENT_REASONS:
            raise EndScreenError(
                "insufficient-views requires --insufficient-reason"
            )
        if insufficient_reason == "low_views":
            if not period_data_complete_confirmed:
                raise EndScreenError(
                    "low_views requires --confirm-period-data-complete"
                )
            if normalized_missing:
                raise EndScreenError("low_views conflicts with missing-metric")
            _validate_rate(click_rate, required=True)
            _validate_traffic_views(end_screen_traffic_views, required=True)
        else:
            if not normalized_missing:
                raise EndScreenError(
                    "analytics_unavailable requires --missing-metric"
                )
            available_metrics = VALID_MISSING_METRICS.difference(normalized_missing)
            if available_metrics and not period_data_complete_confirmed:
                raise EndScreenError(
                    "available metrics require --confirm-period-data-complete"
                )
            if not available_metrics and period_data_complete_confirmed:
                raise EndScreenError(
                    "all metrics unavailable conflicts with complete-period confirmation"
                )
            _validate_rate(
                click_rate, required="click_rate" not in normalized_missing
            )
            _validate_traffic_views(
                end_screen_traffic_views,
                required="end_screen_traffic_views" not in normalized_missing,
            )
            if "click_rate" in normalized_missing and click_rate is not None:
                raise EndScreenError("missing click_rate must not include a value")
            if (
                "end_screen_traffic_views" in normalized_missing
                and end_screen_traffic_views is not None
            ):
                raise EndScreenError(
                    "missing end_screen_traffic_views must not include a value"
                )
        outcome_value = "insufficient_views"

    result = {
        "outcome": outcome_value,
        "click_rate": click_rate,
        "end_screen_traffic_views": end_screen_traffic_views,
        "sample_sufficient": sample_sufficient,
        "insufficient_reason": insufficient_reason,
        "missing_metrics": normalized_missing,
        "period_data_complete_confirmed": period_data_complete_confirmed,
        "observation_start_date": manifest["observation_start_date"],
        "observation_end_date": manifest["observation_end_date"],
        "notes": str(notes).strip(),
        "recorded_at": recorded_at,
        "setup_unchanged_confirmed": True,
    }
    return {
        **manifest,
        "status": "completed",
        "completed_at": recorded_at,
        "result": result,
    }


def complete_experiment(
    spec: ChannelSpec,
    experiment_id: str,
    *,
    outcome: str | None = None,
    click_rate: float | None = None,
    end_screen_traffic_views: int | None = None,
    sample_sufficient: bool | None = None,
    insufficient_reason: str | None = None,
    missing_metrics: Sequence[str] = (),
    period_data_complete_confirmed: bool = False,
    notes: str = "",
    setup_unchanged_confirmed: bool = False,
    setup_changed: bool = False,
    now: datetime | None = None,
) -> dict:
    """観測結果を保存する。schema v1の既存running記録も完了できる。"""
    current = _now_utc(now)
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "running":
            raise EndScreenError("only a running end screen test can be completed")
        if manifest.get("schema_version") == LEGACY_SCHEMA_VERSION:
            if (
                end_screen_traffic_views is not None
                or sample_sufficient is not None
                or insufficient_reason is not None
                or missing_metrics
                or period_data_complete_confirmed
                or setup_changed
            ):
                raise EndScreenError("legacy schema does not accept v2 result fields")
            updated, _ = _complete_legacy(
                manifest,
                outcome=outcome,
                click_rate=click_rate,
                notes=notes,
                setup_unchanged_confirmed=setup_unchanged_confirmed,
                recorded_at=current.isoformat(),
            )
        else:
            updated = _complete_v2(
                manifest,
                outcome=outcome,
                click_rate=click_rate,
                end_screen_traffic_views=end_screen_traffic_views,
                sample_sufficient=sample_sufficient,
                insufficient_reason=insufficient_reason,
                missing_metrics=missing_metrics,
                period_data_complete_confirmed=period_data_complete_confirmed,
                setup_unchanged_confirmed=setup_unchanged_confirmed,
                setup_changed=setup_changed,
                notes=notes,
                current=current,
            )
            _validate_cross_experiment_measurements(
                updated,
                (
                    existing
                    for existing in _all_manifests(spec)
                    if existing.get("experiment_id") != experiment_id
                ),
            )
        _validate_manifest_result(updated, path)
        memo = (
            _result_memo(updated)
            if updated.get("schema_version") == SCHEMA_VERSION
            else _legacy_result_memo(updated)
        )
        _write_text_atomic(path.parent / "next_idea_memo.md", memo)
        _write_manifest(path, updated)
    return updated


def show_experiment(spec: ChannelSpec, experiment_id: str) -> dict:
    _validate_root_readable(_root(spec))
    return _load_manifest(
        _manifest_path(spec, experiment_id), expected_channel=spec.id
    )


def _target_observation_key(manifest: dict) -> tuple[str, str, str]:
    return (
        str(manifest["end_screen_setup"]["target_video_id"]),
        str(manifest["observation_start_date"]),
        str(manifest["observation_end_date"]),
    )


def _source_comparison_key(manifest: dict) -> tuple[str, str, int, str, str]:
    return (
        str((manifest.get("source") or {}).get("corner") or ""),
        str(manifest["comparison_key"]),
        int(manifest["observation_days"]),
        str(manifest["end_screen_setup"]["variant"]),
        str(manifest["video_id"]),
    )


def _validate_cross_experiment_measurements(
    candidate: dict, existing_manifests: Iterator[dict]
) -> None:
    """Prevent duplicate videos and target-wide traffic from becoming fake samples."""
    result = candidate.get("result") or {}
    candidate_views = result.get("end_screen_traffic_views")
    candidate_target_key = _target_observation_key(candidate)
    candidate_source_key = _source_comparison_key(candidate)
    for existing in existing_manifests:
        if existing.get("schema_version") != SCHEMA_VERSION:
            continue
        existing_result = existing.get("result")
        if not isinstance(existing_result, dict):
            continue
        existing_views = existing_result.get("end_screen_traffic_views")
        if (
            candidate_views is not None
            and existing_views is not None
            and _target_observation_key(existing) == candidate_target_key
            and existing_views != candidate_views
        ):
            raise EndScreenError(
                "target-wide end-screen traffic must match the existing observation "
                f"for {candidate_target_key[0]} {candidate_target_key[1]}..{candidate_target_key[2]}"
            )
        if (
            result.get("outcome") == "observed"
            and existing_result.get("outcome") == "observed"
            and _source_comparison_key(existing) == candidate_source_key
        ):
            raise EndScreenError(
                "an observed result already exists for this source video, variant, "
                "comparison key, corner, and observation length"
            )


def _distinct_source_entries(entries: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for entry in sorted(
        entries,
        key=lambda item: (
            str((item.get("result") or {}).get("recorded_at") or ""),
            str(item.get("experiment_id") or ""),
        ),
    ):
        latest[str(entry["video_id"])] = entry
    return [latest[key] for key in sorted(latest)]


def _setup_profile_summary(signature: str, entries: list[dict]) -> dict:
    distinct_entries = _distinct_source_entries(entries)
    rates = [float(item["result"]["click_rate"]) for item in distinct_entries]
    profile = entries[0]["end_screen_setup"]["comparison_profile"]
    if any(
        item["end_screen_setup"]["comparison_profile"] != profile for item in entries
    ):
        raise EndScreenError(f"setup signature collision or profile mismatch: {signature}")
    return {
        "setup_signature": signature,
        "comparison_profile": profile,
        "observed_experiment_count": len(entries),
        "distinct_source_video_count": len(distinct_entries),
        "median_click_rate": statistics.median(rates) if rates else None,
        "source_deduplication": "latest_recorded_observation",
    }


def _variant_summary(entries: list[dict]) -> dict:
    by_signature: dict[str, list[dict]] = {}
    for entry in entries:
        signature = str(entry["end_screen_setup"]["setup_signature"])
        by_signature.setdefault(signature, []).append(entry)
    profiles = [
        _setup_profile_summary(signature, by_signature[signature])
        for signature in sorted(by_signature)
    ]
    return {
        "observed_experiment_count": len(entries),
        "setup_profile_count": len(profiles),
        "profiles": profiles,
        "aggregate_median_click_rate": None,
        "aggregate_interpretation": "none_across_setup_profiles",
    }


def _target_traffic_context(entries: list[dict]) -> list[dict]:
    observations: dict[tuple[str, str, str], dict] = {}
    for entry in entries:
        result = entry["result"]
        views = result.get("end_screen_traffic_views")
        if views is None:
            continue
        key = _target_observation_key(entry)
        current = observations.get(key)
        if current is None:
            current = {
                "target_video_id": key[0],
                "observation_start_date": key[1],
                "observation_end_date": key[2],
                "end_screen_traffic_views": views,
                "source_video_ids": set(),
                "variants": set(),
            }
            observations[key] = current
        elif current["end_screen_traffic_views"] != views:
            raise EndScreenError(
                "conflicting target-wide end-screen traffic observations for "
                f"{key[0]} {key[1]}..{key[2]}"
            )
        current["source_video_ids"].add(str(entry["video_id"]))
        current["variants"].add(str(entry["end_screen_setup"]["variant"]))

    rendered: list[dict] = []
    for key in sorted(observations):
        item = observations[key]
        rendered.append(
            {
                "target_video_id": item["target_video_id"],
                "observation_start_date": item["observation_start_date"],
                "observation_end_date": item["observation_end_date"],
                "end_screen_traffic_views": item["end_screen_traffic_views"],
                "source_video_count": len(item["source_video_ids"]),
                "variants_present": sorted(item["variants"]),
                "interpretation": "context_only_not_source_or_variant_attributed",
            }
        )
    return rendered


def summary_experiments(spec: ChannelSpec) -> dict:
    """同一corner/cohort/期間の記述統計を返す。勝者・因果は判定しない。"""
    manifests = _all_manifests(spec)
    groups: dict[tuple[str, str, int], dict[str, list[dict]]] = {}
    all_observed: list[dict] = []
    legacy_count = 0
    excluded_count = 0
    for manifest in manifests:
        if manifest.get("schema_version") == LEGACY_SCHEMA_VERSION:
            legacy_count += 1
            continue
        result = manifest.get("result")
        if (
            manifest.get("status") != "completed"
            or not isinstance(result, dict)
            or result.get("outcome") != "observed"
        ):
            excluded_count += 1
            continue
        key = (
            str((manifest.get("source") or {}).get("corner") or ""),
            str(manifest["comparison_key"]),
            int(manifest["observation_days"]),
        )
        variant = str(manifest["end_screen_setup"]["variant"])
        all_observed.append(manifest)
        groups.setdefault(
            key, {SINGLE_VARIANT: [], MULTI_VARIANT: []}
        )[variant].append(manifest)

    _target_traffic_context(all_observed)
    rendered: list[dict] = []
    for key in sorted(groups):
        by_variant = groups[key]
        variant_stats = {
            variant: _variant_summary(by_variant[variant])
            for variant in (SINGLE_VARIANT, MULTI_VARIANT)
        }
        incompatible_profiles = any(
            variant_stats[variant]["setup_profile_count"] > 1
            for variant in (SINGLE_VARIANT, MULTI_VARIANT)
        )
        target_profiles = {
            variant: (
                variant_stats[variant]["profiles"][0]["comparison_profile"][
                    "target"
                ]
                if variant_stats[variant]["setup_profile_count"] == 1
                else None
            )
            for variant in (SINGLE_VARIANT, MULTI_VARIANT)
        }
        has_both_target_profiles = all(
            target_profiles[variant] is not None
            for variant in (SINGLE_VARIANT, MULTI_VARIANT)
        )
        incompatible_cross_variant_target = (
            has_both_target_profiles
            and target_profiles[SINGLE_VARIANT] != target_profiles[MULTI_VARIANT]
        )
        shared_target_profile = (
            target_profiles[SINGLE_VARIANT]
            if has_both_target_profiles and not incompatible_cross_variant_target
            else None
        )
        ready = (
            not incompatible_profiles
            and not incompatible_cross_variant_target
            and shared_target_profile is not None
            and all(
                variant_stats[variant]["setup_profile_count"] == 1
                and variant_stats[variant]["profiles"][0][
                    "distinct_source_video_count"
                ]
                >= MIN_DISTINCT_VIDEOS_PER_VARIANT
                for variant in (SINGLE_VARIANT, MULTI_VARIANT)
            )
        )
        group_entries = by_variant[SINGLE_VARIANT] + by_variant[MULTI_VARIANT]
        rendered.append(
            {
                "corner": key[0],
                "comparison_key": key[1],
                "observation_days": key[2],
                "status": (
                    "incompatible_setup_profiles"
                    if incompatible_profiles
                    else (
                        "incompatible_cross_variant_target_profile"
                        if incompatible_cross_variant_target
                        else (
                            "ready_for_descriptive_comparison"
                            if ready
                            else "insufficient_comparable_experiments"
                        )
                    )
                ),
                "required_distinct_videos_per_variant": (
                    MIN_DISTINCT_VIDEOS_PER_VARIANT
                ),
                "variants": variant_stats,
                "cross_variant_target_profiles": target_profiles,
                "shared_target_profile": shared_target_profile,
                "shared_target_signature": (
                    _setup_signature(shared_target_profile)
                    if shared_target_profile is not None
                    else None
                ),
                "target_end_screen_traffic_context": _target_traffic_context(
                    group_entries
                ),
                "winner": None,
                "interpretation": "descriptive_only_no_causal_claim",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "channel": spec.id,
        "groups": rendered,
        "legacy_experiments_excluded": legacy_count,
        "non_observed_experiments_excluded": excluded_count,
    }


def _extra_element_arg(value: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"extra element must be a JSON object: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("extra element must be a JSON object")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YouTube終了画面variantの比較実験をローカル管理（YouTube書込みなし）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--channel", required=True)
    plan.add_argument("--video-id", required=True)
    plan.add_argument("--link-video-id", required=True)
    plan.add_argument("--variant", choices=sorted(VALID_VARIANTS), required=True)
    plan.add_argument(
        "--extra-element",
        action="append",
        type=_extra_element_arg,
        default=[],
        help=(
            "repeatable JSON with type, selection, reference, timing, and position"
        ),
    )
    plan.add_argument("--target-timing", default=DEFAULT_TARGET_TIMING)
    plan.add_argument("--target-position", default=DEFAULT_TARGET_POSITION)
    plan.add_argument("--comparison-key", required=True)
    plan.add_argument(
        "--observation-days", type=int, choices=sorted(VALID_OBSERVATION_DAYS), default=7
    )
    plan.add_argument("--confirm-content-direct", action="store_true")

    start = subparsers.add_parser("start")
    start.add_argument("--channel", required=True)
    start.add_argument("--experiment-id", required=True)
    start.add_argument("--confirm-studio-setup", action="store_true")

    complete = subparsers.add_parser("complete")
    complete.add_argument("--channel", required=True)
    complete.add_argument("--experiment-id", required=True)
    complete.add_argument(
        "--outcome",
        choices=sorted(LEGACY_VALID_OUTCOMES),
        help="schema v1 records only",
    )
    mode = complete.add_mutually_exclusive_group()
    mode.add_argument("--sample-sufficient", action="store_true")
    mode.add_argument("--insufficient-views", action="store_true")
    mode.add_argument("--setup-changed", action="store_true")
    complete.add_argument("--click-rate", type=float)
    complete.add_argument("--end-screen-traffic-views", type=int)
    complete.add_argument(
        "--insufficient-reason", choices=sorted(VALID_INSUFFICIENT_REASONS)
    )
    complete.add_argument(
        "--missing-metric",
        action="append",
        choices=sorted(VALID_MISSING_METRICS),
        default=[],
    )
    complete.add_argument("--confirm-period-data-complete", action="store_true")
    complete.add_argument("--notes", default="")
    complete.add_argument("--confirm-setup-unchanged", action="store_true")

    show = subparsers.add_parser("show")
    show.add_argument("--channel", required=True)
    show.add_argument("--experiment-id", required=True)

    summary = subparsers.add_parser("summary")
    summary.add_argument("--channel", required=True)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    spec = channel.load(args.channel)
    try:
        if args.command == "plan":
            manifest = plan_experiment(
                spec,
                video_id=args.video_id,
                link_video_id=args.link_video_id,
                variant=args.variant,
                extra_elements=args.extra_element,
                target_timing=args.target_timing,
                target_position=args.target_position,
                comparison_key=args.comparison_key,
                observation_days=args.observation_days,
                content_direct_confirmed=args.confirm_content_direct,
            )
        elif args.command == "start":
            manifest = start_experiment(
                spec,
                args.experiment_id,
                studio_setup_confirmed=args.confirm_studio_setup,
            )
        elif args.command == "complete":
            sample_sufficient: bool | None = None
            if args.sample_sufficient:
                sample_sufficient = True
            elif args.insufficient_views:
                sample_sufficient = False
            manifest = complete_experiment(
                spec,
                args.experiment_id,
                outcome=args.outcome,
                click_rate=args.click_rate,
                end_screen_traffic_views=args.end_screen_traffic_views,
                sample_sufficient=sample_sufficient,
                insufficient_reason=args.insufficient_reason,
                missing_metrics=args.missing_metric,
                period_data_complete_confirmed=args.confirm_period_data_complete,
                notes=args.notes,
                setup_unchanged_confirmed=args.confirm_setup_unchanged,
                setup_changed=args.setup_changed,
            )
        elif args.command == "show":
            manifest = show_experiment(spec, args.experiment_id)
        elif args.command == "summary":
            manifest = summary_experiments(spec)
        else:
            raise EndScreenError(f"unknown command: {args.command}")
    except EndScreenError as exc:
        print(f"[doci] 終了画面: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
