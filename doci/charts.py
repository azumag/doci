"""図表・グラフを HTML→画像/動画で生成（解説シーン用）。

AI画像生成は文字・数値が崩れて図表に不適。HTML/CSS/SVG で正確・鮮明・スタイル統一して
作り、Chrome ヘッドレスで描画する。chart 仕様(dict)を受け取る。

- render_chart(spec, out_png)        … 最終状態(p=1)の静止 PNG（後方互換・フォールバック）
- render_chart_video(spec, out_mp4)  … 入場アニメ(0→1)を mp4 化（棒伸び/カウントアップ/順次出現）

デザイン: 墨地＋ヴィネット＋微細グレイン、明朝の見出し、ゴールド(ミシュランの星)＋
転換のレッド。起承転結(place)をアイブロウに昇格。アニメは ?p=0..1 を JS が読み、その
進捗時点の1フレームを描く（Chrome を p ごと、または iframe フィルムストリップで撮影）。

chart 仕様の例:
  {"type":"bar",   "title":..., "unit":..., "data":[{"label":..,"value":N}...], "source":..}
  {"type":"stat",  "title":..., "value":"7フラン", "caption":.., "source":..}
  {"type":"compare","title":.., "items":[{"label":..,"value":".."}...], "source":..}
  {"type":"timeline","title":.., "events":[{"year":"1924","label":..}...], "source":..}
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import signal
import subprocess
import tempfile
import time
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


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _units(text) -> float:
    return sum(0.6 if ord(c) < 128 else 1.0 for c in str(text or "")) or 1.0


def _fit_vw(text, budget_vw: float, cap_vw: float) -> float:
    """1行表示の大きな文字を幅 budget_vw に収めるフォントサイズ(vw)を算出。
    全角≈1em、半角(ASCII)≈0.6em として概算し、cap_vw を上限にする。"""
    return round(min(cap_vw, budget_vw / _units(text)), 2)


def _compare_unit(s) -> str:
    """値から数字グループ＋スケール語を除いた「単位部分」を返す（例: 約3250万ドル→ドル）。"""
    s = re.sub(r"^(約|およそ|ほぼ)", "", str(s or "").strip())
    s = re.sub(r"\d[\d,\.]*\s*(兆|億|万|千)?", "", s, count=1)
    return s.strip()


def _bar_comparable(items: list) -> bool:
    """compare を棒グラフ化してよいか。全項目が数量化でき、数字グループが1つだけ、
    単位部分が全項目で一致する時のみ True（比率の誤解を防ぐ）。"""
    vals = [str(it.get("value") or "") for it in items]
    mags = [_magnitude(v) for v in vals]
    if len(items) < 2 or any(m is None or m <= 0 for m in mags):
        return False
    if any(len(re.findall(r"\d[\d,\.]*", v)) != 1 for v in vals):
        return False
    return len({_compare_unit(v) for v in vals}) == 1


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else f"{v:g}"


def _count_attrs(value):
    """値文字列を「カウントアップ可能な整数＋接頭辞＋接尾辞」に分解。
    例: 「約3,000台」→{pre:約,target:3000,suf:台,comma:1} /「7フラン」→{target:7,suf:フラン}
    /「140億本」→{target:140,suf:億本}。小数や数字無しは None（=カウントせず最終値表示）。"""
    s = str(value or "").strip()
    m = re.match(r"^(約|およそ|ほぼ|年間|月間|時速|およそ)?\s*([\d,]+(?:\.\d+)?)\s*(兆|億|万|千)?(.*)$", s)
    if not m:
        return None
    numstr = m.group(2)
    try:
        num = float(numstr.replace(",", ""))
    except ValueError:
        return None
    if num != int(num):
        return None
    suf = ((m.group(3) or "") + (m.group(4) or "")).strip()
    return {"pre": m.group(1) or "", "target": int(num), "suf": suf,
            "comma": ("," in numstr) or num >= 1000}


# 起承転結 → アイブロウのキッカー名（意味ある構造マーカー）
_ACT = {"起": "発端", "承": "展開", "転": "転換", "結": "結末"}

# ===== デザインシステム（墨地＋ゴールド＋ミシュランレッド、明朝見出し） =====
_GRAIN = (
    "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
    "width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' "
    "baseFrequency='0.9' numOctaves='2'/></filter><rect width='100%25' height='100%25' "
    "filter='url(%23n)'/></svg>\")"
)

_BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden}
body{background:radial-gradient(120% 90% at 50% 28%,#17120b 0%,#0b0a0c 55%,#070609 100%);
  font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;color:#f3ecdd}
.grain{position:fixed;inset:0;opacity:.05;pointer-events:none;mix-blend-mode:overlay;
  background-image:__GRAIN__}
.frame{position:fixed;inset:2.4vh 4.2vw;border:.18vh solid rgba(232,182,90,.22);
  border-radius:.6vh;pointer-events:none}
.frame::after{content:'';position:absolute;inset:.9vh;border:.1vh solid rgba(232,182,90,.10);
  border-radius:.4vh}
.vig{position:fixed;inset:0;pointer-events:none;
  background:radial-gradient(120% 80% at 50% 38%,transparent 55%,rgba(0,0,0,.55) 100%)}
/* 下40vhは字幕帯として確実に空ける */
.wrap{position:relative;width:100%;height:100%;padding:7vh 9vw 40vh 9vw;
  display:flex;flex-direction:column}
/* アイブロウ：起承転結を意味ある構造マーカーに昇格 */
.eyebrow{display:flex;align-items:center;gap:1.8vw;margin-bottom:2.6vh;flex-shrink:0}
.act{font-family:'Hiragino Mincho ProN',serif;font-size:3.0vh;font-weight:600;color:#0b0a08;
  background:linear-gradient(135deg,#f8dd97,#e8b65a);width:6.4vh;height:6.4vh;border-radius:50%;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  box-shadow:0 .8vh 2.4vh rgba(232,182,90,.28),inset 0 .2vh .4vh rgba(255,255,255,.5)}
.krule{height:.18vh;flex:1;background:linear-gradient(90deg,rgba(232,182,90,.55),rgba(232,182,90,0))}
.kicker{font-family:'Hiragino Mincho ProN',serif;font-size:2.4vh;letter-spacing:.45em;
  color:#caa05a;text-indent:.45em;flex-shrink:0}
/* 見出し：明朝で印刷物の遺産感 */
.title{font-family:'Hiragino Mincho ProN','Hiragino Mincho Pro',serif;font-weight:600;
  font-size:4.4vh;line-height:1.25;color:#f6efe1;letter-spacing:.02em;flex-shrink:0}
.title .em{color:#d8503a;font-weight:700;padding:0 .12em}
.trule{height:.32vh;width:100%;background:linear-gradient(90deg,#e8b65a,#f8dd97);
  border-radius:.3vh;box-shadow:0 0 1.4vh rgba(232,182,90,.4);transform-origin:left;
  margin-top:1.4vh;flex-shrink:0}
.unit{font-size:2.3vh;color:#9a9486;margin-top:.8vh;flex-shrink:0}
.body{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center}
/* 長文を文節ごとに順次フェード表示する用（不可視でも場所は確保＝出現時に文がずれない） */
.w{opacity:0}
/* 出典は画面最下部に小さく（字幕帯より下）。本体レイアウトからは外して常に一番下に固定。 */
.source{position:absolute;left:9vw;right:9vw;bottom:2.4vh;font-size:1.7vh;color:#6f6657;
  text-align:center;line-height:1.35}
/* ---- stat（大数字＋星 / リングゲージ） ---- */
.stat{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  position:relative;gap:2.2vh}
.star-bg{position:absolute;width:30vh;height:30vh;top:50%;left:50%;
  transform:translate(-50%,-52%);filter:drop-shadow(0 0 3vh rgba(232,182,90,.18))}
.num{position:relative;display:flex;align-items:baseline;gap:1.0vw;line-height:.9;z-index:1}
.num .v{font-weight:900;letter-spacing:-.02em;
  background:linear-gradient(180deg,#fbeec6 0%,#f0c674 45%,#d99a3c 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  text-shadow:0 1vh 3vh rgba(0,0,0,.55)}
.num .u{font-family:'Hiragino Mincho ProN',serif;font-weight:600;font-size:5.4vh;color:#e7d6ad}
.stat-cap{font-size:3.2vh;line-height:1.5;color:#cdc4b2;text-align:center;max-width:80vw;
  margin:0 auto;font-feature-settings:"palt"}
.gauge{position:relative;width:34vh;height:34vh;border-radius:50%;display:flex;
  align-items:center;justify-content:center;box-shadow:0 0 4vh rgba(232,182,90,.15)}
.gauge-hole{position:absolute;width:24vh;height:24vh;border-radius:50%;
  background:radial-gradient(circle,#15110b 0%,#0d0b08 100%)}
.gauge-val{position:relative;font-size:6.4vh;font-weight:900;color:#f4c25c}
/* ---- bar（横棒） ---- */
.bars{display:flex;flex-direction:column;justify-content:center;gap:3.4vh}
.bar-row{display:flex;align-items:center;gap:2vw}
.bar-label{width:16vw;font-size:2.8vh;color:#e7e1d4;text-align:right;flex-shrink:0;white-space:nowrap}
.bar-track{flex:1;height:6.4vh;background:#1b1610;border-radius:1vh;overflow:hidden;
  box-shadow:inset 0 .3vh .8vh rgba(0,0,0,.6),inset 0 0 0 .12vh rgba(232,182,90,.12)}
.bar-fill{height:100%;width:0;background:linear-gradient(90deg,#c9772a,#f4c25c);
  border-radius:1vh;box-shadow:0 0 2vh rgba(240,180,80,.3)}
.bar-val{font-weight:800;color:#f4c25c;white-space:nowrap;flex-shrink:0;text-align:left;min-width:22vw}
/* ---- cbar（同単位の比較棒） ---- */
.cbars{display:flex;flex-direction:column;justify-content:center;gap:5vh;width:100%}
.cbar-label{font-size:3.0vh;color:#cfc8ba;margin-bottom:1.4vh}
.cbar-line{display:flex;align-items:center;gap:2.5vw}
.cbar-track{flex:1;height:6vh;background:#1b1610;border-radius:1vh;overflow:hidden;
  box-shadow:inset 0 .3vh .8vh rgba(0,0,0,.6),inset 0 0 0 .12vh rgba(232,182,90,.12)}
.cbar-fill{height:100%;width:0;background:linear-gradient(90deg,#c9772a,#f4c25c);
  border-radius:1vh;box-shadow:0 0 2vh rgba(240,180,80,.3)}
.cbar-val{font-weight:800;color:#f4c25c;white-space:nowrap;flex-shrink:0;text-align:left;min-width:36vw}
/* ---- compare（数量化できない比較はカードを縦積み） ---- */
.compare{flex:1;display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:1.4vh}
.cmp-item{display:flex;align-items:center;justify-content:space-between;gap:4vw;
  background:linear-gradient(135deg,#1c160e,#120e09);border:.16vh solid #3a2f20;
  border-left:.7vh solid #e8b65a;border-radius:1.6vh;padding:2.4vh 5vw;
  box-shadow:0 1.4vh 3vh rgba(0,0,0,.4)}
.cmp-val{font-weight:900;color:#f4c25c;line-height:1;white-space:nowrap;flex-shrink:0}
.cmp-label{font-size:2.9vh;color:#cfc8ba;text-align:right;line-height:1.3}
.cmp-arrow{align-self:center;font-size:3.8vh;color:#d8503a;line-height:1;margin:.2vh 0}
/* ---- timeline（年表：線が下に伸び、出来事が順に出る） ---- */
.timeline{flex:1;display:flex;flex-direction:column;justify-content:space-evenly;position:relative}
.tl-line{position:absolute;left:1.15vw;top:1.5vh;bottom:1.5vh;width:.32vh;
  background:linear-gradient(180deg,#e8b65a,#6e5a3a);transform-origin:top;border-radius:.3vh}
.tl-event{display:flex;align-items:flex-start;gap:2.4vw;position:relative;z-index:1}
.tl-dot{flex-shrink:0;margin-top:.9vh;filter:drop-shadow(0 0 .8vh rgba(240,180,80,.5))}
.tl-year{font-weight:800;color:#f4c25c;flex-shrink:0;white-space:nowrap;padding-right:2.6vw}
.tl-label{color:#e7e1d4;line-height:1.3;padding-top:.2vh}
"""

