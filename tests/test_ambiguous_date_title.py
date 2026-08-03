"""issue #57: タイトルの過去年月と「変更日/確認日」の取り違えを防ぐテスト。

対象: ai_text.check_ambiguous_date_title / research._attempt (verified_at付与) /
run_daily._apply_ambiguous_date_title_check / youtube.update_title_description。
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, patch

from doci import ai_text, research, run_daily, youtube


def _fact(
    *,
    effective_date: str = "",
    date_role: str = "",
    verified_at: str = "",
    source_url: str = "https://support.google.com/youtube/answer/x",
) -> dict:
    return {
        "claim": "テスト用の事実",
        "source_url": source_url,
        "effective_date": effective_date,
        "date_role": date_role,
        "verified_at": verified_at,
    }


class CheckAmbiguousDateTitleTest(unittest.TestCase):
    def test_no_match_without_date_or_freshness_wording(self) -> None:
        self.assertIsNone(
            ai_text.check_ambiguous_date_title("新規視聴者を増やす3つの工夫", None)
        )

    def test_year_pattern_detected_without_facts_is_unsupported(self) -> None:
        result = ai_text.check_ambiguous_date_title(
            "2025年3月の変更とエンゲージドビューの真実", None
        )
        self.assertIsNotNone(result)
        self.assertIn("year", result["matched_patterns"])
        self.assertFalse(result["supported"])
        self.assertEqual(
            set(result["missing"]),
            {"effective_date", "date_role", "verified_at", "source_url"},
        )

    def test_compound_year_and_revision_patterns_all_detected(self) -> None:
        result = ai_text.check_ambiguous_date_title(
            "【2025年7月改訂】登録者数では見えない「真のファン」を診断する3つの指標",
            None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            set(result["matched_patterns"]), {"year", "month_revision", "revision"}
        )

    def test_number_of_items_is_not_mistaken_for_month(self) -> None:
        result = ai_text.check_ambiguous_date_title("3つの指標を徹底解説", None)
        self.assertIsNone(result)

    def test_supported_when_title_year_matches_effective_date(self) -> None:
        facts = [
            _fact(
                effective_date="2025-03-31",
                date_role="historical_event",
                verified_at="2026-08-03",
            )
        ]
        result = ai_text.check_ambiguous_date_title(
            "2025年3月の変更とエンゲージドビューの真実", facts
        )
        self.assertTrue(result["supported"])
        self.assertEqual(result["missing"], [])

    def test_unsupported_when_effective_date_field_itself_is_missing(self) -> None:
        facts = [
            _fact(
                effective_date="",
                date_role="historical_event",
                verified_at="2026-08-03",
            )
        ]
        result = ai_text.check_ambiguous_date_title(
            "2025年3月の変更とエンゲージドビューの真実", facts
        )
        self.assertFalse(result["supported"])

    def test_title_year_matching_only_verified_at_is_a_confusion(self) -> None:
        # 完了条件4の核: タイトルの年(2025)がeffective_date(2020)ではなく
        # verified_at(2025)にしか対応しない=変更日と確認日の取り違え。
        facts = [
            _fact(
                effective_date="2020-01-01",
                date_role="historical_event",
                verified_at="2025-07-15",
            )
        ]
        result = ai_text.check_ambiguous_date_title(
            "【2025年7月改訂】登録者数では見えない「真のファン」を診断する3つの指標",
            facts,
        )
        self.assertFalse(result["supported"])
        self.assertIn("確認日(verified_at)", result["reason"])

    def test_bare_four_digit_number_is_not_mistaken_for_a_year(self) -> None:
        # レビュー指摘: 「登録者2000人」等の4桁数値は「年」を伴わないため
        # 年として扱わず、日付なしの鮮度表現(current_as_ofの有無)で判定する。
        facts = [
            _fact(
                effective_date="2020-01-01",
                date_role="current_as_of",
                verified_at="2026-08-03",
            )
        ]
        result = ai_text.check_ambiguous_date_title(
            "登録者2000人を超える最新戦略", facts
        )
        self.assertTrue(result["supported"])

    def test_same_year_different_month_is_a_confusion(self) -> None:
        # レビュー指摘: 「7月改訂」と「effective_date=2025-03-31」は同じ年(2025)
        # だが月が異なるため取り違え。年のみの比較では見逃していた。
        facts = [
            _fact(
                effective_date="2025-03-31",
                date_role="historical_event",
                verified_at="2026-08-03",
            )
        ]
        result = ai_text.check_ambiguous_date_title(
            "【2025年7月改訂】登録者数では見えない「真のファン」を診断する3つの指標",
            facts,
        )
        self.assertFalse(result["supported"])

    def test_same_year_same_month_is_supported(self) -> None:
        facts = [
            _fact(
                effective_date="2025-07-01",
                date_role="historical_event",
                verified_at="2026-08-03",
            )
        ]
        result = ai_text.check_ambiguous_date_title(
            "【2025年7月改訂】登録者数では見えない「真のファン」を診断する3つの指標",
            facts,
        )
        self.assertTrue(result["supported"])

    def test_year_only_granularity_fact_cannot_contradict_month(self) -> None:
        # factがYYYY粒度までしか記録していない場合、月の矛盾は確認できない
        # ため年一致のみで根拠として認める(記録されている粒度が限界)。
        facts = [
            _fact(
                effective_date="2025",
                date_role="historical_event",
                verified_at="2026-08-03",
            )
        ]
        result = ai_text.check_ambiguous_date_title(
            "【2025年7月改訂】登録者数では見えない「真のファン」を診断する3つの指標",
            facts,
        )
        self.assertTrue(result["supported"])

    def test_dateless_freshness_wording_supported_only_with_current_as_of(self) -> None:
        unsupported = ai_text.check_ambiguous_date_title(
            "最新版アルゴリズム解説",
            [_fact(effective_date="2025-01-01", date_role="historical_event", verified_at="2026-08-03")],
        )
        self.assertFalse(unsupported["supported"])

        supported = ai_text.check_ambiguous_date_title(
            "最新版アルゴリズム解説",
            [_fact(effective_date="2025-01-01", date_role="current_as_of", verified_at="2026-08-03")],
        )
        self.assertTrue(supported["supported"])


class ResearchVerifiedAtStampingTest(unittest.TestCase):
    def _payload(self, **fact_overrides) -> dict:
        fact = {
            "claim": "確認済みの事実",
            "source_url": "https://support.google.com/youtube/answer/x",
        }
        fact.update(fact_overrides)
        return {
            "topic": "テスト題材",
            "angle": "",
            "canonical_theme": "",
            "format": "",
            "novelty_type": "new",
            "novelty_axis": "",
            "viewpoint": "",
            "comparison_key": "",
            "parent_topic": "",
            "parent_topic_id": "",
            "novelty_reason": "",
            "youtube_creator_audience": "",
            "youtube_creator_problem": "",
            "viewer_action": "",
            "theme_fit": "clear",
            "theme_fit_reason": "",
            "facts": [fact],
        }

    def test_llm_reported_verified_at_is_overwritten_by_code(self) -> None:
        payload = self._payload(verified_at="2000-01-01")
        with mock.patch.object(
            research.llm,
            "run_claude",
            return_value=json.dumps(payload, ensure_ascii=False),
        ):
            result = research._attempt("prompt", backend_override="claude")
        self.assertNotEqual(result["facts"][0]["verified_at"], "2000-01-01")
        self.assertRegex(result["facts"][0]["verified_at"], r"^\d{4}-\d{2}-\d{2}$")

    def test_malformed_effective_date_is_normalized_to_empty(self) -> None:
        payload = self._payload(effective_date="2025年3月")
        with mock.patch.object(
            research.llm,
            "run_claude",
            return_value=json.dumps(payload, ensure_ascii=False),
        ):
            result = research._attempt("prompt", backend_override="claude")
        self.assertEqual(result["facts"][0]["effective_date"], "")

    def test_valid_effective_date_is_preserved(self) -> None:
        payload = self._payload(effective_date="2025-03-31")
        with mock.patch.object(
            research.llm,
            "run_claude",
            return_value=json.dumps(payload, ensure_ascii=False),
        ):
            result = research._attempt("prompt", backend_override="claude")
        self.assertEqual(result["facts"][0]["effective_date"], "2025-03-31")

    def test_unknown_date_role_is_normalized_to_none(self) -> None:
        payload = self._payload(date_role="unknown_value")
        with mock.patch.object(
            research.llm,
            "run_claude",
            return_value=json.dumps(payload, ensure_ascii=False),
        ):
            result = research._attempt("prompt", backend_override="claude")
        self.assertEqual(result["facts"][0]["date_role"], "none")

    def test_valid_date_role_is_preserved(self) -> None:
        payload = self._payload(date_role="historical_event")
        with mock.patch.object(
            research.llm,
            "run_claude",
            return_value=json.dumps(payload, ensure_ascii=False),
        ):
            result = research._attempt("prompt", backend_override="claude")
        self.assertEqual(result["facts"][0]["date_role"], "historical_event")

    def test_prompt_mentions_new_date_fields_and_verification_ownership(self) -> None:
        self.assertIn("effective_date", research._PROMPT)
        self.assertIn("date_role", research._PROMPT)
        self.assertIn("確認日はシステム側が別途記録する", research._PROMPT)


class ApplyAmbiguousDateTitleCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = SimpleNamespace(
            id="youtube-growth",
            pipeline={"ambiguous_date_title_check": True},
        )
        self.spec.pipeline_get = lambda key, default=None: self.spec.pipeline.get(
            key, default
        )

    def test_disabled_by_default_when_pipeline_flag_missing(self) -> None:
        spec = SimpleNamespace(
            pipeline={}, pipeline_get=lambda key, default=None: default
        )
        script = {"title": "2025年3月の変更点"}
        with mock.patch.object(
            ai_text, "check_ambiguous_date_title"
        ) as check_mock:
            run_daily._apply_ambiguous_date_title_check(spec, script)
        check_mock.assert_not_called()
        self.assertNotIn("_ambiguous_date_title_check", script)

    def test_records_match_and_logs_when_unsupported(self) -> None:
        script = {
            "title": "2025年3月の変更とエンゲージドビューの真実",
            "_research": {"facts": []},
        }
        with mock.patch.object(run_daily, "_log") as log_mock:
            run_daily._apply_ambiguous_date_title_check(self.spec, script)
        self.assertTrue(script["_ambiguous_date_title_check"]["checked"])
        match = script["_ambiguous_date_title_check"]["match"]
        self.assertIsNotNone(match)
        self.assertFalse(match["supported"])
        log_mock.assert_called_once()
        self.assertIn("曖昧日付タイトルの疑い", log_mock.call_args.args[0])

    def test_records_no_match_without_logging_for_plain_title(self) -> None:
        script = {"title": "新規視聴者を増やす3つの工夫"}
        with mock.patch.object(run_daily, "_log") as log_mock:
            run_daily._apply_ambiguous_date_title_check(self.spec, script)
        self.assertIsNone(script["_ambiguous_date_title_check"]["match"])
        log_mock.assert_not_called()

    def test_missing_research_is_treated_as_no_facts(self) -> None:
        script = {"title": "2025年3月の変更とエンゲージドビューの真実"}
        run_daily._apply_ambiguous_date_title_check(self.spec, script)
        match = script["_ambiguous_date_title_check"]["match"]
        self.assertFalse(match["supported"])


class UpdateTitleDescriptionTest(unittest.TestCase):
    def test_preserves_other_snippet_fields_on_title_only_update(self) -> None:
        service = MagicMock()
        videos = service.videos.return_value
        videos.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {
                        "title": "旧タイトル",
                        "description": "旧説明",
                        "tags": ["a", "b"],
                        "categoryId": "22",
                        "publishedAt": "2026-01-01T00:00:00Z",
                    }
                }
            ]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()) as creds_mock,
            patch.object(youtube, "_build_service", return_value=service),
        ):
            result = youtube.update_title_description(
                "video123",
                title="新タイトル",
                token_file=None,
                client_secret_file=None,
            )
        self.assertEqual(result, "updated")
        body = videos.update.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["title"], "新タイトル")
        self.assertEqual(body["snippet"]["description"], "旧説明")
        self.assertEqual(body["snippet"]["tags"], ["a", "b"])
        self.assertEqual(body["snippet"]["categoryId"], "22")
        self.assertNotIn("publishedAt", body["snippet"])
        self.assertEqual(creds_mock.call_args.kwargs["scopes"], youtube.MANAGE_SCOPES)

    def test_description_only_update_keeps_current_title(self) -> None:
        service = MagicMock()
        videos = service.videos.return_value
        videos.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "既存タイトル", "description": "旧説明", "categoryId": "22"}}]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            youtube.update_title_description(
                "video123", description="新説明", token_file=None, client_secret_file=None
            )
        body = videos.update.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["title"], "既存タイトル")
        self.assertEqual(body["snippet"]["description"], "新説明")

    def test_expected_title_mismatch_raises_and_does_not_update(self) -> None:
        service = MagicMock()
        videos = service.videos.return_value
        videos.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "実際のタイトル", "categoryId": "22"}}]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            with self.assertRaisesRegex(RuntimeError, "actual='実際のタイトル'"):
                youtube.update_title_description(
                    "video123",
                    title="新タイトル",
                    expected_title="想定タイトル",
                    token_file=None,
                    client_secret_file=None,
                )
        videos.update.assert_not_called()

    def test_unchanged_when_new_values_equal_current(self) -> None:
        service = MagicMock()
        videos = service.videos.return_value
        videos.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "同じ", "description": "同じ説明", "categoryId": "22"}}]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            result = youtube.update_title_description(
                "video123",
                title="同じ",
                description="同じ説明",
                token_file=None,
                client_secret_file=None,
            )
        self.assertEqual(result, "unchanged")
        videos.update.assert_not_called()

    def test_requires_at_least_one_of_title_or_description(self) -> None:
        with self.assertRaises(ValueError):
            youtube.update_title_description("video123")

    def test_rejects_title_with_angle_brackets(self) -> None:
        with self.assertRaises(ValueError):
            youtube.update_title_description("video123", title="悪意 <script>")


if __name__ == "__main__":
    unittest.main()
