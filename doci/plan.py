"""構成プラン段（起承転結＋図表策定; issue #2）。

minimax-M3 が「構成（起承転結）」と「解説に効く図表」を設計し、qwen が本文を執筆する
二段構え。図表のデータは裏取り事実に基づく正確な値にし、本文側(qwen)は図表を chart_id で
配置するだけにして数値の取り違えを防ぐ。
経路は PLAN_BACKEND で選択（既定 opencode-go 経由、codex で直契約MiniMax APIを利用）。
"""
from __future__ import annotations

from . import ai_text, config, llm
from .channel import CornerSpec

_TYPES = {"bar", "stat", "compare", "timeline", "donut", "line"}

_PROMPT = """\
あなたは日本語ショート動画の構成作家です。次のコーナーの題材について、起承転結の構成と、解説に効く図表を設計してください（本文は書きません）。

コーナー: {label}
{research}
{avoid_block}
やること:
1. 起承転結の4ビートを設計する（各1行で要点）。**起＝「つかみ」**にする——ただし年号や出来事などの
   具体的事実そのものではなく、この題材が本当に問いたい「哲学的な問い・伝えたいテーマ」を一言で示す
   （例:「人類は何を信じたいのか」）。教科書的な背景説明・自己紹介・雑学クイズ振り（「〜って知ってい
   ますか」式）にはしない。承で調査の具体（年・数値・固有名・出来事）を展開してこの問いを裏付け、転で
   視点を裏返し、結は起で立てた問いに呼応する形で、断定せず軽く締める（同じ反語テンプレを毎回使わない）。
2. データ・比較・年表・印象的な数字が「図表にすると一目で分かる」箇所だけ、図表を0〜3個設計する。
   無理に作らない（数値や対比が無ければ0個でよい）。各図表は**そのまま描画できる完全な仕様**にし、
   データは上の裏取り事実に基づく正確な値にする。place はその図表を出すビート(起/承/転/結)。

図表の型と仕様:
- bar(棒): {{"place":"承","type":"bar","title":"...","unit":"単位：億個","data":[{{"label":"1926-27年","value":3.35,"display":"3.35億"}}],"source":"..."}}
- stat(大数字1つ): {{"place":"承","type":"stat","title":"...","value":"1000時間","caption":"短い補足","source":"..."}}
- compare(2〜3値の対比): {{"place":"転","type":"compare","title":"...","items":[{{"value":"2500h","label":"作れた寿命"}},{{"value":"1000h","label":"協定の上限"}}],"source":"..."}}
- timeline(年表): {{"place":"結","type":"timeline","title":"...","events":[{{"year":"1924","label":"..."}}],"source":"..."}}
- donut(構成比・シェア、2〜5項目): {{"place":"承","type":"donut","title":"...","items":[{{"label":"労働者","value":38.6,"display":"38.6%"}},{{"label":"資本家","value":30.0,"display":"30.0%"}}],"source":"..."}}
- line(推移・折れ線、3〜8点): {{"place":"転","type":"line","title":"...","unit":"万台","points":[{{"x":"1997","y":85.6,"display":"85.6万台"}},{{"x":"2010","y":42.3,"display":"42.3万台"}}],"source":"..."}}

制約:
- bar は全項目が同じ単位のときだけ使う(異なる単位の羅列は compare か stat にする)。
- 項目数の上限: bar≤4, compare 2〜3, timeline 3〜6, donut 2〜5, line 3〜8。
- place は構成上の配置指定であり画面には表示されない。
- 起（つかみの問い）には通常、図表を置かない。起は具体的な数値を持たない問い・テーマなので、
  図表の具体的データは承以降(承/転/結)に置く。
- 金額はドル・ルーブル等の外貨のまま出さず、日本円換算した value/display にする(例:「6500万ドル」ではなく
  「約97億円」)。正確なレートが不明なら概算でよい。単位もマイル・ポンド等の外国単位は使わず、
  キロメートル・キログラム等の日本で馴染みの単位にする(本文の言及と数値が一致するように)。

出力は次の JSON のみ（前後に説明やコードフェンスを付けない）:
{{"topic":"きょうの題材（短い日本語）",
  "beats":[{{"role":"起","gist":"..."}},{{"role":"承","gist":"..."}},{{"role":"転","gist":"..."}},{{"role":"結","gist":"..."}}],
  "charts":[ ...上記いずれかの図表仕様を0〜3個... ]}}
"""