# window.__apply(p) で進捗 p(0..1)時点の状態を全要素へ反映。
# 静止/フィルムストリップは ?p をロード時に適用、CDP は __apply(p) を毎フレーム評価。
# よりダイナミックに: オーバーシュート強め＋数字はカウント中わずかに拡大。
_ANIM_JS = """
window.__apply=function(P){
  P=Math.max(0,Math.min(1,P));
  const cl=x=>Math.max(0,Math.min(1,x));
  const eo=x=>1-Math.pow(1-x,3);
  const eb=x=>{const c=2.1;return x>=1?1:1+(--x)*x*((c+1)*x+c)};
  const seg=(el,k)=>{const a=el.dataset[k].split(',').map(parseFloat);return cl((P-a[0])/(a[1]-a[0]))};
  document.querySelectorAll('[data-rev]').forEach(el=>{const s=seg(el,'rev');
    el.style.opacity=eo(s);el.style.transform=`translateY(${(1-eb(s))*3.0}vh)`});
  document.querySelectorAll('[data-fade]').forEach(el=>{el.style.opacity=eo(seg(el,'fade'))});
  document.querySelectorAll('[data-pop]').forEach(el=>{const s=seg(el,'pop');
    el.style.opacity=eo(s);el.style.transform=`scale(${eb(s)})`});
  document.querySelectorAll('[data-grow]').forEach(el=>{
    el.style.width=(eo(seg(el,'grow'))*parseFloat(el.dataset.w))+'%'});
  document.querySelectorAll('[data-drawx]').forEach(el=>{el.style.transform=`scaleX(${eo(seg(el,'drawx'))})`});
  document.querySelectorAll('[data-drawy]').forEach(el=>{el.style.transform=`scaleY(${eo(seg(el,'drawy'))})`});
  document.querySelectorAll('[data-star]').forEach(el=>{const s=seg(el,'star');
    el.style.opacity=eo(s)*parseFloat(el.dataset.op||'0.55');
    el.style.transform=`translate(-50%,-52%) scale(${0.5+eb(s)*0.5}) rotate(${(1-s)*-45}deg)`});
  document.querySelectorAll('[data-gauge]').forEach(el=>{const t=eo(seg(el,'gauge'));
    const pct=parseFloat(el.dataset.pct)*t;
    el.style.background=`conic-gradient(#f4c25c 0 ${pct}%,#2a2218 ${pct}% 100%)`});
  document.querySelectorAll('[data-count]').forEach(el=>{const s=seg(el,'count');
    const tg=parseFloat(el.dataset.target);let v=Math.round(eo(s)*tg);
    let str=el.dataset.comma==='1'?v.toLocaleString('en-US'):(''+v);
    el.textContent=(el.dataset.pre||'')+str+(el.dataset.suf||'');
    el.style.display='inline-block';el.style.transform=`scale(${1+(1-eo(s))*0.08})`});
  void document.body.offsetHeight;
};
(function(){const u=new URLSearchParams(location.search).get('p');
  window.__apply(u==null?1:parseFloat(u))})();
"""


