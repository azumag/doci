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


def _sentence_intonation(sentence: str, base: float, vary: bool) -> float:
    """文末・長さから、その文の抑揚倍率を内容連動で微調整（issue #1）。

    vary=False なら base のまま。疑問・感嘆は少し豊かに、長い説明文は少し
    落ち着かせる。揺れは控えめ（±約12%）でクランプする。
    """
    if not vary:
        return base
    s = sentence.rstrip()
    if s.endswith(("？", "?", "！", "!")):
        f = 1.12
    elif len(s) >= 40:  # 長い説明文は抑揚を抑えて落ち着かせる
        f = 0.92
    else:
        f = 1.0
    return max(0.9, min(1.4, round(base * f, 3)))


def _apply_params(
    q: dict, speed: float, pitch: float, intonation: float, volume: float
) -> dict:
    """audio_query に話速/ピッチ/抑揚/音量を反映（issue #1）。"""
    q["speedScale"] = speed
    q["pitchScale"] = pitch
    q["intonationScale"] = intonation
    q["volumeScale"] = volume
    # 文・句ごとに合成して連結するため、前後パディングが継ぎ目ごとに無音を生む。
    # 語頭パディングを除き文末を控えめにして、発話を連続的に（シーンのカットと無音の
    # 重なりで「音声が途切れた」と感じる問題への対策）。
    q["prePhonemeLength"] = config.VOICE_PRE_PHONEME
    q["postPhonemeLength"] = config.VOICE_POST_PHONEME
    return q


def synthesize(
    text: str,
    speaker: int,
    out_path: Path,
    *,
    speed: float = 1.0,
    pitch: float = 0.0,
    intonation: float = 1.0,
    intonation_vary: bool = False,
    volume: float = 1.0,
) -> TtsResult:
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
        into = _sentence_intonation(s, intonation, intonation_vary)
        _apply_params(q, speed, pitch, into, volume)
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
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--pitch", type=float, default=0.0)
    ap.add_argument("--intonation", type=float, default=1.0)
    ap.add_argument("--intonation-vary", action="store_true")
    ap.add_argument("--volume", type=float, default=1.0)
    args = ap.parse_args()
    res = synthesize(
        args.text, args.speaker, Path(args.out),
        speed=args.speed, pitch=args.pitch, intonation=args.intonation,
        intonation_vary=args.intonation_vary, volume=args.volume,
    )
    print(f"wav={res.wav_path} duration={res.duration:.2f}s segments={len(res.segments)}")
    for seg in res.segments:
        print(f"  [{seg.start:5.2f}-{seg.end:5.2f}] {seg.text}")


if __name__ == "__main__":
    main()