def _research_block(research: dict | None) -> str:
    if not research or not research.get("facts"):
        return "（リサーチ無し。一般的な知識で構成してよい）"
    lines = [f"題材: {research.get('topic','')}", "裏取り済みの事実:"]
    lines += [f"- {f.get('claim','')}" for f in research["facts"]]
    return "\n".join(lines)


def _avoid_block(avoid_topics: list[str] | None) -> str:
    """avoid_topicsは新しい(=優先度が高い)順。プロンプト長を抑えるため先頭20件だけ載せる。"""
    topics = [t.strip() for t in (avoid_topics or []) if t and t.strip()]
    if not topics:
        return ""
    joined = "、".join(topics[:20])
    return (
        "最近すでに扱った題材（言い換えず、比喩・具体例・結論のいずれかが"
        f"明確に異なる別の題材を選ぶこと）: {joined}"
    )


def make_plan(
    corner: CornerSpec,
    research: dict | None,
    avoid_topics: list[str] | None = None,
) -> dict | None:
    """構成＋図表を設計。失敗時は None（呼び出し側は構成プラン無しで続行）。

    avoid_topics はresearch無しコーナーのcooldown照合で重複と判定された直近題材。
    与えると、次の設計でその題材と実質同じ結論・構成にならないよう避けさせる。
    """
    prompt = _PROMPT.format(
        label=corner.label,
        research=_research_block(research),
        avoid_block=_avoid_block(avoid_topics),
    )
    last_err: Exception | None = None
    for _ in range(2):
        try:
            if config.PLAN_BACKEND == "codex":
                # Web取得は不要なタスクなので fetch ガードは無効化(min_web_fetches=0)。
                # プランは chart_bg より出力が大きいため timeout は長めに取る。
                raw = llm.run_codex(prompt, config.CODEX_MODEL, timeout=240, min_web_fetches=0)
            elif config.PLAN_MODEL.startswith("opencode-go/"):
                raw = ai_text._run_opencode_go(prompt, config.PLAN_MODEL)
            else:
                raw = ai_text._run_opencode(prompt, config.PLAN_MODEL, "")
            data = llm.extract_json(raw)
            beats = data.get("beats")
            if not isinstance(beats, list) or len(beats) < 3:
                raise ValueError(f"beats が不十分: {str(data)[:200]}")
            # 図表は型が正しいものだけ採用（id を採番）
            charts = []
            for c in data.get("charts") or []:
                if isinstance(c, dict) and c.get("type") in _TYPES:
                    c["id"] = len(charts)
                    charts.append(c)
            data["charts"] = charts
            return data
        except (ValueError, RuntimeError) as e:
            last_err = e
    raise last_err or ValueError("プラン生成に失敗")


def brief_for_prompt(plan: dict) -> str:
    """執筆(qwen)プロンプトへ差し込む構成＋図表ブロック。"""
    lines = ["## 構成プラン（この起承転結に沿って書く）"]
    for b in plan.get("beats", []):
        lines.append(f"- {b.get('role','')}: {b.get('gist','')}")
    charts = plan.get("charts") or []
    if charts:
        lines.append(
            "\n## 図表（下記の図表を動画に出す。置き方を厳守）:"
            "\n- 図表は **scenes 配列の独立した要素**として置く。図表を出す scene を "
            "`{\"chart_id\": 番号, \"caption\": \"短い字幕\", \"visual_prompt\": \"\"}` の形にする"
            "（chart の scene では visual_prompt は空文字でよい）。本文がその図表に触れるビート付近に置く。"
            "\n- **narration（読み上げ本文）の中には `{...}` や chart_id を絶対に書かない。** "
            "本文は普通の文章だけにする。マーカーJSONを本文に混ぜると、そのまま読み上げられて事故になる。"
            "\n- 図表の数値は本文でも正しく言及する。"
        )
        for c in charts:
            place = c.get("place", "")
            desc = c.get("title", "")
            lines.append(f"- chart_id={c['id']}（{place}・{c.get('type')}）: {desc}")
    else:
        lines.append("\n（図表は無し。映像は従来どおり visual_prompt で。）")
    return "\n".join(lines)