def _star_svg(cls, anim_attr, *, fill="none", stroke="#e8b65a", sw="1.2", style=""):
    st = f' style="{style}"' if style else ""
    return (
        f'<svg class="{cls}" {anim_attr}{st} viewBox="0 0 100 100">'
        f'<path fill="{fill}" stroke="{stroke}" stroke-width="{sw}" stroke-linejoin="round" '
        'd="M50 6 L61 38 L95 38 L67 58 L78 92 L50 71 L22 92 L33 58 L5 38 L39 38 Z"/></svg>'
    )


def _count_or_text(value, cls, *, a, b, style="", unit_cls=None):
    """値を「カウントアップする数字＋接尾辞」か、不可なら静的テキストで返す。"""
    st = f' style="{style}"' if style else ""
    ca = _count_attrs(value)
    if ca and unit_cls and ca["suf"]:
        # 接尾辞を別 class で大きく見せる（stat ヒーロー用）
        return (
            f'<span class="{cls}"{st} data-count="{a:.2f},{b:.2f}" data-target="{ca["target"]}" '
            f'data-pre="{_esc(ca["pre"])}" data-comma="{1 if ca["comma"] else 0}">0</span>'
            f'<span class="{unit_cls}">{_esc(ca["suf"])}</span>'
        )
    if ca:
        return (
            f'<span class="{cls}"{st} data-count="{a:.2f},{b:.2f}" data-target="{ca["target"]}" '
            f'data-pre="{_esc(ca["pre"])}" data-suf="{_esc(ca["suf"])}" '
            f'data-comma="{1 if ca["comma"] else 0}">0</span>'
        )
    return f'<span class="{cls}"{st} data-rev="{a:.2f},{b:.2f}">{_esc(value)}</span>'


