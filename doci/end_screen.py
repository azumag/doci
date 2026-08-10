"""YouTube終了画面の1枠を手動運用するための記録CLI（issue #165）。

YouTubeへの書込みは行わない。dociの投稿履歴にある通常動画を対象に、次回設定する
動画要素（video要素1枠のみ）を固定し、Studioのエンゲージメントにある終了画面要素の
クリック率で検証する計画・開始・結果をローカルへ記録する。結果は次回企画メモにも
書き出す。

終了画面は「登録・動画・再生リストを同時に並べるだけ」にするのをやめ、次の1本に
内容が直結するvideo要素を1枠だけ設定する運用を固定する。
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
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from . import channel
from .channel import ChannelSpec


SCHEMA_VERSION = 1
OFFICIAL_HELP_URL = "https://support.google.com/youtube/answer/6388789?hl=ja"
VALID_OUTCOMES = frozenset(
    {
        "clicked",
        "not_clicked",
        "insufficient_views",
        "stopped_changed_setup",
    }
)
ACTIVE_STATUSES = frozenset({"planned", "running"})
VALID_STATUSES = frozenset({"planned", "running", "completed", "invalidated"})
_EXPERIMENT_ID_RE = re.compile(r"esc-[0-9a-f]{16}")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_LINK_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_PLAN_FIELDS = (
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
_TIMESTAMP_FIELD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class EndScreenError(ValueError):
    """終了画面計画または状態遷移が安全に実行できない。"""


def _root(spec: ChannelSpec) -> Path:
    return spec.output_dir / "end_screen_tests"


def _ensure_root_dir(path: Path) -> Path:
    """記録先ルートを実ディレクトリとして検証する（symlinkは拒否）。"""
    if path.is_symlink():
        raise EndScreenError(f"end screen test root must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise EndScreenError(f"end screen test root is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise EndScreenError(f"end screen test root must not be a symlink: {path}")
    return path


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


def _validate_root_readable(path: Path) -> None:
    """読み取り時もrootが実ディレクトリであることを検証する（symlink拒否）。"""
    if path.is_symlink():
        raise EndScreenError(f"end screen test root must not be a symlink: {path}")
    if not path.is_dir():
        raise EndScreenError(f"end screen test root is not a directory: {path}")


def _now_iso(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise EndScreenError("now must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat()


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    """1チャンネルの終了画面記録を直列化する。"""
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


def _plan_checksum(manifest: dict) -> str:
    stable = {key: manifest[key] for key in _PLAN_FIELDS if key in manifest}
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _validate_manifest_plan(data: dict, path: Path) -> None:
    if not isinstance(data, dict):
        raise EndScreenError(f"invalid manifest: {path}")
    if not path.name == "manifest.json":
        raise EndScreenError(f"manifest file name mismatch: {path.name!r}")
    experiment_id = str(data.get("experiment_id") or "")
    directory = path.parent.name
    if experiment_id != directory:
        raise EndScreenError(
            f"experiment_id/directory mismatch: {experiment_id!r} != {directory!r}"
        )
    missing = [key for key in _PLAN_FIELDS if key not in data]
    if missing:
        raise EndScreenError(
            f"end screen manifest is missing fields: {', '.join(missing)}"
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise EndScreenError(f"unsupported schema version: {data.get('schema_version')}")
    if data.get("status") not in VALID_STATUSES:
        raise EndScreenError(f"invalid status: {data.get('status')!r}")
    setup = data.get("end_screen_setup")
    if not isinstance(setup, dict):
        raise EndScreenError("end_screen_setup must be an object")
    element = str(setup.get("element") or "")
    if element != "video":
        raise EndScreenError("end screen element must be a single video element")
    link_id_raw = setup.get("link_video_id")
    if not isinstance(link_id_raw, str):
        raise EndScreenError("link_video_id must be a string")
    link_id = link_id_raw
    if not _LINK_VIDEO_ID_RE.fullmatch(link_id):
        raise EndScreenError("end screen requires a valid link_video_id")
    video_id_raw = data.get("video_id")
    if not isinstance(video_id_raw, str):
        raise EndScreenError("video_id must be a string")
    video_id = video_id_raw
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise EndScreenError(f"invalid video_id: {video_id!r}")
    if link_id == video_id:
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
    expected = _plan_checksum(data)
    actual = str(data.get("plan_sha256") or "")
    if not hmac.compare_digest(actual, expected):
        raise EndScreenError("end screen plan checksum mismatch")
    source = data.get("source")
    if not isinstance(source, dict):
        raise EndScreenError("source must be an object")
    if str(source.get("tier") or "") != "longform":
        raise EndScreenError("source tier must be longform")
    if str(source.get("youtube_privacy") or "") not in {"public", "unlisted"}:
        raise EndScreenError("source youtube_privacy must be public or unlisted")


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


def _validate_manifest_status(data: dict) -> None:
    """status別の状態schemaを検証する（planned/running/terminal）。"""
    status = str(data.get("status") or "")
    if status == "planned":
        for field in ("started_at", "completed_at", "result"):
            if field in data:
                raise EndScreenError(
                    f"planned manifest must not have {field}"
                )
    elif status == "running":
        _validate_timestamp(data.get("started_at"), "started_at")
        for field in ("completed_at", "result"):
            if field in data:
                raise EndScreenError(
                    f"running manifest must not have {field}"
                )
    elif status in ("completed", "invalidated"):
        _validate_timestamp(data.get("started_at"), "started_at")
        _validate_timestamp(data.get("completed_at"), "completed_at")
    else:
        raise EndScreenError(f"invalid status: {status!r}")


def _validate_manifest_result(data: dict, path: Path) -> None:
    _validate_manifest_plan(data, path)
    _validate_manifest_status(data)
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
            raise EndScreenError("non-stopped outcomes require completed status")
        if result.get("setup_unchanged_confirmed") is not True:
            raise EndScreenError("setup_unchanged_confirmed must be true")
    recorded_at = _validate_timestamp(result.get("recorded_at"), "recorded_at")
    click_rate = result.get("click_rate")
    if outcome in ("insufficient_views", "stopped_changed_setup"):
        if click_rate is not None:
            raise EndScreenError(f"click_rate must be null for outcome {outcome!r}")
    else:
        if not isinstance(click_rate, (int, float)) or isinstance(click_rate, bool):
            raise EndScreenError("click_rate must be a finite number for this outcome")
        if not (0.0 <= float(click_rate) <= 100.0):
            raise EndScreenError("click_rate must be between 0 and 100")
        if outcome == "clicked" and float(click_rate) == 0.0:
            raise EndScreenError("clicked outcome requires a positive click_rate")
        if outcome == "not_clicked" and float(click_rate) > 0.0:
            raise EndScreenError("not_clicked outcome requires a zero click_rate")


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
    if expected_channel is not None and str(data.get("channel") or "") != expected_channel:
        raise EndScreenError(
            f"channel mismatch: expected {expected_channel}, got {data.get('channel')!r}"
        )
    _validate_manifest_plan(data, path)
    _validate_manifest_status(data)
    if data.get("status") in ("completed", "invalidated"):
        _validate_manifest_result(data, path)
    return data


def _all_manifests(spec: ChannelSpec) -> list[dict]:
    root = _root(spec)
    if not root.is_dir():
        return []
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


def _history_video(spec: ChannelSpec, video_id: str) -> dict:
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise EndScreenError(f"invalid YouTube video id: {video_id!r}")
    if not spec.history_file.exists():
        raise EndScreenError(f"doci history is missing: {spec.history_file}")
    found: dict | None = None
    for line in spec.history_file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("video_id") or "") == video_id:
            found = row
    if found is None:
        raise EndScreenError(
            f"video is not present in doci history: {video_id}"
        )
    if found.get("tier") != "longform":
        raise EndScreenError("end screens are not available for Shorts")
    if str(found.get("status") or "") != "published":
        raise EndScreenError("video is not recorded as published")
    if found.get("youtube_privacy") not in {"public", "unlisted"}:
        raise EndScreenError(
            "video privacy must be recorded as public or unlisted"
        )
    return found


def _safe_cell(value: object) -> str:
    return str(value or "").replace("|", "｜").replace("\n", " ")


def _plan_markdown(manifest: dict) -> str:
    setup = manifest["end_screen_setup"]
    warnings = manifest.get("warnings") or []
    warning_text = "\n".join(f"- {item}" for item in warnings) or "- なし"
    return f"""# 終了画面1枠の検証計画

