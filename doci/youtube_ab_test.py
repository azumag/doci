"""YouTube StudioのネイティブA/Bテストを手動運用するための記録CLI。

YouTubeへの書込みは行わない。dociの投稿履歴にある通常動画を対象に、Studioへ
登録する2〜3案を固定し、開始・結果・テスト中の手動変更による無効化をローカルの
マニフェストへ記録する。結果は次回企画メモにも書き出す。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from PIL import Image, UnidentifiedImageError

from . import channel
from .channel import ChannelSpec


SCHEMA_VERSION = 1
OFFICIAL_HELP_URL = "https://support.google.com/youtube/answer/16391400?hl=ja"
VALID_MODES = frozenset({"title", "thumbnail", "both"})
VALID_OUTCOMES = frozenset(
    {"winner", "performed_same", "inconclusive", "stopped_manual_change"}
)
ACTIVE_STATUSES = frozenset({"planned", "running"})
VALID_STATUSES = frozenset({"planned", "running", "completed", "invalidated"})
_EXPERIMENT_ID_RE = re.compile(r"yab-[0-9a-f]{16}")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_VARIANT_LABELS = ("A", "B", "C")
_IMAGE_FORMAT_SUFFIX = {"JPEG": ".jpg", "PNG": ".png"}
_PLAN_FIELDS = (
    "schema_version",
    "experiment_id",
    "channel",
    "video_id",
    "mode",
    "created_at",
    "official_help_url",
    "decision_metric",
    "manual_changes_prohibited_while_running",
    "expected_completion_within_days",
    "source",
    "variants",
    "warnings",
)


class YouTubeABTestError(ValueError):
    """A/Bテスト計画または状態遷移が安全に実行できない。"""


def _root(spec: ChannelSpec) -> Path:
    return spec.output_dir / "youtube_ab_tests"


def _manifest_path(spec: ChannelSpec, experiment_id: str) -> Path:
    if not _EXPERIMENT_ID_RE.fullmatch(experiment_id):
        raise YouTubeABTestError(f"invalid experiment id: {experiment_id!r}")
    directory = _root(spec) / experiment_id
    if directory.is_symlink():
        raise YouTubeABTestError(f"A/B test directory must not be a symlink: {directory}")
    return directory / "manifest.json"


def _now_iso(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise YouTubeABTestError("now must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat()


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    root = _root(spec)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _write_manifest(path: Path, manifest: dict) -> None:
    _write_text_atomic(
        path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )


def _plan_checksum(manifest: dict) -> str:
    payload = {key: manifest.get(key) for key in _PLAN_FIELDS}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_manifest_variants(data: dict, path: Path) -> None:
    mode = data.get("mode")
    variants = data.get("variants")
    if not isinstance(variants, list) or len(variants) not in {2, 3}:
        raise YouTubeABTestError(f"invalid A/B test manifest variants: {path}")
    if [item.get("label") if isinstance(item, dict) else None for item in variants] != list(
        _VARIANT_LABELS[: len(variants)]
    ):
        raise YouTubeABTestError(f"invalid A/B test manifest labels: {path}")

    titles: list[str] = []
    thumbnail_hashes: list[str] = []
    for variant in variants:
        if not isinstance(variant, dict):
            raise YouTubeABTestError(f"invalid A/B test variant object: {path}")
        title = variant.get("title")
        thumbnail = variant.get("thumbnail")
        if mode in {"title", "both"}:
            if not isinstance(title, str) or _normalise_titles([title]) != [title]:
                raise YouTubeABTestError(f"invalid A/B test variant title: {path}")
            titles.append(title)
        elif "title" in variant:
            raise YouTubeABTestError(f"thumbnail mode contains a title: {path}")
        if mode in {"thumbnail", "both"}:
            if not isinstance(thumbnail, dict):
                raise YouTubeABTestError(f"missing A/B test variant thumbnail: {path}")
            label = variant["label"]
            filename = thumbnail.get("file")
            digest = thumbnail.get("sha256")
            width = thumbnail.get("width")
            height = thumbnail.get("height")
            if (
                filename not in {f"variant_{label}.png", f"variant_{label}.jpg"}
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not isinstance(width, int)
                or isinstance(width, bool)
                or width <= 0
                or not isinstance(height, int)
                or isinstance(height, bool)
                or height <= 0
            ):
                raise YouTubeABTestError(
                    f"invalid A/B test variant thumbnail metadata: {path}"
                )
            thumbnail_hashes.append(digest)
        elif "thumbnail" in variant:
            raise YouTubeABTestError(f"title mode contains a thumbnail: {path}")
    if titles and len(set(titles)) != len(titles):
        raise YouTubeABTestError(f"duplicate A/B test manifest titles: {path}")
    if thumbnail_hashes and len(set(thumbnail_hashes)) != len(thumbnail_hashes):
        raise YouTubeABTestError(f"duplicate A/B test manifest thumbnails: {path}")


def _validate_manifest_result(data: dict, path: Path) -> None:
    status = data.get("status")
    result = data.get("result")
    if status == "planned":
        if any(key in data for key in ("started_at", "completed_at", "result")):
            raise YouTubeABTestError(f"planned A/B test has result state: {path}")
        return
    if not isinstance(data.get("started_at"), str) or not data["started_at"]:
        raise YouTubeABTestError(f"started A/B test lacks started_at: {path}")
    if status == "running":
        if any(key in data for key in ("completed_at", "result")):
            raise YouTubeABTestError(f"running A/B test has result state: {path}")
        return
    if (
        not isinstance(data.get("completed_at"), str)
        or not data["completed_at"]
        or not isinstance(result, dict)
    ):
        raise YouTubeABTestError(f"completed A/B test lacks result state: {path}")
    outcome = result.get("outcome")
    winner = result.get("winner_variant")
    labels = {item["label"] for item in data["variants"]}
    no_manual_change = result.get("no_manual_change_confirmed")
    if status == "invalidated":
        if (
            outcome != "stopped_manual_change"
            or winner is not None
            or no_manual_change is not False
        ):
            raise YouTubeABTestError(f"invalid invalidated A/B test result: {path}")
        return
    if outcome not in {"winner", "performed_same", "inconclusive"}:
        raise YouTubeABTestError(f"invalid completed A/B test outcome: {path}")
    if no_manual_change is not True:
        raise YouTubeABTestError(
            f"completed A/B test lacks no-manual-change confirmation: {path}"
        )
    if (outcome == "winner" and winner not in labels) or (
        outcome != "winner" and winner is not None
    ):
        raise YouTubeABTestError(f"invalid completed A/B test winner: {path}")


def _load_manifest(path: Path, *, expected_channel: str | None = None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YouTubeABTestError(f"invalid A/B test manifest: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise YouTubeABTestError(f"invalid A/B test manifest object: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise YouTubeABTestError(f"unsupported A/B test manifest schema: {path}")
    if data.get("experiment_id") != path.parent.name:
        raise YouTubeABTestError(f"A/B test manifest id mismatch: {path}")
    if expected_channel is not None and data.get("channel") != expected_channel:
        raise YouTubeABTestError(f"A/B test manifest channel mismatch: {path}")
    if data.get("mode") not in VALID_MODES:
        raise YouTubeABTestError(f"invalid A/B test manifest mode: {path}")
    if data.get("status") not in VALID_STATUSES:
        raise YouTubeABTestError(f"invalid A/B test manifest status: {path}")
    if not isinstance(data.get("video_id"), str) or not _VIDEO_ID_RE.fullmatch(
        data["video_id"]
    ):
        raise YouTubeABTestError(f"invalid A/B test manifest video id: {path}")
    _validate_manifest_variants(data, path)
    _validate_manifest_result(data, path)
    expected_checksum = data.get("plan_sha256")
    if (
        not isinstance(expected_checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_checksum) is None
        or not hmac.compare_digest(expected_checksum, _plan_checksum(data))
    ):
        raise YouTubeABTestError(f"A/B test plan checksum mismatch: {path}")
    return data


def _all_manifests(spec: ChannelSpec) -> list[dict]:
    root = _root(spec)
    if not root.exists():
        return []
    manifests: list[dict] = []
    for directory in sorted(root.iterdir()):
        if not _EXPERIMENT_ID_RE.fullmatch(directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise YouTubeABTestError(
                f"invalid A/B test experiment directory: {directory}"
            )
        path = directory / "manifest.json"
        if not path.is_file():
            raise YouTubeABTestError(f"A/B test manifest is missing: {path}")
        manifests.append(_load_manifest(path, expected_channel=spec.id))
    return manifests


def _history_video(spec: ChannelSpec, video_id: str) -> dict:
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeABTestError(f"invalid YouTube video id: {video_id!r}")
    if not spec.history_file.exists():
        raise YouTubeABTestError(f"doci history is missing: {spec.history_file}")
    found: dict | None = None
    for line in spec.history_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("video_id") or "") == video_id:
            found = row
    if found is None:
        raise YouTubeABTestError(
            f"video is not present in doci history: {video_id}"
        )
    if found.get("tier") != "longform":
        raise YouTubeABTestError("YouTube Studio A/B tests are not available for Shorts")
    if str(found.get("status") or "") != "published":
        raise YouTubeABTestError("video is not recorded as published")
    if found.get("youtube_privacy") not in {"public", "unlisted"}:
        raise YouTubeABTestError(
            "video privacy must be recorded as public or unlisted"
        )
    return found


def _normalise_titles(values: Sequence[str]) -> list[str]:
    titles = [" ".join(str(value).split()) for value in values]
    if any(not title for title in titles):
        raise YouTubeABTestError("title variants must not be empty")
    if any(len(title) > 100 for title in titles):
        raise YouTubeABTestError("YouTube title variants must be at most 100 characters")
    if len(set(titles)) != len(titles):
        raise YouTubeABTestError("title variants must be distinct")
    return titles


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _thumbnail_metadata(source: Path) -> tuple[str, int, int, str]:
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise YouTubeABTestError(f"thumbnail is not a file: {path}")
    try:
        with Image.open(path) as image:
            image.verify()
            image_format = str(image.format or "").upper()
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError) as exc:
        raise YouTubeABTestError(f"invalid thumbnail image: {path}: {exc}") from exc
    suffix = _IMAGE_FORMAT_SUFFIX.get(image_format)
    if suffix is None:
        raise YouTubeABTestError("thumbnail variants must be PNG or JPEG images")
    digest = _sha256_file(path)
    return suffix, int(width), int(height), digest


def _validate_frozen_thumbnails(directory: Path, manifest: dict) -> None:
    if manifest["mode"] == "title":
        return
    for variant in manifest["variants"]:
        expected = variant["thumbnail"]
        path = directory / expected["file"]
        if path.is_symlink() or not path.is_file() or path.parent != directory:
            raise YouTubeABTestError(f"frozen thumbnail is missing or unsafe: {path}")
        suffix, width, height, digest = _thumbnail_metadata(path)
        if (
            path.suffix != suffix
            or width != expected["width"]
            or height != expected["height"]
            or not hmac.compare_digest(digest, expected["sha256"])
        ):
            raise YouTubeABTestError(f"frozen thumbnail changed after planning: {path}")


def _variant_count(
    mode: str,
    titles: Sequence[str],
    thumbnail_paths: Sequence[Path],
) -> int:
    if mode not in VALID_MODES:
        raise YouTubeABTestError(f"invalid A/B test mode: {mode!r}")
    if mode == "title":
        if thumbnail_paths:
            raise YouTubeABTestError("title mode must not include thumbnail variants")
        count = len(titles)
    elif mode == "thumbnail":
        if titles:
            raise YouTubeABTestError("thumbnail mode must not include title variants")
        count = len(thumbnail_paths)
    else:
        if len(titles) != len(thumbnail_paths):
            raise YouTubeABTestError(
                "both mode requires one title and one thumbnail per variant"
            )
        count = len(titles)
    if count not in {2, 3}:
        raise YouTubeABTestError("YouTube Studio A/B tests require 2 or 3 variants")
    return count


def _safe_cell(value: object) -> str:
    return str(value or "").replace("|", "｜").replace("\n", " ")


def _plan_markdown(manifest: dict) -> str:
    rows = []
    for variant in manifest["variants"]:
        thumbnail = variant.get("thumbnail") or {}
        rows.append(
            "| {label} | {title} | {thumbnail} |".format(
                label=variant["label"],
                title=_safe_cell(variant.get("title")) or "（固定）",
                thumbnail=_safe_cell(thumbnail.get("file")) or "（固定）",
            )
        )
    warnings = manifest.get("warnings") or []
    warning_text = "\n".join(f"- {item}" for item in warnings) or "- なし"
    return f"""# YouTube Studio A/Bテスト計画