def _bar(spec: dict) -> str:
    data = spec.get("data") or []
    mx = max((float(d.get("value") or 0) for d in data), default=1) or 1
    disps = [str(d.get("display") or _fmt(float(d.get("value") or 0))) for d in data]
    longest = max(disps, key=_units, default="")
    vfs = _fit_vw(longest, 20.0, 4.2)
    n = len(data) or 1
    rows = []
    for i, (d, disp) in enumerate(zip(data, disps)):
        v = float(d.get("value") or 0)
        pct = max(4.0, v / mx * 100.0)
        a = 0.16 + i * (0.66 / n)
        b = min(0.94, a + 0.5)
        val = _count_or_text(disp, "bar-val", a=a, b=b, style=f"font-size:{vfs}vw")
        rows.append(
            f'<div class="bar-row" data-fade="{max(0,a-0.1):.2f},{a+0.08:.2f}">'
            f'<div class="bar-label">{_esc(d.get("label"))}</div>'
            f'<div class="bar-track"><div class="bar-fill" data-grow="{a:.2f},{b:.2f}" '
            f'data-w="{pct:.1f}"></div></div>{val}</div>'
        )
    return f'<div class="bars">{"".join(rows)}</div>'


def _stat(spec: dict) -> str:
    val = spec.get("value")
    cap = spec.get("caption")
    # キャプションは長文になりがちなので文節ごとに順次表示（一気に読ませない）。
    cap_html = f'<div class="stat-cap">{_reveal_words(cap, 0.5, 0.95)}</div>' if cap else ""
    pct = _percent(val)
    if pct is not None and 0 <= pct <= 100:
        gauge = (
            f'<div class="gauge" data-gauge=".18,.9" data-pct="{pct:.1f}" '
            f'style="background:conic-gradient(#f4c25c 0 0%,#2a2218 0% 100%)">'
            f'<div class="gauge-hole"></div>'
            f'<div class="gauge-val" data-count=".18,.9" data-target="{int(pct)}" '
            f'data-suf="%" data-comma="0">0</div></div>'
        )
        return f'<div class="stat">{gauge}{cap_html}</div>'
    fs = _fit_vw(val, 70.0, 22.0)
    star = _star_svg("star-bg", 'data-star=".18,.66" data-op="0.55"')
    num = _count_or_text(val, "v", a=0.18, b=0.9, style=f"font-size:{fs}vw", unit_cls="u")
    return f'<div class="stat">{star}<div class="num">{num}</div>{cap_html}</div>'


