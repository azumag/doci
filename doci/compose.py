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
    static: bool = False  # 図表など：Ken Burns を掛けず静止表示（可読性優先）


# --- 字幕（発話フルテロップ: issue #5） ---
SUB_LINE_CHARS = 13          # 1行あたり文字数（縦9:16基準。横16:9は幅比で自動拡張）
SUB_MAX_LINES = 2            # 最大行数
SUB_MAX_CHARS = SUB_LINE_CHARS * SUB_MAX_LINES  # 1チャンク最大文字数(=26)
SUB_MIN_DUR = 0.7           # 最小表示秒（短すぎるチャンクは結合してチラつき防止）
SUB_Y_RATIO = 0.64         # 縦位置（中央やや下。下部UIを避ける）

# 折り返しの自然さ用（日本語の禁則・区切り）
_NO_LINE_START = set(  # 行頭に置かない（小書き仮名・長音・閉じ括弧・句読点）
    "ぁぃぅぇぉっゃゅょゎヵヶァィゥェォッャュョヮ・ーｰ〜、。，．！？…）」』】〕〉》”’!?,.)]}"
)
_NO_LINE_END = set("（「『【〈《“‘([{")  # 行末に置かない（開き括弧）
_BREAK_AFTER_STRONG = set("、。，．！？…")  # この直後で折ると自然（強）
_BREAK_AFTER_SOFT = set("はがをにへでともやのね")  # 助詞の直後（弱）


def _wrap(text: str, width: int = SUB_LINE_CHARS, max_lines: int = SUB_MAX_LINES) -> str:
    """1チャンクを最大 max_lines 行に。語の途中で割れないよう、2行は中央付近で均等に、
    かつ読点・助詞の後／禁則を避けた位置で折る。"""
    text = (text or "").replace("\n", "").strip().strip("、")
    n = len(text)
    if n <= width:
        return text
    target = (n + 1) // 2  # 中央で均等に割る
    best_i, best_score = None, None
    for i in range(2, n - 1):  # 各行2字以上（孤立を避ける）
        if text[i] in _NO_LINE_START:        # 行頭禁止文字の前では折らない
            continue
        if text[i - 1] in _NO_LINE_END:      # 開き括弧の直後では折らない
            continue
        score = abs(i - target)
        if text[i - 1] in _BREAK_AFTER_STRONG:
            score -= 6                        # 読点・句点の後を最優先
        elif text[i - 1] in _BREAK_AFTER_SOFT:
            score -= 3                        # 助詞の後を優遇
        if best_score is None or score < best_score:
            best_score, best_i = score, i
    i = best_i if best_i is not None else min(target, n - 1)
    return text[:i] + "\n" + text[i:]


