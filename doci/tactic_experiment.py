"""Tactic issues #194/#196 の手動比較を安全に記録するCLI。

YouTubeへの書込みは行わない。既存動画をbaselineとして計画を固定し、後から
公開された同一corner・同一tierのcandidateを結び付ける。完了時にYouTube Studio
から人が転記した指標だけを保存し、勝者や因果は判定しない。
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
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from . import channel
from .channel import ChannelSpec


SCHEMA_VERSION = 1
VALID_KINDS = frozenset({"thumbnail_traffic", "shorts_hook"})
VALID_STATUSES = frozenset({"planned", "running", "completed"})
ACTIVE_STATUSES = frozenset({"planned", "running"})
_EXPERIMENT_ID_RE = re.compile(r"tactic-[0-9a-f]{16}")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_SOURCE_RE = re.compile(r"[A-Z][A-Z0-9_]{1,39}")
PACIFIC = ZoneInfo("America/Los_Angeles")
_PLAN_FIELDS = (
    "schema_version",
    "experiment_id",
    "channel",
    "kind",
    "issue_number",
    "comparison_key",
    "planned_change",
    "observation_days",
    "observation_timezone",
    "observation_start_policy",
    "observation_completion_policy",
    "fixed_variables",
    "baseline",
    "created_at",
    "youtube_write",
    "interpretation_policy",
)


class TacticExperimentError(ValueError):
    """手動比較の入力または状態遷移が安全でない。"""


def _root(spec: ChannelSpec) -> Path:
    return spec.output_dir / "tactic_experiments"


def _manifest_path(spec: ChannelSpec, experiment_id: str) -> Path:
    if not _EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise TacticExperimentError(f"invalid experiment id: {experiment_id!r}")
    directory = _root(spec) / experiment_id
    if directory.is_symlink():
        raise TacticExperimentError(f"experiment directory must not be a symlink: {directory}")
    return directory / "manifest.json"


def _now_iso(now: datetime | None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise TacticExperimentError("now must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    root = _root(spec)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise TacticExperimentError(f"unsafe experiment root: {root}")
    lock_path = root / ".lock"
    if lock_path.is_symlink():
        raise TacticExperimentError(f"experiment lock must not be a symlink: {lock_path}")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with temp.open("w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _write_manifest(path: Path, manifest: dict) -> None:
    _write_text_atomic(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _plan_checksum(manifest: dict) -> str:
    payload = {key: manifest.get(key) for key in _PLAN_FIELDS}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binding_checksum(manifest: dict) -> str:
    payload = {
        "candidate": manifest.get("candidate"),
        "started_at": manifest.get("started_at"),
        "confirmations": manifest.get("confirmations"),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_checksum(manifest: dict) -> str:
    payload = {
        "completed_at": manifest.get("completed_at"),
        "result": manifest.get("result"),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalise_text(value: str, *, field: str, max_length: int) -> str:
    result = " ".join(str(value).split())
    if not result:
        raise TacticExperimentError(f"{field} must not be empty")
    if len(result) > max_length:
        raise TacticExperimentError(f"{field} must be at most {max_length} characters")
    return result


def _history_video(spec: ChannelSpec, video_id: str) -> dict:
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise TacticExperimentError(f"invalid YouTube video id: {video_id!r}")
    if not spec.history_file.exists():
        raise TacticExperimentError(f"doci history is missing: {spec.history_file}")
    found: dict | None = None
    for line in spec.history_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("video_id") or "") == video_id:
            found = row
    if found is None:
        raise TacticExperimentError(f"video is not present in doci history: {video_id}")
    if found.get("channel") != spec.id:
        raise TacticExperimentError("video channel does not match the selected channel")
    if str(found.get("status") or "") != "published":
        raise TacticExperimentError("video is not recorded as published")
    if found.get("youtube_privacy") not in {"public", "unlisted"}:
        raise TacticExperimentError("video privacy must be recorded as public or unlisted")
    if not str(found.get("corner") or "").strip():
        raise TacticExperimentError("video corner is missing from doci history")
    if found.get("tier") not in {"short", "long_short", "longform"}:
        raise TacticExperimentError("video tier is missing or unsupported")
    _history_datetime(found.get("ts"), field="video")
    return found


def _history_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise TacticExperimentError(f"{field} history timestamp must be timezone-aware ISO")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TacticExperimentError(
            f"{field} history timestamp must be timezone-aware ISO"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TacticExperimentError(
            f"{field} history timestamp must be timezone-aware ISO"
        )
    return parsed


def _video_snapshot(row: dict) -> dict:
    return {
        "video_id": str(row["video_id"]),
        "title": str(row.get("title") or ""),
        "corner": str(row["corner"]),
        "tier": str(row["tier"]),
        "history_ts": str(row.get("ts") or ""),
        "youtube_privacy": str(row["youtube_privacy"]),
    }


def _validate_kind_video(kind: str, row: dict) -> None:
    tier = row.get("tier")
    if kind == "thumbnail_traffic" and tier != "longform":
        raise TacticExperimentError("thumbnail_traffic requires a longform video")
    if kind == "shorts_hook" and tier not in {"short", "long_short"}:
        raise TacticExperimentError("shorts_hook requires a short or long_short video")


def _fixed_variables(kind: str) -> list[str]:
    if kind == "thumbnail_traffic":
        return ["corner", "tier", "audience", "topic", "format", "all_except_thumbnail"]
    return [
        "corner",
        "tier",
        "audience",
        "topic",
        "duration_band",
        "all_except_first_second",
    ]


def _numeric(value: object, *, field: str, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TacticExperimentError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (
        maximum is not None and number > maximum
    ):
        raise TacticExperimentError(f"{field} is out of range")
    return number


def _validate_metrics(kind: str, metrics: object) -> dict:
    if not isinstance(metrics, dict):
        raise TacticExperimentError("metrics must be a JSON object")
    if metrics.get("available") is False:
        if set(metrics) != {"available", "reason"}:
            raise TacticExperimentError(
                "unavailable metrics require only available=false and reason"
            )
        return {
            "available": False,
            "reason": _normalise_text(
                str(metrics.get("reason") or ""), field="metrics reason", max_length=400
            ),
        }
    if metrics.get("available") is True:
        metrics = {key: value for key, value in metrics.items() if key != "available"}
    if kind == "shorts_hook":
        stored_swiped = metrics.pop("swiped_away_percent", None)
        if set(metrics) != {"shown_in_feed", "chose_to_view_percent"}:
            raise TacticExperimentError(
                "shorts_hook metrics require shown_in_feed and chose_to_view_percent"
            )
        shown = metrics["shown_in_feed"]
        if isinstance(shown, bool) or not isinstance(shown, int) or shown <= 0:
            raise TacticExperimentError("shown_in_feed must be a positive integer")
        percent = _numeric(
            metrics["chose_to_view_percent"],
            field="chose_to_view_percent",
            maximum=100,
        )
        derived_swiped = round(100.0 - percent, 6)
        if stored_swiped is not None and _numeric(
            stored_swiped, field="swiped_away_percent", maximum=100
        ) != derived_swiped:
            raise TacticExperimentError("swiped_away_percent is inconsistent")
        return {
            "available": True,
            "shown_in_feed": shown,
            "chose_to_view_percent": percent,
            "swiped_away_percent": derived_swiped,
        }

    if set(metrics) != {"traffic_sources", "impressions_funnel"} or not isinstance(
        metrics.get("traffic_sources"), dict
    ) or not isinstance(metrics.get("impressions_funnel"), dict):
        raise TacticExperimentError(
            "thumbnail_traffic metrics require traffic_sources and impressions_funnel objects"
        )
    sources = metrics["traffic_sources"]
    if not sources:
        raise TacticExperimentError("traffic_sources must not be empty")
    normalised: dict[str, int] = {}
    for source, views in sorted(sources.items()):
        if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
            raise TacticExperimentError(f"invalid traffic source: {source!r}")
        if isinstance(views, bool) or not isinstance(views, int) or views < 0:
            raise TacticExperimentError(
                f"traffic_sources.{source} must be a non-negative integer view count"
            )
        normalised[source] = views
    funnel = metrics["impressions_funnel"]
    if set(funnel) != {"impressions", "ctr_percent", "watch_time_minutes"}:
        raise TacticExperimentError(
            "impressions_funnel requires impressions, ctr_percent, and watch_time_minutes"
        )
    impressions = funnel["impressions"]
    if isinstance(impressions, bool) or not isinstance(impressions, int) or impressions <= 0:
        raise TacticExperimentError("impressions_funnel.impressions must be a positive integer")
    return {
        "available": True,
        "traffic_sources": normalised,
        "impressions_funnel": {
            "impressions": impressions,
            "ctr_percent": _numeric(
                funnel["ctr_percent"], field="impressions_funnel.ctr_percent", maximum=100
            ),
            "watch_time_minutes": _numeric(
                funnel["watch_time_minutes"], field="impressions_funnel.watch_time_minutes"
            ),
        },
    }


def _deltas(kind: str, baseline: dict, candidate: dict) -> dict:
    if not baseline.get("available") or not candidate.get("available"):
        return {}
    if kind == "shorts_hook":
        return {
            "shown_in_feed": candidate["shown_in_feed"] - baseline["shown_in_feed"],
            "chose_to_view_percentage_points": round(
                candidate["chose_to_view_percent"]
                - baseline["chose_to_view_percent"],
                6,
            ),
        }
    source_deltas: dict[str, int] = {}
    for source in baseline["traffic_sources"]:
        source_deltas[source] = (
            candidate["traffic_sources"][source] - baseline["traffic_sources"][source]
        )
    before = baseline["impressions_funnel"]
    after = candidate["impressions_funnel"]
    return {
        "traffic_sources": source_deltas,
        "impressions_funnel": {
            "impressions": after["impressions"] - before["impressions"],
            "ctr_percentage_points": round(after["ctr_percent"] - before["ctr_percent"], 6),
            "watch_time_minutes": round(
                after["watch_time_minutes"] - before["watch_time_minutes"], 6
            ),
        },
    }


def _validate_result_state(data: dict, path: Path) -> None:
    status = data.get("status")
    if status == "planned":
        if any(key in data for key in ("candidate", "started_at", "result", "completed_at")):
            raise TacticExperimentError(f"planned experiment has later state: {path}")
        return
    candidate = data.get("candidate")
    if not isinstance(candidate, dict) or not _VIDEO_ID_RE.fullmatch(
        str(candidate.get("video_id") or "")
    ):
        raise TacticExperimentError(f"running experiment lacks candidate: {path}")
    if (
        candidate.get("corner") != data.get("baseline", {}).get("corner")
        or candidate.get("tier") != data.get("baseline", {}).get("tier")
        or candidate.get("video_id") == data.get("baseline", {}).get("video_id")
    ):
        raise TacticExperimentError(f"running experiment candidate cohort is invalid: {path}")
    _validate_kind_video(data["kind"], candidate)
    _snapshot_date(candidate, field="candidate")
    confirmations = data.get("confirmations")
    if confirmations != {
        "same_cohort": True,
        "only_planned_variable_changed": True,
    }:
        raise TacticExperimentError(f"running experiment confirmations are invalid: {path}")
    binding = data.get("binding_sha256")
    if (
        not isinstance(binding, str)
        or re.fullmatch(r"[0-9a-f]{64}", binding) is None
        or not hmac.compare_digest(binding, _binding_checksum(data))
    ):
        raise TacticExperimentError(f"experiment candidate binding checksum mismatch: {path}")
    if not isinstance(data.get("started_at"), str) or not data["started_at"]:
        raise TacticExperimentError(f"running experiment lacks started_at: {path}")
    if status == "running":
        if any(key in data for key in ("result", "completed_at")):
            raise TacticExperimentError(f"running experiment has result: {path}")
        return
    if not isinstance(data.get("completed_at"), str) or not isinstance(
        data.get("result"), dict
    ):
        raise TacticExperimentError(f"completed experiment lacks result: {path}")
    result = data["result"]
    if result.get("same_observation_window_confirmed") is not True:
        raise TacticExperimentError(f"observation window confirmation is missing: {path}")
    if result.get("studio_values_transcribed_confirmed") is not True:
        raise TacticExperimentError(f"Studio transcription confirmation is missing: {path}")
    if not isinstance(result.get("recorded_at"), str) or not result["recorded_at"]:
        raise TacticExperimentError(f"result recorded_at is missing: {path}")
    if not isinstance(result.get("notes"), str):
        raise TacticExperimentError(f"result notes are invalid: {path}")
    baseline_metrics = _validate_metrics(data["kind"], result.get("baseline_metrics"))
    candidate_metrics = _validate_metrics(data["kind"], result.get("candidate_metrics"))
    if (
        baseline_metrics.get("available")
        and candidate_metrics.get("available")
        and data["kind"] == "thumbnail_traffic"
        and set(baseline_metrics["traffic_sources"])
        != set(candidate_metrics["traffic_sources"])
    ):
        raise TacticExperimentError(f"traffic source sets differ: {path}")
    if result.get("deltas") != _deltas(data["kind"], baseline_metrics, candidate_metrics):
        raise TacticExperimentError(f"experiment deltas are inconsistent: {path}")
    expected_interpretation = (
        "descriptive_only_no_causal_winner"
        if baseline_metrics.get("available") and candidate_metrics.get("available")
        else "insufficient_data"
    )
    if result.get("interpretation") != expected_interpretation:
        raise TacticExperimentError(f"experiment interpretation is invalid: {path}")
    try:
        recorded_at = datetime.fromisoformat(result["recorded_at"])
    except ValueError as exc:
        raise TacticExperimentError(f"result recorded_at is invalid: {path}") from exc
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise TacticExperimentError(f"result recorded_at is not timezone-aware: {path}")
    _validate_observation_windows(
        data, result.get("observation_windows"), as_of=recorded_at
    )
    result_checksum = data.get("result_sha256")
    if (
        not isinstance(result_checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", result_checksum) is None
        or not hmac.compare_digest(result_checksum, _result_checksum(data))
    ):
        raise TacticExperimentError(f"experiment result checksum mismatch: {path}")


def _parse_date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        raise TacticExperimentError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise TacticExperimentError(f"{field} must be an ISO date") from exc


def _snapshot_date(snapshot: object, *, field: str) -> date:
    if not isinstance(snapshot, dict):
        raise TacticExperimentError(f"{field} video snapshot is invalid")
    return _history_datetime(
        snapshot.get("history_ts"), field=field
    ).astimezone(PACIFIC).date()


def _validate_observation_windows(
    manifest: dict, windows: object, *, as_of: datetime | None = None
) -> dict:
    if not isinstance(windows, dict) or set(windows) != {"baseline", "candidate"}:
        raise TacticExperimentError("observation_windows require baseline and candidate")
    normalised: dict[str, dict] = {}
    for name in ("baseline", "candidate"):
        window = windows[name]
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise TacticExperimentError(f"{name} observation window requires start and end")
        start = _parse_date(window["start"], field=f"{name} observation start")
        end = _parse_date(window["end"], field=f"{name} observation end")
        if end < start or (end - start).days + 1 != manifest["observation_days"]:
            raise TacticExperimentError(
                f"{name} observation window must be {manifest['observation_days']} days"
            )
        expected_start = _snapshot_date(manifest[name], field=name) + timedelta(days=1)
        if start != expected_start:
            raise TacticExperimentError(
                f"{name} observation window must start on the first full Pacific day after publication"
            )
        latest_completed_day = (
            as_of.astimezone(PACIFIC).date() - timedelta(days=1)
            if as_of is not None
            else None
        )
        if latest_completed_day is not None and end > latest_completed_day:
            raise TacticExperimentError(
                f"{name} observation window must not exceed the latest completed Pacific day"
            )
        normalised[name] = {"start": start.isoformat(), "end": end.isoformat()}
    return normalised


def _load_manifest(path: Path, *, expected_channel: str | None = None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TacticExperimentError(f"invalid experiment manifest: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TacticExperimentError(f"invalid experiment manifest object: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise TacticExperimentError(f"unsupported experiment schema: {path}")
    if data.get("experiment_id") != path.parent.name:
        raise TacticExperimentError(f"experiment id mismatch: {path}")
    if expected_channel is not None and data.get("channel") != expected_channel:
        raise TacticExperimentError(f"experiment channel mismatch: {path}")
    if data.get("kind") not in VALID_KINDS or data.get("status") not in VALID_STATUSES:
        raise TacticExperimentError(f"invalid experiment kind or status: {path}")
    expected_issue = 194 if data.get("kind") == "thumbnail_traffic" else 196
    if data.get("issue_number") != expected_issue:
        raise TacticExperimentError(f"invalid experiment issue number: {path}")
    if (
        not isinstance(data.get("observation_days"), int)
        or isinstance(data.get("observation_days"), bool)
        or not 1 <= data["observation_days"] <= 28
    ):
        raise TacticExperimentError(f"invalid experiment observation days: {path}")
    if data.get("observation_timezone") != "America/Los_Angeles":
        raise TacticExperimentError(f"invalid experiment observation timezone: {path}")
    if data.get("observation_start_policy") != "first_full_day_after_publication":
        raise TacticExperimentError(f"invalid experiment observation start policy: {path}")
    if data.get("observation_completion_policy") != "through_previous_completed_day":
        raise TacticExperimentError(
            f"invalid experiment observation completion policy: {path}"
        )
    comparison_key = data.get("comparison_key")
    planned_change = data.get("planned_change")
    if not isinstance(comparison_key, str) or _normalise_text(
        comparison_key, field="comparison_key", max_length=120
    ) != comparison_key:
        raise TacticExperimentError(f"invalid experiment comparison key: {path}")
    if not isinstance(planned_change, str) or _normalise_text(
        planned_change, field="planned_change", max_length=500
    ) != planned_change:
        raise TacticExperimentError(f"invalid experiment planned change: {path}")
    baseline = data.get("baseline")
    if (
        not isinstance(baseline, dict)
        or not _VIDEO_ID_RE.fullmatch(str(baseline.get("video_id") or ""))
        or not str(baseline.get("corner") or "")
        or baseline.get("tier") not in {"short", "long_short", "longform"}
    ):
        raise TacticExperimentError(f"invalid experiment baseline: {path}")
    _validate_kind_video(data["kind"], baseline)
    _snapshot_date(baseline, field="baseline")
    if data.get("youtube_write") is not False:
        raise TacticExperimentError(f"experiment must remain read-only: {path}")
    if data.get("interpretation_policy") != "descriptive_only_no_causal_winner":
        raise TacticExperimentError(f"invalid interpretation policy: {path}")
    if data.get("fixed_variables") != _fixed_variables(data["kind"]):
        raise TacticExperimentError(f"invalid fixed variables: {path}")
    expected = data.get("plan_sha256")
    if (
        not isinstance(expected, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        or not hmac.compare_digest(expected, _plan_checksum(data))
    ):
        raise TacticExperimentError(f"experiment plan checksum mismatch: {path}")
    _validate_result_state(data, path)
    return data


def _all_manifests(spec: ChannelSpec) -> list[dict]:
    root = _root(spec)
    if not root.exists():
        return []
    rows: list[dict] = []
    for directory in sorted(root.iterdir()):
        if not _EXPERIMENT_ID_RE.fullmatch(directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise TacticExperimentError(f"unsafe experiment directory: {directory}")
        path = directory / "manifest.json"
        if not path.is_file():
            raise TacticExperimentError(f"experiment manifest is missing: {path}")
        rows.append(_load_manifest(path, expected_channel=spec.id))
    return rows


def _plan_markdown(manifest: dict) -> str:
    metric = (
        "流入元ごとのviews、および別ファネルのインプレッション・CTR・総再生時間"
        if manifest["kind"] == "thumbnail_traffic"
        else "フィード表示数・視聴を選択した割合"
    )
    return f"""# Tactic比較計画

