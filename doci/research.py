"""前段リサーチ (issue #6)。

claude CLI の Web ツールで、コーナーに合う「きょうの題材」を1つ選び、
検証可能な具体事実（人名・年・数字・定義・具体例）を実ソースで裏取りして返す。
下書き(minimax-m3)はこの「参考事実」を具体として織り込む。出典は本文には出さない。
"""
from __future__ import annotations

from . import config, corners, llm

_PROMPT = """\
あなたは日本語ショート動画の構成リサーチャーです。次のコーナー向けに、きょう扱う題材を1つ選び、Web検索で裏取りした具体的事実を集めてください。

コーナー: {label}
最近すでに扱った題材（重複を避ける）: {past}

やること:
1. このコーナーに合う、具体的で語り甲斐のある題材を1つ選ぶ（抽象概念そのものでなく、出来事・人物・制度・数字に落ちるもの）。
2. WebSearch / WebFetch で確認し、台本に織り込める「検証済みの具体事実」を5〜7個集める。
   - 人名・年号・数値・定義・固有の出来事・印象的な具体例を優先。
   - 不確かなものは入れない。各事実に出典URLを付ける。

出力は次の JSON のみ（前後に説明やコードフェンスを付けない）:
{{"topic": "きょうの題材（短い日本語）",
  "angle": "視聴者がハッとする切り口（1文）",
  "facts": [{{"claim": "検証済みの具体事実（日本語・1文）", "source_url": "...", "source_title": "..."}}]}}
"""


def web_research(corner: corners.Corner, past_topics: list[str]) -> dict | None:
    """題材選定＋Web裏取り。失敗時は None（呼び出し側はリサーチ無しで続行）。"""
    past = "、".join(past_topics[-20:]) if past_topics else "（まだありません）"
    prompt = _PROMPT.format(label=corner.label, past=past)
    raw = llm.run_claude(
        prompt,
        config.RESEARCH_MODEL,
        allowed_tools=["WebSearch", "WebFetch"],
        timeout=config.SCRIPT_LLM_TIMEOUT,
    )
    data = llm.extract_json(raw)
    facts = data.get("facts")
    if not data.get("topic") or not isinstance(facts, list) or not facts:
        raise ValueError(f"リサーチ結果が不十分です: {str(data)[:300]}")
    # 出典の無い事実は除外（裏取り済みのみ採用）
    data["facts"] = [f for f in facts if isinstance(f, dict) and f.get("claim") and f.get("source_url")]
    if not data["facts"]:
        raise ValueError("出典付きの事実がありませんでした")
    return data


def brief_for_prompt(research: dict) -> str:
    """下書きプロンプトへ差し込む参考事実ブロックを組み立てる。"""
    lines = [
        "## きょうの題材（リサーチ済み・これで書く。テーマ選定は不要）",
        f"題材: {research.get('topic', '')}",
    ]
    if research.get("angle"):
        lines.append(f"切り口: {research['angle']}")
    lines.append(
        "\n## 参考事実（Webで裏取り済み。最低2つを具体として自然に本文へ織り込む。"
        "年・数値・固有名は正確に。これらは検証済みなので事実として述べてよい。出典は本文に書かない）:"
    )
    for f in research.get("facts", []):
        lines.append(f"- {f.get('claim', '')}")
    return "\n".join(lines)