def _compare(spec: dict) -> str:
    items = spec.get("items") or []
    if _bar_comparable(items):
        mags = [_magnitude(it.get("value")) for it in items]
        mx = max(mags)
        longest = max((str(it.get("value") or "") for it in items), key=_units, default="")
        vfs = _fit_vw(longest, 34.0, 4.6)
        nb = len(items) or 1
        rows = []
        for i, (it, m) in enumerate(zip(items, mags)):
            pct = max(8.0, m / mx * 100.0)
            a = 0.16 + i * (0.66 / nb)
            b = min(0.94, a + 0.5)
            val = _count_or_text(it.get("value"), "cbar-val", a=a, b=b, style=f"font-size:{vfs}vw")
            rows.append(
                f'<div class="cbar-row" data-fade="{max(0,a-0.1):.2f},{a+0.08:.2f}">'
                f'<div class="cbar-label">{_esc(it.get("label"))}</div>'
                f'<div class="cbar-line"><div class="cbar-track">'
                f'<div class="cbar-fill" data-grow="{a:.2f},{b:.2f}" data-w="{pct:.1f}"></div></div>{val}</div></div>'
            )
        return f'<div class="cbars">{"".join(rows)}</div>'
    longest = max((str(it.get("value") or "") for it in items), key=_units, default="")
    fs = _fit_vw(longest, 22.0, 9.0)
    nc = len(items) or 1
    parts = []
    for i, it in enumerate(items):
        a = 0.16 + i * (0.7 / nc)
        b = min(0.94, a + 0.42)
        if i:
            parts.append(f'<div class="cmp-arrow" data-pop="{max(0,a-0.1):.2f},{a+0.06:.2f}">↓</div>')
        val = _count_or_text(it.get("value"), "cmp-val", a=a + 0.04, b=b, style=f"font-size:{fs}vw")
        parts.append(
            f'<div class="cmp-item" data-rev="{a:.2f},{b:.2f}">{val}'
            f'<div class="cmp-label">{_esc(it.get("label"))}</div></div>'
        )
    return f'<div class="compare">{"".join(parts)}</div>'