- experiment_id: `{manifest['experiment_id']}`
- issue: `#{manifest['issue_number']}`
- kind: `{manifest['kind']}`
- baseline_video_id: `{manifest['baseline']['video_id']}`
- comparison_key: `{manifest['comparison_key']}`
- 変更する1変数: {manifest['planned_change']}
- 記録指標: {metric}
- 観測timezone: `{manifest['observation_timezone']}`
- 観測開始: 各動画の公開翌日（最初の完全な太平洋時間日）
- 観測終了: 記録時点の最新完了太平洋時間日まで

## 手順

1. 同じcorner・tier・視聴者・近い題材の次動画を用意します。
2. 上記の1変数以外を固定し、公開後に`start`でcandidateを結び付けます。
3. baselineとcandidateを同じ観測窓・同じStudio画面で確認し、`complete`へJSONで転記します。
4. 差分は記述統計です。勝者や因果を決めず、別の指標を混ぜません。

このCLIはYouTubeを変更しません。
"""


def _result_markdown(manifest: dict) -> str:
    result = manifest["result"]
    return f"""# Tactic比較結果

- experiment_id: `{manifest['experiment_id']}`
- issue: `#{manifest['issue_number']}`
- kind: `{manifest['kind']}`
- baseline_video_id: `{manifest['baseline']['video_id']}`
- candidate_video_id: `{manifest['candidate']['video_id']}`
- comparison_key: `{manifest['comparison_key']}`
- interpretation: `{result['interpretation']}`