def _segment_chunks(text: str) -> list[str]:
    """1文を画面に収まる ≤SUB_MAX_CHARS のチャンク列に分割。
    「。！？」は表示せず除去、「、」を優先の区切りに使う。
    """
    import re as _re

    text = text.replace("。", "").replace("！", "").replace("？", "").strip()
    if not text:
        return []
    phrases = [p for p in _re.split(r"(?<=、)", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in phrases:
        if cur and len(cur) + len(p) > SUB_MAX_CHARS:
            chunks.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        chunks.append(cur)
    # 単一句が長すぎる場合のハード分割
    out: list[str] = []
    for c in chunks:
        while len(c) > SUB_MAX_CHARS:
            out.append(c[:SUB_MAX_CHARS])
            c = c[SUB_MAX_CHARS:]
        out.append(c)
    return [c.strip("、") for c in out if c.strip("、")]


def build_subtitles(segments) -> list[tuple[str, float, float]]:
    """発話 segments（文ごとの text/start/end）を字幕チャンク（text,start,end）列に。
    各文の時間窓 [start,end] を、チャンクの文字数比で按分する（MVP）。
    最小表示時間を満たさないチャンクは、同一文内の前後の隣チャンクへ結合して
    チラつきを防ぐ（文をまたぐ結合はしない＝読みの一貫性を保つ）。
    """
    raw: list[list] = []  # [text, start, end, seg_id]
    for si, seg in enumerate(segments):
        chunks = _segment_chunks(seg.text)
        if not chunks:
            continue
        total = sum(len(c) for c in chunks) or 1
        span = max(seg.end - seg.start, 0.001)
        t = seg.start
        for c in chunks:
            d = span * (len(c) / total)
            raw.append([c, t, t + d, si])
            t += d
        raw[-1][2] = seg.end  # 丸め誤差を末尾で吸収

    i = 0
    while i < len(raw):
        c, s, e, sid = raw[i]
        if (e - s) >= SUB_MIN_DUR:
            i += 1
            continue
        # 前（同一文・溢れない）へ結合
        if i > 0 and raw[i - 1][3] == sid and len(raw[i - 1][0]) + len(c) <= SUB_MAX_CHARS:
            raw[i - 1][0] += c
            raw[i - 1][2] = e
            raw.pop(i)
            continue
        # 次（同一文・溢れない）へ前置き結合
        if i + 1 < len(raw) and raw[i + 1][3] == sid and len(c) + len(raw[i + 1][0]) <= SUB_MAX_CHARS:
            raw[i + 1][0] = c + raw[i + 1][0]
            raw[i + 1][1] = s
            raw.pop(i)
            continue
        i += 1  # 結合できない短チャンクは諦める（稀な一語文など）
    return [(c, s, e) for c, s, e, _ in raw]


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


def _render_caption_png(text: str, out_png: Path, W: int, H: int) -> bool:
    """字幕を透過PNGに描画。成功時 True。フォント/Pillow 不在なら False。"""
    font_path = _font_path()
    if not font_path:
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False

    # フォントは短辺基準（横16:9でも縦9:16でも見た目の文字サイズを一定に保つ）。
    size = int(min(W, H) * 0.060)
    try:
        font = ImageFont.truetype(font_path, size, index=0)
    except Exception:
        return False

    # 横16:9は横幅が広いので1行の文字数を幅比で広げ、無駄な折り返しを減らす。
    per_line = SUB_LINE_CHARS if W <= H else max(SUB_LINE_CHARS, round(SUB_LINE_CHARS * W / H))
    lines = _wrap(text, width=per_line).split("\n")
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    line_h = int(size * 1.35)
    text_h = line_h * len(lines)
    text_w = max((draw.textlength(ln, font=font) for ln in lines), default=0)
    pad_x, pad_y = int(size * 0.7), int(size * 0.45)
    box_w, box_h = int(text_w + pad_x * 2), int(text_h + pad_y * 2)
    x0 = (W - box_w) // 2
    y0 = int(H * SUB_Y_RATIO)
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


def _build_scene_clip(scene: Scene, dur: float, idx: int, tmp: Path, W: int, H: int) -> Path:
    fps = config.VIDEO_FPS
    out = tmp / f"scene_{idx:02d}.mp4"
    frames = max(1, round(dur * fps))
    tail = ["-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)]
    if scene.is_video:
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},fps={fps},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(scene.path),
               "-t", f"{dur}", "-vf", vf, *tail]
    elif scene.static:
        # 図表など: Ken Burns を掛けず静止。枠に収め、はみ出しは暗色でパッド（文字を切らない）。
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x0a0a0c,"
              f"fps={fps},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{dur}", "-i", str(scene.path),
               "-vf", vf, *tail]
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


