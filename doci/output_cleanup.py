"""アップロード済みworkdirから再生成可能な媒体ファイルだけを除去する。

`output/<channel>/<run>` はアップロード完了までの一時領域として扱う。一方で、
`script.json` や図表仕様JSONは再生成の入力なので保持する。削除対象は拡張子を
明示した媒体ファイルだけに限定し、履歴でアップロード成功を確認できないrunには
触れない。
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from . import channel
from .channel import ChannelSpec


MEDIA_SUFFIXES = frozenset(
    {
        ".aac",
        ".avif",
        ".avi",
        ".bmp",
        ".flac",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".png",
        ".tif",
        ".tiff",
        ".wav",
        ".webm",
        ".webp",
    }
)
RECOVERY_MANIFEST = "recovery.json"
CLEANUP_LOCK = ".output-cleanup.lock"
_COMPLETED_STATUSES = frozenset({"ok", "skipped"})


@dataclass(frozen=True)
class CleanupResult:
    workdir: str
    status: str
    files: int
    bytes: int
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "workdir": self.workdir,
            "status": self.status,
            "files": self.files,
            "bytes": self.bytes,
            "errors": list(self.errors),
        }


def _result_status(result: object) -> str:
    if isinstance(result, Mapping):
        return str(result.get("status") or "")
    return str(getattr(result, "status", "") or "")


def publish_results_complete(results: Iterable[object]) -> bool:
    """少なくとも1投稿が成功し、再送が必要な結果を含まない場合だけTrue。"""
    statuses = [_result_status(result) for result in results]
    return bool(statuses) and "ok" in statuses and all(
        status in _COMPLETED_STATUSES for status in statuses
    )


def history_row_upload_complete(row: Mapping[str, object]) -> bool:
    """現行履歴とvideo_idだけを持つ旧履歴のアップロード完了を判定する。"""
    manual_recovery = row.get("manual_recovery")
    if isinstance(manual_recovery, Mapping):
        return (
            str(manual_recovery.get("status") or "") == "published"
            and bool(manual_recovery.get("video_id"))
            and bool(manual_recovery.get("recovery_reason"))
        )
    if (
        str(row.get("status") or "") == "published"
        and bool(row.get("video_id"))
        and bool(row.get("recovery_reason"))
    ):
        return True
    results = row.get("publish")
    if isinstance(results, list) and results:
        return publish_results_complete(results)
    status = str(row.get("status") or "")
    return bool(row.get("video_id")) and status in {"", "published"}


def _validated_workdir(output_dir: Path, workdir: Path) -> Path:
    output_root = Path(output_dir).resolve()
    candidate = Path(workdir).resolve()
    if candidate.parent != output_root:
        raise ValueError(
            f"cleanup target must be a direct child of {output_root}: {candidate}"
        )
    if not candidate.is_dir():
        raise ValueError(f"cleanup target is not a directory: {candidate}")
    return candidate


def _validated_script(workdir: Path) -> dict[str, object]:
    script_path = workdir / "script.json"
    if not script_path.is_file():
        raise ValueError(f"recovery input is missing: {script_path}")
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid recovery input: {script_path}: {exc}") from exc
    scenes = script.get("scenes") if isinstance(script, dict) else None
    if not (
        isinstance(script, dict)
        and isinstance(script.get("title"), str)
        and bool(str(script.get("title") or "").strip())
        and isinstance(script.get("description"), str)
        and isinstance(script.get("tags"), (list, str))
        and isinstance(script.get("narration"), str)
        and bool(str(script.get("narration") or "").strip())
        and isinstance(scenes, list)
        and bool(scenes)
        and all(isinstance(scene, Mapping) for scene in scenes)
    ):
        raise ValueError(
            f"recovery input lacks a regenerable script structure: {script_path}"
        )
    return script


def _media_files(workdir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in workdir.rglob("*")
            if path.suffix.lower() in MEDIA_SUFFIXES
            and (path.is_file() or path.is_symlink())
        ),
        key=lambda path: path.as_posix(),
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    # 同一processの複数threadや古いtmp残骸とも衝突しない名前にする。
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        # renameのdirectory entryまで媒体unlinkより先に耐久化する。
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _existing_recovery(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_missing(
    saved: Mapping[str, object],
    supplemental: Mapping[str, object],
) -> dict[str, object]:
    """既に保存した実行時値を優先し、後続情報は不足キーだけ補う。"""
    merged = copy.deepcopy(dict(saved))
    for key, value in supplemental.items():
        current = merged.get(key)
        if key not in merged:
            merged[key] = copy.deepcopy(value)
        elif isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_missing(current, value)
    return merged


def cleanup_workdir(
    output_dir: Path,
    workdir: Path,
    *,
    apply: bool,
    recovery: Mapping[str, object] | None = None,
) -> CleanupResult:
    """媒体だけを削除する。apply=Falseは読み取り専用のpreview。"""
    target = _validated_workdir(output_dir, workdir)
    if not apply:
        # previewではlock fileも作らず、完全に読み取り専用にする。
        _validated_script(target)
        media = _media_files(target)
        total_bytes = sum(path.lstat().st_size for path in media)
        return CleanupResult(str(target), "preview", len(media), total_bytes)

    # 自動整理と保守コマンドが重なってもmanifestとunlinkを競合させない。
    lock_path = target / CLEANUP_LOCK
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return _cleanup_workdir_locked(target, recovery=recovery)


def _cleanup_workdir_locked(
    target: Path,
    *,
    recovery: Mapping[str, object] | None,
) -> CleanupResult:
    """workdir lockを保持した状態で検証から完了記録まで実行する。"""
    # 写真を捨てても再生成できるという契約を満たせないrunでは削除しない。
    _validated_script(target)
    media_sizes: dict[Path, int] = {}
    for path in _media_files(target):
        try:
            media_sizes[path] = path.lstat().st_size
        except FileNotFoundError:
            # lockを無視する外部処理が先に消しても冪等に扱う。
            continue
    media = list(media_sizes)
    total_bytes = sum(media_sizes.values())

    # 新規run直後でもscriptの内容とdirectory entryを先に耐久化する。
    script_fd = os.open(target / "script.json", os.O_RDONLY)
    try:
        os.fsync(script_fd)
    finally:
        os.close(script_fd)
    target_fd = os.open(target, os.O_RDONLY)
    try:
        os.fsync(target_fd)
    finally:
        os.close(target_fd)

    manifest_path = target / RECOVERY_MANIFEST
    manifest = _existing_recovery(manifest_path)
    saved_recovery = manifest.get("recovery")
    merged_recovery = (
        copy.deepcopy(saved_recovery) if isinstance(saved_recovery, dict) else {}
    )
    if recovery:
        merged_recovery = _merge_missing(merged_recovery, recovery)
    planned = {
        "schema_version": 1,
        "recovery": merged_recovery,
        "cleanup": {
            "status": "planned",
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "media_files": len(media),
            "media_bytes": total_bytes,
        },
    }
    # manifestを先に耐久保存し、途中停止しても再生成情報が媒体より先に失われないようにする。
    _write_json_atomic(manifest_path, planned)

    deleted_files = 0
    deleted_bytes = 0
    errors: list[str] = []
    for path in media:
        try:
            size = path.lstat().st_size
            path.unlink()
        except FileNotFoundError:
            # 同じ規約を使わない外部削除との競合も成功済みとして扱う。
            continue
        except OSError as exc:
            errors.append(f"{path.relative_to(target)}: {exc}")
        else:
            deleted_files += 1
            deleted_bytes += size

    # 媒体だけだった子ディレクトリは空なら除去する。JSON等があればそのまま残る。
    directories = sorted(
        (path for path in target.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass

    status = "partial" if errors else "cleaned" if deleted_files else "already_clean"
    completed = {
        **planned,
        "cleanup": {
            **planned["cleanup"],
            "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "deleted_files": deleted_files,
            "deleted_bytes": deleted_bytes,
            "errors": errors,
        },
    }
    try:
        _write_json_atomic(manifest_path, completed)
    except OSError as exc:
        # planned manifestは媒体より先に保存済みなので復元情報は残っている。
        # 削除後の追記失敗を投げ直すとcallerが「媒体を保持した」と誤認するため、
        # partialとして明示し、保守コマンドの再実行で冪等に完了させる。
        errors.append(f"{RECOVERY_MANIFEST}: completion update failed: {exc}")
        status = "partial"
    return CleanupResult(
        str(target),
        status,
        deleted_files,
        deleted_bytes,
        tuple(errors),
    )


def _history_rows(spec: ChannelSpec) -> list[dict[str, object]]:
    if not spec.history_file.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line in spec.history_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _reservation_keys(row: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    keys: list[tuple[str, str]] = []
    for field in ("topic_ledger_reservation_id", "reservation_id"):
        value = str(row.get(field) or "")
        if value:
            keys.append((field, value))
    return tuple(keys)


def _latest_rows_by_workdir(
    rows: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """workdir行へ、明示的なpublishing復旧terminalを予約IDで関連付ける。"""
    recovered_by_reservation: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        if not (
            str(row.get("status") or "") == "published"
            and bool(row.get("video_id"))
            and bool(row.get("recovery_reason"))
        ):
            continue
        for key in _reservation_keys(row):
            recovered_by_reservation[key] = row

    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        raw_workdir = row.get("workdir")
        if not isinstance(raw_workdir, str) or not raw_workdir:
            continue
        candidate = dict(row)
        if not history_row_upload_complete(candidate):
            terminal = next(
                (
                    recovered_by_reservation[key]
                    for key in _reservation_keys(row)
                    if key in recovered_by_reservation
                ),
                None,
            )
            if terminal is not None:
                candidate.update(terminal)
                candidate["workdir"] = raw_workdir
                candidate["manual_recovery"] = dict(terminal)
        latest[raw_workdir] = candidate
    return latest


def _history_recovery(
    spec: ChannelSpec,
    row: Mapping[str, object],
    workdir: Path,
) -> dict[str, object]:
    script = _validated_script(workdir)
    corner_key = str(row.get("corner") or script.get("_corner") or "")
    recovery: dict[str, object] = {
        "script": "script.json",
        "channel": spec.id,
        "corner": corner_key or None,
        "date": script.get("_date"),
        "title": script.get("title"),
        "video_id": row.get("video_id"),
        "render": {
            key: row.get(key)
            for key in ("duration_sec", "tier", "platforms")
            if row.get(key) is not None
        },
        "publish": row.get("publish"),
        "history": dict(row),
    }
    corners = getattr(spec, "corners", {})
    if corner_key and corner_key in corners:
        try:
            corner = corners[corner_key]
            voice = spec.voice_for(corner)
        except Exception:
            pass
        else:
            recovery["voice"] = {
                "key": corner.voice_key,
                "speaker": voice.speaker,
                "speed": voice.speed,
                "pitch": voice.pitch,
                "intonation": voice.intonation,
                "intonation_vary": voice.intonation_vary,
                "volume": voice.volume,
                "label": voice.label,
            }
    return recovery


def cleanup_uploaded_outputs(spec: ChannelSpec, *, apply: bool) -> dict[str, object]:
    """履歴上アップロード完了済みのworkdirだけを一括整理する。"""
    latest_by_workdir = _latest_rows_by_workdir(_history_rows(spec))

    results: list[CleanupResult] = []
    skipped_paths: list[str] = []
    for raw_workdir, row in latest_by_workdir.items():
        if not history_row_upload_complete(row):
            continue
        workdir = Path(raw_workdir)
        if not workdir.is_absolute():
            workdir = spec.output_dir / workdir
        if not workdir.exists():
            continue
        try:
            result = cleanup_workdir(
                spec.output_dir,
                workdir,
                apply=apply,
                recovery=_history_recovery(spec, row, workdir),
            )
        except ValueError:
            skipped_paths.append(str(workdir))
            continue
        results.append(result)

    return {
        "channel": spec.id,
        "mode": "apply" if apply else "preview",
        "workdirs": len(results),
        "files": sum(result.files for result in results),
        "bytes": sum(result.bytes for result in results),
        "errors": sum(bool(result.errors) for result in results),
        "skipped_unsafe_paths": skipped_paths,
        "results": [result.to_dict() for result in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="アップロード完了済みoutputの媒体を整理（既定はpreview）"
    )
    parser.add_argument(
        "--channel",
        action="append",
        dest="channels",
        help="対象チャンネルID（複数指定可、未指定なら全チャンネル）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際に媒体を削除する（未指定時はpreviewのみ）",
    )
    args = parser.parse_args()
    channel_ids = args.channels or channel.discover()
    summaries = [
        cleanup_uploaded_outputs(channel.load(channel_id), apply=args.apply)
        for channel_id in channel_ids
    ]
    payload = {
        "mode": "apply" if args.apply else "preview",
        "channels": summaries,
        "workdirs": sum(int(item["workdirs"]) for item in summaries),
        "files": sum(int(item["files"]) for item in summaries),
        "bytes": sum(int(item["bytes"]) for item in summaries),
        "errors": sum(int(item["errors"]) for item in summaries),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
