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
import re
import subprocess
import tempfile
from pathlib import Path

from . import config

_MULT = {"兆": 1e12, "億": 1e8, "万": 1e4, "千": 1e3}


def _magnitude(s) -> float | None:
    """値文字列から数量を抽出（棒の比率算出用）。「6500万ドル」→6.5e7 /「約3250万」→3.25e7 /
    「1ドル」→1 /「永久」→None（数値なし）。最初の数字＋直後の単位語(万億兆千)を解釈する。"""
    m = re.search(r"(\d[\d,\.]*)\s*(兆|億|万|千)?", str(s or ""))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2):
        num *= _MULT[m.group(2)]
    return num


def _percent(s) -> float | None:
    """文字列中の「N%」を取り出す（リングゲージ用）。無ければ None。"""
    m = re.search(r"(\d[\d\.]*)\s*[%％]", str(s or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None

_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 動画と揃えた暗色＋アンバーのデザインシステム。サイズは vh/vw で縦横両対応。
_BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
body{background:linear-gradient(135deg,#1d1711 0%,#0a0a0c 100%);
  font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;color:#f4efe6}
/* 下部は字幕帯(約64%〜)を避けるため本体を上側に寄せる */
/* 字幕は画面の約64%位置から下に出る(compose SUB_Y_RATIO)。本体・出典はその上(≲60vh)に
   収め、下40vhは字幕帯として確実に空ける（図表と字幕の重なり防止）。 */
.wrap{width:100%;height:100%;display:flex;flex-direction:column;
  padding:5vh 8vw 40vh 8vw;position:relative}
.title{font-size:4.8vh;font-weight:800;line-height:1.2;letter-spacing:.01em;
  color:#fbf6ec;border-left:.7vh solid #f0b450;padding-left:1.6vw;margin-bottom:.6vh}
.unit{font-size:2.6vh;color:#9a9486;margin-bottom:3.5vh}
.body{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;justify-content:center}
/* 出典は本体の下(通常フロー)に置き、内容長に依らず必ずコンテンツの直下に来るようにする。
   下40vhは字幕帯として空けてあるので、ここでも字幕とは重ならない。 */
.source{font-size:1.9vh;color:#7a7468;text-align:right;line-height:1.35;margin-top:1.4vh;flex-shrink:0}
/* 棒グラフ（値は棒の外＝固定幅の右列。font-size は最長値に合わせ Python 側で指定） */
.bars{display:flex;flex-direction:column;justify-content:center;gap:3.4vh}
.bar-row{display:flex;align-items:center;gap:2vw}
.bar-label{width:16vw;font-size:3vh;color:#e7e1d4;text-align:right;flex-shrink:0;white-space:nowrap}
.bar-track{flex:1;height:6.5vh;background:#241e16;border-radius:1vh;overflow:hidden}
.bar-fill{height:100%;background:linear-gradient(90deg,#e8862f,#f4c25c);border-radius:1vh;
  box-shadow:0 0 2vh rgba(240,180,80,.25)}
.bar-val{width:22vw;font-weight:800;color:#f4c25c;white-space:nowrap;flex-shrink:0;text-align:left}
/* 大数字（font-size は内容長に応じ Python 側で算出しインライン指定） */
.stat{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2.6vh}
.stat-num{font-weight:900;line-height:1.05;white-space:nowrap;max-width:94vw;
  background:linear-gradient(90deg,#f0b450,#f4d98a);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;letter-spacing:-.01em}
.stat-accent{width:30vw;height:1vh;border-radius:1vh;background:linear-gradient(90deg,#e8862f,#f4c25c)}
.stat-cap{font-size:3.8vh;color:#d8d2c4;text-align:center;max-width:84vw;line-height:1.45;line-break:strict}
/* リングゲージ（割合の視覚化） */
.gauge{position:relative;width:34vh;height:34vh;border-radius:50%;
  display:flex;align-items:center;justify-content:center}
.gauge-hole{position:absolute;width:23vh;height:23vh;border-radius:50%;
  background:radial-gradient(circle,#15110b 0%,#0d0b08 100%)}
.gauge-val{position:relative;font-size:6.6vh;font-weight:900;color:#f4c25c;white-space:nowrap}
/* 比較・棒（数量を比率で見せる） */
.cbars{display:flex;flex-direction:column;justify-content:center;gap:5vh;width:100%}
.cbar-label{font-size:3.2vh;color:#cfc8ba;margin-bottom:1.4vh;line-break:strict}
.cbar-line{display:flex;align-items:center;gap:2.5vw}
.cbar-track{flex:1;height:6vh;background:#241e16;border-radius:1vh;overflow:hidden}
.cbar-fill{height:100%;background:linear-gradient(90deg,#e8862f,#f4c25c);border-radius:1vh;
  box-shadow:0 0 2vh rgba(240,180,80,.25)}
/* 値は固定幅の右列に置き、全行のトラック幅を揃える＝棒の長さ比較を正確にする。
   font-size は最長値に合わせ Python 側でインライン指定（列内に収め見切れ防止）。 */
.cbar-val{width:36vw;font-weight:800;color:#f4c25c;white-space:nowrap;flex-shrink:0;text-align:left}
/* 比較（数量化できない場合）: 全幅カードを縦に積み、間に下向き矢印。
   横並びだとカードが細くラベルが不自然に折返すため、縦積みで各ラベルを1行に収める。 */
.compare{flex:1;display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:1.6vh}
.cmp-item{display:flex;align-items:center;justify-content:space-between;gap:4vw;
  background:#1a140d;border:.3vh solid #3a2f20;border-radius:2vh;padding:3vh 5vw}
.cmp-val{font-weight:900;color:#f4c25c;line-height:1;white-space:nowrap;flex-shrink:0}
.cmp-label{font-size:3.4vh;color:#cfc8ba;text-align:right;line-height:1.3;line-break:strict}
.cmp-arrow{font-size:5vh;color:#e8862f;align-self:center;line-height:1}
/* 年表 */
.timeline{flex:1;display:flex;flex-direction:column;justify-content:space-evenly;gap:0}
.tl-event{display:flex;align-items:flex-start;gap:2.2vw;position:relative;padding-bottom:1.4vh}
.tl-event:not(:last-child)::before{content:'';position:absolute;left:1.05vw;top:2.6vh;bottom:0;
  width:.35vh;background:#3a2f20}
.tl-dot{width:1.8vw;height:1.8vw;border-radius:50%;background:#f0b450;flex-shrink:0;margin-top:1vh;
  box-shadow:0 0 0 .6vh rgba(240,180,80,.18)}
.tl-year{font-size:3.4vh;font-weight:800;color:#f4c25c;flex-shrink:0;white-space:nowrap;padding-right:2.6vw}
.tl-label{font-size:2.9vh;color:#e7e1d4;line-height:1.3;padding-top:.2vh;line-break:strict}
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
    disps = [str(d.get("display") or _fmt(float(d.get("value") or 0))) for d in data]
    # 値は棒の外（固定幅の右列）に置く。棒の内側に入れると短い棒で値が左へはみ出し
    # bar-track の overflow:hidden に切られて「140億本→0億本」のように化けるため。
    longest = max(disps, key=_units, default="")
    vfs = _fit_vw(longest, 20.0, 4.2)
    rows = []
    for d, disp in zip(data, disps):
        v = float(d.get("value") or 0)
        pct = max(4.0, v / mx * 100.0)
        rows.append(
            f'<div class="bar-row"><div class="bar-label">{_esc(d.get("label"))}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>'
            f'<div class="bar-val" style="font-size:{vfs}vw">{_esc(disp)}</div></div>'
        )
    return f'<div class="bars">{"".join(rows)}</div>'


def _stat(spec: dict) -> str:
    val = spec.get("value")
    cap = spec.get("caption")
    pct = _percent(val)
    # 値が割合(N%)なら、リングゲージで視覚化して「チャート感」を出す。
    if pct is not None and 0 <= pct <= 100:
        ring = (
            f'<div class="gauge" style="background:'
            f'conic-gradient(#f4c25c 0 {pct:.1f}%,#2a2218 {pct:.1f}% 100%)">'
            f'<div class="gauge-hole"></div>'
            f'<div class="gauge-val">{_esc(val)}</div></div>'
        )
        cap_html = f'<div class="stat-cap">{_esc(cap)}</div>' if cap else ""
        return f'<div class="stat">{ring}{cap_html}</div>'
    # 割合でない大数字は、視認性最優先で大きく見せ、下にアクセントバーを敷く。
    fs = _fit_vw(val, 74.0, 22.0)
    cap_html = f'<div class="stat-cap">{_esc(cap)}</div>' if cap else ""
    return (
        f'<div class="stat"><div class="stat-num" style="font-size:{fs}vw">{_esc(val)}</div>'
        f'<div class="stat-accent"></div>{cap_html}</div>'
    )


def _units(text) -> float:
    return sum(0.6 if ord(c) < 128 else 1.0 for c in str(text or "")) or 1.0


def _compare_unit(s) -> str:
    """値から数字グループ＋スケール語を除いた「単位部分」を返す（例: 約3250万ドル→ドル）。"""
    s = re.sub(r"^(約|およそ|ほぼ)", "", str(s or "").strip())
    s = re.sub(r"\d[\d,\.]*\s*(兆|億|万|千)?", "", s, count=1)
    return s.strip()


def _bar_comparable(items: list) -> bool:
    """compare を棒グラフ化してよいか。比率が誤解を生まない条件のみ許可:
    全項目が数量化でき、各値の数字グループが1つだけ（"12時間30分"や"1/2"等の複合を弾く）、
    かつ単位部分が全項目で一致（"2倍" vs "1/2" のような異単位を弾く）。"""
    vals = [str(it.get("value") or "") for it in items]
    mags = [_magnitude(v) for v in vals]
    if len(items) < 2 or any(m is None or m <= 0 for m in mags):
        return False
    if any(len(re.findall(r"\d[\d,\.]*", v)) != 1 for v in vals):
        return False
    return len({_compare_unit(v) for v in vals}) == 1


def _compare(spec: dict) -> str:
    items = spec.get("items") or []
    mags = [_magnitude(it.get("value")) for it in items]
    # 数量が同一単位で素直に比較できる時だけ、比率に応じた棒グラフにする（誤解防止）。
    if _bar_comparable(items):
        mx = max(mags)
        # 値は固定幅の右列。一番長い値に合わせフォントを列内に収める（見切れ防止）。
        longest_val = max((str(it.get("value") or "") for it in items), key=_units, default="")
        vfs = _fit_vw(longest_val, 34.0, 4.6)
        rows = []
        for it, m in zip(items, mags):
            pct = max(8.0, m / mx * 100.0)
            rows.append(
                f'<div class="cbar-row">'
                f'<div class="cbar-label">{_esc(it.get("label"))}</div>'
                f'<div class="cbar-line">'
                f'<div class="cbar-track"><div class="cbar-fill" style="width:{pct:.1f}%"></div></div>'
                f'<span class="cbar-val" style="font-size:{vfs}vw">{_esc(it.get("value"))}</span></div>'
                f"</div>"
            )
        return f'<div class="cbars">{"".join(rows)}</div>'
    # 数量化できない比較（例: 1ドル → 永久）は、全幅カードを縦に積み下向き矢印で繋ぐ。
    longest = max((str(it.get("value") or "") for it in items), key=_units, default="")
    fs = _fit_vw(longest, 22.0, 9.0)
    parts = []
    for i, it in enumerate(items):
        if i:
            parts.append('<div class="cmp-arrow">↓</div>')
        parts.append(
            f'<div class="cmp-item"><div class="cmp-val" style="font-size:{fs}vw">{_esc(it.get("value"))}</div>'
            f'<div class="cmp-label">{_esc(it.get("label"))}</div></div>'
        )
    return f'<div class="compare">{"".join(parts)}</div>'


def _timeline(spec: dict) -> str:
    evs = spec.get("events") or []
    # 件数が多いと年表本体が上60vhを超え、下40vhの字幕帯へはみ出す（実走で6件が字幕と重なった）。
    # 既定(≤4件)は等倍、5件以上は件数に反比例で年号/説明/行間/ドットを縮小して上側に収める。
    n = len(evs) or 1
    scale = max(0.5, min(1.0, 4.0 / n))
    year_fs = round(3.4 * scale, 2)
    label_fs = round(2.9 * scale, 2)
    pad = round(1.4 * scale, 2)
    dot_mt = round(1.0 * scale, 2)
    rows = [
        f'<div class="tl-event" style="padding-bottom:{pad}vh">'
        f'<div class="tl-dot" style="margin-top:{dot_mt}vh"></div>'
        f'<div class="tl-year" style="font-size:{year_fs}vh">{_esc(e.get("year"))}</div>'
        f'<div class="tl-label" style="font-size:{label_fs}vh">{_esc(e.get("label"))}</div></div>'
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