def _build_subtitle_track(
    caps: list[tuple[Path, float, float]], total: float, tmp: Path, W: int, H: int, fps: int
) -> Path:
    """全字幕PNGを1本の透過動画(qtrle)に連結。各字幕は時間窓に、隙間は透過で埋める。
    これを最終合成で1回 overlay するだけにし、字幕本数に依らずO(1)の合成にする（長尺高速化）。
    この環境の ffmpeg は libass 非対応のため、ASSではなく透過トラック方式を採る。
    """
    from PIL import Image

    blank = tmp / "cap_blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)
    segs: list[tuple[Path, float]] = []
    t = 0.0
    for png, s, e in sorted(caps, key=lambda c: c[1]):
        if s > t + 1e-3:
            segs.append((blank, s - t))  # 隙間=透過
        segs.append((png, max(e - s, 1.0 / fps)))
        t = max(t, e)
    if total > t + 1e-3:
        segs.append((blank, total - t))
    lines = ["ffconcat version 1.0"]
    for png, dur in segs:
        lines.append(f"file '{Path(png).resolve().as_posix()}'")
        lines.append(f"duration {dur:.3f}")
    if segs:  # concat demuxer は最後の file の duration を効かせるため末尾を1回繰り返す
        lines.append(f"file '{Path(segs[-1][0]).resolve().as_posix()}'")
    listf = tmp / "subs_concat.txt"
    listf.write_text("\n".join(lines), encoding="utf-8")
    out = tmp / "subtrack.mov"
    _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
        "-vf", f"fps={fps},format=rgba", "-c:v", "qtrle", str(out),
    ])
    return out


def compose(
    scenes: list[Scene],
    narration_wav: Path,
    narration_dur: float,
    out_path: Path,
    bgm: Path | None = None,
    segments=None,
    width: int | None = None,
    height: int | None = None,
) -> Path:
    if not scenes:
        raise ValueError("scenes が空です")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    W = width or config.VIDEO_WIDTH
    H = height or config.VIDEO_HEIGHT
    fps = config.VIDEO_FPS
    total = narration_dur + 0.4
    durs = _scene_durations(total, len(scenes))

    with tempfile.TemporaryDirectory(prefix="doci_compose_") as td:
        tmp = Path(td)
        clips = [_build_scene_clip(s, d, i, tmp, W, H) for i, (s, d) in enumerate(zip(scenes, durs))]
        silent = _concat(clips, tmp)

        # 字幕PNG。segments があれば「発話フル字幕」（チャンク窓に同期: issue #5）、
        # 無ければ従来のシーン見出し（シーン窓）にフォールバック。
        caps: list[tuple[Path, float, float]] = []
        if segments:
            for text, s, e in build_subtitles(segments):
                png = tmp / f"cap_{len(caps):03d}.png"
                if _render_caption_png(text, png, W, H):
                    caps.append((png, s, e))
        else:
            t = 0.0
            for sc, d in zip(scenes, durs):
                s, e = t, t + d
                t = e
                if sc.caption:
                    png = tmp / f"cap_{len(caps):03d}.png"
                    if _render_caption_png(sc.caption, png, W, H):
                        caps.append((png, s, e))

        # 字幕は単一の透過トラックに連結して1回だけ overlay（字幕本数に依らずO(1)・長尺高速）。
        inputs = ["-i", str(silent), "-i", str(narration_wav)]
        bgm_idx = None
        if bgm:
            inputs += ["-stream_loop", "-1", "-i", str(bgm)]
            bgm_idx = 2
        vparts: list[str] = []
        if caps:
            subtrack = _build_subtitle_track(caps, total, tmp, W, H, fps)
            sub_idx = 2 + (1 if bgm else 0)
            inputs += ["-i", str(subtrack)]
            vparts.append(f"[0:v][{sub_idx}:v]overlay=0:0[vov]")
            pad_src = "vov"
        else:
            pad_src = "0:v"
        # 末尾途切れ防止: 連結動画はクリップのフレーム量子化で total より僅かに短くなり得る。
        # 最終フレームを複製(tpad)して total を必ず超える長さにし、-t total で正確に切る（-shortest不使用）。
        vparts.append(f"[{pad_src}]tpad=stop_mode=clone:stop_duration=3[vout]")
        vmap = "[vout]"

        afilters: list[str] = []
        if bgm:
            afilters.append(
                f"[{bgm_idx}:a]volume={config.BGM_VOLUME}[bg];"
                f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=3[a]"
            )
            amap = "[a]"
        else:
            amap = "1:a"

        filt = ";".join(vparts + afilters)
        cmd = ["ffmpeg", "-y", *inputs]
        cmd += ["-filter_complex", filt]
        cmd += [
            "-map", vmap, "-map", amap,
            "-r", str(fps), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total}", "-movflags", "+faststart", str(out_path),
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