- experiment_id: `{manifest['experiment_id']}`
- video_id: `{manifest['video_id']}`
- mode: `{manifest['mode']}`
- 判定指標: YouTube Studioの総再生時間シェア
- 公式仕様: {OFFICIAL_HELP_URL}

## 固定する案

| 案 | タイトル | サムネイル |
| --- | --- | --- |
{chr(10).join(rows)}

## 実施手順

1. パソコン版YouTube Studioで対象の通常動画を開きます。
2. A/Bテストで上記2〜3案を同じ順序で登録します。
3. 登録完了後に`start`を記録します。
4. テスト中はタイトル・サムネイルを手動変更しません。変更するとStudio側のテストが停止するため、変更した場合は`stopped_manual_change`として無効化します。
5. Studioの結果（winner / performed_same / inconclusive）を`complete`で記録します。通常は数日から2週間かかります。

## 品質上の注意

{warning_text}
"""


def _result_memo(manifest: dict) -> str:
    result = manifest["result"]
    winner = result.get("winner_variant") or "なし"
    notes = str(result.get("notes") or "").strip() or "記載なし"
    return f"""# 次回企画メモ: YouTube Studio A/Bテスト

- experiment_id: `{manifest['experiment_id']}`
- video_id: `{manifest['video_id']}`
- mode: `{manifest['mode']}`
- outcome: `{result['outcome']}`
- winner_variant: `{winner}`
- 判定指標: YouTube Studioの総再生時間シェア
- 記録日時: `{result['recorded_at']}`

