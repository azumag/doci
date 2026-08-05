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


if __name__ == "__main__":
    unittest.main()
