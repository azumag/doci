"""チャンネル定義（channels/<id>/channel.toml）のロードと検証。"""
from __future__ import annotations

import os
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, voices


class ChannelConfigError(ValueError):
    """channel.toml の内容が不正なときのエラー。"""


@dataclass(frozen=True)
class CornerSpec:
    key: str
    label: str
    persona_path: Path
    corner_path: Path
    voice_key: str


@dataclass(frozen=True)
class StyleSpec:
    """#18 で拡張する style 設定の暫定コンテナ。"""

    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishSpec:
    """#19 で拡張する publish 設定の暫定コンテナ。"""

    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelSpec:
    id: str
    name: str
    root: Path
    corners: dict[str, CornerSpec]
    rotation: list[str]
    voices_path: Path
    style: StyleSpec = field(default_factory=StyleSpec)
    publish: PublishSpec = field(default_factory=PublishSpec)
    pipeline: dict[str, Any] = field(default_factory=dict)

    @property
    def output_dir(self) -> Path:
        return config.OUTPUT / self.id

    @property
    def history_file(self) -> Path:
        return self.output_dir / "history.jsonl"

    def pipeline_get(self, key: str, default: Any = None) -> Any:
        return self.pipeline.get(key, default)


_TOP_LEVEL_KEYS = {"channel", "corners", "voices", "style", "publish", "pipeline"}
_CHANNEL_KEYS = {"id", "name", "rotation"}
_CORNER_KEYS = {"label", "persona", "corner", "voice"}
_PIPELINE_KEYS = {
    "seconds_per_image",
    "max_images",
    "research",
    "factcheck",
    "plan",
    "asset_media",
}


def _warn_unknown(data: dict[str, Any], allowed: set[str], location: str) -> None:
    for key in sorted(set(data) - allowed):
        warnings.warn(
            f"unknown channel setting: {location}{key}",
            UserWarning,
            stacklevel=3,
        )


def _required_str(data: dict[str, Any], key: str, location: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ChannelConfigError(f"missing or invalid required key: {location}{key}")
    return value


def _resolve_path(channel_root: Path, value: str, key: str) -> Path:
    """チャンネル相対を優先し、移行期間はリポジトリルート相対も許容。"""
    raw = Path(value)
    if raw.is_absolute():
        candidate = raw
    else:
        candidate = channel_root / raw
        if not candidate.exists():
            candidate = config.ROOT / raw
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ChannelConfigError(f"referenced file does not exist: {key}={value}")
    return candidate


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChannelConfigError(f"channel config not found: {path}") from exc
    except OSError as exc:
        raise ChannelConfigError(f"failed to read channel config: {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ChannelConfigError(f"invalid TOML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ChannelConfigError(f"channel config must be a TOML table: {path}")
    return data


def load(channel_id: str, *, channels_dir: Path | None = None) -> ChannelSpec:
    """``channels/<id>/channel.toml`` をロードし、参照を検証する。"""
    if not channel_id or Path(channel_id).name != channel_id:
        raise ChannelConfigError(f"invalid channel id: {channel_id!r}")
    base = (channels_dir or (config.ROOT / "channels")).resolve()
    root = (base / channel_id).resolve()
    path = root / "channel.toml"
    data = _read_toml(path)
    _warn_unknown(data, _TOP_LEVEL_KEYS, "")

    channel_data = data.get("channel")
    if not isinstance(channel_data, dict):
        raise ChannelConfigError("missing or invalid required table: channel")
    _warn_unknown(channel_data, _CHANNEL_KEYS, "channel.")
    spec_id = _required_str(channel_data, "id", "channel.")
    if spec_id != channel_id or root.name != channel_id:
        raise ChannelConfigError(
            f"channel.id must match directory name: {spec_id!r} != {channel_id!r}"
        )
    name = _required_str(channel_data, "name", "channel.")

    corners_data = data.get("corners")
    if not isinstance(corners_data, dict) or not corners_data:
        raise ChannelConfigError("missing or invalid required table: corners")

    voices_value = data.get("voices", "voices.json")
    if not isinstance(voices_value, str) or not voices_value.strip():
        raise ChannelConfigError("voices must be a non-empty path string")
    voices_path = _resolve_path(root, voices_value, "voices")
    try:
        channel_voices = voices.load(voices_path)
    except ValueError as exc:
        raise ChannelConfigError(str(exc)) from exc

    corners: dict[str, CornerSpec] = {}
    for key, raw in corners_data.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise ChannelConfigError(f"corners.{key} must be a table")
        _warn_unknown(raw, _CORNER_KEYS, f"corners.{key}.")
        voice_key = _required_str(raw, "voice", f"corners.{key}.")
        if voice_key not in channel_voices:
            raise ChannelConfigError(
                f"corners.{key}.voice references missing voices key: {voice_key}"
            )
        corners[key] = CornerSpec(
            key=key,
            label=_required_str(raw, "label", f"corners.{key}."),
            persona_path=_resolve_path(
                root,
                _required_str(raw, "persona", f"corners.{key}."),
                f"corners.{key}.persona",
            ),
            corner_path=_resolve_path(
                root,
                _required_str(raw, "corner", f"corners.{key}."),
                f"corners.{key}.corner",
            ),
            voice_key=voice_key,
        )

    rotation = channel_data.get("rotation", list(corners))
    if not isinstance(rotation, list) or not all(isinstance(item, str) for item in rotation):
        raise ChannelConfigError("channel.rotation must be a list of corner keys")
    if not rotation:
        raise ChannelConfigError("channel.rotation must not be empty")
    missing = [item for item in rotation if item not in corners]
    if missing:
        raise ChannelConfigError(
            f"channel.rotation references missing corners: {', '.join(missing)}"
        )

    pipeline = data.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise ChannelConfigError("pipeline must be a table")
    _warn_unknown(pipeline, _PIPELINE_KEYS, "pipeline.")
    style = data.get("style", {})
    publish = data.get("publish", {})
    if not isinstance(style, dict):
        raise ChannelConfigError("style must be a table")
    if not isinstance(publish, dict):
        raise ChannelConfigError("publish must be a table")

    return ChannelSpec(
        id=spec_id,
        name=name,
        root=root,
        corners=corners,
        rotation=list(rotation),
        voices_path=voices_path,
        style=StyleSpec(dict(style)),
        publish=PublishSpec(dict(publish)),
        pipeline=dict(pipeline),
    )


def discover(*, channels_dir: Path | None = None) -> list[str]:
    """channel.toml を持つチャンネル ID を安定順で列挙する。"""
    base = channels_dir or (config.ROOT / "channels")
    if not base.is_dir():
        return []
    return sorted(
        child.name
        for child in base.iterdir()
        if child.is_dir() and (child / "channel.toml").is_file()
    )


def default_channel(*, channels_dir: Path | None = None) -> str:
    """環境変数または発見結果から既定チャンネルを決める。"""
    configured = os.environ.get("DOCI_CHANNEL", "").strip()
    if configured:
        return configured
    available = discover(channels_dir=channels_dir)
    if len(available) == 1:
        return available[0]
    if not available:
        raise ChannelConfigError("no channels found; set DOCI_CHANNEL after adding one")
    raise ChannelConfigError(
        "multiple channels found; set DOCI_CHANNEL or pass --channel: "
        + ", ".join(available)
    )