## 運用メモ

{notes}

この結果は対象動画内の同時比較です。別動画へそのまま一般化せず、次の企画では勝因候補を1つだけ仮説として扱います。`performed_same`または`inconclusive`は勝者として扱いません。`stopped_manual_change`はテスト無効です。
"""


def plan_experiment(
    spec: ChannelSpec,
    *,
    video_id: str,
    mode: str,
    titles: Sequence[str] = (),
    thumbnail_paths: Sequence[Path] = (),
    studio_eligible_confirmed: bool = False,
    now: datetime | None = None,
    experiment_id: str | None = None,
) -> dict:
    """Studioへ登録する案を固定し、YouTubeへ触れず計画を保存する。"""
    if not studio_eligible_confirmed:
        raise YouTubeABTestError(
            "confirm desktop Studio access, advanced features, and content eligibility"
        )
    recorded = _history_video(spec, video_id)
    count = _variant_count(mode, titles, thumbnail_paths)
    normalised_titles = _normalise_titles(titles) if titles else []
    created_at = _now_iso(now)

    with _operation_lock(spec):
        for existing in _all_manifests(spec):
            if (
                existing.get("video_id") == video_id
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise YouTubeABTestError(
                    f"active A/B test already exists for video {video_id}: "
                    f"{existing.get('experiment_id')}"
                )
        candidate_id = experiment_id or f"yab-{uuid.uuid4().hex[:16]}"
        target = _manifest_path(spec, candidate_id).parent
        if target.exists():
            raise YouTubeABTestError(f"experiment already exists: {candidate_id}")

        root = _root(spec)
        staging = Path(tempfile.mkdtemp(prefix=".plan-", dir=root))
        warnings: list[str] = []
        variants: list[dict] = []
        thumbnail_digests: set[str] = set()
        try:
            for index in range(count):
                label = _VARIANT_LABELS[index]
                variant: dict[str, object] = {"label": label}
                if mode in {"title", "both"}:
                    variant["title"] = normalised_titles[index]
                if mode in {"thumbnail", "both"}:
                    source = Path(thumbnail_paths[index])
                    source = source.expanduser().resolve()
                    if not source.is_file():
                        raise YouTubeABTestError(
                            f"thumbnail is not a file: {source}"
                        )
                    staged_source = staging / f".variant_{label}.upload"
                    shutil.copyfile(source, staged_source)
                    suffix, width, height, digest = _thumbnail_metadata(staged_source)
                    if digest in thumbnail_digests:
                        raise YouTubeABTestError(
                            "thumbnail variants must have distinct file contents"
                        )
                    thumbnail_digests.add(digest)
                    filename = f"variant_{label}{suffix}"
                    os.replace(staged_source, staging / filename)
                    variant["thumbnail"] = {
                        "file": filename,
                        "sha256": digest,
                        "width": width,
                        "height": height,
                    }
                    if width < 1280 or height < 720:
                        warnings.append(
                            f"案{label}は{width}x{height}です。1280x720未満の案があると、Studioが全案を低解像度化する場合があります。"
                        )
                    if abs((width / height) - (16 / 9)) > 0.02:
                        warnings.append(
                            f"案{label}は16:9ではありません（{width}x{height}）。表示時の切り抜きを確認してください。"
                        )
                variants.append(variant)

            manifest = {
                "schema_version": SCHEMA_VERSION,
                "experiment_id": candidate_id,
                "channel": spec.id,
                "video_id": video_id,
                "mode": mode,
                "status": "planned",
                "created_at": created_at,
                "official_help_url": OFFICIAL_HELP_URL,
                "decision_metric": "youtube_studio.watch_time_share",
                "manual_changes_prohibited_while_running": True,
                "expected_completion_within_days": 14,
                "source": {
                    "title": str(recorded.get("title") or ""),
                    "history_ts": str(recorded.get("ts") or ""),
                    "workdir": str(recorded.get("workdir") or ""),
                    "tier": "longform",
                    "youtube_privacy": recorded.get("youtube_privacy"),
                },
                "variants": variants,
                "warnings": warnings,
            }
            manifest["plan_sha256"] = _plan_checksum(manifest)
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
    studio_started_confirmed: bool = False,
    now: datetime | None = None,
) -> dict:
    """Studioへ同じ案を登録済みであることを確認し、runningへ進める。"""
    if not studio_started_confirmed:
        raise YouTubeABTestError(
            "confirm that the frozen variants were started in YouTube Studio"
        )
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "planned":
            raise YouTubeABTestError("only a planned experiment can be started")
        _validate_frozen_thumbnails(path.parent, manifest)
        for existing in _all_manifests(spec):
            if (
                existing.get("experiment_id") != experiment_id
                and existing.get("video_id") == manifest.get("video_id")
                and existing.get("status") in ACTIVE_STATUSES
            ):
                raise YouTubeABTestError(
                    "another active A/B test exists for this video: "
                    f"{existing.get('experiment_id')}"
                )
        manifest = {
            **manifest,
            "status": "running",
            "started_at": _now_iso(now),
        }
        _write_manifest(path, manifest)
    return manifest


def complete_experiment(
    spec: ChannelSpec,
    experiment_id: str,
    *,
    outcome: str,
    winner_variant: str | None = None,
    notes: str = "",
    no_manual_change_confirmed: bool = False,
    now: datetime | None = None,
) -> dict:
    """Studioの表示結果を記録し、次回企画メモを書き出す。"""
    if outcome not in VALID_OUTCOMES:
        raise YouTubeABTestError(f"invalid A/B test outcome: {outcome!r}")
    if outcome == "stopped_manual_change":
        if no_manual_change_confirmed:
            raise YouTubeABTestError(
                "stopped_manual_change conflicts with no-manual-change confirmation"
            )
    elif not no_manual_change_confirmed:
        raise YouTubeABTestError(
            "confirm that no title or thumbnail was manually changed during the test"
        )
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "running":
            raise YouTubeABTestError("only a running experiment can be completed")
        labels = {str(item.get("label")) for item in manifest.get("variants", [])}
        if outcome == "winner":
            if winner_variant not in labels:
                raise YouTubeABTestError(
                    "winner outcome requires a valid --winner variant label"
                )
        elif winner_variant is not None:
            raise YouTubeABTestError(
                "non-winner outcomes must not record a winner variant"
            )
        recorded_at = _now_iso(now)
        status = "invalidated" if outcome == "stopped_manual_change" else "completed"
        manifest = {
            **manifest,
            "status": status,
            "completed_at": recorded_at,
            "result": {
                "outcome": outcome,
                "winner_variant": winner_variant,
                "notes": str(notes).strip(),
                "recorded_at": recorded_at,
                "no_manual_change_confirmed": no_manual_change_confirmed,
            },
        }
        _write_text_atomic(path.parent / "next_idea_memo.md", _result_memo(manifest))
        _write_manifest(path, manifest)
    return manifest


def show_experiment(spec: ChannelSpec, experiment_id: str) -> dict:
    return _load_manifest(
        _manifest_path(spec, experiment_id),
        expected_channel=spec.id,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YouTube Studio A/Bテストの案と結果をローカル管理（YouTube書込みなし）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--channel", required=True)
    plan.add_argument("--video-id", required=True)
    plan.add_argument("--mode", choices=sorted(VALID_MODES), required=True)
    plan.add_argument("--title", action="append", default=[])
    plan.add_argument("--thumbnail", action="append", type=Path, default=[])
    plan.add_argument("--confirm-studio-eligible", action="store_true")

    start = subparsers.add_parser("start")
    start.add_argument("--channel", required=True)
    start.add_argument("--experiment-id", required=True)
    start.add_argument("--confirm-studio-started", action="store_true")

    complete = subparsers.add_parser("complete")
    complete.add_argument("--channel", required=True)
    complete.add_argument("--experiment-id", required=True)
    complete.add_argument("--outcome", choices=sorted(VALID_OUTCOMES), required=True)
    complete.add_argument("--winner", choices=_VARIANT_LABELS)
    complete.add_argument("--notes", default="")
    complete.add_argument("--confirm-no-manual-change", action="store_true")

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
                video_id=args.video_id,
                mode=args.mode,
                titles=args.title,
                thumbnail_paths=args.thumbnail,
                studio_eligible_confirmed=args.confirm_studio_eligible,
            )
        elif args.command == "start":
            result = start_experiment(
                spec,
                args.experiment_id,
                studio_started_confirmed=args.confirm_studio_started,
            )
        elif args.command == "complete":
            result = complete_experiment(
                spec,
                args.experiment_id,
                outcome=args.outcome,
                winner_variant=args.winner,
                notes=args.notes,
                no_manual_change_confirmed=args.confirm_no_manual_change,
            )
        else:
            result = show_experiment(spec, args.experiment_id)
    except YouTubeABTestError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