```json
{json.dumps(result, ensure_ascii=False, indent=2)}
```

この差は同じcohort内の記述統計です。変更した1変数の因果効果や普遍的な勝者を示しません。
"""


def plan_experiment(
    spec: ChannelSpec,
    *,
    kind: str,
    issue_number: int,
    baseline_video_id: str,
    comparison_key: str,
    planned_change: str,
    observation_days: int = 7,
    one_variable_confirmed: bool = False,
    now: datetime | None = None,
    experiment_id: str | None = None,
) -> dict:
    """baselineと変更する1変数を固定する。"""
    if kind not in VALID_KINDS:
        raise TacticExperimentError(f"invalid experiment kind: {kind!r}")
    expected_issue = 194 if kind == "thumbnail_traffic" else 196
    if issue_number != expected_issue:
        raise TacticExperimentError(f"{kind} must reference issue #{expected_issue}")
    if not one_variable_confirmed:
        raise TacticExperimentError("confirm that exactly one variable will change")
    if (
        not isinstance(observation_days, int)
        or isinstance(observation_days, bool)
        or observation_days < 1
        or observation_days > 28
    ):
        raise TacticExperimentError("observation_days must be between 1 and 28")
    baseline = _history_video(spec, baseline_video_id)
    _validate_kind_video(kind, baseline)
    key = _normalise_text(comparison_key, field="comparison_key", max_length=120)
    change = _normalise_text(planned_change, field="planned_change", max_length=500)
    created_at = _now_iso(now)
    with _operation_lock(spec):
        for existing in _all_manifests(spec):
            if (
                existing.get("kind") == kind
                and existing.get("baseline", {}).get("video_id") == baseline_video_id
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise TacticExperimentError(
                    f"active experiment already exists for baseline {baseline_video_id}"
                )
        candidate_id = experiment_id or f"tactic-{uuid.uuid4().hex[:16]}"
        target = _manifest_path(spec, candidate_id).parent
        if target.exists():
            raise TacticExperimentError(f"experiment already exists: {candidate_id}")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": candidate_id,
            "channel": spec.id,
            "kind": kind,
            "issue_number": issue_number,
            "comparison_key": key,
            "planned_change": change,
            "observation_days": observation_days,
            "observation_timezone": "America/Los_Angeles",
            "observation_start_policy": "first_full_day_after_publication",
            "observation_completion_policy": "through_previous_completed_day",
            "fixed_variables": _fixed_variables(kind),
            "baseline": _video_snapshot(baseline),
            "status": "planned",
            "created_at": created_at,
            "youtube_write": False,
            "interpretation_policy": "descriptive_only_no_causal_winner",
        }
        manifest["plan_sha256"] = _plan_checksum(manifest)
        staging = Path(tempfile.mkdtemp(prefix=".plan-", dir=_root(spec)))
        try:
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
    candidate_video_id: str,
    same_cohort_confirmed: bool = False,
    only_planned_variable_changed_confirmed: bool = False,
    now: datetime | None = None,
) -> dict:
    """公開済みcandidateを固定し、観測中へ進める。"""
    if not same_cohort_confirmed:
        raise TacticExperimentError("confirm that baseline and candidate target the same cohort")
    if not only_planned_variable_changed_confirmed:
        raise TacticExperimentError("confirm that only the planned variable changed")
    candidate = _history_video(spec, candidate_video_id)
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest["status"] != "planned":
            raise TacticExperimentError("only a planned experiment can be started")
        if candidate_video_id == manifest["baseline"]["video_id"]:
            raise TacticExperimentError("candidate must differ from baseline")
        _validate_kind_video(manifest["kind"], candidate)
        if candidate.get("corner") != manifest["baseline"]["corner"]:
            raise TacticExperimentError("candidate corner must match baseline")
        if candidate.get("tier") != manifest["baseline"]["tier"]:
            raise TacticExperimentError("candidate tier must match baseline")
        baseline_ts = manifest["baseline"].get("history_ts")
        candidate_ts = candidate.get("ts")
        baseline_time = _history_datetime(baseline_ts, field="baseline")
        candidate_time = _history_datetime(candidate_ts, field="candidate")
        if candidate_time <= baseline_time:
            raise TacticExperimentError("candidate must be published after baseline")
        for existing in _all_manifests(spec):
            if (
                existing.get("experiment_id") != experiment_id
                and existing.get("candidate", {}).get("video_id") == candidate_video_id
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise TacticExperimentError(
                    f"candidate is already in an active experiment: {candidate_video_id}"
                )
        started_at = _now_iso(now)
        manifest = {
            **manifest,
            "status": "running",
            "candidate": _video_snapshot(candidate),
            "started_at": started_at,
            "confirmations": {
                "same_cohort": True,
                "only_planned_variable_changed": True,
            },
        }
        manifest["binding_sha256"] = _binding_checksum(manifest)
        _write_manifest(path, manifest)
    return manifest


def complete_experiment(
    spec: ChannelSpec,
    experiment_id: str,
    *,
    baseline_metrics: object,
    candidate_metrics: object,
    baseline_observation_start: str,
    baseline_observation_end: str,
    candidate_observation_start: str,
    candidate_observation_end: str,
    same_observation_window_confirmed: bool = False,
    studio_values_transcribed_confirmed: bool = False,
    notes: str = "",
    now: datetime | None = None,
) -> dict:
    """同じ観測窓のStudio値を保存し、記述差分だけを作る。"""
    if not same_observation_window_confirmed:
        raise TacticExperimentError("confirm that both videos use the same observation window")
    if not studio_values_transcribed_confirmed:
        raise TacticExperimentError("confirm that values were transcribed from YouTube Studio")
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest["status"] != "running":
            raise TacticExperimentError("only a running experiment can be completed")
        baseline = _validate_metrics(manifest["kind"], baseline_metrics)
        candidate = _validate_metrics(manifest["kind"], candidate_metrics)
        if (
            baseline.get("available")
            and candidate.get("available")
            and manifest["kind"] == "thumbnail_traffic"
            and set(baseline["traffic_sources"])
            != set(candidate["traffic_sources"])
        ):
            raise TacticExperimentError(
                "baseline and candidate must contain the same traffic source set"
            )
        completed_at = _now_iso(now)
        windows = _validate_observation_windows(
            manifest,
            {
                "baseline": {
                    "start": baseline_observation_start,
                    "end": baseline_observation_end,
                },
                "candidate": {
                    "start": candidate_observation_start,
                    "end": candidate_observation_end,
                },
            },
            as_of=datetime.fromisoformat(completed_at),
        )
        result = {
            "baseline_metrics": baseline,
            "candidate_metrics": candidate,
            "deltas": _deltas(manifest["kind"], baseline, candidate),
            "interpretation": (
                "descriptive_only_no_causal_winner"
                if baseline.get("available") and candidate.get("available")
                else "insufficient_data"
            ),
            "observation_windows": windows,
            "same_observation_window_confirmed": True,
            "studio_values_transcribed_confirmed": True,
            "notes": str(notes).strip(),
            "recorded_at": completed_at,
        }
        manifest = {
            **manifest,
            "status": "completed",
            "completed_at": completed_at,
            "result": result,
        }
        manifest["result_sha256"] = _result_checksum(manifest)
        _write_text_atomic(path.parent / "result.md", _result_markdown(manifest))
        _write_manifest(path, manifest)
    return manifest


def show_experiment(spec: ChannelSpec, experiment_id: str) -> dict:
    return _load_manifest(_manifest_path(spec, experiment_id), expected_channel=spec.id)


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("metrics JSON must be an object")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="tactic施策の1変数手動比較をローカル管理（YouTube書込みなし）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--channel", required=True)
    plan.add_argument("--kind", choices=sorted(VALID_KINDS), required=True)
    plan.add_argument("--issue", type=int, required=True)
    plan.add_argument("--baseline-video-id", required=True)
    plan.add_argument("--comparison-key", required=True)
    plan.add_argument("--planned-change", required=True)
    plan.add_argument("--observation-days", type=int, default=7)
    plan.add_argument("--confirm-one-variable", action="store_true")

    start = subparsers.add_parser("start")
    start.add_argument("--channel", required=True)
    start.add_argument("--experiment-id", required=True)
    start.add_argument("--candidate-video-id", required=True)
    start.add_argument("--confirm-same-cohort", action="store_true")
    start.add_argument("--confirm-only-planned-variable", action="store_true")

    complete = subparsers.add_parser("complete")
    complete.add_argument("--channel", required=True)
    complete.add_argument("--experiment-id", required=True)
    complete.add_argument("--baseline-metrics-json", type=_json_object, required=True)
    complete.add_argument("--candidate-metrics-json", type=_json_object, required=True)
    complete.add_argument("--confirm-same-observation-window", action="store_true")
    complete.add_argument("--confirm-studio-values", action="store_true")
    complete.add_argument("--baseline-observation-start", required=True)
    complete.add_argument("--baseline-observation-end", required=True)
    complete.add_argument("--candidate-observation-start", required=True)
    complete.add_argument("--candidate-observation-end", required=True)
    complete.add_argument("--notes", default="")

    show = subparsers.add_parser("show")
    show.add_argument("--channel", required=True)
    show.add_argument("--experiment-id", required=True)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    spec = channel.load(args.channel)
    try:
        if args.command == "plan":
            result = plan_experiment(
                spec,
                kind=args.kind,
                issue_number=args.issue,
                baseline_video_id=args.baseline_video_id,
                comparison_key=args.comparison_key,
                planned_change=args.planned_change,
                observation_days=args.observation_days,
                one_variable_confirmed=args.confirm_one_variable,
            )
        elif args.command == "start":
            result = start_experiment(
                spec,
                args.experiment_id,
                candidate_video_id=args.candidate_video_id,
                same_cohort_confirmed=args.confirm_same_cohort,
                only_planned_variable_changed_confirmed=args.confirm_only_planned_variable,
            )
        elif args.command == "complete":
            result = complete_experiment(
                spec,
                args.experiment_id,
                baseline_metrics=args.baseline_metrics_json,
                candidate_metrics=args.candidate_metrics_json,
                same_observation_window_confirmed=args.confirm_same_observation_window,
                studio_values_transcribed_confirmed=args.confirm_studio_values,
                baseline_observation_start=args.baseline_observation_start,
                baseline_observation_end=args.baseline_observation_end,
                candidate_observation_start=args.candidate_observation_start,
                candidate_observation_end=args.candidate_observation_end,
                notes=args.notes,
            )
        else:
            result = show_experiment(spec, args.experiment_id)
    except TacticExperimentError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
