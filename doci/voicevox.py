"""VOICEVOX 音声合成（soren の voicevox_tts.sh を Python 移植）。

ナレーションを文単位に分割し、文ごとに audio_query→synthesis して WAV を結合。
副産物として文ごとの再生長（字幕タイミング用）を返す。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class Segment:
    text: str
    start: float
    end: float


@dataclass
class TtsResult:
    wav_path: Path
    duration: float
    segments: list[Segment] = field(default_factory=list)


def _healthy(base: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def active_base() -> str:
    for base in (config.VOICEVOX_URL, config.VOICEVOX_URL_FALLBACK):
        if base and _healthy(base):
            return base
    raise RuntimeError(
        f"VOICEVOX に到達できません: {config.VOICEVOX_URL} / {config.VOICEVOX_URL_FALLBACK}"
    )


def split_sentences(text: str) -> list[str]:
    """。！？で文分割（句点は残す）。長すぎる文は読点でも分割。"""
    text = text.replace("\n", " ").strip()
    parts = re.split(r"(?<=[。！？])", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > 60:
            sub = re.split(r"(?<=、)", p)
            out.extend(s.strip() for s in sub if s.strip())
        else:
            out.append(p)
    return out


def _audio_query(base: str, text: str, speaker: int) -> dict:
    url = f"{base}/audio_query?speaker={speaker}&text={urllib.parse.quote(text)}"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _synthesis(base: str, query: dict, speaker: int) -> bytes:
    url = f"{base}/synthesis?speaker={speaker}"
    req = urllib.request.Request(
        url,
        data=json.dumps(query).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "audio/wav"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def synthesize(text: str, speaker: int, out_path: Path) -> TtsResult:
    base = active_base()
    sentences = split_sentences(text)
    if not sentences:
        raise ValueError("合成するテキストが空です")

    frames_list: list[bytes] = []
    params = None  # (framerate, sampwidth, nchannels)
    segments: list[Segment] = []
    cursor = 0.0

    for s in sentences:
        q = _audio_query(base, s, speaker)
        wav = _synthesis(base, q, speaker)
        with wave.open(io.BytesIO(wav), "rb") as w:
            fr, sw, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
            n = w.getnframes()
            frames = w.readframes(n)
        if params is None:
            params = (fr, sw, ch)
        dur = n / float(fr)
        segments.append(Segment(text=s, start=cursor, end=cursor + dur))
        cursor += dur
        frames_list.append(frames)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fr, sw, ch = params
    with wave.open(str(out_path), "wb") as out:
        out.setnchannels(ch)
        out.setsampwidth(sw)
        out.setframerate(fr)
        for f in frames_list:
            out.writeframes(f)

    return TtsResult(wav_path=out_path, duration=cursor, segments=segments)


def main() -> None:
    ap = argparse.ArgumentParser(description="VOICEVOX 合成")
    ap.add_argument("--text", required=True)
    ap.add_argument("--speaker", type=int, default=config.VOICE_CHINESE_AI)
    ap.add_argument("--out", default=str(config.OUTPUT / "tts_test.wav"))
    args = ap.parse_args()
    res = synthesize(args.text, args.speaker, Path(args.out))
    print(f"wav={res.wav_path} duration={res.duration:.2f}s segments={len(res.segments)}")
    for seg in res.segments:
        print(f"  [{seg.start:5.2f}-{seg.end:5.2f}] {seg.text}")


if __name__ == "__main__":
    main()
