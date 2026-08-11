"""Issue #138: Shortsから関連動画への橋渡し検証。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import shorts_bridge, youtube


class ShortsBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=root / "output" / "youtube-growth",
            history_file=root / "output" / "youtube-growth" / "history.jsonl",
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    token=root / "youtube-token.json",
                    analytics_token=root / "youtube-analytics-token.json",
                    client_secret=root / "youtube-client-secret.json",
                )
            ),
        )
        self.spec.history_file.parent.mkdir(parents=True)
        self.source_id = "SourceA12345"
        self.target_id = "TargetA12345"
        self.bridge_text = "この続きは関連動画で確かめてください。"
        self.plan_now = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
        self.complete_now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        self.rows: list[dict] = []
        self._add_video(
            self.source_id,
            tier="short",
            corner="shorts",
            privacy="public",
            narration="前半の説明です。" * 80 + self.bridge_text,
        )
        self._add_video(
            self.target_id,
            tier="longform",
            corner="video",
            privacy="unlisted",
            narration="遷移先の詳しい説明です。",
        )

    def _add_video(
        self,
        video_id: str,
        *,
        tier: str,
        corner: str,
        privacy: str = "public",
        status: str = "published",
        narration: str = "動画の説明です。",
    ) -> None:
        workdir = Path(self.tmp.name) / "workdirs" / video_id
        workdir.mkdir(parents=True)
        (workdir / "script.json").write_text(
            json.dumps({"narration": narration}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.rows.append(
            {
                "ts": "2026-08-10T00:00:00+00:00",
                "channel": self.spec.id,
                "corner": corner,
                "title": f"動画 {video_id}",
                "video_id": video_id,
                "status": status,
                "tier": tier,
                "youtube_privacy": privacy,
                "workdir": str(workdir),
            }
        )
        self._write_history()

    def _write_history(self) -> None:
        self.spec.history_file.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows
            ),
            encoding="utf-8",
        )

    def _replace_row(self, video_id: str, **values) -> None:
        for row in self.rows:
            if row["video_id"] == video_id:
                row.update(values)
                self._write_history()
                return
        self.fail(f"missing test history row: {video_id}")

    def _history_row(self, video_id: str) -> dict:
        return next(row for row in self.rows if row["video_id"] == video_id)

    def _set_narration(self, video_id: str, narration: str) -> None:
        path = Path(self._history_row(video_id)["workdir"]) / "script.json"
        path.write_text(
            json.dumps({"narration": narration}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _plan(self, experiment_id: str = "sbr-0000000000000001") -> dict:
        return shorts_bridge.plan_experiment(
            self.spec,
            source_video_id=self.source_id,
            target_video_id=self.target_id,
            bridge_text=self.bridge_text,
            observation_days=7,
            content_direct_confirmed=True,
            now=self.plan_now,
            experiment_id=experiment_id,
        )

    def _start(self, experiment_id: str = "sbr-0000000000000001") -> dict:
        return shorts_bridge.start_experiment(
            self.spec,
            experiment_id,
            studio_setup_confirmed=True,
            now=self.plan_now,
        )

    def _metrics(
        self,
        source_id: str | None = None,
        target_id: str | None = None,
        *,
        source_views: int | None = 1000,
        attributed_views: int | None = 25,
        data_through: str | None = "2026-08-17",
        availability_probe_end: str = "2026-08-18",
    ) -> dict:
        return {
            "source_video_id": source_id or self.source_id,
            "target_video_id": target_id or self.target_id,
            "start_date": "2026-08-11",
            "end_date": "2026-08-17",
            "availability_probe_end_date": availability_probe_end,
            "views_data_through_date": data_through,
            "source_views": source_views,
            "attributed_target_views": attributed_views,
            "attribution_source_type": "RELATED_VIDEO",
            "attribution_detail_limit": 25,
        }

    def _manifest_path(self, experiment_id: str) -> Path:
        return (
            self.spec.output_dir
            / "shorts_bridge_tests"
            / experiment_id
            / "manifest.json"
        )

    def test_plan_records_real_bridge_without_youtube_write(self) -> None:
        manifest = self._plan()

        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(manifest["source"]["tier"], "short")
        self.assertEqual(manifest["target"]["tier"], "longform")
        self.assertEqual(
            manifest["bridge_setup"]["bridge_text"], self.bridge_text
        )
        self.assertTrue(manifest["bridge_setup"]["content_direct_confirmed"])
        self.assertFalse(manifest["bridge_setup"]["youtube_write_performed"])
        setup = manifest["bridge_setup"]
        self.assertEqual(setup["final_section"], "last_third")
        self.assertGreaterEqual(
            setup["bridge_start_char"], setup["final_section_start_char"]
        )
        self.assertEqual(
            setup["bridge_end_char"],
            setup["bridge_start_char"] + len(self.bridge_text),
        )
        self.assertRegex(manifest["source"]["narration_sha256"], r"^[0-9a-f]{64}$")
        plan = self._manifest_path(manifest["experiment_id"]).with_name("plan.md")
        text = plan.read_text(encoding="utf-8")
        self.assertIn(shorts_bridge.OFFICIAL_HELP_URL, text)
        self.assertIn("5%等の万能基準を使いません", text)

    def test_plan_requires_content_match_and_final_narration_evidence(self) -> None:
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "directly"):
            shorts_bridge.plan_experiment(
                self.spec,
                source_video_id=self.source_id,
                target_video_id=self.target_id,
                bridge_text=self.bridge_text,
            )
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "final third"):
            shorts_bridge.plan_experiment(
                self.spec,
                source_video_id=self.source_id,
                target_video_id=self.target_id,
                bridge_text="台本に存在しない橋渡し文です。",
                content_direct_confirmed=True,
            )

    def test_final_third_rejects_real_text_at_opening_or_middle(self) -> None:
        source = self._history_row(self.source_id)
        for length in (240, 565, 1800):
            for position in (0, length // 2):
                with self.subTest(length=length, position=position):
                    filler_before = "あ" * position
                    filler_after = "い" * (
                        length - position - len(self.bridge_text)
                    )
                    self._set_narration(
                        self.source_id,
                        filler_before + self.bridge_text + filler_after,
                    )
                    with self.assertRaisesRegex(
                        shorts_bridge.ShortsBridgeError, "final third"
                    ):
                        shorts_bridge._bridge_narration_evidence(
                            source, self.bridge_text
                        )

        self._set_narration(
            self.source_id,
            "う" * (565 - len(self.bridge_text)) + self.bridge_text,
        )
        _, _, evidence = shorts_bridge._bridge_narration_evidence(
            source, self.bridge_text
        )
        self.assertGreaterEqual(
            evidence["bridge_start_char"],
            evidence["final_section_start_char"],
        )

    def test_plan_rejects_non_short_unpublished_private_and_self_link(self) -> None:
        cases = (
            ("tier", "longform", "must be a YouTube Short"),
            ("status", "publishing", "not recorded as published"),
            ("youtube_privacy", "private", "public or unlisted"),
        )
        original = next(row.copy() for row in self.rows if row["video_id"] == self.source_id)
        for field, value, message in cases:
            with self.subTest(field=field):
                self._replace_row(self.source_id, **{field: value})
                with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, message):
                    self._plan()
                self._replace_row(
                    self.source_id,
                    **{key: value for key, value in original.items() if key != "video_id"},
                )
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "must differ"):
            shorts_bridge.plan_experiment(
                self.spec,
                source_video_id=self.source_id,
                target_video_id=self.source_id,
                bridge_text=self.bridge_text,
                content_direct_confirmed=True,
            )

        self._replace_row(self.target_id, tier="short")
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "longform"):
            self._plan()

    def test_plan_rejects_duplicate_active_test(self) -> None:
        self._plan()
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "already exists"):
            self._plan("sbr-0000000000000002")

    def test_start_requires_studio_confirmation_and_uses_full_pacific_days(self) -> None:
        self._plan()
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "YouTube Studio"):
            shorts_bridge.start_experiment(
                self.spec, "sbr-0000000000000001", now=self.plan_now
            )

        manifest = self._start()
        self.assertEqual(manifest["status"], "running")
        self.assertEqual(manifest["observation_start_date"], "2026-08-11")
        self.assertEqual(manifest["observation_end_date"], "2026-08-17")

    def test_complete_records_observed_ratio_but_no_universal_threshold(self) -> None:
        self._plan()
        self._start()
        with mock.patch.object(
            shorts_bridge.youtube,
            "shorts_bridge_metrics",
            return_value=self._metrics(),
        ) as readback:
            manifest = shorts_bridge.complete_experiment(
                self.spec,
                "sbr-0000000000000001",
                setup_unchanged_confirmed=True,
                notes="次は橋渡し文だけを変えて比較する",
                now=self.complete_now,
            )

        readback.assert_called_once_with(
            self.source_id,
            self.target_id,
            start_date="2026-08-11",
            end_date="2026-08-17",
            availability_end_date="2026-08-18",
            token_file=self.spec.publish.youtube.analytics_token,
            client_secret_file=self.spec.publish.youtube.client_secret,
        )
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["result"]["status"], "observed")
        self.assertEqual(manifest["result"]["transition_ratio_percent"], 2.5)
        self.assertFalse(manifest["result"]["universal_threshold_applied"])
        self.assertTrue(manifest["result"]["analytics_period_confirmed"])
        comparison = manifest["result"]["comparison"]
        self.assertEqual(comparison["comparable_count"], 1)
        self.assertIsNone(comparison["median_transition_ratio_percent"])
        memo = self._manifest_path(manifest["experiment_id"]).with_name(
            "next_idea_memo.md"
        ).read_text(encoding="utf-8")
        self.assertIn("クリック率ではなく", memo)
        self.assertIn("因果や勝者は自動判定しません", memo)

    def test_missing_related_source_is_insufficient_not_zero(self) -> None:
        self._plan()
        self._start()
        with mock.patch.object(
            shorts_bridge.youtube,
            "shorts_bridge_metrics",
            return_value=self._metrics(attributed_views=None),
        ):
            manifest = shorts_bridge.complete_experiment(
                self.spec,
                "sbr-0000000000000001",
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )

        result = manifest["result"]
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["attributed_target_views"])
        self.assertIsNone(result["transition_ratio_percent"])
        self.assertIn("確認できませんでした", result["reason"])

    def test_incomplete_analytics_period_stays_running_until_retry(self) -> None:
        self._plan()
        self._start()
        incomplete = self._metrics(
            source_views=None,
            attributed_views=None,
            data_through="2026-08-15",
        )
        with mock.patch.object(
            shorts_bridge.youtube,
            "shorts_bridge_metrics",
            return_value=incomplete,
        ):
            with self.assertRaisesRegex(
                shorts_bridge.ShortsBridgeError, "not available through"
            ):
                shorts_bridge.complete_experiment(
                    self.spec,
                    "sbr-0000000000000001",
                    setup_unchanged_confirmed=True,
                    now=self.complete_now,
                )
        self.assertEqual(
            shorts_bridge.show_experiment(
                self.spec, "sbr-0000000000000001"
            )["status"],
            "running",
        )

        with mock.patch.object(
            shorts_bridge.youtube,
            "shorts_bridge_metrics",
            return_value=self._metrics(),
        ):
            completed = shorts_bridge.complete_experiment(
                self.spec,
                "sbr-0000000000000001",
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            completed["result"]["views_data_through_date"], "2026-08-17"
        )

    def test_unverifiable_empty_period_completes_as_insufficient_not_zero(self) -> None:
        self._plan()
        self._start()
        settled_now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        unavailable = self._metrics(
            source_views=None,
            attributed_views=None,
            data_through=None,
            availability_probe_end="2026-08-25",
        )
        with mock.patch.object(
            shorts_bridge.youtube,
            "shorts_bridge_metrics",
            return_value=unavailable,
        ):
            completed = shorts_bridge.complete_experiment(
                self.spec,
                "sbr-0000000000000001",
                setup_unchanged_confirmed=True,
                now=settled_now,
            )

        result = completed["result"]
        self.assertEqual(result["status"], "insufficient_data")
        self.assertFalse(result["analytics_period_confirmed"])
        self.assertIsNone(result["source_views"])
        self.assertIsNone(result["attributed_target_views"])
        self.assertIsNone(result["transition_ratio_percent"])
        self.assertIn("0とせず取得不可", result["reason"])

    def test_setup_change_invalidates_without_analytics_call(self) -> None:
        self._plan()
        self._start()
        with mock.patch.object(
            shorts_bridge.youtube, "shorts_bridge_metrics"
        ) as readback:
            manifest = shorts_bridge.complete_experiment(
                self.spec,
                "sbr-0000000000000001",
                setup_changed=True,
                notes="関連動画を差し替えた",
                now=self.plan_now,
            )
        readback.assert_not_called()
        self.assertEqual(manifest["status"], "invalidated")
        self.assertEqual(manifest["result"]["status"], "stopped_changed_setup")
        shown = shorts_bridge.show_experiment(
            self.spec, "sbr-0000000000000001"
        )
        self.assertEqual(shown["status"], "invalidated")

    def test_early_api_failure_and_provenance_mismatch_leave_running(self) -> None:
        self._plan()
        self._start()
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "not complete"):
            shorts_bridge.complete_experiment(
                self.spec,
                "sbr-0000000000000001",
                setup_unchanged_confirmed=True,
                now=self.plan_now,
            )

        for returned, message in (
            (RuntimeError("network unavailable"), "readback failed"),
            (self._metrics(source_id="WrongSource1"), "provenance mismatch"),
        ):
            with self.subTest(message=message), mock.patch.object(
                shorts_bridge.youtube,
                "shorts_bridge_metrics",
                side_effect=returned if isinstance(returned, Exception) else None,
                return_value=None if isinstance(returned, Exception) else returned,
            ):
                with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, message):
                    shorts_bridge.complete_experiment(
                        self.spec,
                        "sbr-0000000000000001",
                        setup_unchanged_confirmed=True,
                        now=self.complete_now,
                    )
                self.assertEqual(
                    shorts_bridge.show_experiment(
                        self.spec, "sbr-0000000000000001"
                    )["status"],
                    "running",
                )

    def test_three_comparable_observations_enable_relative_median(self) -> None:
        completed: dict | None = None
        for index, attributed in enumerate((10, 20, 30), start=1):
            source_id = f"CmpSrc{index:06d}"
            target_id = f"CmpTgt{index:06d}"
            bridge = f"続きの検証{index}は関連動画で確認してください。"
            self._add_video(
                source_id,
                tier="short",
                corner="shorts",
                narration="比較用の前半です。" * 70 + bridge,
            )
            self._add_video(target_id, tier="longform", corner="video")
            experiment_id = f"sbr-{index + 10:016x}"
            shorts_bridge.plan_experiment(
                self.spec,
                source_video_id=source_id,
                target_video_id=target_id,
                bridge_text=bridge,
                content_direct_confirmed=True,
                now=self.plan_now,
                experiment_id=experiment_id,
            )
            shorts_bridge.start_experiment(
                self.spec,
                experiment_id,
                studio_setup_confirmed=True,
                now=self.plan_now,
            )
            metrics = self._metrics(
                source_id,
                target_id,
                source_views=1000,
                attributed_views=attributed,
            )
            with mock.patch.object(
                shorts_bridge.youtube,
                "shorts_bridge_metrics",
                return_value=metrics,
            ):
                completed = shorts_bridge.complete_experiment(
                    self.spec,
                    experiment_id,
                    setup_unchanged_confirmed=True,
                    now=self.complete_now,
                )

        self.assertIsNotNone(completed)
        comparison = completed["result"]["comparison"]
        self.assertEqual(comparison["status"], "ready")
        self.assertEqual(comparison["comparable_count"], 3)
        self.assertEqual(comparison["median_transition_ratio_percent"], 2.0)
        self.assertFalse(comparison["universal_threshold_applied"])
        summary = shorts_bridge.summarize_experiments(self.spec)
        self.assertEqual(len(summary["groups"]), 1)
        self.assertEqual(summary["groups"][0]["status"], "ready")

    def test_tampered_plan_checksum_is_rejected(self) -> None:
        self._plan()
        path = self._manifest_path("sbr-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["warnings"][0] = "改変された警告文"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(shorts_bridge.ShortsBridgeError, "checksum"):
            shorts_bridge.show_experiment(self.spec, "sbr-0000000000000001")


class ShortsBridgeYouTubeMetricsTest(unittest.TestCase):
    class Request:
        def __init__(self, response: dict) -> None:
            self.response = response

        def execute(self) -> dict:
            return self.response

    class Reports:
        def __init__(self, responses: list[dict]) -> None:
            self.responses = list(responses)
            self.queries: list[dict] = []

        def query(self, **kwargs):
            self.queries.append(kwargs)
            return ShortsBridgeYouTubeMetricsTest.Request(self.responses.pop(0))

    class Service:
        def __init__(self, responses: list[dict]) -> None:
            self.resource = ShortsBridgeYouTubeMetricsTest.Reports(responses)

        def reports(self):
            return self.resource

    def _read(
        self,
        target_rows: list[list],
        *,
        data_through: str | None = "2026-08-17",
        availability_end: str = "2026-08-18",
    ) -> tuple[dict, Reports, list[str]]:
        source_id = "SourceA12345"
        availability_rows = [[data_through, 100]] if data_through else []
        service = self.Service(
            [
                {
                    "columnHeaders": [{"name": "day"}, {"name": "views"}],
                    "rows": availability_rows,
                },
                {
                    "columnHeaders": [{"name": "video"}, {"name": "views"}],
                    "rows": [[source_id, 1000]],
                },
                {
                    "columnHeaders": [
                        {"name": "insightTrafficSourceDetail"},
                        {"name": "views"},
                    ],
                    "rows": target_rows,
                },
            ]
        )
        with mock.patch.object(
            youtube, "_load_credentials", return_value=object()
        ) as load_credentials, mock.patch(
            "googleapiclient.discovery.build", return_value=service
        ):
            result = youtube.shorts_bridge_metrics(
                source_id,
                "TargetA12345",
                start_date="2026-08-11",
                end_date="2026-08-17",
                availability_end_date=availability_end,
            )
        self.assertTrue(
            load_credentials.call_args.kwargs["exact_scopes"]
        )
        return (
            result,
            service.resource,
            load_credentials.call_args.kwargs["scopes"],
        )

    def test_reads_same_period_and_sums_only_matching_related_source(self) -> None:
        result, reports, scopes = self._read(
            [
                ["SourceA12345", 21],
                ["Different111", 99],
                ["SourceA12345", 4],
            ]
        )

        self.assertEqual(result["source_views"], 1000)
        self.assertEqual(result["attributed_target_views"], 25)
        self.assertEqual(result["views_data_through_date"], "2026-08-17")
        self.assertEqual(scopes, youtube.ANALYTICS_READONLY_SCOPES)
        self.assertNotIn(youtube.SCOPES[0], scopes)
        self.assertEqual(len(reports.queries), 3)
        self.assertEqual(reports.queries[0]["dimensions"], "day")
        self.assertEqual(reports.queries[0]["startDate"], "2026-08-11")
        self.assertEqual(reports.queries[0]["endDate"], "2026-08-18")
        self.assertEqual(reports.queries[1]["endDate"], "2026-08-17")
        self.assertEqual(
            reports.queries[2]["dimensions"], "insightTrafficSourceDetail"
        )
        self.assertIn(
            "insightTrafficSourceType==RELATED_VIDEO",
            reports.queries[2]["filters"],
        )
        self.assertEqual(reports.queries[2]["maxResults"], 25)

    def test_missing_source_detail_is_none_but_explicit_zero_is_zero(self) -> None:
        missing, _, _ = self._read([["Different111", 8]])
        explicit_zero, _, _ = self._read([["SourceA12345", 0]])

        self.assertIsNone(missing["attributed_target_views"])
        self.assertEqual(explicit_zero["attributed_target_views"], 0)

    def test_incomplete_daily_availability_skips_metric_queries(self) -> None:
        result, reports, _ = self._read([], data_through="2026-08-15")

        self.assertEqual(result["views_data_through_date"], "2026-08-15")
        self.assertIsNone(result["source_views"])
        self.assertIsNone(result["attributed_target_views"])
        self.assertEqual(len(reports.queries), 1)

    def test_post_observation_day_proves_period_availability(self) -> None:
        result, reports, _ = self._read(
            [["SourceA12345", 5]], data_through="2026-08-18"
        )

        self.assertEqual(result["views_data_through_date"], "2026-08-18")
        self.assertEqual(result["source_views"], 1000)
        self.assertEqual(result["attributed_target_views"], 5)
        self.assertEqual(len(reports.queries), 3)


if __name__ == "__main__":
    unittest.main()
