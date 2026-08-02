"""issue #58: narration 中の鉤括弧「」除去のテスト。

対象: ai_text._strip_bracket_quotes、ai_text._validate。
"""
from __future__ import annotations

import unittest

from doci import ai_text


class StripBracketQuotesTest(unittest.TestCase):
    def test_removes_bracket_quote_characters_but_keeps_the_quoted_words(self) -> None:
        self.assertEqual(
            ai_text._strip_bracket_quotes("これは「本当の問い」なのでしょうか。"),
            "これは本当の問いなのでしょうか。",
        )

    def test_removes_multiple_occurrences(self) -> None:
        self.assertEqual(
            ai_text._strip_bracket_quotes("「土法高炉」は「農家の庭」で作られました。"),
            "土法高炉は農家の庭で作られました。",
        )

    def test_text_without_brackets_is_unchanged(self) -> None:
        text = "括弧を含まない普通の文です。"
        self.assertEqual(ai_text._strip_bracket_quotes(text), text)

    def test_none_or_empty_input_returns_empty_string(self) -> None:
        self.assertEqual(ai_text._strip_bracket_quotes(""), "")
        self.assertEqual(ai_text._strip_bracket_quotes(None), "")


class ValidateStripsBracketQuotesTest(unittest.TestCase):
    def _script(self, narration: str) -> dict:
        return {
            "title": "テスト",
            "description": "概要",
            "tags": ["a", "b"],
            "narration": narration,
            "scenes": [{}],
        }

    def test_validate_strips_bracket_quotes_from_narration(self) -> None:
        script = ai_text._validate(
            self._script("これは「問い」から始まる文章です。")
        )
        self.assertEqual(script["narration"], "これは問いから始まる文章です。")

    def test_validate_keeps_narration_without_brackets_unchanged(self) -> None:
        narration = "問いから始まる文章です。"
        script = ai_text._validate(self._script(narration))
        self.assertEqual(script["narration"], narration)

    def test_bracket_adjacent_to_forbidden_phrase_is_not_falsely_flagged(self) -> None:
        # 「突然」ですが → 括弧除去後は「突然ですが」と結合するが、原文には
        # 禁止フレーズが存在しないため cold-open 違反として弾かれてはならない。
        script = ai_text._validate(
            self._script("「突然」ですが、この話には裏があります。")
        )
        self.assertEqual(script["narration"], "突然ですが、この話には裏があります。")

    def test_cold_open_violation_in_raw_narration_still_raises(self) -> None:
        with self.assertRaises(ValueError):
            ai_text._validate(self._script("突然ですが、この話には裏があります。"))


if __name__ == "__main__":
    unittest.main()
