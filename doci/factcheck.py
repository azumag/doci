"""後段ファクトチェック (issue #6)。

下書き(minimax-m3)の narration を、別モデル(opus)＋Web検証で点検し、
事実誤り・未裏付けの断定を自動修正した narration を返す。クロスモデルで
独立視点を入れ、確認バイアスを避ける。文体・長さ・カタカナ表記は維持させる。
"""
from __future__ import annotations

from . import config, llm

_PROMPT = """\
あなたは事実確認の編集者です。次のラジオ台本ナレーションを点検し、事実の誤りや「裏付けの無い断定」を修正した最終版を返してください。別の作者が書いた草稿を、独立した視点で厳しく検証してください。

# 検証の方針
- 人名・年号・数値・固有の出来事・因果関係の主張を重点的に確認する。必要なら WebSearch / WebFetch で裏取りする。
- 誤りは正しい事実に直す。確証が取れない断定は「一般論」や控えめな言い方に和らげる（削除しすぎない）。
- 事実に関わらない語り口・主張・ユーモアは変えない。

# 厳守する制約（元の台本ルール）
- 文体は「ですます調」を維持。
- 固有名詞・外来語は読み上げ用に必ずカタカナ（ラテン文字を本文に残さない）。
- 全体の長さ・構成はほぼ維持（大幅な増減をしない）。最初の一文の掴みは保つ。

{reference}
# 点検対象のナレーション
{narration}

出力は次の JSON のみ（前後に説明やコードフェンスを付けない）:
{{"narration": "修正後の最終ナレーション全文",
  "changed": true/false,
  "issues": [{{"before": "問題のあった記述", "after": "修正後", "reason": "理由（出典があれば併記）"}}]}}
"""


def _reference_block(research: dict | None) -> str:
    if not research or not research.get("facts"):
        return ""
    lines = ["# 参考（リサーチ済みの検証済み事実）"]
    for f in research["facts"]:
        src = f.get("source_url", "")
        lines.append(f"- {f.get('claim', '')}" + (f"（出典: {src}）" if src else ""))
    return "\n".join(lines) + "\n"


def verify_and_correct(narration: str, research: dict | None = None) -> dict | None:
    """narration を検証・自動修正。失敗時は None（呼び出し側は元のまま続行）。"""
    if not narration.strip():
        return None
    prompt = _PROMPT.format(reference=_reference_block(research), narration=narration)
    raw = llm.run_claude(
        prompt,
        config.FACTCHECK_MODEL,
        allowed_tools=["WebSearch", "WebFetch"],
        timeout=config.SCRIPT_LLM_TIMEOUT,
    )
    data = llm.extract_json(raw)
    if not data.get("narration", "").strip():
        raise ValueError("ファクトチェック結果に narration がありません")
    return data
