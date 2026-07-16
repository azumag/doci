"""図表・グラフを HTML→画像/動画で生成（解説シーン用）。

AI画像生成は文字・数値が崩れて図表に不適。HTML/CSS/SVG で正確・鮮明・スタイル統一して
作り、Chrome ヘッドレスで描画する。chart 仕様(dict)を受け取る。

- render_chart(spec, out_png)        … 最終状態(p=1)の静止 PNG（後方互換・フォールバック）
- render_chart_video(spec, out_mp4)  … 入場アニメ(0→1)を mp4 化（棒伸び/カウントアップ/順次出現）

デザイン: 墨地＋ヴィネット＋微細グレイン、明朝の見出し、ゴールド(ミシュランの星)＋
転換のレッド。アニメは ?p=0..1 を JS が読み、その進捗時点の1フレームを描く
（Chrome を p ごと、または iframe フィルムストリップで撮影）。

レイアウト: `_avail_vh()` が見出し・単位行を差し引いた本体の利用可能な縦領域(vh)を見積もり、
各ビルダーは項目数から自然高さを算出して `s=min(1, budget/natural)` を行高・フォント・
gap に掛け、下限フロアで可読性を保つ（項目数が多くてもはみ出さない）。
spec の "place"（起承転結）フィールドは受け取っても画面には表示しない。

chart 仕様の例:
  {"type":"bar",   "title":..., "unit":..., "data":[{"label":..,"value":N}...], "source":..}
  {"type":"stat",  "title":..., "value":"7フラン", "caption":.., "source":..}
  {"type":"compare","title":.., "items":[{"label":..,"value":".."}...], "source":..}
  {"type":"timeline","title":.., "events":[{"year":"1924","label":..}...], "source":..}
  {"type":"donut", "title":..., "unit":..., "items":[{"label":..,"value":38.6,"display":"38.6%"}...], "source":..}
  {"type":"line",  "title":..., "unit":..., "points":[{"x":"1997","y":85.6,"display":"85.6万台"}...], "source":..}
"""
from __future__ import annotations

import base64
import html
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import config
from .channel import ChartStyle

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
/* 背景写真/動画の上に敷く暗幕（文字可読性を確保しつつ背景は透かす） */
.scrim{position:fixed;inset:0;pointer-events:none;
  background:linear-gradient(180deg,rgba(8,7,9,.74) 0%,rgba(8,7,9,.5) 38%,rgba(8,7,9,.7) 70%,rgba(8,7,9,.86) 100%)}
/* 下40vhは字幕帯として確実に空ける */
.wrap{position:relative;width:100%;height:100%;padding:7vh 9vw 40vh 9vw;
  display:flex;flex-direction:column}
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
/* 出典は画面最下部に小さく（字幕帯より下）。本体レイアウトからは外して常に一番下に固定。
   長い出典でも折り返さず常に1行に収める（Python側で40字に切り詰め済み）。 */