def _timeline(spec: dict) -> str:
    evs = spec.get("events") or []
    n = len(evs) or 1
    scale = max(0.5, min(1.0, 4.0 / n))
    year_fs = round(3.4 * scale, 2)
    label_fs = round(2.9 * scale, 2)
    dot = round(2.4 * scale, 2)
    rows = []
    for i, e in enumerate(evs):
        # 各出来事を尺いっぱいに割り振り（slot）。ドット→年号→ラベルは文節ごとに順次表示。
        a = 0.12 + i * (0.8 / n)
        slot = (0.8 / n)
        b = min(0.98, a + slot * 0.96)
        star = _star_svg(
            "tl-dot", f'data-pop="{a:.3f},{a + 0.06:.3f}"', fill="#f0c674", stroke="none", sw="0",
            style=f"width:{dot}vw;height:{dot}vw",
        )
        label = _reveal_words(e.get("label"), a + 0.05, b)
        rows.append(
            f'<div class="tl-event">{star}'
            f'<div class="tl-year" data-rev="{a:.3f},{a + 0.1:.3f}" style="font-size:{year_fs}vh">'
            f'{_esc(e.get("year"))}</div>'
            f'<div class="tl-label" style="font-size:{label_fs}vh">{label}</div></div>'
        )
    line = '<div class="tl-line" data-drawy=".1,.92"></div>'
    return f'<div class="timeline">{line}{"".join(rows)}</div>'


_BUILDERS = {"bar": _bar, "stat": _stat, "compare": _compare, "timeline": _timeline}


def _eyebrow(spec: dict, win: str) -> str:
    place = str(spec.get("place") or "").strip()
    if not place:
        return ""
    kicker = _ACT.get(place, "")
    k_html = f'<span class="kicker">{_esc(kicker)}</span>' if kicker else ""
    return (
        f'<div class="eyebrow" data-rev="{win}">'
        f'<span class="act">{_esc(place)}</span><span class="krule"></span>{k_html}</div>'
    )


def _title_html(title) -> str:
    return _esc(title).replace("→", '<span class="em">→</span>')


def _title_fs(title) -> float:
    """見出しを2行以内に収めるフォントサイズ(vh)。長いほど縮小（はみ出し・3行化を防ぐ）。"""
    return round(max(3.4, min(4.6, 62.0 / _units(title))), 2)


def _wsec(a: float, b: float, duration) -> str:
    """秒指定の表示窓を p 分率(0..1)に変換。構造要素(見出し等)を尺に依らず一定秒で素早く出す用。
    duration 不明(静止 p=1 描画)時は近似値で代用（p=1 なので結果に影響しない）。"""
    d = duration if (duration and duration > 0) else 8.0
    return f"{min(0.9, a / d):.3f},{min(0.98, b / d):.3f}"


def _clean_source(s) -> str:
    """出典文字列から「裏取り済み事実/情報」のメタ文言を除く。残りが括弧囲みだけなら外す。"""
    s = str(s or "").strip()
    s = re.sub(r"^(裏取り済み(事実|情報))[\s:：、]*", "", s).strip()
    m = re.fullmatch(r"[（(](.*)[)）]", s)
    if m:
        s = m.group(1).strip()
    return s


# 文節風チャンクの区切り（助詞・読点・閉じ括弧の直後で切る＝読み単位に近づける）
_W_BREAK = set("、。・，！？）」』】〕はがをにへでともやのねよかと")


def _chunk_text(text, maxlen: int = 6) -> list[str]:
    """日本語の長文を「文節風チャンク」に分割（順次表示の単位）。カタカナ語・英数字は
    分断しない。助詞/読点の直後、または maxlen 到達で切る。"""
    text = str(text or "").strip()
    if not text:
        return []
    tokens = re.findall(r"[ァ-ヴー・]+|[A-Za-z0-9%＋\-—,./（）()]+|.", text)
    chunks, cur = [], ""
    for t in tokens:
        cur += t
        if (t[-1] in _W_BREAK and len(cur) >= 2) or len(cur) >= maxlen:
            chunks.append(cur)
            cur = ""
    if cur:
        if chunks and len(cur) <= 1:
            chunks[-1] += cur
        else:
            chunks.append(cur)
    return chunks


