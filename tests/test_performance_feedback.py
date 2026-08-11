from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import history, performance, performance_report


class PerformanceFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        pipeline: dict = {}
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=self.root,
            history_file=self.root / "history.jsonl",
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    token=self.root / "token.json",
                    analytics_token=self.root / "analytics-token.json",
                    client_secret=self.root / "client.json",
                )
            ),
            pipeline=pipeline,
            pipeline_get=pipeline.get,
        )

    def _history(self, count: int = 2) -> None:
        rows = []
        for index in range(count):
            rows.append(
                {
                    "ts": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    "channel": self.spec.id,
                    "corner": "video" if index % 2 else "shorts",
                    "title": f"Title {index}",
                    "topic": f"Topic {index}",
                    "video_id": f"id-{index}",
                    "status": "published",
                }
            )
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )

    def test_sync_records_basic_metrics_and_exact_missing_scope_action(self) -> None:
        self._history()
        details = [
            {
                "video_id": f"id-{index}",
                "title": f"Title {index}",
                "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                "privacy_status": "unlisted",
                "duration": "PT1M",
                "views": index,
                "likes": 0,
                "comments": 0,
            }
            for index in range(2)
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=False),
            patch.object(performance.youtube, "video_analytics") as analytics_mock,
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        analytics_mock.assert_not_called()
        self.assertFalse(snapshot["analytics"]["available"])
        self.assertIn("YouTube Analytics API", snapshot["analytics"]["reason"])
        self.assertIn(
            "--auth --analytics-readonly --channel youtube-growth",
            snapshot["analytics"]["reason"],
        )
        self.assertEqual(snapshot["videos"][1]["data_api"]["views"], 1)
        self.assertEqual(snapshot["videos"][0]["topic"], "Topic 0")
        self.assertNotIn("topic_concepts", snapshot["videos"][0])
        self.assertEqual(
            len((self.root / "performance.jsonl").read_text().splitlines()), 1
        )
        decision = performance.build_decision(self.spec, snapshot)
        self.assertIn(
            "--auth --analytics-readonly --channel youtube-growth",
            decision["reason"],
        )
        self.assertFalse(
            decision["source_status"]["analytics"]["available"]
        )

        # 指標不変なら同じsnapshotを重複追記しない。
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=False),
        ):
            performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, 3, tzinfo=timezone.utc),
            )
        self.assertEqual(
            len((self.root / "performance.jsonl").read_text().splitlines()), 1
        )

    def test_insufficient_unlisted_zero_views_never_becomes_guidance(self) -> None:
        snapshot = {
            "collected_at": "2026-07-26T00:00:00+00:00",
            "videos": [
                {
                    "video_id": f"id-{index}",
                    "privacy_status": "unlisted",
                    "topic_concepts": ["retention"],
                    "data_api": {"views": 0},
                    "analytics": None,
                }
                for index in range(20)
            ],
        }

        decision = performance.build_decision(self.spec, snapshot)

        self.assertEqual(decision["status"], "insufficient_data")
        self.assertEqual(decision["eligible_video_ids"], [])
        self.assertEqual(decision["guidance"], "")

    def test_analytics_failure_still_persists_data_api_snapshot(self) -> None:
        self.spec.pipeline["performance_feedback"] = True
        self._history()
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "unlisted",
                "duration": "PT1M",
                "views": 3,
                "likes": 0,
                "comments": 0,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(
                performance.youtube,
                "video_analytics",
                side_effect=RuntimeError("YouTube Analytics API is disabled"),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertFalse(snapshot["analytics"]["available"])
        self.assertIn("API is disabled", snapshot["analytics"]["reason"])
        self.assertFalse(
            snapshot["retention_by_subscribed_status"]["available"]
        )
        self.assertIn(
            "API is disabled",
            snapshot["retention_by_subscribed_status"]["reason"],
        )
        self.assertEqual(snapshot["videos"][0]["data_api"]["views"], 3)
        self.assertTrue((self.root / "performance.jsonl").exists())

    def test_sync_records_traffic_sources_and_search_terms(self) -> None:
        """issue #164: Analyticsが返すトラフィックソースと検索語句を
        snapshotの各videoのanalyticsへ保存する。取得できない動画は空のまま。"""
        self._history(count=2)
        rows = []
        for index in range(2):
            rows.append(
                {
                    "ts": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    "channel": self.spec.id,
                    "corner": "shorts",
                    "title": f"Title {index}",
                    "topic": f"Topic {index}",
                    "video_id": f"id-{index}",
                    "status": "published",
                    "topic_metadata": (
                        {"gap_query": "ネタ切れ 解消"} if index == 0 else {}
                    ),
                }
            )
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": f"id-{index}",
                "title": f"Title {index}",
                "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
            for index in range(2)
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40, "SHORTS": 10}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=(
                    {"id-0": [{"term": "ショート 企画", "views": 30}]},
                    {},
                ),
            ) as search_mock,
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertTrue(snapshot["traffic_sources"]["available"])
        video0 = snapshot["videos"][0]
        self.assertEqual(video0["analytics"]["traffic_sources"], {"YT_SEARCH": 40, "SHORTS": 10})
        self.assertEqual(
            video0["analytics"]["search_terms"],
            [{"term": "ショート 企画", "views": 30}],
        )
        # Analytics行が無い動画はtraffic系も空のまま（欠落を0と断定しない）。
        video1 = snapshot["videos"][1]
        self.assertIsNone(video1["analytics"])
        self.assertIn("topic_metadata", video0)
        # gap_query付き動画だけが検索語句APIの照会対象になる（Sol review指摘）。
        self.assertEqual(search_mock.call_args.args[0], ["id-0"])

    def test_search_terms_only_queries_videos_returned_by_data_api(self) -> None:
        """issue #164 (Claude review指摘): gap_query付きでも、Data APIが
        snapshot出力に返さない動画（削除済み等）は検索語句APIの照会対象にしない。"""
        rows = []
        for index in range(3):
            rows.append(
                {
                    "ts": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    "channel": self.spec.id,
                    "corner": "shorts",
                    "title": f"Title {index}",
                    "topic": f"Topic {index}",
                    "video_id": f"id-{index}",
                    "status": "published",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                }
            )
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        # id-2 は Data API が返さない（削除済み等）。
        details = [
            {
                "video_id": f"id-{index}",
                "title": f"Title {index}",
                "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
            for index in range(2)
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=(
                    {"id-0": [{"term": "ショート 企画", "views": 30}]},
                    {},
                ),
            ) as search_mock,
        ):
            performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        # id-2（Data APIが返さない）は照会対象外。snapshot出力に使われる
        # id-0 / id-1 だけが照会される。
        self.assertEqual(search_mock.call_args.args[0], ["id-0", "id-1"])

    def test_sync_records_retention_curves(self) -> None:
        """issue #149: Analyticsが返す維持率カーブをsnapshotの各videoの
        analyticsへ保存する。取得できない動画は空のまま。"""
        self._history(count=1)
        rows = [
            {
                "ts": "2026-07-01T00:00:00+00:00",
                "channel": self.spec.id,
                "corner": "video",
                "title": "Title 0",
                "topic": "Topic 0",
                "video_id": "id-0",
                "status": "published",
            }
        ]
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=({}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=(
                    {
                        "id-0": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.5, "watch_ratio": 0.40},
                            {"elapsed_ratio": 1.0, "watch_ratio": 0.30},
                        ]
                    },
                    {},
                ),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertTrue(snapshot["retention_curve"]["available"])
        self.assertEqual(
            snapshot["videos"][0]["analytics"]["retention_curve"],
            [
                {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
                {"elapsed_ratio": 0.5, "watch_ratio": 0.40},
                {"elapsed_ratio": 1.0, "watch_ratio": 0.30},
            ],
        )

    def test_sync_records_latest_subscribed_status_retention_with_readonly_token(
        self,
    ) -> None:
        """issue #128: performance feedback有効時だけ、最新動画の購読状態別
        カーブをAnalytics専用tokenで取得して別フィールドへ保存する。"""
        self.spec.pipeline["performance_feedback"] = True
        self._history(count=1)
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        base_curve = [
            {"elapsed_ratio": index / 10, "watch_ratio": 0.9 - index / 20}
            for index in range(1, 7)
        ]
        segmented = {
            "id-0": {
                "SUBSCRIBED": [
                    {**point, "segment_impressions": 30} for point in base_curve
                ],
                "UNSUBSCRIBED": [
                    {
                        **point,
                        "watch_ratio": point["watch_ratio"] - 0.1,
                        "segment_impressions": 40,
                    }
                    for point in base_curve
                ],
            }
        }
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(
                performance.youtube, "_token_has_scopes", return_value=True
            ) as token_scopes,
            patch.object(
                performance.youtube, "video_analytics", return_value=analytics_rows
            ) as analytics,
            patch.object(
                performance.youtube, "video_traffic_sources", return_value={}
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=({"id-0": base_curve}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves_by_subscribed_status",
                return_value=(segmented, {}),
            ) as segmented_readback,
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        token_scopes.assert_called_once_with(
            self.spec.publish.youtube.analytics_token,
            performance.youtube.ANALYTICS_READONLY_SCOPES,
            exact=True,
        )
        self.assertEqual(
            analytics.call_args.kwargs["token_file"],
            self.spec.publish.youtube.analytics_token,
        )
        self.assertEqual(
            segmented_readback.call_args.kwargs["token_file"],
            self.spec.publish.youtube.analytics_token,
        )
        self.assertTrue(snapshot["retention_by_subscribed_status"]["available"])
        self.assertEqual(
            snapshot["retention_by_subscribed_status"]["queried_video_ids"],
            ["id-0"],
        )
        self.assertEqual(
            snapshot["videos"][0]["analytics"][
                "retention_by_subscribed_status"
            ],
            segmented["id-0"],
        )

    def test_latest_video_ids_per_corner_limits_each_corner(self) -> None:
        history_rows = {}
        details = []
        for index in range(8):
            video_id = f"video-{index}"
            history_rows[video_id] = {
                "corner": "video",
                "ts": f"2026-07-{index + 1:02d}T00:00:00+00:00",
            }
            details.append(
                {
                    "video_id": video_id,
                    "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                }
            )
        for index in range(2):
            video_id = f"short-{index}"
            history_rows[video_id] = {
                "corner": "shorts",
                "ts": f"2026-07-{index + 1:02d}T00:00:00+00:00",
            }
            details.append(
                {
                    "video_id": video_id,
                    "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                }
            )

        selected = performance._latest_video_ids_per_corner(
            history_rows,
            details,
            list(history_rows),
        )

        self.assertEqual(
            [video_id for video_id in selected if video_id.startswith("video-")],
            ["video-7", "video-6", "video-5", "video-4", "video-3"],
        )
        self.assertEqual(
            [video_id for video_id in selected if video_id.startswith("short-")],
            ["short-1", "short-0"],
        )

    def test_segmented_retention_failure_preserves_aggregate_curve(self) -> None:
        self.spec.pipeline["performance_feedback"] = True
        self._history(count=1)
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_rows = [{"video_id": "id-0", "views": 100}]
        base_curve = [
            {"elapsed_ratio": index / 10, "watch_ratio": 0.9 - index / 20}
            for index in range(1, 7)
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(
                performance.youtube, "video_analytics", return_value=analytics_rows
            ),
            patch.object(
                performance.youtube, "video_traffic_sources", return_value={}
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=({"id-0": base_curve}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves_by_subscribed_status",
                side_effect=RuntimeError("segment quota exhausted"),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertTrue(snapshot["retention_curve"]["available"])
        self.assertEqual(
            snapshot["videos"][0]["analytics"]["retention_curve"], base_curve
        )
        self.assertFalse(
            snapshot["retention_by_subscribed_status"]["available"]
        )
        self.assertIn(
            "segment quota exhausted",
            snapshot["retention_by_subscribed_status"]["reason"],
        )
        self.assertNotIn(
            "retention_by_subscribed_status",
            snapshot["videos"][0]["analytics"],
        )

    def test_subscribed_status_retention_comparison_is_sample_gated_and_mapped(
        self,
    ) -> None:
        subscribed = []
        unsubscribed = []
        for index in range(1, 7):
            elapsed = index / 10
            subscribed.append(
                {
                    "elapsed_ratio": elapsed,
                    "watch_ratio": 0.9 - index * 0.02,
                    "segment_impressions": 30,
                }
            )
            unsubscribed.append(
                {
                    "elapsed_ratio": elapsed,
                    "watch_ratio": 0.9 - index * 0.06,
                    "segment_impressions": 40,
                }
            )
        curves = {
            "SUBSCRIBED": subscribed,
            "UNSUBSCRIBED": unsubscribed,
        }
        result = performance.subscribed_status_retention_comparison(
            curves,
            "PT100S",
            {"scenes": [{"caption": "導入"}, {"caption": "展開"}]},
        )

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["actionable"])
        self.assertEqual(result["higher_segment"], "SUBSCRIBED")
        self.assertAlmostEqual(result["elapsed_seconds"], 60.0)
        self.assertEqual(result["scene_caption"], "展開")
        self.assertGreaterEqual(abs(result["gap_ratio"]), 0.08)

        insufficient = performance.subscribed_status_retention_comparison(
            {
                status: [
                    {**point, "segment_impressions": 19}
                    for point in points
                ]
                for status, points in curves.items()
            },
            "PT100S",
        )
        self.assertEqual(insufficient["status"], "insufficient_data")
        self.assertEqual(insufficient["reliable_point_count"], 0)

    def test_subscribed_status_retention_comparison_threshold_is_inclusive(
        self,
    ) -> None:
        def curves(gap: float) -> dict:
            return {
                "SUBSCRIBED": [
                    {
                        "elapsed_ratio": index / 10,
                        "watch_ratio": 0.8,
                        "segment_impressions": 30,
                    }
                    for index in range(1, 7)
                ],
                "UNSUBSCRIBED": [
                    {
                        "elapsed_ratio": index / 10,
                        "watch_ratio": 0.8 - gap,
                        "segment_impressions": 30,
                    }
                    for index in range(1, 7)
                ],
            }

        exact = performance.subscribed_status_retention_comparison(
            curves(0.08), "PT1M"
        )
        below = performance.subscribed_status_retention_comparison(
            curves(0.079), "PT1M"
        )

        self.assertEqual(exact["status"], "ready")
        self.assertTrue(exact["actionable"])
        self.assertEqual(below["status"], "no_clear_difference")
        self.assertFalse(below["actionable"])

    def test_subscribed_status_retention_comparison_uses_official_point_range(
        self,
    ) -> None:
        ratios = [0.0, 0.01, 0.25, 0.5, 0.75, 1.0, 1.01]
        curves = {
            status: [
                {
                    "elapsed_ratio": ratio,
                    "watch_ratio": 0.8 if status == "SUBSCRIBED" else 0.7,
                    "segment_impressions": 30,
                }
                for ratio in ratios
            ]
            for status in ("SUBSCRIBED", "UNSUBSCRIBED")
        }

        result = performance.subscribed_status_retention_comparison(
            curves, "PT1M"
        )

        self.assertEqual(result["common_point_count"], 5)
        self.assertEqual(result["reliable_point_count"], 5)
        self.assertEqual(result["status"], "ready")

    def test_retention_queries_only_videos_with_analytics_rows(self) -> None:
        """issue #149 (Sol review指摘): Analytics実績が無い動画は維持率APIの
        照会対象にしない。snapshotには retention_curve キーを付けない。"""
        rows = []
        for index in range(2):
            rows.append(
                {
                    "ts": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    "channel": self.spec.id,
                    "corner": "video",
                    "title": f"Title {index}",
                    "topic": f"Topic {index}",
                    "video_id": f"id-{index}",
                    "status": "published",
                    "workdir": str(self.root / "run-0"),
                }
            )
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": f"id-{index}",
                "title": f"Title {index}",
                "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
            for index in range(2)
        ]
        # id-0 のみAnalytics実績あり（id-1は古い/無実績）
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=({}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=(
                    {"id-0": [{"elapsed_ratio": 0.5, "watch_ratio": 0.60}]},
                    {},
                ),
            ) as retention_mock,
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertEqual(retention_mock.call_args.args[0], ["id-0"])
        self.assertEqual(
            snapshot["videos"][0]["analytics"]["retention_curve"],
            [{"elapsed_ratio": 0.5, "watch_ratio": 0.60}],
        )
        # 照会対象外の id-1 は retention_curve キー自体を持たない。
        analytics_v1 = snapshot["videos"][1]["analytics"]
        if isinstance(analytics_v1, dict):
            self.assertNotIn("retention_curve", analytics_v1)
        else:
            self.assertIsNone(analytics_v1)

    def test_retention_sync_workdir_is_scoped_to_output_dir(self) -> None:
        """issue #149 (Sol review指摘): snapshotへ保存されるworkdirは出力領域
        配下のみ。領域外パスは空になる。"""
        self._history(count=1)
        rows = [
            {
                "ts": "2026-07-01T00:00:00+00:00",
                "channel": self.spec.id,
                "corner": "video",
                "title": "Title 0",
                "topic": "Topic 0",
                "video_id": "id-0",
                "status": "published",
                "workdir": "/etc/passwd-adjacent",
            }
        ]
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=({}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=(
                    {"id-0": [{"elapsed_ratio": 0.5, "watch_ratio": 0.60}]},
                    {},
                ),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertEqual(snapshot["videos"][0]["workdir"], "")

    def test_retention_snapshot_workdir_enables_scene_readback(self) -> None:
        """issue #149: 出力領域内のworkdirがsnapshotへ保存され、そこから
        script.json の scene caption を読み取れる（sync→report実経路の回帰）。"""
        workdir = self.root / "run-0"
        workdir.mkdir()
        (workdir / "script.json").write_text(
            json.dumps(
                {
                    "narration": "あ" * 50,
                    "scenes": [{"caption": "導入"}, {"caption": "展開"}],
                }
            ),
            encoding="utf-8",
        )
        rows = [
            {
                "ts": "2026-07-01T00:00:00+00:00",
                "channel": self.spec.id,
                "corner": "video",
                "title": "Title 0",
                "topic": "Topic 0",
                "video_id": "id-0",
                "status": "published",
                "workdir": str(workdir),
            }
        ]
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=({}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=(
                    {
                        "id-0": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.5, "watch_ratio": 0.40},
                            {"elapsed_ratio": 1.0, "watch_ratio": 0.30},
                        ]
                    },
                    {},
                ),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        row = snapshot["videos"][0]
        self.assertEqual(row["workdir"], str(workdir.resolve()))
        script = performance_report._script_for_video(row)
        self.assertEqual(script["scenes"][0]["caption"], "導入")

    def test_traffic_status_change_writes_new_snapshot_row(self) -> None:
        """issue #164 (Sol review指摘4): traffic_sources のavailable/reasonが
        変化した場合、snapshot署名に含まれ新しい行が追記される。"""
        self._history(count=1)
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(performance.youtube, "video_search_terms", return_value={}),
        ):
            first = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )
        self.assertFalse(first["traffic_sources"]["available"])
        self.assertIn("boom", first["traffic_sources"]["reason"])

        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 1}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=(
                    {"id-0": [{"term": "語句", "views": 1}]},
                    {},
                ),
            ),
        ):
            second = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, 1, tzinfo=timezone.utc),
            )

        self.assertTrue(second["traffic_sources"]["available"])
        lines = (self.root / "performance.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)

    def test_search_term_global_failure_keeps_traffic_sources_available(self) -> None:
        """issue #164 (Sol review指摘): 検索語句の全体障害（HTTP 403等）でも
        traffic sourceの実データとavailable=Trueは保持し、search_termsの
        statusは別途Falseにする。"""
        self._history(count=1)
        rows = [
            {
                "ts": "2026-07-01T00:00:00+00:00",
                "channel": self.spec.id,
                "corner": "shorts",
                "title": "Title 0",
                "topic": "Topic 0",
                "video_id": "id-0",
                "status": "published",
                "topic_metadata": {"gap_query": "ネタ切れ 解消"},
            }
        ]
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_rows = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_rows),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                side_effect=RuntimeError("quota exceeded"),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertTrue(snapshot["traffic_sources"]["available"])
        self.assertEqual(
            snapshot["videos"][0]["analytics"]["traffic_sources"],
            {"YT_SEARCH": 40},
        )
        self.assertFalse(snapshot["search_terms"]["available"])
        self.assertIn("quota exceeded", snapshot["search_terms"]["reason"])

    def test_search_term_video_specific_failures_recorded_but_keep_available(self) -> None:
        """issue #164: 動画固有エラー（HTTP 400等）は available=True を維持し
        失敗video_idをstatusへ記録、成功分の検索語句も保持する。"""
        self._history(count=2)
        rows = []
        for index in range(2):
            rows.append(
                {
                    "ts": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                    "channel": self.spec.id,
                    "corner": "shorts",
                    "title": f"Title {index}",
                    "topic": f"Topic {index}",
                    "video_id": f"id-{index}",
                    "status": "published",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                }
            )
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": f"id-{index}",
                "title": f"Title {index}",
                "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
            for index in range(2)
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(
                performance.youtube,
                "video_analytics",
                return_value=[
                    {
                        "video_id": "id-0",
                        "views": 100,
                        "engaged_views": 60,
                        "estimated_minutes_watched": 90.0,
                        "average_view_duration": 45.0,
                        "average_view_percentage": 72.4,
                        "likes": 5,
                        "comments": 2,
                    },
                    {
                        "video_id": "id-1",
                        "views": 100,
                        "engaged_views": 60,
                        "estimated_minutes_watched": 90.0,
                        "average_view_duration": 45.0,
                        "average_view_percentage": 72.4,
                        "likes": 5,
                        "comments": 2,
                    },
                ],
            ),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}, "id-1": {"YT_SEARCH": 20}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=(
                    {"id-1": [{"term": "ネタ切れ 解消", "views": 20}]},
                    {"id-0": "HTTP 400: privacy threshold"},
                ),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertTrue(snapshot["search_terms"]["available"])
        self.assertEqual(snapshot["search_terms"]["failed_video_ids"], ["id-0"])
        self.assertEqual(
            snapshot["videos"][1]["analytics"]["search_terms"],
            [{"term": "ネタ切れ 解消", "views": 20}],
        )

    def test_search_terms_unavailable_when_all_videos_fail_video_specific(self) -> None:
        """issue #164 (Sol review指摘): 全動画が動画固有エラーで失敗した場合、
        video_search_terms() が例外を送出し search_terms.available=False になる。"""
        self._history(count=1)
        rows = [
            {
                "ts": "2026-07-01T00:00:00+00:00",
                "channel": self.spec.id,
                "corner": "shorts",
                "title": "Title 0",
                "topic": "Topic 0",
                "video_id": "id-0",
                "status": "published",
                "topic_metadata": {"gap_query": "ネタ切れ 解消"},
            }
        ]
        self.spec.history_file.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(
                performance.youtube,
                "video_analytics",
                return_value=[
                    {
                        "video_id": "id-0",
                        "views": 100,
                        "engaged_views": 60,
                        "estimated_minutes_watched": 90.0,
                        "average_view_duration": 45.0,
                        "average_view_percentage": 72.4,
                        "likes": 5,
                        "comments": 2,
                    }
                ],
            ),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                side_effect=RuntimeError("all videos failed video-specific error"),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertFalse(snapshot["search_terms"]["available"])
        self.assertIn(
            "all videos failed",
            snapshot["search_terms"]["reason"],
        )
        # traffic sourceの実データは保持される。
        self.assertTrue(snapshot["traffic_sources"]["available"])
        self.assertEqual(
            snapshot["videos"][0]["analytics"]["traffic_sources"],
            {"YT_SEARCH": 40},
        )

    def test_format_traits_are_scoped_and_exclude_topic_text(self) -> None:
        workdir = self.root / "run"
        workdir.mkdir()
        (workdir / "script.json").write_text(
            json.dumps(
                {
                    "title": "retentionという題材",
                    "scenes": [
                        {"caption": "one"},
                        {"caption": "two", "chart": {"type": "bar"}},
                    ],
                }
            ),
            encoding="utf-8",
        )

        traits = performance._format_traits(
            self.spec,
            {
                "tier": "longform",
                "duration_sec": 190,
                "workdir": str(workdir),
            },
        )

        self.assertEqual(
            traits,
            [
                "tier:longform",
                "duration:180s_or_more",
                "scenes:1_to_4",
                "chart:present",
            ],
        )
        self.assertNotIn("retention", " ".join(traits))

    def test_sync_records_share_30d_separately(self) -> None:
        """issue #144 (Sol review指摘): 共有率は90日集計とは別に、過去30日
        集計を `share_30d` として保存する。30暦日（開始-終了が29日差）を
        太平洋時間基準で計算し、shorts動画だけを専用APIで取得する。"""
        self._history(count=1)
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-06-28T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_90d = [
            {
                "video_id": "id-0",
                "views": 5000,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(
                performance.youtube,
                "video_analytics",
                return_value=analytics_90d,
            ) as analytics_mock,
            patch.object(
                performance.youtube,
                "video_share_metrics",
                return_value=[
                    {"video_id": "id-0", "views": 500, "shares": 8}
                ],
            ) as share_metrics_mock,
            patch.object(performance.youtube, "video_traffic_sources", return_value={}),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=({}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=({}, {}),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertTrue(snapshot["share_30d"]["available"])
        self.assertEqual(
            snapshot["share_30d"]["start_date"], "2026-06-25"
        )
        self.assertEqual(
            snapshot["share_30d"]["end_date"], "2026-07-24"
        )
        start = datetime.fromisoformat(
            snapshot["share_30d"]["start_date"]
        ).date()
        end = datetime.fromisoformat(
            snapshot["share_30d"]["end_date"]
        ).date()
        self.assertEqual((end - start).days, 29)
        self.assertEqual(
            snapshot["videos"][0]["share_30d"],
            {"shares": 8, "views": 500},
        )
        # 90日集計のanalyticsにはsharesを混ぜない（共有率はshare_30dのみ）。
        self.assertNotIn("shares", snapshot["videos"][0]["analytics"])
        # video_analytics は90日分の1回だけ（共有率は専用関数）。
        analytics_mock.assert_called_once()
        # 専用関数はshorts IDだけ・views,sharesのみで呼ばれる。
        self.assertEqual(share_metrics_mock.call_args.args[0], ["id-0"])
        self.assertEqual(
            share_metrics_mock.call_args.kwargs["start_date"], "2026-06-25"
        )
        self.assertEqual(
            share_metrics_mock.call_args.kwargs["end_date"], "2026-07-24"
        )

    def test_sync_share_30d_excludes_non_shorts_videos(self) -> None:
        """issue #144 (Sol review指摘): 共有率の30日集計はshorts動画だけを
        照会し、video/analytics動画を渡さない。"""
        self._history(count=2)  # index 0=shorts, index 1=video
        details = [
            {
                "video_id": f"id-{index}",
                "title": f"Title {index}",
                "published_at": f"2026-07-{index + 1:02d}T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
            for index in range(2)
        ]
        analytics_90d = [
            {
                "video_id": f"id-{index}",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
                "shares": 1,
            }
            for index in range(2)
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_90d),
            patch.object(
                performance.youtube,
                "video_share_metrics",
                return_value=[],
            ) as share_metrics_mock,
            patch.object(performance.youtube, "video_traffic_sources", return_value={}),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=({}, {}),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=({}, {}),
            ),
        ):
            performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )
        # shorts（id-0）だけが照会対象。
        self.assertEqual(share_metrics_mock.call_args.args[0], ["id-0"])

    def test_sync_share_metrics_failure_keeps_other_data(self) -> None:
        """issue #144 (Sol review指摘): `video_share_metrics` だけが例外を
        出しても90日analytics・traffic・search・retentionは保持され、
        `share_30d.available=False` とreasonが保存される。"""
        self._history(count=1)
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        analytics_90d = [
            {
                "video_id": "id-0",
                "views": 100,
                "engaged_views": 60,
                "estimated_minutes_watched": 90.0,
                "average_view_duration": 45.0,
                "average_view_percentage": 72.4,
                "likes": 5,
                "comments": 2,
            }
        ]
        retention_rows = [
            {
                "elapsed_ratio": 0.0,
                "watch_ratio": 0.90,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(performance.youtube, "video_analytics", return_value=analytics_90d),
            patch.object(
                performance.youtube,
                "video_share_metrics",
                side_effect=RuntimeError("share metrics broken"),
            ),
            patch.object(
                performance.youtube,
                "video_traffic_sources",
                return_value={"id-0": {"YT_SEARCH": 40}},
            ),
            patch.object(
                performance.youtube,
                "video_search_terms",
                return_value=(
                    {"id-0": [{"term": "ショート 企画", "views": 30}]},
                    {},
                ),
            ),
            patch.object(
                performance.youtube,
                "video_retention_curves",
                return_value=(
                    {"id-0": retention_rows},
                    {},
                ),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertFalse(snapshot["share_30d"]["available"])
        self.assertIn("共有率(30日)readback失敗", snapshot["share_30d"]["reason"])
        # 90日analyticsは保持される。
        self.assertEqual(snapshot["videos"][0]["analytics"]["views"], 100)
        self.assertNotIn("share_30d", snapshot["videos"][0])
        # traffic/search/retentionも保持される。
        self.assertTrue(snapshot["traffic_sources"]["available"])
        self.assertTrue(snapshot["search_terms"]["available"])
        self.assertTrue(snapshot["retention_curve"]["available"])
        video = snapshot["videos"][0]
        self.assertEqual(
            video["analytics"]["traffic_sources"], {"YT_SEARCH": 40}
        )
        # 検索語句はgap_query付き動画だけ照会されるため、この動画では空。
        self.assertEqual(video["analytics"]["search_terms"], [])
        self.assertEqual(video["analytics"]["retention_curve"], retention_rows)

    def test_sync_share_metrics_runs_even_when_90d_analytics_fails(self) -> None:
        """issue #144 (Sol review指摘): 90日Analyticsが失敗しても共有率
        専用クエリは独立に実行され、`share_30d` が保存される。"""
        self._history(count=1)
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(
                performance.youtube,
                "video_analytics",
                side_effect=RuntimeError("90d analytics broken"),
            ),
            patch.object(
                performance.youtube,
                "video_share_metrics",
                return_value=[
                    {"video_id": "id-0", "views": 500, "shares": 8}
                ],
            ) as share_metrics_mock,
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertFalse(snapshot["analytics"]["available"])
        share_metrics_mock.assert_called_once()
        self.assertTrue(snapshot["share_30d"]["available"])
        self.assertEqual(
            snapshot["videos"][0]["share_30d"],
            {"shares": 8, "views": 500},
        )

    def test_sync_failure_reasons_do_not_claim_other_status_saved(self) -> None:
        """issue #144 (Sol review指摘): 各statusの理由文は自身の取得結果だけを
        説明し、他方の保存状態（90日指標のみ・Data APIのみ等）を断定しない。"""
        self._history(count=1)
        details = [
            {
                "video_id": "id-0",
                "title": "Title 0",
                "published_at": "2026-07-01T00:00:00Z",
                "privacy_status": "public",
                "duration": "PT1M",
                "views": 100,
                "likes": 0,
                "comments": 0,
            }
        ]
        with (
            patch.object(performance.youtube, "video_details", return_value=details),
            patch.object(performance.youtube, "_token_has_scopes", return_value=True),
            patch.object(
                performance.youtube,
                "video_analytics",
                side_effect=RuntimeError("90d broken"),
            ),
            patch.object(
                performance.youtube,
                "video_share_metrics",
                side_effect=RuntimeError("share broken"),
            ),
        ):
            snapshot = performance.sync(
                self.spec,
                now=datetime(2026, 7, 26, tzinfo=timezone.utc),
            )
        analytics_reason = snapshot["analytics"]["reason"]
        share_reason = snapshot["share_30d"]["reason"]
        # 90日失敗の理由は90日自身の失敗を説明し、共有率保存を断定しない。
        self.assertIn("90日Analytics readback失敗", analytics_reason)
        self.assertNotIn("共有率", analytics_reason)
        # 共有率失敗の理由は共有率自身の失敗を説明し、90日指標の保存を断定しない。
        self.assertIn("共有率(30日)readback失敗", share_reason)
        self.assertNotIn("90日指標のみ保存", share_reason)

    def test_analytics_relative_signal_creates_traceable_guarded_guidance(self) -> None:
        videos = []
        for index in range(8):
            upper = index >= 6
            videos.append(
                {
                    "video_id": f"id-{index}",
                    "corner": "video",
                    "title": f"NEVER INCLUDE TITLE {index}",
                    "topic": f"NEVER INCLUDE TOPIC {index}",
                    "format_traits": (
                        [
                            "tier:long_short",
                            "duration:60_to_179s",
                            "chart:present",
                        ]
                        if upper
                        else [
                            "tier:long_short",
                            "duration:60_to_179s",
                            "chart:absent",
                        ]
                    ),
                    "privacy_status": "unlisted",
                    "data_api": {"views": 100},
                    "analytics": {
                        "views": 100,
                        "average_view_percentage": 40 + index * 5,
                    },
                }
            )
        snapshot = {
            "collected_at": "2026-07-26T00:00:00+00:00",
            "videos": videos,
        }

        decision = performance.build_decision(
            self.spec,
            snapshot,
            corner_key="video",
        )

        self.assertEqual(decision["status"], "active")
        self.assertIn("youtube_analytics_api_v2", decision["metric"])
        self.assertIn("decision", decision["guidance"])
        self.assertIn("30日cooldown", decision["guidance"])
        self.assertNotIn("NEVER INCLUDE TITLE", decision["guidance"])
        self.assertNotIn("NEVER INCLUDE TOPIC", decision["guidance"])
        self.assertNotIn("concept:", decision["guidance"])
        self.assertEqual(
            decision["format_cohort"],
            "duration:60_to_179s|tier:long_short",
        )
        self.assertEqual(decision["positive_traits"], ["chart:present"])
        self.assertEqual(decision["negative_traits"], [])
        self.assertEqual(len(decision["top_video_ids"]), 2)
        self.assertEqual(len(decision["bottom_video_ids"]), 2)
        stored = json.loads(
            (self.root / "performance_decision.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["decision_id"], decision["decision_id"])

        # 仮説生成は自動適用と切り離されているため、同じsnapshotへ何度
        # build_decisionを呼んでも常に同じ"active"仮説を返す（予約や
        # waiting状態への遷移はない）。
        repeated = performance.build_decision(
            self.spec,
            snapshot,
            corner_key="video",
        )
        self.assertEqual(repeated["status"], "active")
        self.assertEqual(repeated["decision_id"], decision["decision_id"])


if __name__ == "__main__":
    unittest.main()