- experiment_id: `{manifest['experiment_id']}`
- video_id: `{manifest['video_id']}`
- 要素: `video` 1枠のみ（登録・再生リストは並べない）
- リンク先: `{_safe_cell(setup.get('link_video_id'))}`
- 判定指標: YouTube Studioの終了画面要素クリック率
- 公式仕様: {OFFICIAL_HELP_URL}

## 実施手順

1. パソコン版YouTube Studioで対象の通常動画を開きます。
2. 終了画面を編集し、次の1本に内容が直結するvideo要素を1枠だけ設定します。
   登録ボタン・再生リスト・他の動画を同時に並べません。
3. 設定完了後に`start`を記録します。
4. テスト中は終了画面の構成を手動変更しません。変更した場合は
   `stopped_changed_setup`として無効化します。
5. Studioのアナリティクス → エンゲージメント → 終了画面要素のクリック率を
   `complete`で記録します。視聴回数が少なすぎて判定できない場合は
   `insufficient_views`にします。

## 品質上の注意

{warning_text}
"""


def _result_memo(manifest: dict) -> str:
    result = manifest["result"]
    notes = str(result.get("notes") or "").strip() or "記載なし"
    click_rate = result.get("click_rate")
    click_rate_text = (
        f"{float(click_rate):.2f}%" if click_rate is not None else "記録なし"
    )
    outcome_label = {
        "clicked": "クリックあり（1枠設定が次の一本へつながった）",
        "not_clicked": "クリックなし",
        "insufficient_views": "判定材料不足",
        "stopped_changed_setup": "構成変更により無効",
    }.get(str(result.get("outcome") or ""), str(result.get("outcome") or ""))
    return f"""# 次回企画メモ: 終了画面1枠の検証

