"""Python内蔵プロンプト定数の手書き静的レジストリ（自動探索はしない）。

汎用的な「モジュール変数の文字列リテラルを走査」する自動探索は、無関係な文字列
リテラルまで書き換え対象にしてしまいうる。ここでは既知の11個の定数だけに限定し、
各定数を安全に編集するために必要な情報（呼び出し箇所の実kwarg名・この内容を
アサートしている既存テスト）を手作業で記録する。`fields` は定数の現在のテキストから
逆算できない情報（プレースホルダを削除されても検出できない）なので、レジストリで
明示的に持つ。

新しいプロンプト定数を追加編集対象にしたい場合は、このファイルへ1行追加する
（それ以外の場所を触る必要はない）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptConstant:
    id: str
    relpath: str
    name: str
    fields: frozenset[str]
    call_site: str
    guarded_by: tuple[str, ...]
    description: str


REGISTRY: tuple[PromptConstant, ...] = (
    PromptConstant(
        id="ai_text:_SEMANTIC_DUPLICATE_PROMPT",
        relpath="doci/ai_text.py",
        name="_SEMANTIC_DUPLICATE_PROMPT",
        fields=frozenset({"candidate", "numbered"}),
        call_site="doci/ai_text.py:774",
        guarded_by=(),
        description="題材の意味的重複判定に使うプロンプト",
    ),
    PromptConstant(
        id="ai_text:_TITLE_PATTERN_DUPLICATE_PROMPT",
        relpath="doci/ai_text.py",
        name="_TITLE_PATTERN_DUPLICATE_PROMPT",
        fields=frozenset({"candidate", "numbered"}),
        call_site="doci/ai_text.py:846",
        guarded_by=(),
        description="タイトルの型の重複判定に使うプロンプト",
    ),
    PromptConstant(
        id="ai_text:_NARRATION_OPENING_PATTERN_DUPLICATE_PROMPT",
        relpath="doci/ai_text.py",
        name="_NARRATION_OPENING_PATTERN_DUPLICATE_PROMPT",
        fields=frozenset({"candidate", "numbered"}),
        call_site="doci/ai_text.py:930",
        guarded_by=(),
        description="ナレーション書き出しの型の重複判定に使うプロンプト",
    ),
    PromptConstant(
        id="ai_text:_ENGAGEMENT_COMMENT_PROMPT",
        relpath="doci/ai_text.py",
        name="_ENGAGEMENT_COMMENT_PROMPT",
        fields=frozenset({"corner_label", "title", "description", "narration_excerpt"}),
        call_site="doci/ai_text.py:1378",
        guarded_by=(),
        description="closing_sentence以外(debate)のエンゲージメントコメント生成プロンプト",
    ),
    PromptConstant(
        id="ai_text:_CALL_TO_ACTION_COMMENT_PROMPT",
        relpath="doci/ai_text.py",
        name="_CALL_TO_ACTION_COMMENT_PROMPT",
        fields=frozenset(
            {"corner_label", "title", "description", "viewer_action", "narration_excerpt"}
        ),
        call_site="doci/ai_text.py:1370",
        guarded_by=(),
        description="call_to_action方式のエンゲージメントコメント生成プロンプト",
    ),
    PromptConstant(
        id="factcheck:_PROMPT",
        relpath="doci/factcheck.py",
        name="_PROMPT",
        fields=frozenset({"reference", "narration", "web_howto"}),
        call_site="doci/factcheck.py:735",
        guarded_by=("tests/test_viewer_segment_claims.py",),
        description="ファクトチェック監査資料の収集プロンプト",
    ),
    PromptConstant(
        id="factcheck:_AUDIT_PROMPT",
        relpath="doci/factcheck.py",
        name="_AUDIT_PROMPT",
        fields=frozenset({"reference", "narration"}),
        call_site="doci/factcheck.py:777",
        guarded_by=(),
        description="ファクトチェック監査（事実確認）プロンプト",
    ),
    PromptConstant(
        id="factcheck:_REWRITE_PROMPT",
        relpath="doci/factcheck.py",
        name="_REWRITE_PROMPT",
        fields=frozenset({"narration", "audit"}),
        call_site="doci/factcheck.py:545",
        guarded_by=(),
        description="監査結果に基づくナレーション書き換えプロンプト",
    ),
    PromptConstant(
        id="research:_PROMPT",
        relpath="doci/research.py",
        name="_PROMPT",
        fields=frozenset(
            {
                "label",
                "channel_guidance",
                "past",
                "web_howto",
                "video_case_study_rule",
                "extra_rules",
                "factcheck_focus",
                "search_fallback_rule",
                "topic_selection_rule",
                "external_materials",
            }
        ),
        call_site="doci/research.py:1968",
        guarded_by=("tests/test_ambiguous_date_title.py", "tests/test_research_prompt.py"),
        description="題材リサーチプロンプト（前段リサーチ＋ファクトチェック資料収集の両方で使用）",
    ),
    PromptConstant(
        id="plan:_PROMPT",
        relpath="doci/plan.py",
        name="_PROMPT",
        fields=frozenset({"label", "research", "avoid_block"}),
        call_site="doci/plan.py:86",
        guarded_by=(),
        description="構成プラン(起承転結＋図表)設計プロンプト",
    ),
    PromptConstant(
        id="tactic_backfill:_EXTRACT_PROMPT",
        relpath="doci/tactic_backfill.py",
        name="_EXTRACT_PROMPT",
        fields=frozenset({"narration"}),
        call_site="doci/tactic_backfill.py:120",
        guarded_by=("tests/test_tactic_backfill.py",),
        description="動画内で紹介された運用施策(viewer_action)の抽出プロンプト",
    ),
)

BY_ID: dict[str, PromptConstant] = {entry.id: entry for entry in REGISTRY}
