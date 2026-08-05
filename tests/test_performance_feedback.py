from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import history, performance


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
        self.assertIn("--auth --analytics --channel youtube-growth", snapshot["analytics"]["reason"])
        self.assertEqual(snapshot["videos"][1]["data_api"]["views"], 1)
        self.assertEqual(snapshot["videos"][0]["topic"], "Topic 0")
        self.assertNotIn("topic_concepts", snapshot["videos"][0])
        self.assertEqual(
            len((self.root / "performance.jsonl").read_text().splitlines()), 1
        )
        decision = performance.build_decision(self.spec, snapshot)
        self.assertIn(
            "--auth --analytics --channel youtube-growth",
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
        self.assertEqual(snapshot["videos"][0]["data_api"]["views"], 3)
        self.assertTrue((self.root / "performance.jsonl").exists())

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
