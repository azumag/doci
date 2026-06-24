"""図表・グラフを HTML→画像で生成（解説シーン用）。

AI画像生成は文字・数値が崩れて図表に不適。HTML/CSS/SVG で正確・鮮明・スタイル統一して
作り、Chrome ヘッドレスで PNG 化する。chart 仕様(dict)を受け取り out_png に描画して返す。

chart 仕様の例:
  {"type":"bar",   "title":..., "unit":..., "data":[{"label":..,"value":N}...], "source":..}
  {"type":"stat",  "title":..., "value":"63人に1人", "caption":.., "source":..}
  {"type":"compare","title":.., "items":[{"label":..,"value":".."}...], "source":..}  # 左→右
  {"type":"timeline","title":.., "events":[{"year":"1924","label":..}...], "source":..}
"""
from __future__ import annotations

import html
import subprocess
import tempfile
from pathlib import Path

from . import config

_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 動画と揃えた暗色＋アンバーのデザインシステム。サイズは vh/vw で縦横両対応。
_BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
body{background:linear-gradient(135deg,#1d1711 0%,#0a0a0c 100%);
  font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;color:#f4efe6}
/* 下部は字幕帯(約64%〜)を避けるため本体を上側に寄せる */
.wrap{width:100%;height:100%;display:flex;flex-direction:column;
  padding:6vh 8vw 26vh 8vw;position:relative}
.title{font-size:5.4vh;font-weight:800;line-height:1.25;letter-spacing:.01em;
  color:#fbf6ec;border-left:.7vh solid #f0b450;padding-left:1.6vw;margin-bottom:1vh}
.unit{font-size:2.6vh;color:#9a9486;margin-bottom:3.5vh}
.body{flex:1;display:flex;flex-direction:column;justify-content:center}
.source{position:absolute;left:8vw;right:8vw;bottom:26.5vh;font-size:1.9vh;color:#7a7468;
  text-align:right;line-height:1.35}
/* 棒グラフ */
.bars{display:flex;flex-direction:column;gap:3.2vh}
.bar-row{display:flex;align-items:center;gap:1.6vw}
.bar-label{width:24vw;font-size:3.4vh;color:#e7e1d4;text-align:right;flex-shrink:0}
.bar-track{flex:1;height:7.5vh;background:#241e16;border-radius:1vh;overflow:hidden}
.bar-fill{height:100%;background:linear-gradient(90deg,#e8862f,#f4c25c);border-radius:1vh;
  display:flex;align-items:center;justify-content:flex-end}
.bar-val{font-size:3.8vh;font-weight:800;color:#fff;padding:0 1.4vw;white-space:nowrap}
/* 大数字（font-size は内容長に応じ Python 側で算出しインライン指定） */
.stat{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2.4vh}
.stat-num{font-weight:900;line-height:1.05;white-space:nowrap;max-width:94vw;
  background:linear-gradient(90deg,#f0b450,#f4d98a);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;letter-spacing:-.01em}
.stat-cap{font-size:4.2vh;color:#d8d2c4;text-align:center;max-width:78vw;line-height:1.4}
/* 比較（左→右） */
.compare{flex:1;display:flex;align-items:center;justify-content:center;gap:2.5vw}
.cmp-item{display:flex;flex-direction:column;align-items:center;gap:1.6vh;flex:1;min-width:0;max-width:40vw;
  background:#1a140d;border:.3vh solid #3a2f20;border-radius:2vh;padding:4vh 2.5vw}
.cmp-val{font-weight:900;color:#f4c25c;line-height:1;white-space:nowrap}
.cmp-label{font-size:3.2vh;color:#cfc8ba;text-align:center;line-height:1.35}
.cmp-arrow{font-size:8vh;color:#e8862f;flex-shrink:0}
/* 年表 */
.timeline{flex:1;display:flex;flex-direction:column;justify-content:center;gap:0}
.tl-event{display:flex;align-items:flex-start;gap:2.5vw;position:relative;padding-bottom:4.5vh}
.tl-event:not(:last-child)::before{content:'';position:absolute;left:1.05vw;top:3.4vh;bottom:0;
  width:.35vh;background:#3a2f20}
.tl-dot{width:2.1vw;height:2.1vw;border-radius:50%;background:#f0b450;flex-shrink:0;margin-top:1.4vh;
  box-shadow:0 0 0 .8vh rgba(240,180,80,.18)}
.tl-year{font-size:4vh;font-weight:800;color:#f4c25c;flex-shrink:0;white-space:nowrap;padding-right:3vw}
.tl-label{font-size:3.3vh;color:#e7e1d4;line-height:1.4;padding-top:.4vh}
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fit_vw(text, budget_vw: float, cap_vw: float) -> float:
    """1行表示の大きな文字を幅 budget_vw に収めるフォントサイズ(vw)を算出。
    全角≈1em、半角(ASCII)≈0.6em として概算し、cap_vw を上限にする。
    内容が長いほど自動で小さくなり、はみ出し・1文字折返しを防ぐ。"""
    units = sum(0.6 if ord(c) < 128 else 1.0 for c in str(text or "")) or 1.0
    return round(min(cap_vw, budget_vw / units), 2)


def _page(title: str, unit: str, body: str, source: str) -> str:
    unit_html = f'<div class="unit">{_esc(unit)}</div>' if unit else ""
    src_html = f'<div class="source">出典: {_esc(source)}</div>' if source else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        + _BASE_CSS
        + "</style></head><body><div class='wrap'>"
        + f'<div class="title">{_esc(title)}</div>{unit_html}'
        + f'<div class="body">{body}</div>{src_html}'
        + "</div></body></html>"
    )


def _bar(spec: dict) -> str:
    data = spec.get("data") or []
    mx = max((float(d.get("value") or 0) for d in data), default=1) or 1
    rows = []
    for d in data:
        v = float(d.get("value") or 0)
        pct = max(6.0, v / mx * 100.0)
        disp = d.get("display") or _fmt(v)
        rows.append(
            f'<div class="bar-row"><div class="bar-label">{_esc(d.get("label"))}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%">'
            f'<span class="bar-val">{_esc(disp)}</span></div></div></div>'
        )
    return f'<div class="bars">{"".join(rows)}</div>'


def _stat(spec: dict) -> str:
    val = spec.get("value")
    # 実フォント(weight900)は概算より約2割太いため予算を絞り、はみ出しを確実に防ぐ。
    fs = _fit_vw(val, 74.0, 22.0)
    return (
        f'<div class="stat"><div class="stat-num" style="font-size:{fs}vw">{_esc(val)}</div>'
        f'<div class="stat-cap">{_esc(spec.get("caption"))}</div></div>'
    )


def _compare(spec: dict) -> str:
    items = spec.get("items") or []
    parts = []
    for i, it in enumerate(items):
        if i:
            parts.append('<div class="cmp-arrow">→</div>')
        fs = _fit_vw(it.get("value"), 26.0, 13.0)
        parts.append(
            f'<div class="cmp-item"><div class="cmp-val" style="font-size:{fs}vw">{_esc(it.get("value"))}</div>'
            f'<div class="cmp-label">{_esc(it.get("label"))}</div></div>'
        )
    return f'<div class="compare">{"".join(parts)}</div>'


def _timeline(spec: dict) -> str:
    evs = spec.get("events") or []
    rows = [
        f'<div class="tl-event"><div class="tl-dot"></div>'
        f'<div class="tl-year">{_esc(e.get("year"))}</div>'
        f'<div class="tl-label">{_esc(e.get("label"))}</div></div>'
        for e in evs
    ]
    return f'<div class="timeline">{"".join(rows)}</div>'


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else f"{v:g}"


_BUILDERS = {"bar": _bar, "stat": _stat, "compare": _compare, "timeline": _timeline}


def chart_html(spec: dict) -> str:
    builder = _BUILDERS.get(spec.get("type", ""))
    if not builder:
        raise ValueError(f"未対応の chart type: {spec.get('type')}")
    return _page(spec.get("title", ""), spec.get("unit", ""), builder(spec), spec.get("source", ""))


def render_chart(spec: dict, out_png: Path, width: int | None = None, height: int | None = None) -> Path:
    """chart 仕様を out_png に描画して返す。Chrome ヘッドレスで HTML→PNG。"""
    W = width or config.VIDEO_WIDTH
    H = height or config.VIDEO_HEIGHT
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(chart_html(spec))
        html_path = f.name
    cmd = [
        _CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", f"--window-size={W},{H}",
        f"--screenshot={out_png}", f"file://{html_path}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    Path(html_path).unlink(missing_ok=True)
    if not out_png.exists():
        raise RuntimeError(f"Chrome 描画失敗: {proc.stderr[-400:]}")
    return out_png
