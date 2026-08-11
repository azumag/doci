"""Issues #165/#171: YouTube終了画面の比較実験記録。"""
from __future__ import annotations

import json
import math
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import end_screen


class EndScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=root / "output" / "youtube-growth",
            history_file=root / "output" / "youtube-growth" / "history.jsonl",
        )
        self.spec.history_file.parent.mkdir(parents=True)
        self.video_id = "AbCdEf12345"
        self.link_video_id = "ZzYyXx98765"
        self.now = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
        self.complete_now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
        self._write_history()

    def _write_history(self, *, target_overrides: dict | None = None, **overrides) -> None:
        row = {
            "ts": "2026-08-10T00:00:00+00:00",
            "channel": "youtube-growth",
            "corner": "video",
            "title": "現在のタイトル",
            "video_id": self.video_id,
            "status": "published",
            "tier": "longform",
            "youtube_privacy": "unlisted",
            "workdir": "/tmp/workdir",
        }
        row.update(overrides)
        target = {
            "ts": "2026-08-09T00:00:00+00:00",
            "channel": "youtube-growth",
            "corner": "video",
            "title": "遷移先のタイトル",
            "video_id": self.link_video_id,
            "status": "published",
            "tier": "longform",
            "youtube_privacy": "public",
            "workdir": "/tmp/target-workdir",
        }
        target.update(target_overrides or {})
        self.spec.history_file.write_text(
            "\n".join(
                json.dumps(item, ensure_ascii=False) for item in (row, target)
            )
            + "\n",
            encoding="utf-8",
        )

    def _plan(self, experiment_id: str = "esc-0000000000000001") -> dict:
        return end_screen.plan_experiment(
            self.spec,
            video_id=self.video_id,
            link_video_id=self.link_video_id,
            variant=end_screen.SINGLE_VARIANT,
            comparison_key="同ジャンル-通常動画",
            observation_days=7,
            content_direct_confirmed=True,
            now=self.now,
            experiment_id=experiment_id,
        )

    @staticmethod
    def _extra_element(
        element_type: str,
        *,
        position: str = "bottom_right",
        reference: str | None = None,
    ) -> dict:
        if element_type == "subscribe":
            selection = "current_channel"
            reference = None
        else:
            selection = "specific"
            reference = reference or {
                "video": "ExtraVid0001",
                "playlist": "PL-example-playlist",
                "channel": "UC-example-channel",
                "link": "https://example.com/next",
            }[element_type]
        return {
            "type": element_type,
            "selection": selection,
            "reference": reference,
            "timing": "last_20_seconds_to_end",
            "position": position,
        }

    def _start(self, experiment_id: str = "esc-0000000000000001") -> dict:
        return end_screen.start_experiment(
            self.spec,
            experiment_id,
            studio_setup_confirmed=True,
            now=self.now,
        )

    def _complete_observed(
        self,
        experiment_id: str = "esc-0000000000000001",
        **overrides,
    ) -> dict:
        kwargs = {
            "sample_sufficient": True,
            "click_rate": 3.5,
            "end_screen_traffic_views": 12,
            "period_data_complete_confirmed": True,
            "setup_unchanged_confirmed": True,
            "now": self.complete_now,
        }
        kwargs.update(overrides)
        return end_screen.complete_experiment(self.spec, experiment_id, **kwargs)

    def _manifest_file(self, experiment_id: str) -> Path:
        return (
            self.spec.output_dir
            / "end_screen_tests"
            / experiment_id
            / "manifest.json"
        )

    def test_plan_records_single_video_as_experiment_variant_without_youtube_write(self) -> None:
        manifest = self._plan()

        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["decision_metric"], "two_stage_end_screen_transition")
        setup = manifest["end_screen_setup"]
        self.assertEqual(setup["variant"], "single_related_video")
        self.assertEqual(setup["target_element_type"], "video")
        self.assertEqual(setup["target_video_id"], self.link_video_id)
        self.assertEqual(setup["target_timing"], "last_20_seconds_to_end")
        self.assertEqual(setup["target_position"], "center")
        self.assertEqual(len(setup["setup_signature"]), 64)
        self.assertEqual(
            setup["comparison_profile"]["reference_normalization"],
            "specific_ids_and_urls_excluded",
        )
        self.assertEqual(setup["element_count"], 1)
        self.assertEqual(setup["extra_elements"], [])
        self.assertEqual(setup["extra_element_types"], [])
        self.assertEqual(manifest["observation_days"], 7)
        self.assertEqual(manifest["comparison_key"], "同ジャンル-通常動画")
        self.assertFalse(manifest["measurement"]["source_specific_attribution"])
        root = self.spec.output_dir / "end_screen_tests" / manifest["experiment_id"]
        self.assertTrue((root / "manifest.json").is_file())
        plan = (root / "plan.md").read_text(encoding="utf-8")
        self.assertIn("1枠は最適解ではなく実験条件", plan)
        self.assertIn("全終了画面の集計", plan)
        self.assertIn(end_screen.OFFICIAL_HELP_URL, plan)

    def test_plan_records_multi_element_baseline(self) -> None:
        manifest = end_screen.plan_experiment(
            self.spec,
            video_id=self.video_id,
            link_video_id=self.link_video_id,
            variant=end_screen.MULTI_VARIANT,
            extra_elements=(
                self._extra_element("subscribe", position="bottom_right"),
                self._extra_element("playlist", position="top_left"),
            ),
            comparison_key="同ジャンル-通常動画",
            observation_days=28,
            content_direct_confirmed=True,
            now=self.now,
            experiment_id="esc-0000000000000002",
        )

        setup = manifest["end_screen_setup"]
        self.assertEqual(setup["variant"], "multi_element_baseline")
        self.assertEqual(setup["element_count"], 3)
        self.assertEqual(setup["extra_element_types"], ["subscribe", "playlist"])
        self.assertEqual(
            setup["extra_elements"][1]["reference"], "PL-example-playlist"
        )
        self.assertEqual(manifest["observation_days"], 28)

    def test_plan_requires_reproducible_extra_element_configuration(self) -> None:
        invalid_playlist = self._extra_element("playlist")
        invalid_playlist["reference"] = None
        duplicate_subscribe = (
            self._extra_element("subscribe", position="bottom_left"),
            self._extra_element("subscribe", position="bottom_right"),
        )
        position_collision = (
            self._extra_element("playlist", position="center"),
        )
        cases = (
            ((invalid_playlist,), "reference must be a string"),
            (duplicate_subscribe, "only one subscribe"),
            (position_collision, "positions must be unique"),
        )
        for index, (extras, message) in enumerate(cases):
            with self.subTest(message=message):
                with self.assertRaisesRegex(end_screen.EndScreenError, message):
                    end_screen.plan_experiment(
                        self.spec,
                        video_id=self.video_id,
                        link_video_id=self.link_video_id,
                        variant=end_screen.MULTI_VARIANT,
                        extra_elements=extras,
                        comparison_key="再現可能baseline",
                        content_direct_confirmed=True,
                        experiment_id=f"esc-{index + 300:016d}",
                    )

    def test_extra_element_reference_is_covered_by_plan_checksum(self) -> None:
        experiment_id = "esc-0000000000000303"
        end_screen.plan_experiment(
            self.spec,
            video_id=self.video_id,
            link_video_id=self.link_video_id,
            variant=end_screen.MULTI_VARIANT,
            extra_elements=(self._extra_element("playlist"),),
            comparison_key="再現可能baseline",
            content_direct_confirmed=True,
            experiment_id=experiment_id,
        )
        path = self._manifest_file(experiment_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["extra_elements"][0]["reference"] = "PL-changed"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "checksum mismatch"):
            end_screen.start_experiment(
                self.spec,
                experiment_id,
                studio_setup_confirmed=True,
            )

    def test_setup_signature_normalizes_content_specific_references(self) -> None:
        manifests = []
        for index, playlist_id in enumerate(("PL-first", "PL-second")):
            source_id = f"NormSource{index:02d}"
            self._write_history(video_id=source_id)
            manifests.append(
                end_screen.plan_experiment(
                    self.spec,
                    video_id=source_id,
                    link_video_id=self.link_video_id,
                    variant=end_screen.MULTI_VARIANT,
                    extra_elements=(
                        self._extra_element("playlist", reference=playlist_id),
                    ),
                    comparison_key="normalized-references",
                    content_direct_confirmed=True,
                    experiment_id=f"esc-{index + 305:016d}",
                )
            )

        first_setup = manifests[0]["end_screen_setup"]
        second_setup = manifests[1]["end_screen_setup"]
        self.assertNotEqual(
            first_setup["extra_elements"][0]["reference"],
            second_setup["extra_elements"][0]["reference"],
        )
        self.assertEqual(first_setup["setup_signature"], second_setup["setup_signature"])
        self.assertEqual(
            first_setup["comparison_profile"]["extras"][0]["reference_scope"],
            "content_specific",
        )

    def test_plan_rejects_variant_shape_observation_and_comparison_key(self) -> None:
        cases = (
            ({"variant": end_screen.SINGLE_VARIANT, "extra_elements": (self._extra_element("subscribe"),)}, "must not have extra"),
            ({"variant": end_screen.MULTI_VARIANT, "extra_elements": ()}, "requires 1 to 3"),
            (
                {
                    "variant": end_screen.MULTI_VARIANT,
                    "extra_elements": tuple(
                        self._extra_element("video", position=f"position-{item}", reference=f"ExtraVid{item:05d}")
                        for item in range(4)
                    ),
                },
                "requires 1 to 3",
            ),
            ({"observation_days": 14}, "must be 7 or 28"),
            ({"comparison_key": "bad\nkey"}, "without newlines"),
        )
        for index, (overrides, message) in enumerate(cases):
            kwargs = {
                "video_id": self.video_id,
                "link_video_id": self.link_video_id,
                "variant": end_screen.SINGLE_VARIANT,
                "extra_elements": (),
                "comparison_key": "同ジャンル",
                "observation_days": 7,
                "content_direct_confirmed": True,
                "experiment_id": f"esc-{index + 70:016d}",
            }
            kwargs.update(overrides)
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(end_screen.EndScreenError, message):
                    end_screen.plan_experiment(self.spec, **kwargs)

    def test_plan_requires_same_channel_published_longform_target(self) -> None:
        for field, value, message in (
            ("tier", "short", "not available for Shorts"),
            ("status", "publishing", "target video is not recorded as published"),
            ("youtube_privacy", "private", "target video privacy"),
        ):
            with self.subTest(field=field):
                self._write_history(target_overrides={field: value})
                with self.assertRaisesRegex(end_screen.EndScreenError, message):
                    self._plan()
        self._write_history()

    def test_plan_requires_content_direct_confirmation(self) -> None:
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "directly continues",
        ):
            end_screen.plan_experiment(
                self.spec,
                video_id=self.video_id,
                link_video_id=self.link_video_id,
            )

    def test_plan_rejects_self_link(self) -> None:
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "must differ",
        ):
            end_screen.plan_experiment(
                self.spec,
                video_id=self.video_id,
                link_video_id=self.video_id,
                content_direct_confirmed=True,
            )

    def test_plan_rejects_short_private_or_unpublished_videos(self) -> None:
        for field, value, message in (
            ("tier", "short", "not available for Shorts"),
            ("youtube_privacy", "private", "public or unlisted"),
            ("status", "publishing", "not recorded as published"),
        ):
            with self.subTest(field=field, value=value):
                self._write_history(**{field: value})
                with self.assertRaisesRegex(end_screen.EndScreenError, message):
                    end_screen.plan_experiment(
                        self.spec,
                        video_id=self.video_id,
                        link_video_id=self.link_video_id,
                        content_direct_confirmed=True,
                    )
                self._write_history()

    def test_plan_rejects_duplicate_active_test_for_video(self) -> None:
        self._plan("esc-0000000000000001")
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "active end screen test already exists",
        ):
            self._plan("esc-0000000000000002")

    def test_start_requires_studio_setup_confirmation(self) -> None:
        self._plan()
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "confirm that the planned end screen",
        ):
            end_screen.start_experiment(self.spec, "esc-0000000000000001")

    def test_start_moves_planned_to_running(self) -> None:
        self._plan()
        manifest = self._start()
        self.assertEqual(manifest["status"], "running")
        self.assertIn("started_at", manifest)
        self.assertEqual(manifest["observation_start_date"], "2026-08-11")
        self.assertEqual(manifest["observation_end_date"], "2026-08-17")

    def test_observation_window_is_derived_from_started_at(self) -> None:
        self._plan()
        self._start()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["observation_start_date"] = "2026-08-10"
        data["observation_end_date"] = "2026-08-16"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "must follow"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_complete_requires_running_and_confirmation(self) -> None:
        self._plan()
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "only a running",
        ):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                sample_sufficient=True,
                click_rate=3.5,
                end_screen_traffic_views=12,
                period_data_complete_confirmed=True,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )
        self._start()
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "was not manually changed",
        ):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                sample_sufficient=True,
                click_rate=3.5,
                end_screen_traffic_views=12,
                period_data_complete_confirmed=True,
                now=self.complete_now,
            )

    def test_complete_records_two_stage_metrics_and_memo(self) -> None:
        self._plan()
        self._start()
        manifest = self._complete_observed(
            notes="次の一本の冒頭が視聴された",
        )
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["result"]["outcome"], "observed")
        self.assertEqual(manifest["result"]["click_rate"], 3.5)
        self.assertEqual(manifest["result"]["end_screen_traffic_views"], 12)
        self.assertTrue(manifest["result"]["sample_sufficient"])
        memo = (
            self.spec.output_dir
            / "end_screen_tests"
            / "esc-0000000000000001"
            / "next_idea_memo.md"
        ).read_text(encoding="utf-8")
        self.assertIn("終了画面の比較実験", memo)
        self.assertIn("3.5", memo)
        self.assertIn("全終了画面の集計", memo)

    def test_complete_waits_for_fixed_observation_window(self) -> None:
        self._plan()
        self._start()
        with self.assertRaisesRegex(end_screen.EndScreenError, "observation window"):
            self._complete_observed(now=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc))

    def test_complete_records_low_views_without_promoting_it_to_observed(self) -> None:
        self._plan()
        self._start()
        manifest = end_screen.complete_experiment(
            self.spec,
            "esc-0000000000000001",
            sample_sufficient=False,
            insufficient_reason="low_views",
            click_rate=0.0,
            end_screen_traffic_views=0,
            period_data_complete_confirmed=True,
            setup_unchanged_confirmed=True,
            now=self.complete_now,
        )

        self.assertEqual(manifest["result"]["outcome"], "insufficient_views")
        self.assertFalse(manifest["result"]["sample_sufficient"])
        summary = end_screen.summary_experiments(self.spec)
        self.assertEqual(summary["groups"], [])
        self.assertEqual(summary["non_observed_experiments_excluded"], 1)

    def test_complete_records_analytics_unavailable_as_null_not_zero(self) -> None:
        self._plan()
        self._start()
        manifest = end_screen.complete_experiment(
            self.spec,
            "esc-0000000000000001",
            sample_sufficient=False,
            insufficient_reason="analytics_unavailable",
            missing_metrics=("click_rate", "end_screen_traffic_views"),
            setup_unchanged_confirmed=True,
            now=self.complete_now,
        )

        result = manifest["result"]
        self.assertEqual(result["outcome"], "insufficient_views")
        self.assertIsNone(result["click_rate"])
        self.assertIsNone(result["end_screen_traffic_views"])
        self.assertEqual(
            result["missing_metrics"],
            ["click_rate", "end_screen_traffic_views"],
        )

    def test_complete_observed_requires_complete_period_confirmation(self) -> None:
        self._plan()
        self._start()
        with self.assertRaisesRegex(end_screen.EndScreenError, "period-data-complete"):
            self._complete_observed(period_data_complete_confirmed=False)

    def test_summary_requires_multiple_runs_per_variant_and_never_names_winner(self) -> None:
        values = (
            (end_screen.SINGLE_VARIANT, (), 2.0),
            (end_screen.MULTI_VARIANT, ("subscribe",), 3.0),
            (end_screen.SINGLE_VARIANT, (), 4.0),
            (end_screen.MULTI_VARIANT, ("subscribe",), 5.0),
        )
        for index, (variant, extra_types, rate) in enumerate(values):
            source_id = f"SourceVid{index:02d}"
            experiment_id = f"esc-{index + 80:016d}"
            self._write_history(video_id=source_id)
            extras = tuple(
                self._extra_element(item, position=f"position-{extra_index}")
                for extra_index, item in enumerate(extra_types)
            )
            end_screen.plan_experiment(
                self.spec,
                video_id=source_id,
                link_video_id=self.link_video_id,
                variant=variant,
                extra_elements=extras,
                comparison_key="解説-8分級",
                observation_days=7,
                content_direct_confirmed=True,
                now=self.now,
                experiment_id=experiment_id,
            )
            end_screen.start_experiment(
                self.spec,
                experiment_id,
                studio_setup_confirmed=True,
                now=self.now,
            )
            end_screen.complete_experiment(
                self.spec,
                experiment_id,
                sample_sufficient=True,
                click_rate=rate,
                end_screen_traffic_views=12,
                period_data_complete_confirmed=True,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )
            if index == 1:
                early = end_screen.summary_experiments(self.spec)["groups"][0]
                self.assertEqual(
                    early["status"], "insufficient_comparable_experiments"
                )

        group = end_screen.summary_experiments(self.spec)["groups"][0]
        self.assertEqual(group["status"], "ready_for_descriptive_comparison")
        single_stats = group["variants"][end_screen.SINGLE_VARIANT]
        self.assertEqual(single_stats["setup_profile_count"], 1)
        single_profile = single_stats["profiles"][0]
        self.assertEqual(
            single_profile["distinct_source_video_count"],
            2,
        )
        self.assertEqual(single_profile["median_click_rate"], 3.0)
        self.assertEqual(len(single_profile["setup_signature"]), 64)
        self.assertEqual(
            single_profile["comparison_profile"]["reference_normalization"],
            "specific_ids_and_urls_excluded",
        )
        self.assertEqual(len(group["shared_target_signature"]), 64)
        self.assertEqual(
            group["cross_variant_target_profiles"][end_screen.SINGLE_VARIANT],
            group["cross_variant_target_profiles"][end_screen.MULTI_VARIANT],
        )
        self.assertNotIn(
            "median_end_screen_traffic_views",
            group["variants"][end_screen.MULTI_VARIANT]["profiles"][0],
        )
        context = group["target_end_screen_traffic_context"]
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["end_screen_traffic_views"], 12)
        self.assertEqual(context[0]["source_video_count"], 4)
        self.assertEqual(
            context[0]["interpretation"],
            "context_only_not_source_or_variant_attributed",
        )
        self.assertIsNone(group["winner"])
        self.assertEqual(group["interpretation"], "descriptive_only_no_causal_claim")

    def test_summary_counts_distinct_source_videos_not_repeated_manifests(self) -> None:
        self._plan()
        self._start()
        self._complete_observed()

        original = json.loads(
            self._manifest_file("esc-0000000000000001").read_text(encoding="utf-8")
        )
        duplicate_id = "esc-0000000000000099"
        duplicate = {**original, "experiment_id": duplicate_id}
        duplicate["plan_sha256"] = end_screen._plan_checksum(duplicate)
        directory = self._manifest_file(duplicate_id).parent
        directory.mkdir(parents=True)
        self._manifest_file(duplicate_id).write_text(
            json.dumps(duplicate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        group = end_screen.summary_experiments(self.spec)["groups"][0]
        stats = group["variants"][end_screen.SINGLE_VARIANT]
        self.assertEqual(stats["observed_experiment_count"], 2)
        self.assertEqual(stats["profiles"][0]["distinct_source_video_count"], 1)
        self.assertEqual(group["status"], "insufficient_comparable_experiments")

    def test_summary_rejects_corrupt_completed_result(self) -> None:
        self._plan()
        self._start()
        self._complete_observed()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["result"]["click_rate"] = 101.0
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(end_screen.EndScreenError, "between 0 and 100"):
            end_screen.summary_experiments(self.spec)

    def test_summary_holds_when_multi_setup_profiles_differ(self) -> None:
        for index, element_type in enumerate(("subscribe", "playlist")):
            source_id = f"MixedMulti{index:02d}"
            experiment_id = f"esc-{index + 400:016d}"
            self._write_history(video_id=source_id)
            end_screen.plan_experiment(
                self.spec,
                video_id=source_id,
                link_video_id=self.link_video_id,
                variant=end_screen.MULTI_VARIANT,
                extra_elements=(self._extra_element(element_type),),
                comparison_key="profile-mix",
                content_direct_confirmed=True,
                now=self.now,
                experiment_id=experiment_id,
            )
            end_screen.start_experiment(
                self.spec,
                experiment_id,
                studio_setup_confirmed=True,
                now=self.now,
            )
            self._complete_observed(experiment_id)

        group = end_screen.summary_experiments(self.spec)["groups"][0]
        stats = group["variants"][end_screen.MULTI_VARIANT]
        self.assertEqual(group["status"], "incompatible_setup_profiles")
        self.assertEqual(stats["setup_profile_count"], 2)
        self.assertIsNone(stats["aggregate_median_click_rate"])
        self.assertEqual(
            {profile["comparison_profile"]["extras"][0]["type"] for profile in stats["profiles"]},
            {"subscribe", "playlist"},
        )

    def test_summary_holds_when_target_timing_profiles_differ(self) -> None:
        for index, timing in enumerate(
            ("last_20_seconds_to_end", "last_10_seconds_to_end")
        ):
            source_id = f"MixedTiming{index:02d}"
            experiment_id = f"esc-{index + 410:016d}"
            self._write_history(video_id=source_id)
            end_screen.plan_experiment(
                self.spec,
                video_id=source_id,
                link_video_id=self.link_video_id,
                variant=end_screen.SINGLE_VARIANT,
                target_timing=timing,
                comparison_key="timing-mix",
                content_direct_confirmed=True,
                now=self.now,
                experiment_id=experiment_id,
            )
            end_screen.start_experiment(
                self.spec,
                experiment_id,
                studio_setup_confirmed=True,
                now=self.now,
            )
            self._complete_observed(experiment_id)

        group = end_screen.summary_experiments(self.spec)["groups"][0]
        stats = group["variants"][end_screen.SINGLE_VARIANT]
        self.assertEqual(group["status"], "incompatible_setup_profiles")
        self.assertEqual(stats["setup_profile_count"], 2)
        self.assertEqual(
            {
                profile["comparison_profile"]["target"]["timing"]
                for profile in stats["profiles"]
            },
            {"last_20_seconds_to_end", "last_10_seconds_to_end"},
        )

    def test_summary_requires_same_target_profile_across_variants(self) -> None:
        cases = (
            (
                "cross-target-timing",
                ("last_20_seconds_to_end", "center"),
                ("last_10_seconds_to_end", "center"),
            ),
            (
                "cross-target-position",
                ("last_20_seconds_to_end", "center"),
                ("last_20_seconds_to_end", "bottom_right"),
            ),
        )
        for case_index, (comparison_key, single_target, multi_target) in enumerate(cases):
            for variant_index, (variant, target_profile) in enumerate(
                (
                    (end_screen.SINGLE_VARIANT, single_target),
                    (end_screen.MULTI_VARIANT, multi_target),
                )
            ):
                for source_index in range(2):
                    source_id = f"Cross{case_index}{variant_index}{source_index}Vid"
                    experiment_id = (
                        f"esc-{case_index * 10 + variant_index * 2 + source_index + 500:016d}"
                    )
                    self._write_history(video_id=source_id)
                    extras = (
                        (self._extra_element("subscribe", position="top_left"),)
                        if variant == end_screen.MULTI_VARIANT
                        else ()
                    )
                    end_screen.plan_experiment(
                        self.spec,
                        video_id=source_id,
                        link_video_id=self.link_video_id,
                        variant=variant,
                        extra_elements=extras,
                        target_timing=target_profile[0],
                        target_position=target_profile[1],
                        comparison_key=comparison_key,
                        content_direct_confirmed=True,
                        now=self.now,
                        experiment_id=experiment_id,
                    )
                    end_screen.start_experiment(
                        self.spec,
                        experiment_id,
                        studio_setup_confirmed=True,
                        now=self.now,
                    )
                    self._complete_observed(experiment_id)

            groups = end_screen.summary_experiments(self.spec)["groups"]
            group = next(
                item for item in groups if item["comparison_key"] == comparison_key
            )
            self.assertEqual(
                group["status"], "incompatible_cross_variant_target_profile"
            )
            self.assertIsNone(group["shared_target_profile"])
            self.assertIsNone(group["shared_target_signature"])

    def test_same_source_can_be_observed_once_per_variant_but_not_twice_in_one(self) -> None:
        self._plan()
        self._start()
        self._complete_observed()

        repeated_id = "esc-0000000000000100"
        with self.assertRaisesRegex(end_screen.EndScreenError, "already exists"):
            end_screen.plan_experiment(
                self.spec,
                video_id=self.video_id,
                link_video_id=self.link_video_id,
                variant=end_screen.SINGLE_VARIANT,
                comparison_key="同ジャンル-通常動画",
                content_direct_confirmed=True,
                now=self.now,
                experiment_id=repeated_id,
            )
        multi_id = "esc-0000000000000101"
        end_screen.plan_experiment(
            self.spec,
            video_id=self.video_id,
            link_video_id=self.link_video_id,
            variant=end_screen.MULTI_VARIANT,
            extra_elements=(self._extra_element("subscribe"),),
            comparison_key="同ジャンル-通常動画",
            content_direct_confirmed=True,
            now=self.now,
            experiment_id=multi_id,
        )
        end_screen.start_experiment(
            self.spec,
            multi_id,
            studio_setup_confirmed=True,
            now=self.now,
        )
        self._complete_observed(multi_id)
        group = end_screen.summary_experiments(self.spec)["groups"][0]
        self.assertEqual(
            group["variants"][end_screen.SINGLE_VARIANT]["profiles"][0][
                "distinct_source_video_count"
            ],
            1,
        )
        self.assertEqual(
            group["variants"][end_screen.MULTI_VARIANT]["profiles"][0][
                "distinct_source_video_count"
            ],
            1,
        )

    def test_same_target_period_rejects_conflicting_total_traffic(self) -> None:
        self._plan()
        self._start()
        self._complete_observed(end_screen_traffic_views=12)

        second_id = "esc-0000000000000102"
        second_source = "SecondSource01"
        self._write_history(video_id=second_source)
        end_screen.plan_experiment(
            self.spec,
            video_id=second_source,
            link_video_id=self.link_video_id,
            comparison_key="同ジャンル-通常動画",
            content_direct_confirmed=True,
            now=self.now,
            experiment_id=second_id,
        )
        end_screen.start_experiment(
            self.spec,
            second_id,
            studio_setup_confirmed=True,
            now=self.now,
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "traffic must match"):
            self._complete_observed(second_id, end_screen_traffic_views=13)

    def test_complete_rejects_out_of_range_click_rate(self) -> None:
        self._plan()
        self._start()
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "between 0 and 100",
        ):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                sample_sufficient=True,
                click_rate=101.0,
                end_screen_traffic_views=12,
                period_data_complete_confirmed=True,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )

    def test_complete_analytics_unavailable_keeps_each_available_metric(self) -> None:
        cases = (
            (("end_screen_traffic_views",), 3.0, None, True),
            (("click_rate",), None, 12, True),
            (("click_rate", "end_screen_traffic_views"), None, None, False),
        )
        for index, (missing, click_rate, traffic_views, period_complete) in enumerate(cases):
            experiment_id = f"esc-{index + 200:016d}"
            source_id = f"PartialVid{index:02d}"
            self._write_history(video_id=source_id)
            end_screen.plan_experiment(
                self.spec,
                video_id=source_id,
                link_video_id=self.link_video_id,
                content_direct_confirmed=True,
                now=self.now,
                experiment_id=experiment_id,
            )
            end_screen.start_experiment(
                self.spec,
                experiment_id,
                studio_setup_confirmed=True,
                now=self.now,
            )
            result = end_screen.complete_experiment(
                self.spec,
                experiment_id,
                sample_sufficient=False,
                insufficient_reason="analytics_unavailable",
                missing_metrics=missing,
                click_rate=click_rate,
                end_screen_traffic_views=traffic_views,
                period_data_complete_confirmed=period_complete,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )["result"]
            self.assertEqual(result["missing_metrics"], sorted(missing))
            self.assertEqual(result["click_rate"], click_rate)
            self.assertEqual(result["end_screen_traffic_views"], traffic_views)

    def test_complete_rejects_value_for_declared_missing_metric(self) -> None:
        self._plan()
        self._start()
        with self.assertRaisesRegex(end_screen.EndScreenError, "missing click_rate"):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                sample_sufficient=False,
                insufficient_reason="analytics_unavailable",
                missing_metrics=("click_rate",),
                click_rate=3.0,
                end_screen_traffic_views=12,
                period_data_complete_confirmed=True,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )

    def test_show_returns_saved_manifest(self) -> None:
        self._plan()
        manifest = end_screen.show_experiment(self.spec, "esc-0000000000000001")
        self.assertEqual(manifest["experiment_id"], "esc-0000000000000001")
        self.assertEqual(manifest["video_id"], self.video_id)

    def test_plan_rejects_checksum_mismatch_on_start(self) -> None:
        """計画後のmanifest改変（リンク先変更・制約フラグ偽）をstartで拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["comparison_key"] = "改変済みcohort"
        data["plan_sha256"] = "f" * 64
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "checksum mismatch"):
            end_screen.start_experiment(
                self.spec,
                "esc-0000000000000001",
                studio_setup_confirmed=True,
            )

    def test_plan_rejects_variant_setup_mismatch_even_with_recomputed_checksum(self) -> None:
        """variantだけを書き換えてチェックサムを再計算してもstartで拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["variant"] = end_screen.MULTI_VARIANT
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "requires at least one extra"):
            end_screen.start_experiment(
                self.spec,
                "esc-0000000000000001",
                studio_setup_confirmed=True,
            )

    def test_manifest_rejects_self_link_and_id_mismatch(self) -> None:
        """自己リンク・ID/ディレクトリ不一致・非object JSONを拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["target_video_id"] = self.video_id
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "must differ"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # ID/ディレクトリ不一致
        data2 = json.loads(self._manifest_file("esc-0000000000000001").read_text(encoding="utf-8"))
        data2["experiment_id"] = "esc-9999999999999999"
        data2["plan_sha256"] = end_screen._plan_checksum(data2)
        self._manifest_file("esc-0000000000000001").write_text(
            json.dumps(data2, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "mismatch"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 非object JSON
        self._manifest_file("esc-0000000000000001").write_text("[1,2,3]\n", encoding="utf-8")
        with self.assertRaisesRegex(end_screen.EndScreenError, "invalid manifest"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_corrupt_active_manifest_blocks_second_plan(self) -> None:
        """壊れた既存manifest（不正JSON・不正setup・欠落）があると、
        同一動画の2件目planを拒否する（fail-closed・active一意性）。"""
        self._plan("esc-0000000000000001")
        path = self._manifest_file("esc-0000000000000001")
        path.write_text("{broken json\n", encoding="utf-8")
        with self.assertRaises(end_screen.EndScreenError):
            self._plan("esc-0000000000000002")
        self.assertFalse(self._manifest_file("esc-0000000000000002").exists())

        # 不正setup
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["target_element_type"] = "playlist"
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(end_screen.EndScreenError):
            self._plan("esc-0000000000000002")

        # manifest欠落
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        path.unlink()
        with self.assertRaisesRegex(end_screen.EndScreenError, "manifest missing"):
            self._plan("esc-0000000000000002")

    def test_symlink_manifest_directory_blocks_plan(self) -> None:
        """実験ディレクトリがsymlinkなら拒否する。"""
        self._plan("esc-0000000000000001")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        target = self.spec.output_dir / "end_screen_tests" / "esc-0000000000000002"
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(end_screen.EndScreenError, "symlink"):
            self._plan("esc-0000000000000002")

    def test_root_symlink_blocks_plan(self) -> None:
        """記録先ルート自体がsymlinkなら外部書込み前に拒否する。"""
        self._plan("esc-0000000000000001")
        root = self.spec.output_dir / "end_screen_tests"
        outside = Path(self.tmp.name) / "outside-root"
        outside.mkdir()
        root.rename(outside)
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(end_screen.EndScreenError, "root must not be a symlink"):
            self._plan("esc-0000000000000003")

    def test_complete_observed_requires_both_stage_metrics(self) -> None:
        self._plan()
        self._start()
        with self.assertRaisesRegex(end_screen.EndScreenError, "click_rate must be"):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                sample_sufficient=True,
                end_screen_traffic_views=12,
                period_data_complete_confirmed=True,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )

    def test_complete_rejects_nonfinite_rate_and_invalid_traffic_views(self) -> None:
        cases = (
            ({"click_rate": math.nan}, "click_rate"),
            ({"click_rate": math.inf}, "click_rate"),
            ({"click_rate": "3.5"}, "click_rate"),
            ({"end_screen_traffic_views": -1}, "traffic"),
            ({"end_screen_traffic_views": True}, "traffic"),
            ({"end_screen_traffic_views": 1.5}, "traffic"),
        )
        for index, (overrides, label) in enumerate(cases):
            experiment_id = f"esc-{index + 2:016d}"
            video_id = f"AbCdEf{index:05d}"
            with self.subTest(label=label, overrides=overrides):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    now=self.now,
                    experiment_id=experiment_id,
                )
                end_screen.start_experiment(
                    self.spec,
                    experiment_id,
                    studio_setup_confirmed=True,
                    now=self.now,
                )
                with self.assertRaises(end_screen.EndScreenError):
                    kwargs = {
                        "sample_sufficient": True,
                        "click_rate": 3.5,
                        "end_screen_traffic_views": 12,
                        "period_data_complete_confirmed": True,
                        "setup_unchanged_confirmed": True,
                        "now": self.complete_now,
                    }
                    kwargs.update(overrides)
                    end_screen.complete_experiment(
                        self.spec,
                        experiment_id,
                        **kwargs,
                    )

    def test_manifest_result_validates_status_outcome_and_flags(self) -> None:
        """completed/invalidated manifestのstatus-outcome整合・確認フラグ・
        日時・率を検証する。"""
        self._plan()
        self._start()
        self._complete_observed()
        path = self._manifest_file("esc-0000000000000001")

        # status/outcome不一致
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "invalidated"
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "require"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 確認フラグ欠落
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "completed"
        data["result"]["setup_unchanged_confirmed"] = False
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "setup_unchanged_confirmed"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 日時欠落
        data = json.loads(path.read_text(encoding="utf-8"))
        data["result"]["setup_unchanged_confirmed"] = True
        data["result"]["recorded_at"] = ""
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "recorded_at"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_stopped_changed_setup_round_trip_and_followup_plan(self) -> None:
        """stopped_changed_setup は invalidated + 確認フラグFalse + 率Noneで
        有効な終端状態。showで再読込でき、別動画のplanも継続できる。"""
        self._plan()
        self._start()
        manifest = end_screen.complete_experiment(
            self.spec,
            "esc-0000000000000001",
            setup_changed=True,
            notes="構成を変更した",
            now=self.now,
        )
        self.assertEqual(manifest["status"], "invalidated")
        self.assertIsNone(manifest["result"]["click_rate"])

        shown = end_screen.show_experiment(self.spec, "esc-0000000000000001")
        self.assertEqual(shown["status"], "invalidated")
        self.assertIsNone(shown["result"]["click_rate"])

        # 別動画のplanが継続できる
        self._write_history(video_id="NewVidId0001")
        plan = end_screen.plan_experiment(
            self.spec,
            video_id="NewVidId0001",
            link_video_id=self.link_video_id,
            content_direct_confirmed=True,
            experiment_id="esc-0000000000000003",
        )
        self.assertEqual(plan["status"], "planned")

    def test_setup_changed_rejects_measurement_flags(self) -> None:
        self._plan()
        self._start()
        with self.assertRaisesRegex(end_screen.EndScreenError, "must not include"):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                setup_changed=True,
                insufficient_reason="analytics_unavailable",
                now=self.now,
            )

    def test_manifest_result_rejects_invalid_metrics_and_missing_timestamps(self) -> None:
        cases = (
            (
                "esc-0000000000000010",
                lambda d: d["result"].update(click_rate=-0.1),
                "between 0 and 100",
            ),
            (
                "esc-0000000000000011",
                lambda d: d["result"].update(end_screen_traffic_views=None),
                "non-negative integer",
            ),
            (
                "esc-0000000000000012",
                lambda d: d.update(completed_at=""),
                "ISO-8601",
            ),
            (
                "esc-0000000000000013",
                lambda d: d.update(completed_at="2026-08-20T03:00:01+00:00"),
                "must equal",
            ),
        )
        for index, (experiment_id, mutate, message) in enumerate(cases):
            video_id = f"AbCdEf{index + 10:05d}"
            with self.subTest(message=message):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    now=self.now,
                    experiment_id=experiment_id,
                )
                end_screen.start_experiment(
                    self.spec,
                    experiment_id,
                    studio_setup_confirmed=True,
                    now=self.now,
                )
                end_screen.complete_experiment(
                    self.spec,
                    experiment_id,
                    sample_sufficient=True,
                    click_rate=3.5,
                    end_screen_traffic_views=12,
                    period_data_complete_confirmed=True,
                    setup_unchanged_confirmed=True,
                    now=self.complete_now,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                mutate(data)
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(end_screen.EndScreenError, message):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_manifest_rejects_invalid_video_id(self) -> None:
        """対象video_idがYouTube ID形式でないmanifestを、チェックサム再計算後も
        拒否する。"""
        for index, bad in enumerate(("", "not valid", "短い")):
            experiment_id = f"esc-{index + 20:016d}"
            video_id = f"AbCdEf{index + 20:05d}"
            with self.subTest(bad=bad):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["video_id"] = bad
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(end_screen.EndScreenError, "invalid video_id"):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_show_rejects_root_and_manifest_symlinks(self) -> None:
        """showでもroot symlink・manifest file symlinkを拒否する。"""
        self._plan()
        root = self.spec.output_dir / "end_screen_tests"
        outside = Path(self.tmp.name) / "outside-root"
        outside.mkdir()
        root.rename(outside)
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(end_screen.EndScreenError, "root must not be a symlink"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")
        with self.assertRaisesRegex(end_screen.EndScreenError, "root must not be a symlink"):
            end_screen.summary_experiments(self.spec)
        root.unlink()
        outside.rename(root)

        # manifest file symlink
        manifest_path = root / "esc-0000000000000001" / "manifest.json"
        real = manifest_path.read_bytes()
        manifest_path.unlink()
        fake = Path(self.tmp.name) / "fake-manifest.json"
        fake.write_bytes(real)
        manifest_path.symlink_to(fake)
        with self.assertRaisesRegex(end_screen.EndScreenError, "manifest must not be a symlink"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_manifest_rejects_non_string_video_ids(self) -> None:
        """整数等の非文字列IDを、チェックサム再計算後も拒否する。"""
        for index, key in enumerate(("video_id", "target_video_id")):
            experiment_id = f"esc-{index + 30:016d}"
            video_id = f"AbCdEf{index + 30:05d}"
            with self.subTest(key=key):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                if key == "video_id":
                    data["video_id"] = 123456
                else:
                    data["end_screen_setup"]["target_video_id"] = 123456
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(end_screen.EndScreenError, "must be a string"):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_manifest_status_schema_rejects_invalid_transitions(self) -> None:
        """planned/runningへのterminal field混入・runningのstarted_at欠落・
        数値日時を拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")

        # plannedへのterminal field混入
        data = json.loads(path.read_text(encoding="utf-8"))
        data["completed_at"] = self.now.isoformat()
        data["result"] = {"outcome": "observed", "click_rate": 3.5}
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "planned manifest must not"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # runningのstarted_at欠落
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "running"
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "started_at"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 数値日時
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        self._start()
        self._complete_observed()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["started_at"] = 123
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "ISO-8601"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_manifest_status_schema_rejects_explicit_null_fields(self) -> None:
        """planned/runningへの明示的なnullフィールド混入を拒否する。"""
        for index, field in enumerate(("started_at", "completed_at", "result")):
            experiment_id = f"esc-{index + 40:016d}"
            video_id = f"AbCdEf{index + 40:05d}"
            with self.subTest(field=field):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data[field] = None
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    end_screen.EndScreenError,
                    f"planned manifest must not have {field}",
                ):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_manifest_timestamp_rejects_impossible_datetimes(self) -> None:
        """正規表現に一致しても実在しない日時（2月30日・月13・時刻25時・
        不正offset）を拒否する。"""
        for index, bad in enumerate(
            (
            "2026-02-30T00:00:00+00:00",
            "2026-13-01T00:00:00+00:00",
            "2026-01-01T25:00:00+00:00",
            "2026-01-01T00:00:00+99:99",
            )
        ):
            experiment_id = f"esc-{index + 50:016d}"
            video_id = f"AbCdEf{index + 50:05d}"
            with self.subTest(bad=bad):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    now=self.now,
                    experiment_id=experiment_id,
                )
                end_screen.start_experiment(
                    self.spec,
                    experiment_id,
                    studio_setup_confirmed=True,
                    now=self.now,
                )
                end_screen.complete_experiment(
                    self.spec,
                    experiment_id,
                    sample_sufficient=True,
                    click_rate=3.5,
                    end_screen_traffic_views=12,
                    period_data_complete_confirmed=True,
                    setup_unchanged_confirmed=True,
                    now=self.complete_now,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["started_at"] = bad
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(end_screen.EndScreenError):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_legacy_v1_manifest_can_be_read_started_and_completed(self) -> None:
        experiment_id = "esc-ffffffffffffffff"
        directory = self.spec.output_dir / "end_screen_tests" / experiment_id
        directory.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "experiment_id": experiment_id,
            "channel": self.spec.id,
            "video_id": self.video_id,
            "status": "planned",
            "created_at": self.now.isoformat(),
            "official_help_url": end_screen.OFFICIAL_HELP_URL,
            "decision_metric": "youtube_studio.end_screen_click_rate",
            "end_screen_setup": {
                "element": "video",
                "link_video_id": self.link_video_id,
                "single_slot_only": True,
                "subscription_button_prohibited": True,
                "playlist_element_prohibited": True,
                "content_direct_confirmed": True,
            },
            "source": {
                "title": "旧記録",
                "history_ts": "2026-08-10T00:00:00+00:00",
                "workdir": "/tmp/legacy",
                "tier": "longform",
                "youtube_privacy": "unlisted",
            },
            "warnings": [],
        }
        manifest["plan_sha256"] = end_screen._plan_checksum(manifest)
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        shown = end_screen.show_experiment(self.spec, experiment_id)
        self.assertEqual(shown["schema_version"], 1)
        started = end_screen.start_experiment(
            self.spec,
            experiment_id,
            studio_setup_confirmed=True,
            now=self.now,
        )
        self.assertNotIn("observation_start_date", started)
        completed = end_screen.complete_experiment(
            self.spec,
            experiment_id,
            outcome="clicked",
            click_rate=3.5,
            setup_unchanged_confirmed=True,
            now=self.now,
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["outcome"], "clicked")
        memo = (directory / "next_idea_memo.md").read_text(encoding="utf-8")
        self.assertIn("schema v1", memo)


if __name__ == "__main__":
    unittest.main()
