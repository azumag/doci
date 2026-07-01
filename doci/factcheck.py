"""後段ファクトチェック (issue #6)。

下書き(minimax-m3)の narration を、設定されたバックエンド(既定 claude の別モデル opus、
または codex exec+MiniMax-M3)＋Web検証で点検し、事実誤り・未裏付けの断定を自動修正した
narration を返す。クロスモデルで独立視点を入れ、確認バイアスを避ける。
文体・長さ・カタカナ表記は維持させる。
"""
from __future__ import annotations

from . import config, llm

# バックエンドごとの「必要なら裏取りする」手順の言い回し。claude は従来どおり WebSearch/WebFetch
# ツールを使わせる。codex はツールを持たないためシェルの curl 等での取得を明示的に指示する。
_WEB_HOWTO = {
    "claude": "必要なら WebSearch / WebFetch で裏取りする。",
    "codex": (
        "必要ならシェルで curl を使い、Web検索（https://duckduckgo.com/html/?q=... や "
        "Wikipedia API 等）と実ページ取得で裏取りする。内部知識だけに頼らず、疑わしい点は"
        "必ず実際に取得したページで確認すること。"
    ),
}

_PROMPT = """\
あなたは事実確認の編集者です。次のラジオ台本ナレーションを点検し、事実の誤りや「裏付けの無い断定」を修正した最終版を返してください。別の作者が書いた草稿を、独立した視点で厳しく検証してください。

# 検証の方針
- 人名・年号・数値・固有の出来事・因果関係の主張を重点的に確認する。{web_howto}
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


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


def _attempt(prompt: str, backend: str) -> dict:
    if backend == "codex":
        raw = llm.run_codex(
            prompt,
            config.CODEX_MODEL,
            timeout=config.SCRIPT_LLM_TIMEOUT,
            min_web_fetches=1,
        )
    else:
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


def verify_and_correct(narration: str, research: dict | None = None) -> dict | None:
    """narration を検証・自動修正。失敗時は None（呼び出し側は元のまま続行）。

    バックエンド(特に MiniMax-M3)が長い日本語文字列のJSONエスケープを崩し不正JSONを
    返すことがあるため、SCRIPT_FACTCHECK_RETRIES 回まで再試行する（尽きたら最後の例外をraise）。
    """
    if not narration.strip():
        return None
    backend = config.FACTCHECK_BACKEND
    prompt = _PROMPT.format(
        reference=_reference_block(research),
        narration=narration,
        web_howto=_WEB_HOWTO.get(backend, _WEB_HOWTO["claude"]),
    )
    last_err: Exception | None = None
    for attempt in range(1, config.SCRIPT_FACTCHECK_RETRIES + 1):
        try:
            return _attempt(prompt, backend)
        except (ValueError, RuntimeError) as e:  # JSON不正/不十分/CLI失敗を再試行
            last_err = e
            if attempt < config.SCRIPT_FACTCHECK_RETRIES:
                _log(f"ファクトチェック不良(試行{attempt}/{config.SCRIPT_FACTCHECK_RETRIES})→再試行: {str(e)[:120]}")
    raise last_err or ValueError("ファクトチェックに失敗しました")