- experiment_id: `{manifest['experiment_id']}`
- video_id: `{manifest['video_id']}`
- 要素: `video` 1枠
- リンク先: `{_safe_cell((manifest.get('end_screen_setup') or {}).get('link_video_id'))}`
- outcome: `{outcome_label}`
- 終了画面要素クリック率: `{click_rate_text}`
- 判定指標: YouTube Studioの終了画面要素クリック率
- 記録日時: `{result['recorded_at']}`

## 運用メモ

{notes}

この結果は対象動画の終了画面要素の比較です。別動画へそのまま一般化せず、次の企画では
「内容が直結する1枠」という仮説だけを扱います。`not_clicked`・`insufficient_views`は
勝者として扱いません。`stopped_changed_setup`はテスト無効です。
"""


def plan_experiment(
    spec: ChannelSpec,
    *,
    video_id: str,
    link_video_id: str,
    content_direct_confirmed: bool = False,
    now: datetime | None = None,
    experiment_id: str | None = None,
) -> dict:
    """次の1本へ直結する終了画面video要素を1枠だけ固定し、計画を保存する。"""
    if not content_direct_confirmed:
        raise EndScreenError(
            "confirm that the linked video directly continues the current video's content"
        )
    recorded = _history_video(spec, video_id)
    if video_id == link_video_id:
        raise EndScreenError("end screen link_video_id must differ from the video itself")
    if not _LINK_VIDEO_ID_RE.fullmatch(link_video_id):
        raise EndScreenError(f"invalid link video id: {link_video_id!r}")
    created_at = _now_iso(now)

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
        candidate_id = experiment_id or f"esc-{uuid.uuid4().hex[:16]}"
        target = _manifest_path(spec, candidate_id).parent
        if target.exists():
            raise EndScreenError(f"experiment already exists: {candidate_id}")

        root = _root(spec)
        staging = Path(tempfile.mkdtemp(prefix=".plan-", dir=root))
        warnings: list[str] = []
        try:
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "experiment_id": candidate_id,
                "channel": spec.id,
                "video_id": video_id,
                "status": "planned",
                "created_at": created_at,
                "official_help_url": OFFICIAL_HELP_URL,
                "decision_metric": "youtube_studio.end_screen_click_rate",
                "end_screen_setup": {
                    "element": "video",
                    "link_video_id": link_video_id,
                    "single_slot_only": True,
                    "subscription_button_prohibited": True,
                    "playlist_element_prohibited": True,
                    "content_direct_confirmed": True,
                },
                "source": {
                    "title": str(recorded.get("title") or ""),
                    "history_ts": str(recorded.get("ts") or ""),
                    "workdir": str(recorded.get("workdir") or ""),
                    "tier": "longform",
                    "youtube_privacy": recorded.get("youtube_privacy"),
                },
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
    studio_setup_confirmed: bool = False,
    now: datetime | None = None,
) -> dict:
    """Studioへ1枠だけ設定済みであることを確認し、runningへ進める。"""
    if not studio_setup_confirmed:
        raise EndScreenError(
            "confirm that the single end screen video element was set up in YouTube Studio"
        )
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
    click_rate: float | None = None,
    notes: str = "",
    setup_unchanged_confirmed: bool = False,
    now: datetime | None = None,
) -> dict:
    """Studioの終了画面要素クリック率を記録し、次回企画メモを書き出す。"""
    if outcome not in VALID_OUTCOMES:
        raise EndScreenError(f"invalid end screen outcome: {outcome!r}")
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
            raise EndScreenError(
                f"click_rate must not be recorded for outcome {outcome!r}"
            )
    else:
        if not isinstance(click_rate, (int, float)) or isinstance(click_rate, bool):
            raise EndScreenError("click_rate is required for this outcome")
        if not (0.0 <= float(click_rate) <= 100.0):
            raise EndScreenError("click_rate must be between 0 and 100")
        if outcome == "clicked" and float(click_rate) == 0.0:
            raise EndScreenError("clicked outcome requires a positive click_rate")
        if outcome == "not_clicked" and float(click_rate) > 0.0:
            raise EndScreenError("not_clicked outcome requires a zero click_rate")
    with _operation_lock(spec):
        path = _manifest_path(spec, experiment_id)
        manifest = _load_manifest(path, expected_channel=spec.id)
        if manifest.get("status") != "running":
            raise EndScreenError("only a running end screen test can be completed")
        recorded_at = _now_iso(now)
        status = "invalidated" if outcome == "stopped_changed_setup" else "completed"
        manifest = {
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
        # 保存前にvalidatorへ通し、実装と検証ロジックの乖離を防ぐ。
        _validate_manifest_result(manifest, path)
        _write_text_atomic(path.parent / "next_idea_memo.md", _result_memo(manifest))
        _write_manifest(path, manifest)
    return manifest


def show_experiment(spec: ChannelSpec, experiment_id: str) -> dict:
    _validate_root_readable(_root(spec))
    return _load_manifest(
        _manifest_path(spec, experiment_id),
        expected_channel=spec.id,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YouTube終了画面1枠の計画・結果をローカル管理（YouTube書込みなし）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--channel", required=True)
    plan.add_argument("--video-id", required=True)
    plan.add_argument("--link-video-id", required=True)
    plan.add_argument("--confirm-content-direct", action="store_true")

    start = subparsers.add_parser("start")
    start.add_argument("--channel", required=True)
    start.add_argument("--experiment-id", required=True)
    start.add_argument("--confirm-studio-setup", action="store_true")

    complete = subparsers.add_parser("complete")
    complete.add_argument("--channel", required=True)
    complete.add_argument("--experiment-id", required=True)
    complete.add_argument("--outcome", choices=sorted(VALID_OUTCOMES), required=True)
    complete.add_argument("--click-rate", type=float)
    complete.add_argument("--notes", default="")
    complete.add_argument("--confirm-setup-unchanged", action="store_true")

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
            manifest = plan_experiment(
                spec,
                video_id=args.video_id,
                link_video_id=args.link_video_id,
                content_direct_confirmed=args.confirm_content_direct,
            )
        elif args.command == "start":
            manifest = start_experiment(
                spec,
                args.experiment_id,
                studio_setup_confirmed=args.confirm_studio_setup,
            )
        elif args.command == "complete":
            manifest = complete_experiment(
                spec,
                args.experiment_id,
                outcome=args.outcome,
                click_rate=args.click_rate,
                notes=args.notes,
                setup_unchanged_confirmed=args.confirm_setup_unchanged,
            )
        elif args.command == "show":
            manifest = show_experiment(spec, args.experiment_id)
        else:
            raise EndScreenError(f"unknown command: {args.command}")
    except EndScreenError as exc:
        print(f"[doci] 終了画面: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
