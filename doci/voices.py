"""声の設定（話者＋速度/ピッチ/抑揚/音量）を config/voices.json から読む（issue #1）。

これまで voicevox.py は audio_query をそのまま合成しており、speed/pitch/intonation が
一切効いていなかった。ここで voices.json を唯一の真実として読み、コーナーの話者と
パラメータを供給する。json が無い/壊れている場合は config の既定話者にフォールバック。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

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


def _load() -> dict[str, VoiceCfg]:
    path = config.CONFIG_DIR / "voices.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    out: dict[str, VoiceCfg] = {}
    for key, fb in _FALLBACK.items():
        d = data.get(key, {}) if isinstance(data, dict) else {}
        out[key] = VoiceCfg(
            speaker=int(d.get("voicevox_speaker", fb.speaker)),
            speed=float(d.get("speed", 1.0)),
            pitch=float(d.get("pitch", 0.0)),
            intonation=float(d.get("intonation", 1.0)),
            intonation_vary=bool(d.get("intonation_vary", False)),
            volume=float(d.get("volume", 1.0)),
            label=str(d.get("label", "")),
        )
    return out


VOICES = _load()


def get(voice_key: str) -> VoiceCfg:
    return VOICES.get(voice_key) or _FALLBACK[voice_key]
