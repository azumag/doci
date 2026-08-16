"""issue #185: 急上昇直後の新規・ライト・コアで単発／定着を即判定しないためのテスト。

対象: ai_text.check_segment_immediacy_claim / run_daily._apply_viewer_segment_claim_check /
corner_analytics.md・factcheck._PROMPT のガードレール文言。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from doci import ai_text, channel, corners, factcheck, run_daily


class AnalyticsCornerPromptGuardTest(unittest.TestCase):
    def test_analytics_prompt_contains_segment_timescale_guards(self) -> None:
        youtube_growth = channel.load("youtube-growth")
        corner = youtube_growth.corners["analytics"]
        prompt = corners.build_prompt(youtube_growth, corner, "2026-08-16", [])
        for phrase in (
            "即時的な成果指標として扱いません",
            "過去1年のうち1〜5か月",
            "6か月以上",
            "過去28日間のローリング値",
            "短期の再訪",
            "中長期の定着",
            "複数本・シリーズの傾向",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_guards_are_scoped_to_analytics_corner(self) -> None:
        youtube_growth = channel.load("youtube-growth")
        video_corner = youtube_growth.corners["video"]
        prompt = corners.build_prompt(youtube_growth, video_corner, "2026-08-16", [])
        self.assertNotIn("過去28日間のローリング値", prompt)


class CheckSegmentImmediacyClaimTest(unittest.TestCase):
    def test_issue_185_offending_title_is_detected(self) -> None:
        title = (
            "再生数急上昇の後は新規・ライト・コアで判定。"
            "単発流入か継続視聴かを見分ける手順"
        )
        match = ai_text.check_segment_immediacy_claim(title, "")
        self.assertIsNotNone(match)
        self.assertIn("segment_instant_judgment", match["matched_patterns"])

    def test_recording_segments_without_judgment_is_not_detected(self) -> None:
        narration = (
            "公開後の同じ期間に月間視聴者と新規・ライト・コアの内訳を記録します。"
        )
        match = ai_text.check_segment_immediacy_claim("視聴者構成を記録する", narration)
        self.assertIsNone(match)

    def test_narration_attributing_core_increase_to_single_video_is_detected(
        self,
    ) -> None:
        narration = "コアな視聴者が増えたのは、この一本で視聴者が定着した証拠です。"
        match = ai_text.check_segment_immediacy_claim("視聴者の変化", narration)
        self.assertIsNotNone(match)
        self.assertIn(
            "single_video_retention_attribution", match["matched_patterns"]
        )

    def test_correct_timescale_interpretation_is_not_detected(self) -> None:
        narration = (
            "コアな視聴者は過去1年のうち6か月以上視聴した人の分類なので、"
            "一本の動画の直後には変わりません。"
        )
        match = ai_text.check_segment_immediacy_claim("視聴者分類の考え方", narration)
        self.assertIsNone(match)

    def test_title_teaching_segment_difference_is_not_detected(self) -> None:
        title = "新規・ライト・コアの違いと、中長期で定着を観察する方法"
        match = ai_text.check_segment_immediacy_claim(title, "")
        self.assertIsNone(match)

    def test_negated_instant_judgment_is_not_detected(self) -> None:
        # issue #185向けに追加したガードレール（corner_analytics.md）自体が
        # 促す言い回し。ガードレールに従った望ましい記述を誤検出しないこと。
        narration = "新規・ライト・コアで即判定することはできません。"
        match = ai_text.check_segment_immediacy_claim("視聴者構成の見方", narration)
        self.assertIsNone(match)

    def test_negated_quick_judgment_is_not_detected(self) -> None:
        narration = "コアな視聴者の割合ですぐ分かるものではありません。"
        match = ai_text.check_segment_immediacy_claim("視聴者構成の見方", narration)
        self.assertIsNone(match)

    def test_negated_single_video_attribution_is_not_detected(self) -> None:
        narration = "この一本でコアな視聴者が増えたとは言えません。"
        match = ai_text.check_segment_immediacy_claim("視聴者構成の見方", narration)
        self.assertIsNone(match)


class RunDailyViewerSegmentClaimCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = SimpleNamespace(
            id="youtube-growth",
            pipeline={"viewer_segment_claim_check": True},
        )
        self.spec.pipeline_get = lambda key, default=None: self.spec.pipeline.get(
            key, default
        )

    def test_disabled_by_default_when_pipeline_flag_missing(self) -> None:
        spec = SimpleNamespace(
            pipeline={}, pipeline_get=lambda key, default=None: default
        )
        script = {
            "title": "再生数急上昇の後は新規・ライト・コアで判定",
            "narration": "",
        }
        with mock.patch.object(
            ai_text, "check_segment_immediacy_claim"
        ) as check_mock:
            run_daily._apply_viewer_segment_claim_check(spec, script)
        check_mock.assert_not_called()
        self.assertNotIn("_viewer_segment_claim_check", script)

    def test_match_is_recorded_and_logged(self) -> None:
        script = {
            "title": "再生数急上昇の後は新規・ライト・コアで判定",
            "narration": "",
        }
        with mock.patch.object(run_daily, "_log") as log_mock:
            run_daily._apply_viewer_segment_claim_check(self.spec, script)
        self.assertTrue(script["_viewer_segment_claim_check"]["checked"])
        match = script["_viewer_segment_claim_check"]["match"]
        self.assertIsNotNone(match)
        log_mock.assert_called_once()
        self.assertIn("視聴者セグメント即時判定表現の疑い", log_mock.call_args.args[0])

    def test_no_match_is_recorded_as_checked(self) -> None:
        script = {"title": "新規視聴者を増やす3つの工夫", "narration": ""}
        with mock.patch.object(run_daily, "_log") as log_mock:
            run_daily._apply_viewer_segment_claim_check(self.spec, script)
        self.assertEqual(
            script["_viewer_segment_claim_check"],
            {"checked": True, "match": None},
        )
        log_mock.assert_not_called()


class FactcheckPromptGuardTest(unittest.TestCase):
    def test_factcheck_prompt_keeps_segment_timescale_rule(self) -> None:
        self.assertIn("過去28日間のローリング値", factcheck._PROMPT)
        self.assertIn("6か月以上", factcheck._PROMPT)


if __name__ == "__main__":
    unittest.main()
