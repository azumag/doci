"""構成プラン段（起承転結＋図表策定; issue #2）。

minimax-M3 が「構成（起承転結）」と「解説に効く図表」を設計し、qwen が本文を執筆する
二段構え。図表のデータは裏取り事実に基づく正確な値にし、本文側(qwen)は図表を chart_id で
配置するだけにして数値の取り違えを防ぐ。
"""
from __future__ import annotations

from . import ai_text, config, corners, llm

_TYPES = {"bar", "stat", "compare", "timeline"}

_PROMPT = """\
あなたは日本語ショート動画の構成作家です。次のコーナーの題材について、起承転結の構成と、解説に効く図表を設計してください（本文は書きません）。

コーナー: {label}
{research}

やること:
1. 起承転結の4ビートを設計する（各1行で要点。転で視点を裏返し、結は問いかけで終える方向）。
2. データ・比較・年表・印象的な数字が「図表にすると一目で分かる」箇所だけ、図表を0〜3個設計する。
   無理に作らない（数値や対比が無ければ0個でよい）。各図表は**そのまま描画できる完全な仕様**にし、
   データは上の裏取り事実に基づく正確な値にする。place はその図表を出すビート(起/承/転/結)。

図表の型と仕様:
- bar(棒): {{"place":"承","type":"bar","title":"...","unit":"単位：億個","data":[{{"label":"1926-27年","value":3.35,"display":"3.35億"}}],"source":"..."}}
- stat(大数字1つ): {{"place":"起","type":"stat","title":"...","value":"1000時間","caption":"短い補足","source":"..."}}
- compare(2〜3値の対比): {{"place":"転","type":"compare","title":"...","items":[{{"value":"2500h","label":"作れた寿命"}},{{"value":"1000h","label":"協定の上限"}}],"source":"..."}}
- timeline(年表): {{"place":"結","type":"timeline","title":"...","events":[{{"year":"1924","label":"..."}}],"source":"..."}}

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


def make_plan(corner: corners.Corner, research: dict | None) -> dict | None:
    """構成＋図表を設計。失敗時は None（呼び出し側は構成プラン無しで続行）。"""
    prompt = _PROMPT.format(label=corner.label, research=_research_block(research))
    last_err: Exception | None = None
    for _ in range(2):
        try:
            data = llm.extract_json(ai_text._run_opencode(prompt, config.PLAN_MODEL, ""))
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
            "\n## 図表（下記の図表を、本文がその内容に触れる位置の scene に "
            "`{\"chart_id\": 番号, \"caption\": \"短い字幕\"}` として差し込む。"
            "visual_prompt は不要。図表の数値は本文でも正しく言及する。該当ビート付近に置く）:"
        )
        for c in charts:
            place = c.get("place", "")
            desc = c.get("title", "")
            lines.append(f"- chart_id={c['id']}（{place}・{c.get('type')}）: {desc}")
    else:
        lines.append("\n（図表は無し。映像は従来どおり visual_prompt で。）")
    return "\n".join(lines)
