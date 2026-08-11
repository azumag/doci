"""サムネイル自動生成。

このチャンネルはほぼ全動画が縦(9:16、YouTube Shorts扱い)。YouTube公式ヘルプによれば
縦動画に16:9サムネイルを設定するとホーム画面等で自動生成の4:5に置き換わることがあるため、
**縦構図で作り(`render`)、API送信時にのみ16:9キャンバスへ中央配置+背景ぼかし埋め
(ピラーボックス、`to_16x9`)する**設計を採る。

デザインは `doci/charts.py` と同じ墨地＋ゴールド＋明朝のトーンを踏襲するが、chart 用 CSS は
直接共有せず、必要な値だけをここに持つ（chart 専用の大量の未使用CSSを引きずらないため）。
HTML→PNG描画は charts.py の `_chrome_shot`(headless Chromeスクリーンショット汎用ヘルパー)を
そのまま再利用する。
"""
from __future__ import annotations

import html
import re
import subprocess
import tempfile
from pathlib import Path

from . import style_themes
from .channel import ThumbnailStyle
from .charts import _chrome_shot

# ===== デザイン値（charts.py の墨地＋ゴールド＋明朝トーンを複製。charts.py 自体は改変しない） =====
_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
body{background:radial-gradient(120% 90% at 50% 28%,#17120b 0%,#0b0a0c 55%,#070609 100%);
  font-family:'Hiragino Mincho ProN','Hiragino Mincho Pro',serif}
/* 背景写真/動画フレームの上に敷く暗幕（文字可読性を確保しつつ背景は透かす） */
.scrim{position:fixed;inset:0;pointer-events:none;
  background:linear-gradient(180deg,rgba(8,7,9,.74) 0%,rgba(8,7,9,.5) 38%,rgba(8,7,9,.7) 70%,rgba(8,7,9,.86) 100%)}
.vig{position:fixed;inset:0;pointer-events:none;
  background:radial-gradient(120% 80% at 50% 38%,transparent 55%,rgba(0,0,0,.55) 100%)}
/* サムネイルは1点だけ強く見せる用途。タイトルは画面下寄り帯の中でflexboxにより縦横中央寄せする。
   帯を80vh確保するのは、長めのタイトルが複数行に折り返した時に画面下端でクリップされないため
   （実データ検証で37字程度の実タイトルが7行になり58vh帯だと最終行が切れることを確認して拡張）。 */
.wrap{position:fixed;left:0;right:0;bottom:0;height:80vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:0 10vw}
.title{font-weight:700;line-height:1.3;color:#f6efe1;text-align:center;
  max-width:80vw;text-shadow:0 .3vh 2vh rgba(0,0,0,.8)}
.trule{height:.4vh;width:16vw;margin-top:2.6vh;border-radius:.3vh;
  background:linear-gradient(90deg,#e8b65a,#f8dd97);box-shadow:0 0 1.4vh rgba(232,182,90,.4)}
"""


def _units(text: str) -> float:
    """文字数の概算（全角≈1em、半角(ASCII)≈0.6em）。charts.py の同名関数と同じ考え方の簡易版。"""
    return sum(0.6 if ord(c) < 128 else 1.0 for c in str(text or "")) or 1.0


def _title_fs(title: str) -> float:
    """タイトルの長さに応じたフォントサイズ(vh、目安7vh・下限4.4vh)。charts.py のような
    行ごとの動的フィット計算はしない単純な文字数ベースの目安値だが、実タイトル(30〜40字級)で
    画面下端の文字クリップが実測で確認されたため、長い見出しでは緩く縮小して安全域を持たせる。"""
    return round(max(4.4, min(7.0, 210.0 / _units(title))), 2)


_DASH_RE = re.compile(r"[――─—]{2,}")


def display_text(title: str, *, max_len: int = 24) -> str:
    """サムネイル表示用に短縮する。ダッシュ区切りがあれば前半句を使う。無ければ
    読点/クエスチョン/空白の自然な位置で max_len 程度に切り詰める。それでも見つから
    なければハード切り詰め+「…」。元の title(動画メタデータ等)は一切変更しない
    ——これは表示専用のヘルパー。"""
    title = (title or "").strip()
    m = _DASH_RE.search(title)
    if m:
        head = title[:m.start()].strip()
        if head:
            return head
    if len(title) <= max_len:
        return title
    window = title[:max_len]
    best = -1
    for sep in "、？?　 ":
        idx = window.rfind(sep)
        if idx > best:
            best = idx
    if best >= 4:  # 極端に短い断片は避ける
        return window[: best + 1].rstrip("、？?　 ")
    return window.rstrip() + "…"


def _style_css(style: ThumbnailStyle | None) -> str:
    style = style or ThumbnailStyle()
    return (
        _CSS.replace(
            "'Hiragino Mincho ProN','Hiragino Mincho Pro',serif",
            style.font_family,
        )
        .replace("#f6efe1", style.title_color)
        + style_themes.get(style.theme).thumbnail_css
    )


def _html_doc(
    title: str,
    bg_image: Path | None,
    style: ThumbnailStyle | None = None,
) -> str:
    body_style = ""
    scrim = ""
    if bg_image is not None:
        body_style = f" style=\"background:url('file://{Path(bg_image).resolve()}') center/cover no-repeat\""
        scrim = "<div class='scrim'></div>"
    text = display_text(title)
    fs = _title_fs(text)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        + _style_css(style)
        + "</style></head>"
        f"<body{body_style}>"
        + scrim
        + "<div class='vig'></div>"
        "<div class='wrap'>"
        f'<div class="title" style="font-size:{fs}vh">{html.escape(text)}</div>'
        '<div class="trule"></div>'
        "</div></body></html>"
    )


def render(
    title: str,
    out_png: Path,
    *,
    bg_image: Path | None = None,
    width: int = 1080,
    height: int = 1920,
    style: ThumbnailStyle | None = None,
) -> Path:
    """縦構図のタイトルカードを HTML→Chrome で PNG 描画して返す。"""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    doc = _html_doc(title, Path(bg_image) if bg_image else None, style)
    with tempfile.TemporaryDirectory(prefix="doci_thumb_") as td:
        hp = Path(td) / "thumbnail.html"
        hp.write_text(doc, encoding="utf-8")
        if not _chrome_shot(hp, out_png, width, height):
            raise RuntimeError("サムネイル描画失敗（PNG生成されず）")
    return out_png


def _probe_size(png_path: Path) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(png_path)],
        capture_output=True, text=True, timeout=30,
    )
    out = r.stdout.strip()
    if "x" not in out:
        raise RuntimeError(f"ffprobe寸法取得失敗: {png_path} stderr={r.stderr[:300]}")
    w_s, h_s = out.split("x")
    return int(w_s), int(h_s)


def to_16x9(src_png: Path, out_png: Path, *, target_w: int = 1280, target_h: int = 720) -> Path:
    """縦(または任意アスペクト)のPNGを16:9キャンバスへ中央配置+背景ぼかし埋め(ピラーボックス)する。
    入力が既に16:9に近い場合は単純リサイズにフォールバックする。"""
    src_png = Path(src_png)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    w, h = _probe_size(src_png)
    target_ratio = target_w / target_h
    src_ratio = w / h
    if abs(src_ratio - target_ratio) / target_ratio <= 0.05:
        cmd = ["ffmpeg", "-y", "-i", str(src_png), "-vf", f"scale={target_w}:{target_h}",
               "-frames:v", "1", str(out_png)]
    else:
        filt = (
            f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},gblur=sigma=40[bg];"
            f"[0:v]scale=-1:{target_h}[fg];"
            f"[bg][fg]overlay=(W-w)/2:0[out]"
        )
        cmd = ["ffmpeg", "-y", "-i", str(src_png), "-filter_complex", filt, "-map", "[out]",
               "-frames:v", "1", str(out_png)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if not out_png.exists():
        raise RuntimeError(f"16:9変換に失敗（PNG生成されず）: {out_png}")
    out_w, out_h = _probe_size(out_png)
    if (out_w, out_h) != (target_w, target_h):
        raise RuntimeError(f"16:9変換後の寸法が不正: {out_w}x{out_h} (期待 {target_w}x{target_h})")
    return out_png