def _reveal_words(text, a: float, b: float) -> str:
    """長文を文節チャンクに分け、各チャンクを [a,b] の間で順次フェードさせる span 列に。
    チャンクが1つ以下ならエスケープした素のテキストを返す（短文はそのまま）。"""
    chunks = _chunk_text(text)
    if len(chunks) <= 1:
        return _esc(text)
    n = len(chunks)
    span = max(0.001, b - a)
    rev = max(0.05, span * 0.5 / n)
    step = (span - rev) / max(1, n - 1)
    return "".join(
        f'<span class="w" data-fade="{a + i * step:.3f},{a + i * step + rev:.3f}">{_esc(c)}</span>'
        for i, c in enumerate(chunks)
    )


def _page(spec: dict, body: str, duration=None) -> str:
    css = _BASE_CSS.replace("__GRAIN__", _GRAIN)
    unit = spec.get("unit", "")
    source = _clean_source(spec.get("source", ""))
    # 構造要素(アイブロウ/見出し/罫/単位)は尺に依らず冒頭で素早く出す（固定秒→p分率）。
    # データ(本体)は p 分率のまま尺いっぱいに広がり、切替の少し前(p≈1)に完了する。
    unit_html = f'<div class="unit" data-rev="{_wsec(0.5, 1.6, duration)}">{_esc(unit)}</div>' if unit else ""
    src_html = (f'<div class="source" data-rev=".88,1">出典: {_esc(source)}</div>'
                if source else "")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>" + css + "</style></head><body>"
        "<div class='grain'></div><div class='vig'></div><div class='frame'></div>"
        "<div class='wrap'>"
        + _eyebrow(spec, _wsec(0.0, 0.7, duration))
        + f'<div class="title" data-rev="{_wsec(0.25, 1.7, duration)}" '
        + f'style="font-size:{_title_fs(spec.get("title", ""))}vh">'
        + f'{_title_html(spec.get("title", ""))}</div>'
        + f'<div class="trule" data-drawx="{_wsec(0.7, 2.0, duration)}"></div>'
        + unit_html
        + f'<div class="body">{body}</div>'
        + src_html
        + "</div><script>" + _ANIM_JS + "</script></body></html>"
    )


def chart_html(spec: dict, duration=None) -> str:
    builder = _BUILDERS.get(spec.get("type", ""))
    if not builder:
        raise ValueError(f"未対応の chart type: {spec.get('type')}")
    return _page(spec, builder(spec), duration)


# ===== 描画 =====

_VID_FPS = 12       # 入場アニメの fps（尺いっぱいの緩やかなビルドなので低めで十分）
_VID_LEAD = 0.6     # アニメはシーン末尾の lead 秒前に完了（残りは compose が最終フレームを静止保持）


def _chrome_shot(html_path: Path, out_png: Path, w: int, h: int, budget: int = 1500) -> bool:
    cmd = [
        _CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", f"--window-size={w},{h}",
        f"--virtual-time-budget={budget}", f"--screenshot={out_png}", f"file://{html_path}",
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return Path(out_png).exists()


def render_chart(spec: dict, out_png: Path, width: int | None = None, height: int | None = None) -> Path:
    """chart 仕様を out_png に描画して返す（最終状態 p=1 の静止 PNG）。"""
    W = width or config.VIDEO_WIDTH
    H = height or config.VIDEO_HEIGHT
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="doci_chart_") as td:
        hp = Path(td) / "chart.html"
        hp.write_text(chart_html(spec), encoding="utf-8")
        if not _chrome_shot(hp, out_png, W, H, budget=800):
            raise RuntimeError("Chrome 描画失敗（PNG 生成されず）")
    return out_png


