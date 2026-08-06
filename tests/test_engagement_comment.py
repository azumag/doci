"""issue #86: 討論を誘発する自作コメント生成のテスト。

対象: ai_text.generate_engagement_comment。
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


if __name__ == "__main__":
    unittest.main()
