"""VOICEVOX 音声合成（soren の voicevox_tts.sh を Python 移植）。

ナレーションを文単位に分割し、文ごとに audio_query→synthesis して WAV を結合。
副産物として文ごとの再生長（字幕タイミング用）を返す。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import time
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

from . import config, voices


@dataclass
class Segment:
    text: str
    start: float
    end: float
    # narrationとは別の画面表示用表記。文区切りが対応しない場合はNoneにして
    # compose側で音声入力文へ安全にフォールバックする。
    subtitle_text: str | None = None


@dataclass
class TtsResult:
    wav_path: Path
    duration: float
    segments: list[Segment] = field(default_factory=list)
    subtitle_aligned: bool = False


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


def _terminal_sentences(text: str) -> list[str]:
    """。！？で文分割する（句点は残す）。長さによる分割は行わない。"""
    text = text.replace("\n", " ").strip()
    parts = re.split(r"(?<=[。！？])", text)
    return [p.strip() for p in parts if p.strip()]


def split_sentences(text: str) -> list[str]:
    """。！？で文分割（句点は残す）。長すぎる文は読点でも分割。"""
    out: list[str] = []
    for sentence in _terminal_sentences(text):
        if len(sentence) > 60:
            out.extend(
                part.strip()
                for part in re.split(r"(?<=、)", sentence)
                if part.strip()
            )
        else:
            out.append(sentence)
    return out


def align_subtitle_sentences(
    narration_sentences: list[str], subtitle_text: str | None
) -> list[str | None]:
    """字幕本文を音声合成の文区切りへ対応づける。

    字幕本文は表記だけが違い、句読点と文の構成は narration と同じであることを
    生成プロンプトで要求する。モデルがその契約を破った場合は全区間をNoneにし、
    呼び出し側が音声用本文を使って同期を壊さず続行できるようにする。
    """
    if not subtitle_text or not subtitle_text.strip():
        return [None] * len(narration_sentences)
    narration_groups: list[list[str]] = []
    current_group: list[str] = []
    for sentence in narration_sentences:
        current_group.append(sentence)
        if sentence.endswith(("。", "！", "？")):
            narration_groups.append(current_group)
            current_group = []
    if current_group:
        narration_groups.append(current_group)

    subtitle_sentences = _terminal_sentences(subtitle_text)
    if len(subtitle_sentences) != len(narration_groups):
        return [None] * len(narration_sentences)

    aligned: list[str] = []
    for narration_group, subtitle_sentence in zip(
        narration_groups, subtitle_sentences
    ):
        # split_sentences は音声側だけ、60文字超の文を読点で分ける。
        # 表示側の文字数は原綴り化で変わるため、同じ閾値を独立に適用すると
        # 正常な二本文でも区間数がずれる。音声側が作った区間数を正として、
        # 表示側は対応する読点の数だけ分割する。
        if len(narration_group) > 1:
            subtitle_parts = [
                part.strip()
                for part in re.split(r"(?<=、)", subtitle_sentence)
                if part.strip()
            ]
            if len(subtitle_parts) != len(narration_group):
                return [None] * len(narration_sentences)
            aligned.extend(subtitle_parts)
        else:
            aligned.append(subtitle_sentence)
    if len(aligned) != len(narration_sentences):
        return [None] * len(narration_sentences)
    return aligned


_T = TypeVar("_T")


def _request_with_retry(fn: Callable[[], _T], retries: int = 3) -> _T:
    """OSError系（接続断・タイムアウト等）を短い間隔でリトライして実行する。

    VOICEVOXコンテナ（OrbStack）は稀に自己再起動し、その数百ms〜数秒の間だけ
    一時的に不在になることがある。文単位で何度も呼ばれる音声合成HTTPリクエスト
    がその瞬間に当たると RemoteDisconnected 等で失敗するため、短い待機を挟んで
    最大 retries 回まで再試行する。最終試行の例外は呼び出し元にそのまま伝える。
    """
    for attempt in range(retries):
        try:
            return fn()
        except OSError:
            if attempt >= retries - 1:
                raise
            time.sleep(attempt + 1)
    raise AssertionError("unreachable")  # pragma: no cover


def _audio_query(base: str, text: str, speaker: int) -> dict:
    url = f"{base}/audio_query?speaker={speaker}&text={urllib.parse.quote(text)}"

    def _do() -> dict:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    return _request_with_retry(_do)


def _synthesis(base: str, query: dict, speaker: int) -> bytes:
    url = f"{base}/synthesis?speaker={speaker}"
    data = json.dumps(query).encode("utf-8")

    def _do() -> bytes:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "audio/wav"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    return _request_with_retry(_do)


def _sentence_intonation(sentence: str, base: float, vary: bool) -> float:
    """文末・長さから、その文の抑揚倍率を内容連動で微調整（issue #1）。

    vary=False なら base のまま。疑問・感嘆は少し豊かに、長い説明文は少し
    落ち着かせる。揺れは控えめ（±約12%）とし、base からの相対範囲でクランプする
    （base が0.9未満/1.4超でも揺れが潰れないよう、絶対値ではなく base 比で安全弁をかける）。
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
    return max(base * 0.85, min(base * 1.15, round(base * f, 3)))


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
    subtitle_text: str | None = None,
) -> TtsResult:
    base = active_base()
    sentences = split_sentences(text)
    if not sentences:
        raise ValueError("合成するテキストが空です")
    subtitle_sentences = align_subtitle_sentences(sentences, subtitle_text)
    subtitle_aligned = bool(
        subtitle_text
        and subtitle_sentences
        and all(sentence is not None for sentence in subtitle_sentences)
    )

    frames_list: list[bytes] = []
    params = None  # (framerate, sampwidth, nchannels)
    segments: list[Segment] = []
    cursor = 0.0

    for index, s in enumerate(sentences):
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
        segments.append(
            Segment(
                text=s,
                start=cursor,
                end=cursor + dur,
                subtitle_text=subtitle_sentences[index],
            )
        )
        cursor += dur
        frames_list.append(frames)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fr, sw, ch = params
    # 最終文の語尾直後に音声と BGM が同時に止まる「末尾途切れ」感を防ぐため、
    # ナレーション末尾に余韻の無音を足す（継ぎ目の post=0.1 とは別に、最後だけ確保）。
    tail_frames = int(config.VOICE_TAIL_SILENCE * fr)
    tail = b"\x00" * (tail_frames * sw * ch)
    with wave.open(str(out_path), "wb") as out:
        out.setnchannels(ch)
        out.setsampwidth(sw)
        out.setframerate(fr)
        for f in frames_list:
            out.writeframes(f)
        if tail:
            out.writeframes(tail)

    return TtsResult(
        wav_path=out_path,
        duration=cursor + tail_frames / float(fr),
        segments=segments,
        subtitle_aligned=subtitle_aligned,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="VOICEVOX 合成")
    ap.add_argument("--text", required=True)
    ap.add_argument("--voice-key", choices=sorted(voices.VOICES), default="chinese_ai")
    ap.add_argument("--speaker", type=int)
    ap.add_argument("--out", default=str(config.OUTPUT / "tts_test.wav"))
    ap.add_argument("--speed", type=float)
    ap.add_argument("--pitch", type=float)
    ap.add_argument("--intonation", type=float)
    ap.add_argument("--intonation-vary", action="store_true")
    ap.add_argument("--volume", type=float)
    args = ap.parse_args()
    v = voices.get(args.voice_key)
    speaker = args.speaker if args.speaker is not None else v.speaker
    speed = args.speed if args.speed is not None else v.speed
    pitch = args.pitch if args.pitch is not None else v.pitch
    intonation = args.intonation if args.intonation is not None else v.intonation
    intonation_vary = args.intonation_vary or v.intonation_vary
    volume = args.volume if args.volume is not None else v.volume
    res = synthesize(
        args.text, speaker, Path(args.out),
        speed=speed, pitch=pitch, intonation=intonation,
        intonation_vary=intonation_vary, volume=volume,
    )
    print(
        f"wav={res.wav_path} duration={res.duration:.2f}s segments={len(res.segments)} "
        f"voice={args.voice_key} speaker={speaker} speed={speed} pitch={pitch} "
        f"intonation={intonation} volume={volume}"
    )
    for seg in res.segments:
        print(f"  [{seg.start:5.2f}-{seg.end:5.2f}] {seg.text}")


if __name__ == "__main__":
    main()