class _CDP:
    """Chrome DevTools Protocol を pipe(FD3/4)経由で駆動。1 Chrome セッションで多数フレームを
    撮影できる（毎フレーム Chrome 起動＝コールドスタートを避け、尺いっぱいの長尺アニメを高速に）。"""

    def __init__(self, w: int, h: int):
        cmd_r, self._cmd_w = os.pipe()
        self._resp_r, resp_w = os.pipe()
        for fd in (cmd_r, resp_w):
            os.set_inheritable(fd, True)
        fa = [(os.POSIX_SPAWN_DUP2, cmd_r, 3), (os.POSIX_SPAWN_DUP2, resp_w, 4)]
        self._pid = os.posix_spawn(
            _CHROME,
            [_CHROME, "--headless=new", "--disable-gpu", "--remote-debugging-pipe",
             "--force-device-scale-factor=1", f"--window-size={w},{h}",
             "--hide-scrollbars", "about:blank"],
            os.environ, file_actions=fa,
        )
        os.close(cmd_r); os.close(resp_w)
        self._buf = b""
        self._id = 0

    def _read(self):
        while b"\0" not in self._buf:
            chunk = os.read(self._resp_r, 1 << 20)
            if not chunk:
                raise RuntimeError("CDP pipe closed")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method, params=None, sid=None, timeout=30):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        os.write(self._cmd_w, json.dumps(msg).encode() + b"\0")
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = self._read()
            if m.get("id") == self._id:
                if "error" in m:
                    raise RuntimeError(f"CDP {method}: {m['error']}")
                return m.get("result", {})
        raise TimeoutError(f"CDP {method}")

    def wait_event(self, method, sid=None, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = self._read()
            if m.get("method") == method and (sid is None or m.get("sessionId") == sid):
                return m.get("params", {})
        raise TimeoutError(f"CDP event {method}")

    def close(self):
        try:
            os.close(self._cmd_w); os.close(self._resp_r)
        except Exception:
            pass
        try:
            os.kill(self._pid, signal.SIGTERM)
            os.waitpid(self._pid, 0)
        except Exception:
            pass


def render_chart_video(
    spec: dict, out_mp4: Path, duration: float,
    width: int | None = None, height: int | None = None, fps: int = _VID_FPS,
) -> Path:
    """入場アニメ(p:0→1)を `duration` 秒かけて描く mp4 を返す。
    CDP で1セッション・毎フレーム window.__apply(p) を評価して撮影（尺いっぱいの緩やかなビルド）。
    呼び出し側は duration にシーン尺−lead を渡す想定（アニメが切替の少し前に完了し、
    残りは compose の freeze_tail が最終フレームを静止保持する）。"""
    W = width or config.VIDEO_WIDTH
    H = height or config.VIDEO_HEIGHT
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    duration = max(1.0, float(duration))
    frames = max(2, round(duration * fps))
    with tempfile.TemporaryDirectory(prefix="doci_chartvid_") as td:
        td = Path(td)
        hp = td / "chart.html"
        hp.write_text(chart_html(spec, duration=duration), encoding="utf-8")
        fdir = td / "frames"
        fdir.mkdir()
        cdp = _CDP(W, H)
        try:
            cdp.call("Browser.getVersion")
            tgt = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            sid = cdp.call("Target.attachToTarget", {"targetId": tgt, "flatten": True})["sessionId"]
            cdp.call("Page.enable", sid=sid)
            cdp.call("Runtime.enable", sid=sid)
            # viewport を厳密に W×H に固定（headless 既定だと高さがズレ＝奇数になり yuv420p で失敗する）。
            cdp.call("Emulation.setDeviceMetricsOverride",
                     {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False}, sid=sid)
            cdp.call("Page.navigate", {"url": f"file://{hp}"}, sid=sid)
            cdp.wait_event("Page.loadEventFired", sid=sid)
            for i in range(frames):
                p = i / (frames - 1)
                cdp.call("Runtime.evaluate", {"expression": f"window.__apply({p:.5f})"}, sid=sid)
                # JPEG は PNG より大幅に速くエンコードでき、最終的に H.264 へ載るので品質差は無視できる。
                shot = cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": 92}, sid=sid)
                (fdir / f"f{i:04d}.jpg").write_bytes(base64.b64decode(shot["data"]))
        finally:
            cdp.close()
        r = subprocess.run(
            ["ffmpeg", "-y", "-framerate", str(fps), "-i", str(fdir / "f%04d.jpg"),
             "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-r", str(fps), str(out_mp4)],
            capture_output=True, text=True, timeout=300,
        )
    if not out_mp4.exists():
        raise RuntimeError(f"chart 動画生成失敗: {r.stderr[-400:]}")
    return out_mp4
