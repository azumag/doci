"""声の設定（話者＋速度/ピッチ/抑揚/音量）を voices.json から読む（issue #1）。

これまで voicevox.py は audio_query をそのまま合成しており、speed/pitch/intonation が
一切効いていなかった。ここで voices.json を唯一の真実として読み、コーナーの話者と
パラメータを供給する。json が無い/壊れている場合は config の既定話者にフォールバック。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config


@dataclass(frozen=True)
class VoiceCfg:
    speaker: int
    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    intonation_vary: bool = False  # 文ごとに抑揚を微変動させるか
    volume: float = 1.0
    label: str = ""


_FALLBACK = {
    "chinese_ai": VoiceCfg(config.VOICE_CHINESE_AI),
    "american_ai": VoiceCfg(config.VOICE_AMERICAN_AI),
}

_ENV_SPEAKER = {
    "chinese_ai": "VOICE_CHINESE_AI",
    "american_ai": "VOICE_AMERICAN_AI",
}


def _speaker(
    key: str,
    data: dict[str, Any],
    fallback: VoiceCfg | None,
    *,
    env_overrides: bool,
) -> int:
    env_key = _ENV_SPEAKER.get(key)
    if env_overrides and env_key and env_key in os.environ:
        default = fallback.speaker if fallback is not None else int(
            data.get("voicevox_speaker", 0)
        )
        return config.get_int(env_key, default)
    if "voicevox_speaker" in data:
        return int(data["voicevox_speaker"])
    if fallback is not None:
        return fallback.speaker
    raise ValueError(f"voices.{key}.voicevox_speaker is required")


def load(
    path: Path,
    *,
    env_overrides: bool = False,
    fallbacks: dict[str, VoiceCfg] | None = None,
) -> dict[str, VoiceCfg]:
    """voices.json をパス指定で読み込む。

    チャンネル固有設定では ``env_overrides=False`` が既定で、ファイルの値を
    そのまま真実源にする。従来のグローバル設定だけが既存 env 上書きを使う。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"failed to read voices file: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid voices JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"voices file must contain a JSON object: {path}")

    defaults = fallbacks or {}
    out: dict[str, VoiceCfg] = {}
    for key in dict.fromkeys([*defaults, *data]):
        raw = data.get(key, {})
        if not isinstance(raw, dict):
            raise ValueError(f"voices.{key} must be a JSON object")
        fallback = defaults.get(key)
        try:
            out[key] = VoiceCfg(
                speaker=_speaker(
                    key,
                    raw,
                    fallback,
                    env_overrides=env_overrides,
                ),
                speed=float(raw.get("speed", fallback.speed if fallback else 1.0)),
                pitch=float(raw.get("pitch", fallback.pitch if fallback else 0.0)),
                intonation=float(
                    raw.get("intonation", fallback.intonation if fallback else 1.0)
                ),
                intonation_vary=bool(
                    raw.get(
                        "intonation_vary",
                        fallback.intonation_vary if fallback else False,
                    )
                ),
                volume=float(raw.get("volume", fallback.volume if fallback else 1.0)),
                label=str(raw.get("label", fallback.label if fallback else "")),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid voice config for {key}: {exc}") from exc
    return out


def _load() -> dict[str, VoiceCfg]:
    """後方互換用のグローバル voice 設定をロードする。"""
    path = config.ROOT / "channels" / "ideology" / "voices.json"
    if not path.exists():
        return dict(_FALLBACK)
    try:
        return load(path, env_overrides=True, fallbacks=_FALLBACK)
    except ValueError:
        return dict(_FALLBACK)


VOICES = _load()


def get(voice_key: str) -> VoiceCfg:
    return VOICES.get(voice_key) or _FALLBACK[voice_key]
