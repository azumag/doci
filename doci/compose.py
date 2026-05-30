"""ffmpeg 合成: 静止画→Ken Burns / 動画クリップ正規化 → 連結 → BGMミックス＋字幕焼込み。

縦9:16。字幕は Pillow で透過PNGに描画し、core の `overlay` フィルタでシーン尺に同期して焼き込む
（この環境の ffmpeg は drawtext/libass 非対応のため）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config

JP_FONT_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]


@dataclass
class Scene:
    path: Path
    is_video: bool
    caption: str = ""


def _font_path() -> str | None:
    for f in JP_FONT_CANDIDATES:
        if Path(f).exists():
            return f
    return None


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed:\n" + " ".join(cmd[:8]) + " ...\n" + proc.stderr[-1500:]
        )


def _scene_durations(total: float, n: int, min_each: float = 1.5) -> list[float]:
    total = max(total, n * min_each)
    base = total / n
    durs = [round(base, 3)] * n
    durs[-1] = round(total - base * (n - 1), 3)
    return durs


def _wrap(text: str, width: int = 14, max_lines: int = 3) -> str:
    text = (text or "").strip().replace("\n", "")
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= width:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return "\n".join(lines[:max_lines])


def _render_caption_png(text: str, out_png: Path) -> bool:
    """字幕を透過PNGに描画。成功時 True。フォント/Pillow 不在なら False。"""
    font_path = _font_path()
    if not font_path:
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False

    W, H = config.VIDEO_WIDTH, config.VIDEO_HEIGHT
    size = int(W * 0.060)
    try:
        font = ImageFont.truetype(font_path, size, index=0)
    except Exception:
        return False

    lines = _wrap(text).split("\n")
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    line_h = int(size * 1.35)
    text_h = line_h * len(lines)
    text_w = max((draw.textlength(ln, font=font) for ln in lines), default=0)
    pad_x, pad_y = int(size * 0.7), int(size * 0.45)
    box_w, box_h = int(text_w + pad_x * 2), int(text_h + pad_y * 2)
    x0 = (W - box_w) // 2
    y0 = int(H * 0.70)
    draw.rounded_rectangle(
        [x0, y0, x0 + box_w, y0 + box_h], radius=int(size * 0.35), fill=(0, 0, 0, 150)
    )
    stroke = max(2, size // 12)
    cy = y0 + pad_y
    for ln in lines:
        lw = draw.textlength(ln, font=font)
        draw.text(
            ((W - lw) // 2, cy), ln, font=font, fill=(255, 255, 255, 255),
            stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
        )
        cy += line_h
    img.save(out_png)
    return True


def _build_scene_clip(scene: Scene, dur: float, idx: int, tmp: Path) -> Path:
    W, H, fps = config.VIDEO_WIDTH, config.VIDEO_HEIGHT, config.VIDEO_FPS
    out = tmp / f"scene_{idx:02d}.mp4"
    frames = max(1, round(dur * fps))
    tail = ["-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)]
    if scene.is_video:
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},fps={fps},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(scene.path),
               "-t", f"{dur}", "-vf", vf, *tail]
    else:
        vf = (f"scale={W * 2}:-1,"
              f"zoompan=z='min(zoom+0.0010,1.18)':d={frames}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps},"
              f"trim=duration={dur},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{dur}", "-i", str(scene.path),
               "-vf", vf, *tail]
    _run(cmd)
    return out


def _concat(clips: list[Path], tmp: Path) -> Path:
    listf = tmp / "concat.txt"
    listf.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    out = tmp / "silent.mp4"
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy", str(out)])
    return out


def compose(
    scenes: list[Scene],
    narration_wav: Path,
    narration_dur: float,
    out_path: Path,
    bgm: Path | None = None,
) -> Path:
    if not scenes:
        raise ValueError("scenes が空です")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fps = config.VIDEO_FPS
    total = narration_dur + 0.4
    durs = _scene_durations(total, len(scenes))

    with tempfile.TemporaryDirectory(prefix="doci_compose_") as td:
        tmp = Path(td)
        clips = [_build_scene_clip(s, d, i, tmp) for i, (s, d) in enumerate(zip(scenes, durs))]
        silent = _concat(clips, tmp)

        # 字幕PNG（シーン窓に同期）
        caps: list[tuple[Path, float, float]] = []
        t = 0.0
        for sc, d in zip(scenes, durs):
            s, e = t, t + d
            t = e
            if sc.caption:
                png = tmp / f"cap_{len(caps):02d}.png"
                if _render_caption_png(sc.caption, png):
                    caps.append((png, s, e))

        inputs = ["-i", str(silent), "-i", str(narration_wav)]
        bgm_idx = None
        if bgm:
            inputs += ["-stream_loop", "-1", "-i", str(bgm)]
            bgm_idx = 2
        base = 2 + (1 if bgm else 0)
        for png, _, _ in caps:
            inputs += ["-loop", "1", "-i", str(png)]

        vfilters: list[str] = []
        vlabel = "0:v"
        for k, (png, s, e) in enumerate(caps):
            idx = base + k
            out_lbl = f"v{k}"
            vfilters.append(
                f"[{vlabel}][{idx}:v]overlay=0:0:enable='between(t,{s:.3f},{e:.3f})'[{out_lbl}]"
            )
            vlabel = out_lbl
        vmap = f"[{vlabel}]" if vfilters else "0:v"

        afilters: list[str] = []
        if bgm:
            afilters.append(
                f"[{bgm_idx}:a]volume={config.BGM_VOLUME}[bg];"
                f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=3[a]"
            )
            amap = "[a]"
        else:
            amap = "1:a"

        filt = ";".join(vfilters + afilters)
        cmd = ["ffmpeg", "-y", *inputs]
        if filt:
            cmd += ["-filter_complex", filt]
        cmd += [
            "-map", vmap, "-map", amap,
            "-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total}", "-shortest", "-movflags", "+faststart", str(out_path),
        ]
        _run(cmd)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="ffmpeg 合成テスト")
    ap.add_argument("--script", required=True)
    ap.add_argument("--images", nargs="+", required=True)
    ap.add_argument("--narration", required=True)
    ap.add_argument("--narration-dur", type=float, required=True)
    ap.add_argument("--out", default=str(config.OUTPUT / "video.mp4"))
    args = ap.parse_args()
    script = json.load(open(args.script, encoding="utf-8"))
    captions = [s.get("caption", "") for s in script["scenes"]]
    scenes = [
        Scene(path=Path(p), is_video=str(p).endswith(".mp4"),
              caption=captions[i] if i < len(captions) else "")
        for i, p in enumerate(args.images)
    ]
    out = compose(scenes, Path(args.narration), args.narration_dur, Path(args.out), config.bgm_path())
    print(f"video -> {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
