"""issue #86: 討論を誘発する自作コメント生成のテスト。
チャンネル別方式（issue #98）: closing_question / call_to_action も対象。

対象: ai_text.generate_engagement_comment, ai_text._closing_sentence,
ai_text._is_closing_question。
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from doci import ai_text
from doci.channel import CornerSpec


def _corner() -> CornerSpec:
    return CornerSpec(
        key="shorts",
        label="ショート攻略",
        persona_path=Path(__file__),
        corner_path=Path(__file__),
        voice_key="narrator",
    )


class GenerateEngagementCommentTest(unittest.TestCase):
    def test_strips_language_tagged_code_fences(self) -> None:
        with patch.object(
            ai_text, "_dispatch", return_value="```json\nバスタオルは週1で洗うよね？\n```"
        ):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション本文"}
            )
        self.assertEqual(result, "バスタオルは週1で洗うよね？")

    def test_strips_quotes_and_code_fences(self) -> None:
        with patch.object(ai_text, "_dispatch", return_value='```\n"バスタオルは週1で洗うよね？"\n```'):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション本文"}
            )
        self.assertEqual(result, "バスタオルは週1で洗うよね？")

    def test_truncates_overlong_output(self) -> None:
        with patch.object(ai_text, "_dispatch", return_value="あ" * 500):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション"}
            )
        self.assertEqual(len(result), ai_text._MAX_ENGAGEMENT_COMMENT_LENGTH)

    def test_returns_none_when_title_and_narration_are_both_empty(self) -> None:
        with patch.object(ai_text, "_dispatch") as dispatch_mock:
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "", "narration": ""}
            )
        self.assertIsNone(result)
        dispatch_mock.assert_not_called()

    def test_returns_none_on_dispatch_failure(self) -> None:
        with patch.object(
            ai_text, "_dispatch", side_effect=RuntimeError("backend unavailable")
        ):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション"}
            )
        self.assertIsNone(result)

    def test_returns_none_on_dispatch_timeout(self) -> None:
        with patch.object(
            ai_text,
            "_dispatch",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=60),
        ):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション"}
            )
        self.assertIsNone(result)

    def test_returns_none_when_output_contains_url(self) -> None:
        with patch.object(
            ai_text, "_dispatch", return_value="詳しくはhttps://example.com/spamを見てね"
        ):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション"}
            )
        self.assertIsNone(result)

    def test_returns_none_on_dispatch_value_error(self) -> None:
        with patch.object(
            ai_text, "_dispatch", side_effect=ValueError("unknown TEXT_BACKEND")
        ):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション"}
            )
        self.assertIsNone(result)

    def test_returns_none_when_dispatch_returns_blank_text(self) -> None:
        with patch.object(ai_text, "_dispatch", return_value="   \n```\n```  "):
            result = ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション"}
            )
        self.assertIsNone(result)

    def test_prompt_carries_full_narration_and_description(self) -> None:
        """実測(m1B5vH7K-tk動画)で、冒頭300字だけを渡していたため動画本編の
        具体的な判断基準（本文後半にある）が見えず、タイトルを言い換えただけの
        的外れなコメントが生成された事故があった。ナレーションが切り詰められず
        全文渡ること、descriptionも渡ることを回帰テストとして固定する。"""
        long_tail = "本編後半にしかない具体的な判断基準テキスト"
        narration = "導入部。" + "あ" * 400 + long_tail
        with patch.object(ai_text, "_dispatch", return_value="コメント") as dispatch_mock:
            ai_text.generate_engagement_comment(
                _corner(),
                {
                    "title": "タイトル",
                    "narration": narration,
                    "description": "概要欄の要約テキスト",
                },
            )
        prompt = dispatch_mock.call_args.args[0]
        self.assertIn(long_tail, prompt)
        self.assertIn("概要欄の要約テキスト", prompt)

    def test_prompt_forbids_paraphrasing_title_as_generic_opinion(self) -> None:
        """タイトルの言い換えだけの一般論コメントを禁止する指示がプロンプトに
        含まれることを確認する（的外れコメント事故の再発防止）。"""
        with patch.object(ai_text, "_dispatch", return_value="コメント") as dispatch_mock:
            ai_text.generate_engagement_comment(
                _corner(), {"title": "タイトル", "narration": "ナレーション"}
            )
        prompt = dispatch_mock.call_args.args[0]
        self.assertIn("具体的な内容", prompt)
        self.assertIn("言い換えただけ", prompt)

    def test_unknown_mode_falls_back_to_debate_prompt(self) -> None:
        with patch.object(ai_text, "_dispatch", return_value="コメント") as dispatch_mock:
            ai_text.generate_engagement_comment(
                _corner(),
                {"title": "タイトル", "narration": "ナレーション"},
                mode="unknown-mode",
            )
        prompt = dispatch_mock.call_args.args[0]
        self.assertIn("反応・議論したくなる", prompt)


class ClosingSentenceTest(unittest.TestCase):
    def test_extracts_last_sentence_with_terminator(self) -> None:
        result = ai_text._closing_sentence(
            "導入です。中盤の説明です。みなさんはどう思いますか？"
        )
        self.assertEqual(result, "みなさんはどう思いますか？")

    def test_strips_stray_trailing_bracket_from_old_data(self) -> None:
        """実測: output/ideology/2026-07-31_communism_011315の narration が
        鉤括弧で終わっており（_strip_bracket_quotes未適用の旧データ）、
        末尾セグメントが「」」だけになる事故があった。有効な文まで遡って
        抽出できることを確認する。"""
        result = ai_text._closing_sentence(
            "本編です。あなたならどうしますか？」"
        )
        self.assertEqual(result, "あなたならどうしますか？")

    def test_returns_empty_when_no_terminator_present(self) -> None:
        self.assertEqual(ai_text._closing_sentence("句読点が一つも無い文字列"), "")

    def test_returns_empty_for_blank_input(self) -> None:
        self.assertEqual(ai_text._closing_sentence(""), "")
        self.assertEqual(ai_text._closing_sentence("   "), "")

    def test_returns_empty_when_over_max_chars_instead_of_truncating(self) -> None:
        """左truncateすると文頭が欠けた断片が公開投稿されてしまうため、
        収まらない場合は諦めて空文字を返す（変更しない）仕様を固定する。"""
        long_sentence = "あ" * 300 + "。"
        self.assertEqual(ai_text._closing_sentence(long_sentence, max_chars=200), "")


class IsClosingQuestionTest(unittest.TestCase):
    def test_plain_question_mark_is_a_question(self) -> None:
        self.assertTrue(ai_text._is_closing_question("あなたならどうしますか？"))

    def test_deshou_ka_is_a_question(self) -> None:
        self.assertTrue(
            ai_text._is_closing_question(
                "みなさんが選んでいるのは味そのものでしょうか。"
            )
        )

    def test_declarative_sentence_is_not_a_question(self) -> None:
        self.assertFalse(
            ai_text._is_closing_question("その結果で次の判断をします。")
        )

    def test_soft_deshou_without_question_word_is_not_a_question(self) -> None:
        """「〜となるでしょう。」のような予測断定を疑問と誤検出しない。"""
        self.assertFalse(ai_text._is_closing_question("次第に効果が出るでしょう。"))

    def test_soft_deshou_with_question_word_is_a_question(self) -> None:
        self.assertTrue(
            ai_text._is_closing_question(
                "私たちはいったい何を頼りに歩けばよいのでしょう。"
            )
        )


class ClosingQuestionModeTest(unittest.TestCase):
    def test_question_ending_is_posted_verbatim_without_llm_call(self) -> None:
        with patch.object(ai_text, "_dispatch") as dispatch_mock:
            result = ai_text.generate_engagement_comment(
                _corner(),
                {
                    "title": "タイトル",
                    "narration": "導入です。あなたならどうしますか？",
                },
                mode="closing_question",
            )
        dispatch_mock.assert_not_called()
        self.assertEqual(result, "あなたならどうしますか？")

    def test_non_question_ending_falls_back_to_debate_llm(self) -> None:
        with patch.object(
            ai_text, "_dispatch", return_value="議論を誘発する一言"
        ) as dispatch_mock:
            result = ai_text.generate_engagement_comment(
                _corner(),
                {
                    "title": "タイトル",
                    "narration": "導入です。その結果で次の判断をします。",
                },
                mode="closing_question",
            )
        dispatch_mock.assert_called_once()
        prompt = dispatch_mock.call_args.args[0]
        self.assertIn("反応・議論したくなる", prompt)
        self.assertEqual(result, "議論を誘発する一言")

    def test_fallback_llm_failure_returns_none(self) -> None:
        with patch.object(
            ai_text, "_dispatch", side_effect=RuntimeError("backend unavailable")
        ):
            result = ai_text.generate_engagement_comment(
                _corner(),
                {"title": "タイトル", "narration": "断定で終わる文です。"},
                mode="closing_question",
            )
        self.assertIsNone(result)


class CallToActionModeTest(unittest.TestCase):
    def test_uses_viewer_action_as_primary_input(self) -> None:
        with patch.object(ai_text, "_dispatch", return_value="コメント") as dispatch_mock:
            ai_text.generate_engagement_comment(
                _corner(),
                {
                    "title": "タイトル",
                    "narration": "本編ナレーション。",
                    "_research": {
                        "viewer_action": "YouTube Studioで維持率グラフを開いて確認する"
                    },
                },
                mode="call_to_action",
            )
        prompt = dispatch_mock.call_args.args[0]
        self.assertIn("YouTube Studioで維持率グラフを開いて確認する", prompt)

    def test_falls_back_to_closing_sentence_when_viewer_action_empty(self) -> None:
        with patch.object(ai_text, "_dispatch", return_value="コメント") as dispatch_mock:
            ai_text.generate_engagement_comment(
                _corner(),
                {
                    "title": "タイトル",
                    "narration": "導入です。次の一手を記録して判断します。",
                },
                mode="call_to_action",
            )
        prompt = dispatch_mock.call_args.args[0]
        self.assertIn("次の一手を記録して判断します。", prompt)

    def test_returns_none_without_llm_call_when_no_input_available(self) -> None:
        with patch.object(ai_text, "_dispatch") as dispatch_mock:
            result = ai_text.generate_engagement_comment(
                _corner(),
                {"title": "タイトル", "narration": "句読点が一つも無い文字列"},
                mode="call_to_action",
            )
        dispatch_mock.assert_not_called()
        self.assertIsNone(result)

    def test_does_not_fall_back_to_debate_mode(self) -> None:
        """call_to_actionは議論誘発の前提が成立しないチャンネル向けの方式
        のため、入力が無い場合はdebateへフォールバックせず投稿を諦める。"""
        with patch.object(ai_text, "_dispatch") as dispatch_mock:
            ai_text.generate_engagement_comment(
                _corner(),
                {"title": "タイトル", "narration": ""},
                mode="call_to_action",
            )
        dispatch_mock.assert_not_called()

    def test_prompt_forbids_discussion_prompting_language(self) -> None:
        with patch.object(ai_text, "_dispatch", return_value="コメント") as dispatch_mock:
            ai_text.generate_engagement_comment(
                _corner(),
                {
                    "title": "タイトル",
                    "narration": "導入です。次の一手を記録して判断します。",
                },
                mode="call_to_action",
            )
        prompt = dispatch_mock.call_args.args[0]
        self.assertIn("感想募集", prompt)

    def test_output_sanitization_applies_to_call_to_action(self) -> None:
        with patch.object(
            ai_text, "_dispatch", return_value="```\n維持率を確認しよう\n```"
        ):
            result = ai_text.generate_engagement_comment(
                _corner(),
                {
                    "title": "タイトル",
                    "narration": "導入です。次の一手を記録して判断します。",
                },
                mode="call_to_action",
            )
        self.assertEqual(result, "維持率を確認しよう")


if __name__ == "__main__":
    unittest.main()
