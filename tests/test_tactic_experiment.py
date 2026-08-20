"""Issues #194/#196: tactic施策の1変数手動比較。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import tactic_experiment


class TacticExperimentTest(unittest.TestCase):
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
        self.now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        self.rows = [
            self._row("LongBase01", corner="analytics", tier="longform", ts="2026-08-01"),
            self._row("LongNext02", corner="analytics", tier="longform", ts="2026-08-10"),
            self._row("ShortBase1", corner="shorts", tier="short", ts="2026-08-02"),
            self._row("ShortNext2", corner="shorts", tier="short", ts="2026-08-11"),
        ]
        self._write_history()

    @staticmethod
    def _row(video_id: str, *, corner: str, tier: str, ts: str) -> dict:
        return {
            "ts": f"{ts}T12:00:00+00:00",
            "channel": "youtube-growth",
            "corner": corner,
            "title": video_id,
            "video_id": video_id,
            "status": "published",
            "tier": tier,
            "youtube_privacy": "unlisted",
        }

    def _write_history(self) -> None:
        self.spec.history_file.write_text(
            "".join(json.dumps(row) + "\n" for row in self.rows),
            encoding="utf-8",
        )

    def _plan(self, kind: str = "thumbnail_traffic") -> dict:
        thumbnail = kind == "thumbnail_traffic"
        return tactic_experiment.plan_experiment(
            self.spec,
            kind=kind,
            issue_number=194 if thumbnail else 196,
            baseline_video_id="LongBase01" if thumbnail else "ShortBase1",
            comparison_key="same-audience-same-format",
            planned_change="サムネイルだけ変更" if thumbnail else "冒頭1秒だけ変更",
            one_variable_confirmed=True,
            now=self.now,
            experiment_id=(
                "tactic-0000000000000194" if thumbnail else "tactic-0000000000000196"
            ),
        )

    def _start(self, kind: str = "thumbnail_traffic") -> dict:
        manifest = self._plan(kind)
        return tactic_experiment.start_experiment(
            self.spec,
            manifest["experiment_id"],
            candidate_video_id="LongNext02" if kind == "thumbnail_traffic" else "ShortNext2",
            same_cohort_confirmed=True,
            only_planned_variable_changed_confirmed=True,
            now=self.now,
        )

    @staticmethod
    def _windows(kind: str = "thumbnail_traffic") -> dict:
        if kind == "shorts_hook":
            return {
                "baseline_observation_start": "2026-08-03",
                "baseline_observation_end": "2026-08-09",
                "candidate_observation_start": "2026-08-12",
                "candidate_observation_end": "2026-08-18",
            }
        return {
            "baseline_observation_start": "2026-08-02",
            "baseline_observation_end": "2026-08-08",
            "candidate_observation_start": "2026-08-11",
            "candidate_observation_end": "2026-08-17",
        }

    @staticmethod
    def _thumbnail_metrics(impressions: int, ctr: float, watch: float) -> dict:
        return {
            "traffic_sources": {
                "YT_SEARCH": impressions,
                "BROWSE": impressions * 2,
            },
            "impressions_funnel": {
                "impressions": impressions * 3,
                "ctr_percent": ctr,
                "watch_time_minutes": watch,
            },
        }

    def test_thumbnail_plan_start_complete_records_source_metric_deltas(self) -> None:
        running = self._start()
        completed = tactic_experiment.complete_experiment(
            self.spec,
            running["experiment_id"],
            baseline_metrics=self._thumbnail_metrics(100, 4.0, 50.0),
            candidate_metrics=self._thumbnail_metrics(130, 5.5, 70.0),
            same_observation_window_confirmed=True,
            studio_values_transcribed_confirmed=True,
            now=self.now,
            **self._windows(),
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            completed["result"]["deltas"]["impressions_funnel"],
            {
                "impressions": 90,
                "ctr_percentage_points": 1.5,
                "watch_time_minutes": 20.0,
            },
        )
        self.assertEqual(
            completed["result"]["interpretation"],
            "descriptive_only_no_causal_winner",
        )
        directory = self.spec.output_dir / "tactic_experiments" / running["experiment_id"]
        self.assertIn("勝者や因果", (directory / "plan.md").read_text(encoding="utf-8"))
        self.assertIn(
            "流入元ごとのviews、および別ファネル",
            (directory / "plan.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(completed["observation_timezone"], "America/Los_Angeles")
        self.assertIn(
            "最初の完全な太平洋時間日",
            (directory / "plan.md").read_text(encoding="utf-8"),
        )
        self.assertTrue((directory / "result.md").is_file())
        self.assertEqual(
            tactic_experiment.show_experiment(self.spec, running["experiment_id"]),
            completed,
        )

    def test_thumbnail_readme_json_keeps_sources_separate_from_impressions_funnel(self) -> None:
        raw = (
            '{"traffic_sources":{"YT_SEARCH":40,"BROWSE":60},'
            '"impressions_funnel":{"impressions":100,"ctr_percent":4.0,'
            '"watch_time_minutes":50}}'
        )
        parsed = tactic_experiment._json_object(raw)
        metrics = tactic_experiment._validate_metrics("thumbnail_traffic", parsed)
        self.assertEqual(metrics["traffic_sources"], {"BROWSE": 60, "YT_SEARCH": 40})
        self.assertEqual(metrics["impressions_funnel"]["impressions"], 100)
        self.assertNotIsInstance(metrics["traffic_sources"]["YT_SEARCH"], dict)

    def test_shorts_hook_records_two_studio_metrics_and_derived_swipe_rate(self) -> None:
        running = self._start("shorts_hook")
        completed = tactic_experiment.complete_experiment(
            self.spec,
            running["experiment_id"],
            baseline_metrics={"shown_in_feed": 1000, "chose_to_view_percent": 41.5},
            candidate_metrics={"shown_in_feed": 1200, "chose_to_view_percent": 47.0},
            same_observation_window_confirmed=True,
            studio_values_transcribed_confirmed=True,
            now=self.now,
            **self._windows("shorts_hook"),
        )

        result = completed["result"]
        self.assertEqual(result["candidate_metrics"]["swiped_away_percent"], 53.0)
        self.assertEqual(
            result["deltas"],
            {"shown_in_feed": 200, "chose_to_view_percentage_points": 5.5},
        )
        self.assertNotIn("winner", result)
        self.assertEqual(
            tactic_experiment.show_experiment(self.spec, running["experiment_id"]),
            completed,
        )
        followup = tactic_experiment.plan_experiment(
            self.spec,
            kind="shorts_hook",
            issue_number=196,
            baseline_video_id="ShortBase1",
            comparison_key="same-audience-followup",
            planned_change="冒頭1秒の別案だけ変更",
            one_variable_confirmed=True,
            experiment_id="tactic-0000000000000296",
        )
        self.assertEqual(followup["status"], "planned")

    def test_plan_requires_exact_issue_and_one_variable_confirmation(self) -> None:
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "issue #194"):
            tactic_experiment.plan_experiment(
                self.spec,
                kind="thumbnail_traffic",
                issue_number=196,
                baseline_video_id="LongBase01",
                comparison_key="cohort",
                planned_change="thumbnail",
                one_variable_confirmed=True,
            )
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "exactly one"):
            tactic_experiment.plan_experiment(
                self.spec,
                kind="shorts_hook",
                issue_number=196,
                baseline_video_id="ShortBase1",
                comparison_key="cohort",
                planned_change="hook",
            )

    def test_kind_rejects_wrong_video_tier(self) -> None:
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "longform"):
            tactic_experiment.plan_experiment(
                self.spec,
                kind="thumbnail_traffic",
                issue_number=194,
                baseline_video_id="ShortBase1",
                comparison_key="cohort",
                planned_change="thumbnail",
                one_variable_confirmed=True,
            )
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "short"):
            tactic_experiment.plan_experiment(
                self.spec,
                kind="shorts_hook",
                issue_number=196,
                baseline_video_id="LongBase01",
                comparison_key="cohort",
                planned_change="hook",
                one_variable_confirmed=True,
            )

    def test_start_requires_same_corner_tier_and_confirmations(self) -> None:
        manifest = self._plan("shorts_hook")
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "same cohort"):
            tactic_experiment.start_experiment(
                self.spec,
                manifest["experiment_id"],
                candidate_video_id="ShortNext2",
                only_planned_variable_changed_confirmed=True,
            )
        self.rows[-1]["corner"] = "analytics"
        self._write_history()
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "corner"):
            tactic_experiment.start_experiment(
                self.spec,
                manifest["experiment_id"],
                candidate_video_id="ShortNext2",
                same_cohort_confirmed=True,
                only_planned_variable_changed_confirmed=True,
            )

    def test_complete_rejects_different_traffic_source_sets(self) -> None:
        running = self._start()
        candidate = self._thumbnail_metrics(120, 5.0, 60.0)
        del candidate["traffic_sources"]["BROWSE"]
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "same traffic"):
            tactic_experiment.complete_experiment(
                self.spec,
                running["experiment_id"],
                baseline_metrics=self._thumbnail_metrics(100, 4.0, 50.0),
                candidate_metrics=candidate,
                same_observation_window_confirmed=True,
                studio_values_transcribed_confirmed=True,
                **self._windows(),
            )

    def test_complete_rejects_missing_confirmation_and_bad_metric_ranges(self) -> None:
        running = self._start("shorts_hook")
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "same observation"):
            tactic_experiment.complete_experiment(
                self.spec,
                running["experiment_id"],
                baseline_metrics={"shown_in_feed": 10, "chose_to_view_percent": 20},
                candidate_metrics={"shown_in_feed": 10, "chose_to_view_percent": 30},
                studio_values_transcribed_confirmed=True,
                **self._windows("shorts_hook"),
            )
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "out of range"):
            tactic_experiment.complete_experiment(
                self.spec,
                running["experiment_id"],
                baseline_metrics={"shown_in_feed": 10, "chose_to_view_percent": 101},
                candidate_metrics={"shown_in_feed": 10, "chose_to_view_percent": 30},
                same_observation_window_confirmed=True,
                studio_values_transcribed_confirmed=True,
                **self._windows("shorts_hook"),
            )

    def test_unavailable_metrics_complete_without_inventing_zero(self) -> None:
        running = self._start("shorts_hook")
        completed = tactic_experiment.complete_experiment(
            self.spec,
            running["experiment_id"],
            baseline_metrics={"available": False, "reason": "Studio data not ready"},
            candidate_metrics={"shown_in_feed": 100, "chose_to_view_percent": 30},
            same_observation_window_confirmed=True,
            studio_values_transcribed_confirmed=True,
            **self._windows("shorts_hook"),
        )
        self.assertEqual(completed["result"]["interpretation"], "insufficient_data")
        self.assertEqual(completed["result"]["deltas"], {})
        self.assertNotIn("shown_in_feed", completed["result"]["baseline_metrics"])
        self.assertEqual(
            tactic_experiment.show_experiment(self.spec, running["experiment_id"]),
            completed,
        )

    def test_tampered_plan_is_rejected(self) -> None:
        manifest = self._plan()
        path = (
            self.spec.output_dir
            / "tactic_experiments"
            / manifest["experiment_id"]
            / "manifest.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        data["planned_change"] = "タイトルも変更"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "checksum"):
            tactic_experiment.show_experiment(self.spec, manifest["experiment_id"])

    def test_tampered_candidate_binding_is_rejected(self) -> None:
        manifest = self._start("shorts_hook")
        path = (
            self.spec.output_dir
            / "tactic_experiments"
            / manifest["experiment_id"]
            / "manifest.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        data["candidate"]["video_id"] = "Forged999"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "binding checksum"):
            tactic_experiment.show_experiment(self.spec, manifest["experiment_id"])

    def test_complete_rejects_wrong_observation_window_length(self) -> None:
        running = self._start("shorts_hook")
        windows = self._windows("shorts_hook")
        windows["candidate_observation_end"] = "2026-08-16"
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "must be 7 days"):
            tactic_experiment.complete_experiment(
                self.spec,
                running["experiment_id"],
                baseline_metrics={"shown_in_feed": 100, "chose_to_view_percent": 20},
                candidate_metrics={"shown_in_feed": 100, "chose_to_view_percent": 30},
                same_observation_window_confirmed=True,
                studio_values_transcribed_confirmed=True,
                **windows,
            )

    def test_complete_rejects_mismatched_offset_and_future_window(self) -> None:
        running = self._start("shorts_hook")
        metrics = {"shown_in_feed": 100, "chose_to_view_percent": 30}
        windows = self._windows("shorts_hook")
        windows["candidate_observation_start"] = "2026-08-13"
        windows["candidate_observation_end"] = "2026-08-19"
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "first full Pacific"):
            tactic_experiment.complete_experiment(
                self.spec,
                running["experiment_id"],
                baseline_metrics=metrics,
                candidate_metrics=metrics,
                same_observation_window_confirmed=True,
                studio_values_transcribed_confirmed=True,
                now=self.now,
                **windows,
            )
        windows = self._windows("shorts_hook")
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "latest completed Pacific"):
            tactic_experiment.complete_experiment(
                self.spec,
                running["experiment_id"],
                baseline_metrics=metrics,
                candidate_metrics=metrics,
                same_observation_window_confirmed=True,
                studio_values_transcribed_confirmed=True,
                now=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
                **windows,
            )

    def test_pacific_publication_day_controls_first_full_day(self) -> None:
        self.rows[2]["ts"] = "2026-08-02T00:30:00+00:00"
        self.rows[3]["ts"] = "2026-08-11T00:30:00+00:00"
        self._write_history()
        running = self._start("shorts_hook")
        completed = tactic_experiment.complete_experiment(
            self.spec,
            running["experiment_id"],
            baseline_metrics={"shown_in_feed": 100, "chose_to_view_percent": 20},
            candidate_metrics={"shown_in_feed": 100, "chose_to_view_percent": 30},
            same_observation_window_confirmed=True,
            studio_values_transcribed_confirmed=True,
            now=self.now,
            **self._windows(),
        )
        self.assertEqual(
            completed["result"]["observation_windows"]["baseline"]["start"],
            "2026-08-02",
        )

    def test_history_rejects_wrong_channel_and_naive_timestamp(self) -> None:
        self.rows[0]["channel"] = "ideology"
        self._write_history()
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "selected channel"):
            self._plan()
        self.rows[0]["channel"] = "youtube-growth"
        self.rows[0]["ts"] = "2026-08-01T12:00:00"
        self._write_history()
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "timezone-aware"):
            self._plan()

    def test_result_checksum_rejects_equal_delta_metric_tampering(self) -> None:
        completed = tactic_experiment.complete_experiment(
            self.spec,
            self._start("shorts_hook")["experiment_id"],
            baseline_metrics={"shown_in_feed": 100, "chose_to_view_percent": 20},
            candidate_metrics={"shown_in_feed": 120, "chose_to_view_percent": 30},
            same_observation_window_confirmed=True,
            studio_values_transcribed_confirmed=True,
            now=self.now,
            **self._windows("shorts_hook"),
        )
        path = (
            self.spec.output_dir
            / "tactic_experiments"
            / completed["experiment_id"]
            / "manifest.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        data["result"]["baseline_metrics"]["shown_in_feed"] += 1
        data["result"]["candidate_metrics"]["shown_in_feed"] += 1
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(tactic_experiment.TacticExperimentError, "result checksum"):
            tactic_experiment.show_experiment(self.spec, completed["experiment_id"])

    def test_result_memo_write_failure_keeps_running_and_retryable(self) -> None:
        running = self._start("shorts_hook")
        real_write = tactic_experiment._write_text_atomic

        def fail_result(path, text):
            if path.name == "result.md":
                raise OSError("disk full")
            return real_write(path, text)

        kwargs = {
            "baseline_metrics": {"shown_in_feed": 100, "chose_to_view_percent": 20},
            "candidate_metrics": {"shown_in_feed": 120, "chose_to_view_percent": 30},
            "same_observation_window_confirmed": True,
            "studio_values_transcribed_confirmed": True,
            "now": self.now,
            **self._windows("shorts_hook"),
        }
        with mock.patch.object(
            tactic_experiment, "_write_text_atomic", side_effect=fail_result
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                tactic_experiment.complete_experiment(
                    self.spec, running["experiment_id"], **kwargs
                )
        self.assertEqual(
            tactic_experiment.show_experiment(self.spec, running["experiment_id"])[
                "status"
            ],
            "running",
        )
        completed = tactic_experiment.complete_experiment(
            self.spec, running["experiment_id"], **kwargs
        )
        self.assertEqual(completed["status"], "completed")

    def test_read_only_and_no_automatic_application_are_explicit(self) -> None:
        manifest = self._plan("shorts_hook")
        self.assertFalse(manifest["youtube_write"])
        self.assertIn("all_except_first_second", manifest["fixed_variables"])
        plan = (
            self.spec.output_dir
            / "tactic_experiments"
            / manifest["experiment_id"]
            / "plan.md"
        ).read_text(encoding="utf-8")
        self.assertIn("YouTubeを変更しません", plan)


if __name__ == "__main__":
    unittest.main()
