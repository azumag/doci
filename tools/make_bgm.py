"""パブリックドメインの「インターナショナル」MIDI を、stdlib のみで piano 風に
レンダリングして BGM 音源を作る。

- 旋律: Wikimedia Commons の PD MIDI（PD-Internationale / PD melody）
- 演奏（レンダリング）: このスクリプトが生成 → 第三者の録音権は発生しない
したがって出力は権利的にクリーンな BGM として配信に使える。

使い方:
    python tools/make_bgm.py --midi internationale.mid --out channels/ideology/bgm/internationale_piano.mp3
"""
from __future__ import annotations

import argparse
import math
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

SR = 44100


# ---------------- MIDI パーサ（最小実装） ----------------
def _read_vlq(data: bytes, i: int) -> tuple[int, int]:
    val = 0
    while True:
        b = data[i]
        i += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            return val, i


def parse_midi(path: Path):
    data = Path(path).read_bytes()
    if data[:4] != b"MThd":
        raise SystemExit(f"MIDIファイルではありません（先頭={data[:8]!r}）。ダウンロード失敗の可能性。")
    fmt, ntrks, division = struct.unpack(">HHH", data[8:14])
    if division & 0x8000:
        raise SystemExit("SMPTE タイムは未対応です")
    tpq = division  # ticks per quarter

    notes = []  # (start_sec, dur_sec, midi_note, velocity)
    pos = 14
    for _ in range(ntrks):
        assert data[pos:pos + 4] == b"MTrk", "MTrk が見つかりません"
        length = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        i = pos + 8
        end = i + length
        pos = end

        tick = 0
        tempo = 500000  # us per quarter (120 bpm)
        cur_sec = 0.0
        last_tick = 0
        active: dict[tuple[int, int], tuple[float, int]] = {}
        status = 0
        while i < end:
            delta, i = _read_vlq(data, i)
            tick += delta
            cur_sec += (tick - last_tick) * (tempo / 1_000_000.0) / tpq
            last_tick = tick

            b = data[i]
            if b & 0x80:
                status = b
                i += 1
            # running status: status unchanged, b はデータ
            ev = status & 0xF0
            ch = status & 0x0F

            if status == 0xFF:  # meta
                meta_type = data[i]
                i += 1
                mlen, i = _read_vlq(data, i)
                if meta_type == 0x51 and mlen == 3:
                    tempo = struct.unpack(">I", b"\x00" + data[i:i + 3])[0]
                i += mlen
            elif ev in (0x80, 0x90, 0xA0, 0xB0, 0xE0):  # 2 data bytes
                d1 = data[i]; d2 = data[i + 1]; i += 2
                if ev == 0x90 and d2 > 0:
                    active[(ch, d1)] = (cur_sec, d2)
                elif ev == 0x80 or (ev == 0x90 and d2 == 0):
                    st = active.pop((ch, d1), None)
                    if st:
                        s_sec, vel = st
                        notes.append((s_sec, max(0.05, cur_sec - s_sec), d1, vel))
            elif ev in (0xC0, 0xD0):  # 1 data byte
                i += 1
            elif status == 0xF0 or status == 0xF7:  # sysex
                slen, i = _read_vlq(data, i)
                i += slen
            else:
                i += 1
    return notes


# ---------------- 簡易ピアノ風シンセ ----------------
def synth(notes, gain: float = 0.5):
    if not notes:
        raise SystemExit("ノートが空です")
    total = max(s + d for s, d, _, _ in notes) + 1.5
    n = int(total * SR)
    buf = [0.0] * n
    partials = [(1, 1.0), (2, 0.5), (3, 0.25), (4, 0.12), (5, 0.06)]
    for start, dur, note, vel in notes:
        freq = 440.0 * (2.0 ** ((note - 69) / 12.0))
        amp = (vel / 127.0) * gain
        i0 = int(start * SR)
        ln = int(min(dur + 0.5, 6.0) * SR)
        for k in range(ln):
            t = k / SR
            env = math.exp(-t * 2.6)  # 減衰
            if t < 0.005:             # アタック
                env *= t / 0.005
            s = 0.0
            for mult, pa in partials:
                s += pa * math.sin(2 * math.pi * freq * mult * t)
            idx = i0 + k
            if 0 <= idx < n:
                buf[idx] += amp * env * s
    # 正規化
    peak = max(1e-6, max(abs(x) for x in buf))
    scale = 0.89 / peak
    # 全体フェード
    fade = int(0.8 * SR)
    out = bytearray()
    for i, x in enumerate(buf):
        v = x * scale
        if i < fade:
            v *= i / fade
        if i > n - fade:
            v *= max(0.0, (n - i) / fade)
        out += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    return bytes(out), n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--midi", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gain", type=float, default=0.5)
    args = ap.parse_args()

    notes = parse_midi(Path(args.midi))
    pcm, n = synth(notes, gain=args.gain)
    print(f"notes={len(notes)} duration={n / SR:.1f}s")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav_path = tf.name
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)

    if out.suffix.lower() == ".wav":
        Path(wav_path).replace(out)
    else:
        # mp3 等へ変換（libmp3lame）
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libmp3lame", "-b:a", "160k", str(out)],
            check=True, capture_output=True, text=True,
        )
        Path(wav_path).unlink(missing_ok=True)
    print(f"BGM -> {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
