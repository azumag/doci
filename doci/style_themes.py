"""チャンネル別デザインテーマのレジストリ(issue #76)。

`charts.py`/`thumbnail.py`/`chart_seq.py` の墨地＋ゴールド＋明朝という単一デザイン
言語は、`style.chart.palette`等の色だけではレイアウト構造・装飾モチーフ（星型SVG、
額縁ボーダー、グレインテクスチャ）まで差別化できない。本モジュールはテーマ単位で
「追記CSS」と「スタイル既定値」をひとまとめに定義し、`channel.toml`の
`[style] theme = "..."`で選択できるようにする。

設計原則:
- `charts._BASE_CSS`/`thumbnail._CSS`/`chart_seq._overlay_html`のインラインCSSは
  一切書き換えない。各テーマの追記CSSは`<style>`内の既存CSSの後に連結され、
  CSSカスケード（同一詳細度なら後勝ち）で上書きする。
- 星型SVG(`.star-bg`)・額縁(`.frame`)・グレイン(`.grain`)などの構造要素はHTML側に
  残したまま`display:none`で消すため、チャートの6ビルダー(`_bar`/`_stat`/`_compare`/
  `_timeline`/`_donut`/`_line`)のHTML構造・レイアウト計算(vh/vw)は無変更で済む。
- `_DONUT_COLORS`(`charts.py`)のリテラル値(`#f4c25c`,`#d8503a`,`#c9772a`,`#e9e1d2`,
  `#8a6b3d`)をテーマCSS内でそのまま使うと、`charts._apply_style_html()`が
  ページ全体へ最後にかける文字列置換によって、テーマCSSも自動的にチャンネルの
  `style.chart.palette`へ再着色される（新規コード不要）。
- `classic`テーマは追記CSSが全て空文字＝テーマ導入前の出力と完全同一。既存チャンネル
  (`ideology`)の見た目は変わらない。

既知の限界(YAGNI、致命的でないため許容):
- ドーナツ/リングゲージの「未到達区間」の背景色(`#2a2218`)は`charts._ANIM_JS`内の
  JSリテラルで、実行時にJSがinline styleへ直接書き込むためCSSでは上書きできない。
  最終フレーム(p=1)ではリングが100%到達し未到達区間が消えるため、通常の静止画/
  動画出力では視認されない。
- `thumbnail.py`には`charts._apply_style_html`のようなpalette置換機構がない
  (フォント/タイトル色のみ文字列置換)。そのためサムネイル側の追記CSSでアクセント
  色を使う箇所は`_DONUT_COLORS`リテラルに頼らず、テーマの意図した色を直接書く。
- `_apply_style_html`はpaletteを`_DONUT_COLORS`のindex順に逐次文字列置換するため、
  channelのpalette値が別indexの`_DONUT_COLORS`リテラルと一致すると二重置換され得る
  (既存の挙動。テーマCSSはこのリテラルへの依存を広げるため、新テーマ追加時は
  paletteとの衝突に注意する)。
- 要素にインラインstyleで色が書き込まれる場合(例: `charts._timeline`の`.tl-head`の
  `border-top`)、CSS追記だけでは上書きできないため`!important`が必要。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesignTheme:
    """1テーマ分の追記CSSとスタイル既定値。全フィールドは`classic`(現行デザイン)基準。"""

    chart_css: str = ""
    thumbnail_css: str = ""
    overlay_css: str = ""
    thumbnail_font_family: str = "'Hiragino Mincho ProN','Hiragino Mincho Pro',serif"
    thumbnail_title_color: str = "#f6efe1"
    chart_palette: tuple[str, ...] = ()
    video_pad_color: str = "0x0a0a0c"
    subtitle_box_radius: float = 0.35


_TECH_CHART_CSS = """
body{background:linear-gradient(165deg,#0a1622 0%,#0f2038 55%,#0a1420 100%)}
.grain,.frame,.frame::after,.star-bg{display:none}
.title{font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;
  font-weight:900;letter-spacing:0;color:#f2f6fb}
.trule{height:.55vh;width:8vw;border-radius:0;background:#f4c25c;
  box-shadow:none;margin-top:1.6vh}
.unit{color:#7f93ac}
.source{color:#5a6b80}
.stat-lead,.stat-cap{color:#dbe6f2}
.num .u{font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;
  font-weight:700;color:#c7d6e6}
.gauge{box-shadow:none}
.gauge-hole,.donut-hole{background:#0c1826}
.gauge-val,.donut-center{font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;
  font-weight:800}
.bar-label,.cbar-label,.cmp-label,.donut-label{color:#c3d1e0}
.bar-track,.cbar-track{background:#0e1c2c;box-shadow:inset 0 .3vh .8vh rgba(0,0,0,.6),
  inset 0 0 0 .12vh rgba(255,255,255,.08)}
.bar-fill,.cbar-fill{border-radius:.3vh;box-shadow:none;background:#f4c25c}
.cmp-item,.tl-card{background:linear-gradient(135deg,#101f30,#0b1520);
  border:.14vh solid #22344a;border-left:.7vh solid #f4c25c;border-radius:.5vh;
  box-shadow:0 .8vh 1.8vh rgba(0,0,0,.5)}
.tl-card .y{font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;font-weight:800}
.tl-card .t{color:#dbe6f2}
.tl-stem{background:#f4c25c;box-shadow:none;border-radius:.2vh}
/* _timelineビルダーがborder-topをインラインstyleで書くため!importantが必要 */
.tl-head{border-top-color:#f4c25c!important}
.donut-chip{border-radius:.2vh}
.line-block path{stroke-linejoin:miter}
"""

_TECH_THUMBNAIL_CSS = """
body{background:linear-gradient(165deg,#0a1622 0%,#0f2038 55%,#0a1420 100%)}
.wrap{top:0;bottom:auto;height:62vh;align-items:flex-start;justify-content:flex-end;
  padding:8vh 9vw}
.title{text-align:left;font-weight:900;letter-spacing:0}
/* thumbnail.pyにはchart.paletteの再着色機構がないため、ここは_DONUT_COLORSに
   頼らずtechテーマの意図した色(chart_paletteの1色目と同じ赤)を直接書く。 */
.trule{margin-left:0;width:22vw;height:.6vh;border-radius:0;background:#ff3b30;box-shadow:none}
"""

_TECH_OVERLAY_CSS = """
.frame{display:none}
.year{font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;font-weight:900}
.stem{background:#f4c25c;box-shadow:none;border-radius:.2vh}
.head{border-top-color:#f4c25c}
.counter{color:#7f93ac}
"""

THEMES: dict[str, DesignTheme] = {
    "classic": DesignTheme(),
    "tech": DesignTheme(
        chart_css=_TECH_CHART_CSS,
        thumbnail_css=_TECH_THUMBNAIL_CSS,
        overlay_css=_TECH_OVERLAY_CSS,
        thumbnail_font_family="'Hiragino Kaku Gothic ProN','Hiragino Sans',sans-serif",
        thumbnail_title_color="#ffffff",
        chart_palette=("#ff3b30", "#2563eb", "#f59e0b", "#22c55e", "#e2e8f0"),
        video_pad_color="0x07111f",
        subtitle_box_radius=0.0,
    ),
}


def get(theme_key: str | None) -> DesignTheme:
    """テーマ名からDesignThemeを返す。None/未知キーは`classic`にフォールバックする。"""
    return THEMES.get(theme_key or "classic", THEMES["classic"])
