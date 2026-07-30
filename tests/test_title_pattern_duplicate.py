"""issue #37: タイトルの修辞パターン重複検出のテスト。

対象: ai_text.check_title_pattern_duplicate、
run_daily._apply_title_pattern_check。
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from doci import ai_text, run_daily


class CheckTitlePatternDuplicateTest(unittest.TestCase):
    def test_llm_flags_matching_title_as_pattern_duplicate(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps(
                {
                    "duplicate": True,
                    "matched_index": 2,
                    "overlapping_axes": ["proper_noun", "rhetorical_template"],
                    "confidence": 0.82,
                    "reason": "同じ固有名詞と疑問形の使い回し",
                }
            ),
        ):
            result = ai_text.check_title_pattern_duplicate(
                "編集のテンプレ化で離脱される？トヨタのカイゼンに学ぶ",
                ["候補1", "『改善』がチャンネルを殺す？トヨタとAmazonの事例から学ぶ", "候補3"],
            )
        self.assertIsNotNone(result)
        self.assertEqual(
            result["matched_title"],
            "『改善』がチャンネルを殺す？トヨタとAmazonの事例から学ぶ",
        )
        self.assertEqual(result["confidence"], 0.82)
        self.assertEqual(
            result["overlapping_axes"], ["proper_noun", "rhetorical_template"]
        )
        self.assertIn("疑問形", result["reason"])

    def test_llm_says_not_duplicate_returns_none(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps({"duplicate": False}),
        ):
            result = ai_text.check_title_pattern_duplicate("新しいタイトル", ["候補1"])
        self.assertIsNone(result)

    def test_dispatch_failure_returns_none_instead_of_raising(self) -> None:
        with mock.patch.object(
            ai_text, "_dispatch", side_effect=RuntimeError("backend down")
        ):
            result = ai_text.check_title_pattern_duplicate("新しいタイトル", ["候補1"])
        self.assertIsNone(result)

    def test_out_of_range_matched_index_falls_back_to_first_candidate(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps({"duplicate": True, "matched_index": 99}),
        ):
            result = ai_text.check_title_pattern_duplicate(
                "新しいタイトル", ["候補1", "候補2"]
            )
        self.assertEqual(result["matched_title"], "候補1")
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["overlapping_axes"], [])

    def test_no_recent_titles_short_circuits_without_dispatch(self) -> None:
        with mock.patch.object(ai_text, "_dispatch") as dispatch:
            result = ai_text.check_title_pattern_duplicate("新しいタイトル", [])
        dispatch.assert_not_called()
        self.assertIsNone(result)

    def test_non_string_overlapping_axes_are_dropped(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps(
                {
                    "duplicate": True,
                    "matched_index": 1,
                    "overlapping_axes": ["proper_noun", 42, None],
                    "confidence": 0.7,
                }
            ),
        ):
            result = ai_text.check_title_pattern_duplicate("新しいタイトル", ["候補1"])
        self.assertEqual(result["overlapping_axes"], ["proper_noun"])


class ApplyTitlePatternCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = SimpleNamespace(
            id="youtube-growth",
            pipeline={"title_pattern_check": True},
        )
        self.spec.pipeline_get = lambda key, default=None: self.spec.pipeline.get(
            key, default
        )

    def test_disabled_by_default_when_pipeline_flag_missing(self) -> None:
        spec = SimpleNamespace(pipeline={}, pipeline_get=lambda key, default=None: default)
        script = {"title": "新しいタイトル"}
        with mock.patch.object(
            ai_text, "check_title_pattern_duplicate"
        ) as check_mock:
            run_daily._apply_title_pattern_check(spec, script, ["過去のタイトル"], 30)
        check_mock.assert_not_called()
        self.assertNotIn("_title_pattern_check", script)

    def test_skipped_when_cooldown_days_is_zero(self) -> None:
        script = {"title": "新しいタイトル"}
        with mock.patch.object(
            ai_text, "check_title_pattern_duplicate"
        ) as check_mock:
            run_daily._apply_title_pattern_check(
                self.spec, script, ["過去のタイトル"], 0
            )
        check_mock.assert_not_called()
        self.assertNotIn("_title_pattern_check", script)

    def test_records_match_and_logs_when_duplicate_found(self) -> None:
        script = {"title": "新しいタイトル"}
        match = {
            "matched_title": "過去のタイトル",
            "confidence": 0.9,
            "overlapping_axes": ["proper_noun", "problem_word"],
            "reason": "同じ固有名詞と問題語",
        }
        with (
            mock.patch.object(
                ai_text, "check_title_pattern_duplicate", return_value=match
            ) as check_mock,
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_title_pattern_check(
                self.spec, script, ["過去のタイトル"], 30
            )
        check_mock.assert_called_once_with("新しいタイトル", ["過去のタイトル"])
        self.assertEqual(
            script["_title_pattern_check"], {"checked": True, "match": match}
        )
        log_mock.assert_called_once()
        self.assertIn("タイトル修辞パターン重複の疑い", log_mock.call_args.args[0])

    def test_records_no_match_without_logging(self) -> None:
        script = {"title": "新しいタイトル"}
        with (
            mock.patch.object(
                ai_text, "check_title_pattern_duplicate", return_value=None
            ),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_title_pattern_check(
                self.spec, script, ["過去のタイトル"], 30
            )
        self.assertEqual(
            script["_title_pattern_check"], {"checked": True, "match": None}
        )
        log_mock.assert_not_called()

    def test_check_failure_is_recorded_as_no_match_without_raising(self) -> None:
        script = {"title": "新しいタイトル"}
        with (
            mock.patch.object(
                ai_text,
                "check_title_pattern_duplicate",
                side_effect=RuntimeError("backend down"),
            ),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_title_pattern_check(
                self.spec, script, ["過去のタイトル"], 30
            )
        self.assertEqual(
            script["_title_pattern_check"], {"checked": True, "match": None}
        )
        log_mock.assert_called_once()
        self.assertIn("判定に失敗", log_mock.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