.source{position:absolute;left:9vw;right:9vw;bottom:2.4vh;font-size:1.7vh;color:#6f6657;
  text-align:center;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* ---- stat（文脈リード→数字がドンと出るステートメント / 割合はリングゲージ） ---- */
.stat{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2.8vh}
.stat-lead{font-size:3.1vh;line-height:1.5;color:#e9e1d2;text-align:center;max-width:80vw;
  margin:0 auto;font-feature-settings:"palt";text-shadow:0 .2vh 1.2vh rgba(0,0,0,.85)}
.stat-hero{position:relative;display:flex;align-items:center;justify-content:center;align-self:stretch}
.star-bg{position:absolute;width:32vh;height:32vh;top:50%;left:50%;
  transform:translate(-50%,-50%);filter:drop-shadow(0 0 3vh rgba(232,182,90,.2))}
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
.bars{display:flex;flex-direction:column;justify-content:center;gap:4.6vh}
.bar-label{font-size:2.9vh;color:#e7e1d4;margin-bottom:1.4vh;line-height:1.3}
.bar-line{display:flex;align-items:center;gap:2.5vw}
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
/* ---- timeline（出来事カード＋「ぐーん」と伸びる矢印で次へ繋ぐ） ---- */
.timeline{flex:1;display:flex;flex-direction:column;justify-content:center;gap:0}
.tl-card{display:flex;align-items:baseline;gap:2.6vw;
  background:linear-gradient(135deg,#241b11,#16110a);border:.14vh solid #3d3122;
  border-left:.7vh solid #e8b65a;border-radius:1.3vh;padding:1.5vh 4vw;
  box-shadow:0 1vh 2.4vh rgba(0,0,0,.45);transform-origin:left center}
.tl-card .y{font-family:'Hiragino Mincho ProN',serif;font-weight:700;color:#f4c25c;
  flex-shrink:0;white-space:nowrap}
.tl-card .t{color:#ece6d8;line-height:1.3}
.tl-arrow{display:flex;flex-direction:column;align-items:center;align-self:flex-start;margin-left:5.5vw}
.tl-stem{width:.55vh;background:linear-gradient(180deg,#f0b450,#caa05a);transform-origin:top;
  border-radius:.55vh;box-shadow:0 0 1vh rgba(240,180,80,.45)}
.tl-head{width:0;height:0;border-left:1.2vh solid transparent;border-right:1.2vh solid transparent;
  border-top:1.5vh solid #f0b450;margin-top:-.1vh}
/* ---- donut（構成比・シェア） ---- */
.donut-block{flex:1;display:flex;align-items:center;justify-content:center;gap:6vw}
.donut-ringwrap{position:relative;border-radius:50%;flex-shrink:0;
  box-shadow:0 0 3vh rgba(232,182,90,.15)}
.donut{position:absolute;inset:0;border-radius:50%;background:conic-gradient(#2a2218 0 100%)}
.donut-hole{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);border-radius:50%;
  background:radial-gradient(circle,#15110b 0%,#0d0b08 100%);display:flex;align-items:center;
  justify-content:center;box-shadow:inset 0 .3vh 1vh rgba(0,0,0,.5)}
.donut-center{font-family:'Hiragino Mincho ProN',serif;font-weight:800;font-size:4.4vh;
  color:#f4c25c;text-align:center}
.donut-legend{display:flex;flex-direction:column;gap:1.8vh}
.donut-item{display:flex;align-items:center;gap:1.2vw}
.donut-chip{width:1.8vh;height:1.8vh;border-radius:.4vh;flex-shrink:0}
.donut-label{font-size:2.5vh;color:#cfc8ba;white-space:nowrap}
.donut-val{font-size:2.5vh;color:#f4c25c;font-weight:800;margin-left:.6vw;white-space:nowrap}
/* ---- line（推移・折れ線） ---- */
.line-block{flex:1;display:flex;align-items:center;justify-content:center}
.line-block svg{display:block}
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
  document.querySelectorAll('[data-stamp]').forEach(el=>{const s=seg(el,'stamp');
    el.style.opacity=eo(s);el.style.transform=`scale(${1.34-eo(s)*0.34})`});
  document.querySelectorAll('[data-line]').forEach(el=>{const s=eo(seg(el,'line'));
    el.style.strokeDasharray='1';el.style.strokeDashoffset=String(1-s)});
  document.querySelectorAll('[data-donut]').forEach(el=>{
    const stops=JSON.parse(el.dataset.donut);const s=eo(seg(el,'dwin'));
    const total=stops.length?stops[stops.length-1][1]:100;const sweep=total*s;
    const css=[];
    stops.forEach(([a,b,color])=>{const bb=Math.max(a,Math.min(b,sweep));
      css.push(`${color} ${(a/total*100).toFixed(3)}% ${(bb/total*100).toFixed(3)}%`)});
    if(sweep<total)css.push(`#2a2218 ${(sweep/total*100).toFixed(3)}% 100%`);
    el.style.background=`conic-gradient(${css.join(',')})`});
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


# .wrap の左右 padding 9vw ずつを除いた内容幅(vw)
_CONTENT_VW = 82.0
# デザインの基準となる長辺(px)。9:16(1080x1920)で全フォント/間隔(vh指定)をチューニング済み。
_REF_LONG_EDGE = 1920.0


def _vh2vw(w: int, h: int) -> float:
    """vh→vw 換算係数(1vh が何vwか)。実際の解像度から動的に算出(旧: 9:16固定の1.778)。"""
    return h / w if w else 1.778


def _scale(w: int, h: int) -> float:
    """vh基準の文字/間隔サイズを実解像度に合わせて拡大する係数。長辺は常に1920(縦横入替のみ)
    なので、縦9:16(h=1920)なら1.0(無変更)、横16:9(h=1080)なら1920/1080≈1.778倍し、
    vh指定の絶対px相当サイズを両向きで揃える（横向きだと短辺化するhでvhの実pxが縮む問題を補正）。"""
    return round(_REF_LONG_EDGE / h, 4) if h else 1.0


def _avail_vh(spec: dict, w: int, h: int) -> float:
    """本体(.body)が使える縦領域(vh)を見積もる。.wrap は上7vh・下40vh(字幕帯)を確保しており、
    残りから見出し(タイトル行数×行高＋罫)・単位行・安全マージンを差し引いた分が本体の予算。
    タイトル行数は実際の vh→vw 比・タイトル幅82vwから概算する。"""
    title = spec.get("title", "")
    fs = _title_fs(title, w, h)
    lines = max(1, math.ceil(_units(title) * fs * _vh2vw(w, h) / _CONTENT_VW))
    head = lines * fs * 1.25 + 1.4 + 0.32
    unit_h = (_unit_fs(w, h) * 1.4 + 0.8) if spec.get("unit") else 0.0
    return 100.0 - 7.0 - 40.0 - head - unit_h - 1.5


def _unit_fs(w: int, h: int) -> float:
    return round(2.3 * _scale(w, h), 2)


def _fit_scale(natural: float, budget: float, floor: float) -> float:
    """項目数から算出した自然高さ(natural, vh)を budget(vh) に収める倍率。フロアで可読性を保つ。
    floor は縦9:16のゆとりある budget を前提にした可読性の下限であり、横16:9等 budget が
    そもそも floor を満たせないほど小さい場合にまで適用すると本体が枠から溢れる
    （issue #12: 長尺で見出し/字幕帯に食い込む原因）。floor で収まらない時は、はみ出しより
    フィットを優先してさらに縮める（下限0.15は極端な項目数での崩壊のみ防ぐ最終防波堤）。"""
    if natural <= 0:
        return 1.0
    ratio = budget / natural
    if ratio >= floor:
        return round(min(1.0, ratio), 4)
    return round(max(0.15, ratio), 4)


# 各ビルダーの「自然高さ(scale=1時, vh)」算出に使うCSSの基準値（.bar-label/.bar-track/.bars gap）。
_BAR_LABEL_FS, _BAR_LABEL_MB, _BAR_TRACK_H, _BAR_GAP = 2.9, 1.4, 6.4, 4.6


def _bar(spec: dict, w: int, h: int) -> str:
    scale = _scale(w, h)
    data = spec.get("data") or []
    mx = max((float(d.get("value") or 0) for d in data), default=1) or 1
    disps = [str(d.get("display") or _fmt(float(d.get("value") or 0))) for d in data]
    longest = max(disps, key=_units, default="")
    # vw指定は実際の横幅に直結するため、横16:9等 w>1080 では素の値のまま行の高さ(vh基準の
    # トラック)を上回り得る(issue #12: 値テキストが行を突き破る)。scaleで逆補正し絶対px相当を揃え、
    # さらに s も掛けてtrack/labelと同じ比率で縮む(項目数増でtrackだけ縮み値だけ据置→再度溢れるのを防ぐ)。
    vfs_b = _fit_vw(longest, 20.0, 4.2) / scale
    n = len(data) or 1
    label_fs_b, label_mb_b, track_h_b, gap_b = (
        _BAR_LABEL_FS * scale, _BAR_LABEL_MB * scale, _BAR_TRACK_H * scale, _BAR_GAP * scale
    )
    natural = n * (label_fs_b * 1.3 + label_mb_b + track_h_b) + (n - 1) * gap_b
    s = _fit_scale(natural, _avail_vh(spec, w, h), 0.55)
    label_fs = round(label_fs_b * s, 2)
    label_mb = round(label_mb_b * s, 2)
    track_h = round(track_h_b * s, 2)
    gap = round(gap_b * s, 2)
    vfs = round(vfs_b * s, 2)
    rows = []
    for i, (d, disp) in enumerate(zip(data, disps)):
        v = float(d.get("value") or 0)
        pct = max(4.0, v / mx * 100.0)
        a = 0.16 + i * (0.66 / n)
        b = min(0.94, a + 0.5)
        val = _count_or_text(disp, "bar-val", a=a, b=b, style=f"font-size:{vfs}vw")
        rows.append(
            f'<div class="bar-row" data-fade="{max(0,a-0.1):.2f},{a+0.08:.2f}">'
            f'<div class="bar-label" style="font-size:{label_fs}vh;margin-bottom:{label_mb}vh">'
            f'{_esc(d.get("label"))}</div>'
            f'<div class="bar-line"><div class="bar-track" style="height:{track_h}vh"><div class="bar-fill" '
            f'data-grow="{a:.2f},{b:.2f}" data-w="{pct:.1f}"></div></div>{val}</div></div>'
        )
    return f'<div class="bars" style="gap:{gap}vh">{"".join(rows)}</div>'


def _num_inner(val) -> str:
    """値を「数値＋接尾辞(単位)」の span に。カウントしない最終表示用。"""
    ca = _count_attrs(val)
    if ca and ca["suf"]:
        num = f'{ca["target"]:,}' if ca["comma"] else str(ca["target"])
        return f'<span class="v">{_esc(ca["pre"])}{num}</span><span class="u">{_esc(ca["suf"])}</span>'
    return f'<span class="v">{_esc(val)}</span>'


def _stat(spec: dict, w: int, h: int) -> str:
    val = spec.get("value")
    cap = spec.get("caption")
    pct = _percent(val)
    budget = _avail_vh(spec, w, h)
    scale = _scale(w, h)
    lead_fs = round(3.1 * scale, 2)
    # 割合(%)はリングゲージで視覚化する価値がある（量の比較）。文脈を先に、ゲージは後で満ちる。
    if pct is not None and 0 <= pct <= 100:
        lead = (f'<div class="stat-lead" style="font-size:{lead_fs}vh">'
                f'{_reveal_words(cap, 0.12, 0.55)}</div>') if cap else ""
        # ゲージ(既定34vh基準×scale)がbudgetを超える時だけ軽く縮める。
        gauge_vh = round(min(34.0 * scale, budget * 0.75), 2)
        hole_vh = round(gauge_vh * (24.0 / 34.0), 2)
        gauge = (
            f'<div class="gauge" data-gauge=".5,.9" data-pct="{pct:.1f}" '
            f'style="background:conic-gradient(#f4c25c 0 0%,#2a2218 0% 100%);'
            f'width:{gauge_vh}vh;height:{gauge_vh}vh">'
            f'<div class="gauge-hole" style="width:{hole_vh}vh;height:{hole_vh}vh"></div>'
            f'<div class="gauge-val" data-count=".5,.9" data-target="{int(pct)}" '
            f'data-suf="%" data-comma="0">0</div></div>'
        )
        return f'<div class="stat">{lead}<div class="stat-hero">{gauge}</div></div>'
    # 単一の事実(例: 7フラン)はカウントせず、「文脈(キャプション)→数字がドンと出る」ステートメントに。
    fs = _fit_vw(val, 70.0, 22.0)
    # 数字(vw指定)がbudget(vh)を超える時だけ軽く縮める（実際の vh≈vw換算）。
    fs = round(min(fs, budget * _vh2vw(w, h) * 0.75), 2)
    lead = (f'<div class="stat-lead" style="font-size:{lead_fs}vh">'
            f'{_reveal_words(cap, 0.12, 0.62)}</div>') if cap else ""
    star = _star_svg("star-bg", 'data-star=".6,.92" data-op="0.5"')
    num = f'<div class="num" data-stamp=".62,.84" style="font-size:{fs}vw">{_num_inner(val)}</div>'
    return f'<div class="stat">{lead}<div class="stat-hero">{star}{num}</div></div>'


# cbar(同単位比較棒)の基準値。bar と同様の考え方だが cbar 独自の CSS 値を使う。
_CBAR_LABEL_FS, _CBAR_LABEL_MB, _CBAR_TRACK_H, _CBAR_GAP = 3.0, 1.4, 6.0, 5.0
# compare(カード型)の基準値。
_CMP_PAD_V, _CMP_LABEL_FS, _CMP_VAL_FS, _CMP_ARROW_FS, _CMP_GAP = 2.4, 2.9, 5.1, 3.8, 1.4


def _compare(spec: dict, w: int, h: int) -> str:
    items = spec.get("items") or []
    budget = _avail_vh(spec, w, h)
    scale = _scale(w, h)
    if _bar_comparable(items):
        mags = [_magnitude(it.get("value")) for it in items]
        mx = max(mags)
        longest = max((str(it.get("value") or "") for it in items), key=_units, default="")
        # issue #12: vw値の横長補正＋sで縮小(_barと同様、項目数増でtrackだけ縮み値だけ据置を防ぐ)
        vfs_b = _fit_vw(longest, 34.0, 4.6) / scale
        nb = len(items) or 1
        label_fs_b, label_mb_b, track_h_b, gap_b = (
            _CBAR_LABEL_FS * scale, _CBAR_LABEL_MB * scale, _CBAR_TRACK_H * scale, _CBAR_GAP * scale
        )
        natural = nb * (label_fs_b * 1.3 + label_mb_b + track_h_b) + (nb - 1) * gap_b
        s = _fit_scale(natural, budget, 0.55)
        label_fs = round(label_fs_b * s, 2)
        label_mb = round(label_mb_b * s, 2)
        track_h = round(track_h_b * s, 2)
        gap = round(gap_b * s, 2)
        vfs = round(vfs_b * s, 2)
        rows = []
        for i, (it, m) in enumerate(zip(items, mags)):
            pct = max(8.0, m / mx * 100.0)
            a = 0.16 + i * (0.66 / nb)
            b = min(0.94, a + 0.5)
            val = _count_or_text(it.get("value"), "cbar-val", a=a, b=b, style=f"font-size:{vfs}vw")
            rows.append(
                f'<div class="cbar-row" data-fade="{max(0,a-0.1):.2f},{a+0.08:.2f}">'
                f'<div class="cbar-label" style="font-size:{label_fs}vh;margin-bottom:{label_mb}vh">'
                f'{_esc(it.get("label"))}</div>'
                f'<div class="cbar-line"><div class="cbar-track" style="height:{track_h}vh">'
                f'<div class="cbar-fill" data-grow="{a:.2f},{b:.2f}" data-w="{pct:.1f}"></div></div>{val}</div></div>'
            )
        return f'<div class="cbars" style="gap:{gap}vh">{"".join(rows)}</div>'
    longest = max((str(it.get("value") or "") for it in items), key=_units, default="")
    fs = round(_fit_vw(longest, 22.0, 9.0) / scale, 2)  # issue #12: vw値の横長補正(_barと同様)
    nc = len(items) or 1
    pad_v_b, label_fs_b, val_fs_b, arrow_fs_b, gap_b = (
        _CMP_PAD_V * scale, _CMP_LABEL_FS * scale, _CMP_VAL_FS * scale,
        _CMP_ARROW_FS * scale, _CMP_GAP * scale
    )
    natural = nc * (pad_v_b * 2 + max(label_fs_b * 1.3, val_fs_b)) + (nc - 1) * (
        arrow_fs_b + gap_b * 2
    )
    s = _fit_scale(natural, budget, 0.45)
    pad_v = round(pad_v_b * s, 2)
    label_fs = round(label_fs_b * s, 2)
    arrow_fs = round(arrow_fs_b * s, 2)
    gap = round(gap_b * s, 2)
    fs = round(fs * s, 2)
    parts = []
    for i, it in enumerate(items):
        a = 0.16 + i * (0.7 / nc)
        b = min(0.94, a + 0.42)
        if i:
            parts.append(
                f'<div class="cmp-arrow" style="font-size:{arrow_fs}vh" '
                f'data-pop="{max(0,a-0.1):.2f},{a+0.06:.2f}">↓</div>'
            )
        val = _count_or_text(it.get("value"), "cmp-val", a=a + 0.04, b=b, style=f"font-size:{fs}vw")
        parts.append(
            f'<div class="cmp-item" style="padding:{pad_v}vh 5vw" data-rev="{a:.2f},{b:.2f}">{val}'
            f'<div class="cmp-label" style="font-size:{label_fs}vh">{_esc(it.get("label"))}</div></div>'
        )
    return f'<div class="compare" style="gap:{gap}vh">{"".join(parts)}</div>'


# timeline の基準値(scale=1時)。cardpad は上下2回、stem+head+マージンがイベント間の間隔。
_TL_YEAR_FS, _TL_LABEL_FS, _TL_STEM_H, _TL_CARDPAD, _TL_HEAD_H, _TL_HEAD_W = 3.0, 2.55, 1.9, 1.5, 1.5, 1.2
_TL_ARROW_MARGIN = 0.3  # ステム/矢頭間の見た目の余白(近似)


def _timeline(spec: dict, w: int, h: int) -> str:
    evs = spec.get("events") or []
    n = len(evs) or 1
    scale = _scale(w, h)
    year_fs_b, label_fs_b, stem_h_b, cardpad_b, head_h_b, head_w_b = (
        _TL_YEAR_FS * scale, _TL_LABEL_FS * scale, _TL_STEM_H * scale,
        _TL_CARDPAD * scale, _TL_HEAD_H * scale, _TL_HEAD_W * scale
    )
    arrow_margin_b = _TL_ARROW_MARGIN * scale
    natural = n * (cardpad_b * 2 + max(year_fs_b, label_fs_b * 1.3)) + (n - 1) * (
        stem_h_b + head_h_b + arrow_margin_b
    )
    s = _fit_scale(natural, _avail_vh(spec, w, h), 0.42)
    year_fs = round(year_fs_b * s, 2)
    label_fs = round(label_fs_b * s, 2)
    stem_h = round(stem_h_b * s, 2)      # 矢印(stem)の長さ(vh)
    cardpad = round(cardpad_b * s, 2)
    head_h = round(head_h_b * s, 2)
    head_w = round(head_w_b * s, 2)
    parts = []
    for i, e in enumerate(evs):
        # 各出来事を尺いっぱいに割り振り。カードがぐっと出る→矢印が次カードへ「ぐーん」と伸びる。
        a = 0.1 + i * (0.82 / n)
        slot = 0.82 / n
        label = _reveal_words(e.get("label"), a + 0.02, min(0.98, a + slot * 0.82))
        parts.append(
            f'<div class="tl-card" data-rev="{a:.3f},{a + slot * 0.4:.3f}" style="padding:{cardpad}vh 4vw">'
            f'<span class="y" style="font-size:{year_fs}vh">{_esc(e.get("year"))}</span>'
            f'<span class="t" style="font-size:{label_fs}vh">{label}</span></div>'
        )
        if i < len(evs) - 1:
            ar_a = a + slot * 0.45
            ar_b = min(0.99, a + slot * 0.95)
            parts.append(
                f'<div class="tl-arrow">'
                f'<div class="tl-stem" data-drawy="{ar_a:.3f},{ar_b:.3f}" style="height:{stem_h}vh"></div>'
                f'<div class="tl-head" data-pop="{max(0, ar_b - 0.03):.3f},{ar_b + 0.03:.3f}" '
                f'style="border-left:{head_w}vh solid transparent;border-right:{head_w}vh solid transparent;'
                f'border-top:{head_h}vh solid #f0b450"></div></div>'
            )
    return f'<div class="timeline">{"".join(parts)}</div>'


_DONUT_COLORS = ["#f4c25c", "#d8503a", "#c9772a", "#e9e1d2", "#8a6b3d"]
_DONUT_GAP_VW = 6.0    # .donut-block の gap（リング↔凡例）
_DONUT_LEG_FS = 2.5    # 凡例 label/val の基準フォント(vh)
_DONUT_CHIP = 1.8      # 色チップの基準サイズ(vh)


def _donut_layout(items: list, disps: list, budget: float, w: int, h: int) -> tuple[float, float, float, float]:
    """(リング径vh, 凡例フォントvh, チップvh, 凡例幅vw) を決める。
    リング径(vh→vw換算) + gap + 凡例幅 が内容幅82vwを超えないよう、まず凡例を
    ~34vwに縮め(フロア0.6)、残り幅とbudgetの小さい方でリング径を決める。"""
    scale = _scale(w, h)
    vh2vw = _vh2vw(w, h)
    leg_fs_b, chip_b = _DONUT_LEG_FS * scale, _DONUT_CHIP * scale
    if items:
        # 行幅(vw) = チップ + flex gap(チップ↔label) + label + flex gap(label↔val) + val margin + val
        raw_w = max(
            chip_b * vh2vw + 1.2
            + _units(it.get("label")) * leg_fs_b * vh2vw
            + 1.2 + 0.6
            + _units(d) * leg_fs_b * vh2vw
            for it, d in zip(items, disps)
        )
    else:
        raw_w = 0.0
    ls = max(0.6, min(1.0, 34.0 / raw_w)) if raw_w > 34.0 else 1.0
    legend_w = round(raw_w * ls, 2)
    size = round(min(34.0 * scale, budget * 0.72, (_CONTENT_VW - _DONUT_GAP_VW - legend_w) / vh2vw), 2)
    return size, round(leg_fs_b * ls, 2), round(chip_b * ls, 2), legend_w


def _donut(spec: dict, w: int, h: int) -> str:
    items = spec.get("items") or []
    vals = [max(0.0, float(it.get("value") or 0)) for it in items]
    total = sum(vals) or 1.0
    n = len(items) or 1
    disps = [str(it.get("display") or _fmt(vals[i])) for i, it in enumerate(items)]
    size, leg_fs, chip, _legend_w = _donut_layout(items, disps, _avail_vh(spec, w, h), w, h)
    hole = round(size * (24.0 / 34.0), 2)
    center_fs = round(4.4 * size / 34.0, 2)
    # 累積ストップ(0..100%)。__apply が進捗sに応じ 0→各ストップへスイープする conic-gradient を生成。
    stops = []
    acc = 0.0
    for v in vals:
        stops.append((round(acc / total * 100, 3), round((acc + v) / total * 100, 3)))
        acc += v
    max_i = max(range(len(items)), key=lambda i: vals[i]) if items else 0
    donut_stops = [
        [stops[i][0], stops[i][1], _DONUT_COLORS[i % len(_DONUT_COLORS)]]
        for i in range(len(items))
    ]
    legend_rows = []
    for i, (it, disp) in enumerate(zip(items, disps)):
        color = _DONUT_COLORS[i % len(_DONUT_COLORS)]
        a = 0.2 + i * (0.6 / n)
        b = min(0.96, a + 0.6 / n + 0.1)
        legend_rows.append(
            f'<div class="donut-item" data-rev="{a:.2f},{b:.2f}">'
            f'<span class="donut-chip" style="background:{color};width:{chip}vh;height:{chip}vh"></span>'
            f'<span class="donut-label" style="font-size:{leg_fs}vh">{_esc(it.get("label"))}</span>'
            f'<span class="donut-val" style="font-size:{leg_fs}vh">{_esc(disp)}</span></div>'
        )
    center_disp = disps[max_i] if items else ""
    donut_json = json.dumps(donut_stops)
    ring = (
        f'<div class="donut-ringwrap" style="width:{size}vh;height:{size}vh">'
        f"<div class=\"donut\" data-donut='{donut_json}' data-dwin=\".15,.85\" "
        f'style="width:{size}vh;height:{size}vh"></div>'
        f'<div class="donut-hole" style="width:{hole}vh;height:{hole}vh">'
        f'<div class="donut-center" data-stamp=".75,.95" style="font-size:{center_fs}vh">'
        f"{_esc(center_disp)}</div></div></div>"
    )
    return f'<div class="donut-block">{ring}<div class="donut-legend">{"".join(legend_rows)}</div></div>'


def _line(spec: dict, w: int, h: int) -> str:
    points = spec.get("points") or []
    n = len(points) or 1
    budget = _avail_vh(spec, w, h)
    blk_h = round(min(38.0 * _scale(w, h), budget * 0.9), 2)
    ys = [float(p.get("y") or 0) for p in points] or [0.0]
    y_lo, y_hi = min(ys), max(ys)
    span = y_hi - y_lo
    if span <= 0:
        span = max(abs(y_hi), 1.0)  # 全点同一yでも0除算しない
    pad = span * 0.1
    plo, phi = y_lo - pad, y_hi + pad
    pspan = (phi - plo) or 1.0
    VBW, VBH = 100.0, 60.0
    X0, X1 = 10.0, 90.0  # x範囲を内側に寄せ、端点のラベルが viewBox 左右で見切れないようにする
    xs = [X0 + (i / (n - 1) if n > 1 else 0.5) * (X1 - X0) for i in range(n)]
    yc = [VBH - ((y - plo) / pspan) * VBH for y in ys]
    path_d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, yc))
    area_d = path_d + f" L {xs[-1]:.2f},{VBH:.2f} L {xs[0]:.2f},{VBH:.2f} Z"
    max_i = max(range(n), key=lambda i: ys[i])
    emphasize = {0, n - 1, max_i}
    line_a, line_b = 0.10, 0.82

    def _t(i):
        f = i / (n - 1) if n > 1 else 0.5
        return line_a + f * (line_b - line_a)

    step = 2 if n >= 8 else 1
    markers, labels, xlabels = [], [], []
    for i, p in enumerate(points):
        t = _t(i)
        a, b = max(0.0, t - 0.02), min(0.98, t + 0.06)
        is_last = i == n - 1
        r = "1.6" if is_last else "1.1"
        color = "#d8503a" if is_last else "#f4c25c"
        markers.append(
            f'<circle cx="{xs[i]:.2f}" cy="{yc[i]:.2f}" r="{r}" fill="{color}" '
            f'data-pop="{a:.3f},{b:.3f}"/>'
        )
        # 端点のラベルは外側にはみ出さないよう、最初=start / 最後=end / 中間=middle で寄せる。
        anchor = "start" if i == 0 else ("end" if is_last else "middle")
        if i in emphasize:
            disp = p.get("display") or _fmt(ys[i])
            fw = "800" if is_last else "600"
            fsize = 5.6 if is_last else 4.4
            fill = "#d8503a" if is_last else "#f4c25c"
            # 点の上に置き、viewBox 上端(y=0)から出るなら点の下へ（baseline≈フォント高でクランプ）。
            y_text = yc[i] - 3.5
            if y_text < fsize:
                y_text = yc[i] + 7.0
            labels.append(
                f'<text x="{xs[i]:.2f}" y="{y_text:.2f}" font-size="{fsize}" '
                f'font-weight="{fw}" fill="{fill}" text-anchor="{anchor}" '
                f'data-fade="{a:.3f},{min(0.99, b + 0.04):.3f}">{_esc(disp)}</text>'
            )
        if i % step == 0 or is_last:
            xlabels.append(
                f'<text x="{xs[i]:.2f}" y="{VBH + 4.5:.2f}" font-size="3.4" fill="#9a9486" '
                f'text-anchor="{anchor}" data-fade="{a:.3f},{min(0.99, b + 0.05):.3f}">'
                f"{_esc(p.get('x'))}</text>"
            )
    svg = (
        f'<svg viewBox="0 0 {VBW:.0f} {VBH + 9:.0f}" style="width:100%;height:{blk_h}vh">'
        '<defs><linearGradient id="lineFill" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#f4c25c" stop-opacity="0.32"/>'
        '<stop offset="100%" stop-color="#f4c25c" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area_d}" fill="url(#lineFill)" stroke="none" '
        f'data-fade="{line_a:.2f},{min(0.99, line_a + 0.2):.2f}"/>'
        f'<path d="{path_d}" fill="none" stroke="#f4c25c" stroke-width="0.7" '
        f'stroke-linecap="round" stroke-linejoin="round" pathLength="1" '
        f'data-line="{line_a:.2f},{line_b:.2f}"/>'
        + "".join(markers) + "".join(labels) + "".join(xlabels)
        + "</svg>"
    )
    return f'<div class="line-block">{svg}</div>'


_BUILDERS = {
    "bar": _bar, "stat": _stat, "compare": _compare, "timeline": _timeline,
    "donut": _donut, "line": _line,
}


def _title_html(title) -> str:
    return _esc(title).replace("→", '<span class="em">→</span>')


def _title_fs(title, w: int, h: int) -> float:
    """見出しを2行以内に収めるフォントサイズ(vh)。長いほど縮小（はみ出し・3行化を防ぐ）。"""
    base = max(3.4, min(4.6, 62.0 / _units(title)))
    return round(base * _scale(w, h), 2)


def _wsec(a: float, b: float, duration) -> str:
    """秒指定の表示窓を p 分率(0..1)に変換。構造要素(見出し等)を尺に依らず一定秒で素早く出す用。
    duration 不明(静止 p=1 描画)時は近似値で代用（p=1 なので結果に影響しない）。"""
    d = duration if (duration and duration > 0) else 8.0
    return f"{min(0.9, a / d):.3f},{min(0.98, b / d):.3f}"


def _clean_source(s) -> str:
    """出典文字列から「裏取り済み事実/情報」のメタ文言を除く（「の」「より/から」も許容）。
    残りが括弧囲みだけなら外す。2文字未満は空扱い、40字超は末尾を省略する。"""
    s = str(s or "").strip()
    s = re.sub(r"^(裏取り済み)の?(事実|情報)?(より|から)?[\s:：、，,]*", "", s).strip()
    m = re.fullmatch(r"[（(](.*)[)）]", s)
    if m:
        s = m.group(1).strip()
    if len(s) < 2:
        return ""
    if len(s) > 40:
        s = s[:40].rstrip() + "…"
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


def _apply_style_html(html_text: str, style: ChartStyle | None) -> str:
    """既定チャートHTMLへチャンネル palette / font を適用する。"""
    if style is None:
        return html_text
    if style.palette:
        palette = style.palette
        for index, default in enumerate(_DONUT_COLORS):
            html_text = html_text.replace(default, palette[index % len(palette)])
    if style.font is not None:
        font_face = (
            "@font-face{font-family:'DociChannelChart';"
            + "src:url('" + style.font.as_uri() + "')}"
            "body,.title,.num .u,.tl-card .y,.donut-center{"
            "font-family:'DociChannelChart',sans-serif!important}"
        )
        html_text = html_text.replace("</style>", font_face + "</style>", 1)
    return html_text


def _page(
    spec: dict,
    body: str,
    duration=None,
    bg=None,
    w: int = 1080,
    h: int = 1920,
    style: ChartStyle | None = None,
) -> str:
    css = _BASE_CSS.replace("__GRAIN__", _GRAIN)
    # 背景画像があれば body 背景に敷き、暗幕(scrim)で文字可読性を確保する。
    body_attr = f" style=\"background:#0a0a0c url('file://{bg}') center/cover no-repeat\"" if bg else ""
    scrim = "<div class='scrim'></div>" if bg else ""
    unit = spec.get("unit", "")
    source = _clean_source(spec.get("source", ""))
    # 構造要素(見出し/罫/単位)は尺に依らず冒頭で素早く出す（固定秒→p分率）。
    # データ(本体)は p 分率のまま尺いっぱいに広がり、切替の少し前(p≈1)に完了する。
    unit_fs = _unit_fs(w, h)
    unit_html = (f'<div class="unit" style="font-size:{unit_fs}vh" '
                 f'data-rev="{_wsec(0.5, 1.6, duration)}">{_esc(unit)}</div>') if unit else ""
    src_html = (f'<div class="source" data-rev=".88,1">出典: {_esc(source)}</div>'
                if source else "")
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><style>" + css + "</style></head>"
        "<body" + body_attr + ">"
        + scrim
        + "<div class='grain'></div><div class='vig'></div><div class='frame'></div>"
        "<div class='wrap'>"
        + f'<div class="title" data-rev="{_wsec(0.25, 1.7, duration)}" '
        + f'style="font-size:{_title_fs(spec.get("title", ""), w, h)}vh">'
        + f'{_title_html(spec.get("title", ""))}</div>'
        + f'<div class="trule" data-drawx="{_wsec(0.7, 2.0, duration)}"></div>'
        + unit_html
        + f'<div class="body">{body}</div>'
        + src_html
        + "</div><script>" + _ANIM_JS + "</script></body></html>"
    )
    return _apply_style_html(page, style)


def chart_html(
    spec: dict,
    duration=None,
    bg=None,
    width: int | None = None,
    height: int | None = None,
    style: ChartStyle | None = None,
) -> str:
    builder = _BUILDERS.get(spec.get("type", ""))
    if not builder:
        raise ValueError(f"未対応の chart type: {spec.get('type')}")
    w = width or config.VIDEO_WIDTH
    h = height or config.VIDEO_HEIGHT
    return _page(spec, builder(spec, w, h), duration, bg, w, h, style)


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


def render_chart(
    spec: dict,
    out_png: Path,
    width: int | None = None,
    height: int | None = None,
    style: ChartStyle | None = None,
) -> Path:
    """chart 仕様を out_png に描画して返す（最終状態 p=1 の静止 PNG）。"""
    W = width or config.VIDEO_WIDTH
    H = height or config.VIDEO_HEIGHT
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="doci_chart_") as td:
        hp = Path(td) / "chart.html"
        hp.write_text(chart_html(spec, width=W, height=H, style=style), encoding="utf-8")
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
    width: int | None = None, height: int | None = None, fps: int = _VID_FPS, bg=None,
    style: ChartStyle | None = None,
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
        hp.write_text(
            chart_html(
                spec,
                duration=duration,
                bg=bg,
                width=W,
                height=H,
                style=style,
            ),
            encoding="utf-8",
        )
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
