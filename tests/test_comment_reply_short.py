"""Issue #105: コメントステッカー付き返信Shortの手動検証。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import comment_reply_short


class CommentReplyShortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=root / "output" / "youtube-growth",
            corners={"shorts": object(), "video": object(), "analytics": object()},
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    token=root / "youtube-token.json",
                    analytics_token=root / "youtube-analytics-token.json",
                    client_secret=root / "youtube-client-secret.json",
                )
            ),
        )
        self.source_id = "SourceA12345"
        self.reply_id = "ReplyAA12345"
        self.baseline_ids = ["BaseAAA12345", "BaseBBB12345", "BaseCCC12345"]
        self.experiment_id = "crs-0000000000000001"
        self.plan_now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        self.start_now = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
        self.complete_now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

    def _video(
        self,
        video_id: str,
        *,
        published_at: str,
        duration: str = "PT58S",
        privacy: str = "public",
    ) -> dict:
        return {
            "video_id": video_id,
            "channel_id": "owned-channel",
            "title": f"動画 {video_id}",
            "published_at": published_at,
            "duration": duration,
            "privacy_status": privacy,
            "views": 100,
            "comments": 2,
        }

    def _video_payload(
        self,
        *,
        reply_duration: str = "PT58S",
        reply_privacy: str = "public",
        baseline_dates: list[str] | None = None,
    ) -> dict:
        dates = baseline_dates or [
            "2026-08-07T18:00:00Z",
            "2026-08-06T18:00:00Z",
            "2026-08-05T18:00:00Z",
        ]
        videos = [
            self._video(
                self.source_id,
                published_at="2026-08-01T18:00:00Z",
                duration="PT6M",
            ),
            self._video(
                self.reply_id,
                published_at="2026-08-10T18:00:00Z",
                duration=reply_duration,
                privacy=reply_privacy,
            ),
        ]
        videos.extend(
            self._video(video_id, published_at=published_at)
            for video_id, published_at in zip(self.baseline_ids, dates)
        )
        return {"channel_id": "owned-channel", "videos": videos}

    def _plan(self, experiment_id: str | None = None) -> dict:
        return comment_reply_short.plan_experiment(
            self.spec,
            source_video_id=self.source_id,
            source_comment_id="UgxComment.123",
            request_summary="視聴維持率の改善方法を知りたいという質問",
            reply_corner="shorts",
            comparison_key="視聴維持率への回答",
            observation_days=7,
            question_or_request_confirmed=True,
            now=self.plan_now,
            experiment_id=experiment_id or self.experiment_id,
        )

    def _start(
        self,
        *,
        baseline_ids: list[str] | None = None,
        payload: dict | None = None,
    ) -> dict:
        with mock.patch.object(
            comment_reply_short.youtube,
            "owned_video_details_readonly",
            return_value=payload or self._video_payload(),
        ) as readback:
            manifest = comment_reply_short.start_experiment(
                self.spec,
                self.experiment_id,
                reply_video_id=self.reply_id,
                baseline_video_ids=baseline_ids or self.baseline_ids,
                comment_sticker_confirmed=True,
                youtube_app_published_confirmed=True,
                recent_same_type_baselines_confirmed=True,
                now=self.start_now,
            )
        readback.assert_called_once()
        self.assertEqual(
            readback.call_args.kwargs["token_file"],
            self.spec.publish.youtube.analytics_token,
        )
        return manifest

    def _metrics(
        self,
        manifest: dict,
        *,
        data_through: str | None = "2026-08-17",
        rows: list[dict] | None = None,
        probe_end: str = "2026-08-18",
    ) -> dict:
        windows = [manifest["reply"], *manifest["baselines"]]
        if rows is None:
            counts = [
                (1000, 20, 8, 2),
                (800, 8, 4, 2),
                (1200, 12, 7, 3),
                (1000, 10, 5, 2),
            ]
            rows = []
            for video, (views, comments, gained, lost) in zip(windows, counts):
                rows.append(
                    {
                        "video_id": video["video_id"],
                        "start_date": video["observation_start_date"],
                        "end_date": video["observation_end_date"],
                        "views": views,
                        "comments": comments,
                        "subscribers_gained": gained,
                        "subscribers_lost": lost,
                        "net_subscribers": gained - lost,
                    }
                )
        return {
            "source": "youtube_analytics_api_v2",
            "metrics": [
                "views",
                "comments",
                "subscribersGained",
                "subscribersLost",
            ],
            "availability_start_date": manifest["reply"][
                "observation_start_date"
            ],
            "availability_probe_end_date": probe_end,
            "data_through_date": data_through,
            "videos": rows,
        }

    def _manifest_path(self) -> Path:
        return (
            self.spec.output_dir
            / "comment_reply_short_tests"
            / self.experiment_id
            / "manifest.json"
        )

    def test_plan_records_only_safe_comment_summary_and_no_youtube_write(self) -> None:
        manifest = self._plan()

        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(manifest["reply_corner"], "shorts")
        self.assertEqual(
            set(manifest["source_comment"]),
            {
                "source_video_id",
                "comment_id",
                "request_summary",
                "question_or_request_confirmed",
                "commenter_identity_stored",
                "raw_comment_stored",
            },
        )
        self.assertFalse(manifest["source_comment"]["commenter_identity_stored"])
        self.assertFalse(manifest["source_comment"]["raw_comment_stored"])
        self.assertFalse(manifest["manual_workflow"]["youtube_write_performed"])
        plan = self._manifest_path().with_name("plan.md").read_text(encoding="utf-8")
        self.assertIn(comment_reply_short.OFFICIAL_HELP_URL, plan)
        self.assertIn("YouTubeアプリ", plan)
        self.assertIn("3本未満", plan)

    def test_plan_requires_question_confirmation_and_configured_corner(self) -> None:
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "question or request"
        ):
            comment_reply_short.plan_experiment(
                self.spec,
                source_video_id=self.source_id,
                source_comment_id="UgxComment.123",
                request_summary="質問の要約",
                reply_corner="shorts",
                comparison_key="同系統",
            )
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "not configured"
        ):
            comment_reply_short.plan_experiment(
                self.spec,
                source_video_id=self.source_id,
                source_comment_id="UgxComment.123",
                request_summary="質問の要約",
                reply_corner="unknown",
                comparison_key="同系統",
                question_or_request_confirmed=True,
            )

    def test_plan_rejects_duplicate_active_source_comment(self) -> None:
        self._plan()
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "already exists"
        ):
            self._plan("crs-0000000000000002")

    def test_start_verifies_history_free_app_video_and_uses_equal_full_days(self) -> None:
        self._plan()
        manifest = self._start()

        self.assertEqual(manifest["status"], "running")
        self.assertEqual(manifest["owned_channel_id"], "owned-channel")
        self.assertEqual(manifest["reply"]["observation_start_date"], "2026-08-11")
        self.assertEqual(manifest["reply"]["observation_end_date"], "2026-08-17")
        self.assertEqual(
            manifest["baselines"][0]["observation_start_date"], "2026-08-08"
        )
        self.assertEqual(
            manifest["baselines"][0]["observation_end_date"], "2026-08-14"
        )
        self.assertTrue(manifest["manual_confirmation"]["comment_sticker_confirmed"])
        self.assertFalse(manifest["manual_confirmation"]["youtube_write_performed"])

    def test_start_requires_all_manual_confirmations(self) -> None:
        self._plan()
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "confirm the comment sticker"
        ):
            comment_reply_short.start_experiment(
                self.spec,
                self.experiment_id,
                reply_video_id=self.reply_id,
                baseline_video_ids=self.baseline_ids,
            )

    def test_start_rejects_foreign_missing_private_long_and_newer_baseline(self) -> None:
        self._plan()
        missing = self._video_payload()
        missing["videos"] = [
            item for item in missing["videos"] if item["video_id"] != self.reply_id
        ]
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "not found"
        ):
            self._start(payload=missing)

        for payload, message in (
            (self._video_payload(reply_privacy="private"), "public or unlisted"),
            (self._video_payload(reply_duration="PT3M1S"), "exceeds 180"),
            (
                self._video_payload(
                    baseline_dates=[
                        "2026-08-11T18:00:00Z",
                        "2026-08-06T18:00:00Z",
                        "2026-08-05T18:00:00Z",
                    ]
                ),
                "must predate",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    comment_reply_short.CommentReplyShortError, message
                ):
                    self._start(payload=payload)

    def test_complete_records_descriptive_comparison_without_winner(self) -> None:
        self._plan()
        running = self._start()
        with mock.patch.object(
            comment_reply_short.youtube,
            "comment_reply_short_metrics",
            return_value=self._metrics(running),
        ) as readback:
            completed = comment_reply_short.complete_experiment(
                self.spec,
                self.experiment_id,
                setup_unchanged_confirmed=True,
                notes="次は回答形式だけを変えて観測する",
                now=self.complete_now,
            )

        readback.assert_called_once()
        self.assertEqual(
            readback.call_args.kwargs["token_file"],
            self.spec.publish.youtube.analytics_token,
        )
        result = completed["result"]
        self.assertEqual(result["status"], "observed")
        reply = result["observations"][0]
        self.assertEqual(reply["comments_per_1000_views"], 20.0)
        self.assertEqual(reply["net_subscribers"], 6)
        self.assertEqual(reply["net_subscribers_per_1000_views"], 6.0)
        comparison = result["comparison"]
        self.assertEqual(comparison["status"], "ready")
        self.assertEqual(comparison["valid_baseline_count"], 3)
        self.assertEqual(comparison["baseline_medians"]["comments"], 10.0)
        self.assertEqual(
            comparison["reply_minus_baseline_median"]["comments"], 10.0
        )
        self.assertIsNone(comparison["winner"])
        self.assertIsNone(comparison["causal_conclusion"])
        self.assertFalse(comparison["universal_threshold_applied"])
        memo = self._manifest_path().with_name("next_idea_memo.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("watch page", memo)
        self.assertIn("勝者や因果は断定しません", memo)

    def test_one_baseline_records_metrics_but_withholds_median(self) -> None:
        self._plan()
        payload = self._video_payload()
        payload["videos"] = [
            item
            for item in payload["videos"]
            if item["video_id"] in {self.source_id, self.reply_id, self.baseline_ids[0]}
        ]
        running = self._start(
            baseline_ids=[self.baseline_ids[0]], payload=payload
        )
        metrics = self._metrics(running)
        metrics["videos"] = metrics["videos"][:2]
        with mock.patch.object(
            comment_reply_short.youtube,
            "comment_reply_short_metrics",
            return_value=metrics,
        ):
            completed = comment_reply_short.complete_experiment(
                self.spec,
                self.experiment_id,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )

        comparison = completed["result"]["comparison"]
        self.assertEqual(comparison["status"], "insufficient_comparable_baselines")
        self.assertEqual(comparison["valid_baseline_count"], 1)
        self.assertTrue(
            all(value is None for value in comparison["baseline_medians"].values())
        )
        self.assertEqual(completed["result"]["status"], "observed")

    def test_missing_subscriber_metrics_is_insufficient_not_zero(self) -> None:
        self._plan()
        running = self._start()
        metrics = self._metrics(running)
        metrics["videos"][0]["subscribers_gained"] = None
        metrics["videos"][0]["subscribers_lost"] = None
        metrics["videos"][0]["net_subscribers"] = None
        with mock.patch.object(
            comment_reply_short.youtube,
            "comment_reply_short_metrics",
            return_value=metrics,
        ):
            completed = comment_reply_short.complete_experiment(
                self.spec,
                self.experiment_id,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )

        result = completed["result"]
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["observations"][0]["net_subscribers"])
        self.assertIn("登録者増減", result["reason"])

    def test_incomplete_analytics_period_stays_running_then_closes_unavailable(self) -> None:
        self._plan()
        running = self._start()
        incomplete = self._metrics(running, data_through="2026-08-15")
        with mock.patch.object(
            comment_reply_short.youtube,
            "comment_reply_short_metrics",
            return_value=incomplete,
        ):
            with self.assertRaisesRegex(
                comment_reply_short.CommentReplyShortError, "remains running"
            ):
                comment_reply_short.complete_experiment(
                    self.spec,
                    self.experiment_id,
                    setup_unchanged_confirmed=True,
                    now=self.complete_now,
                )
        self.assertEqual(
            comment_reply_short.show_experiment(
                self.spec, self.experiment_id
            )["status"],
            "running",
        )

        late_now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        unavailable = self._metrics(
            running,
            data_through="2026-08-15",
            probe_end="2026-08-25",
        )
        with mock.patch.object(
            comment_reply_short.youtube,
            "comment_reply_short_metrics",
            return_value=unavailable,
        ):
            completed = comment_reply_short.complete_experiment(
                self.spec,
                self.experiment_id,
                setup_unchanged_confirmed=True,
                now=late_now,
            )
        result = completed["result"]
        self.assertEqual(result["status"], "insufficient_data")
        self.assertFalse(result["analytics_period_confirmed"])
        self.assertTrue(
            all(item["views"] is None for item in result["observations"])
        )

    def test_setup_change_invalidates_without_analytics_readback(self) -> None:
        self._plan()
        self._start()
        with mock.patch.object(
            comment_reply_short.youtube, "comment_reply_short_metrics"
        ) as readback:
            invalidated = comment_reply_short.complete_experiment(
                self.spec,
                self.experiment_id,
                setup_changed=True,
                now=self.complete_now,
            )

        readback.assert_not_called()
        self.assertEqual(invalidated["status"], "invalidated")
        self.assertEqual(
            invalidated["result"]["status"], "stopped_changed_setup"
        )
        self.assertEqual(invalidated["result"]["observations"], [])

    def test_completed_reply_short_cannot_be_counted_as_a_new_experiment(self) -> None:
        self._plan()
        running = self._start()
        with mock.patch.object(
            comment_reply_short.youtube,
            "comment_reply_short_metrics",
            return_value=self._metrics(running),
        ):
            comment_reply_short.complete_experiment(
                self.spec,
                self.experiment_id,
                setup_unchanged_confirmed=True,
                now=self.complete_now,
            )

        second_id = "crs-0000000000000002"
        comment_reply_short.plan_experiment(
            self.spec,
            source_video_id=self.source_id,
            source_comment_id="UgxComment.456",
            request_summary="別の質問の要約",
            reply_corner="shorts",
            comparison_key="視聴維持率への回答",
            question_or_request_confirmed=True,
            now=self.plan_now,
            experiment_id=second_id,
        )
        with mock.patch.object(
            comment_reply_short.youtube,
            "owned_video_details_readonly",
            return_value=self._video_payload(),
        ):
            with self.assertRaisesRegex(
                comment_reply_short.CommentReplyShortError,
                "already exists for reply Short",
            ):
                comment_reply_short.start_experiment(
                    self.spec,
                    second_id,
                    reply_video_id=self.reply_id,
                    baseline_video_ids=self.baseline_ids,
                    comment_sticker_confirmed=True,
                    youtube_app_published_confirmed=True,
                    recent_same_type_baselines_confirmed=True,
                    now=self.start_now,
                )

    def test_manifest_tampering_is_rejected_for_plan_setup_and_result(self) -> None:
        self._plan()
        path = self._manifest_path()
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        data["comparison_key"] = "changed"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "plan checksum"
        ):
            comment_reply_short.show_experiment(self.spec, self.experiment_id)

        data = json.loads(original)
        data["source_comment"]["commenter_name"] = "保存してはいけない名前"
        data["plan_sha256"] = comment_reply_short._plan_checksum(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError,
            "source_comment fields",
        ):
            comment_reply_short.show_experiment(self.spec, self.experiment_id)

        path.write_text(original, encoding="utf-8")
        running = self._start()
        self.assertEqual(
            running["running_sha256"],
            comment_reply_short._running_checksum(running),
        )
        running_original = path.read_text(encoding="utf-8")
        data = json.loads(running_original)
        data["started_at"] = "2026-08-11T03:00:01+00:00"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "running checksum"
        ):
            comment_reply_short.show_experiment(self.spec, self.experiment_id)

        data = json.loads(running_original)
        data["reply"]["video_id"] = "Changed12345"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError, "setup checksum"
        ):
            comment_reply_short.show_experiment(self.spec, self.experiment_id)

    def test_terminal_checksum_rejects_coordinated_result_tampering(self) -> None:
        self._plan()
        running = self._start()
        with mock.patch.object(
            comment_reply_short.youtube,
            "comment_reply_short_metrics",
            return_value=self._metrics(running),
        ):
            completed = comment_reply_short.complete_experiment(
                self.spec,
                self.experiment_id,
                setup_unchanged_confirmed=True,
                notes="original",
                now=self.complete_now,
            )
        self.assertEqual(
            completed["terminal_sha256"],
            comment_reply_short._terminal_checksum(completed),
        )
        path = self._manifest_path()
        original = path.read_text(encoding="utf-8")

        def coordinated_result_change(data: dict) -> None:
            observation = data["result"]["observations"][0]
            observation["comments"] = 999
            observation["comments_per_1000_views"] = 999.0
            data["result"]["comparison"] = comment_reply_short._comparison(
                data["result"]["observations"]
            )

        def notes_change(data: dict) -> None:
            data["result"]["notes"] = "changed"

        def completion_time_change(data: dict) -> None:
            data["completed_at"] = "2026-08-19T12:00:01+00:00"

        def remove_checksum(data: dict) -> None:
            data.pop("terminal_sha256")

        for label, mutate in (
            ("coordinated result", coordinated_result_change),
            ("notes", notes_change),
            ("completed_at", completion_time_change),
            ("missing checksum", remove_checksum),
        ):
            with self.subTest(label=label):
                data = json.loads(original)
                mutate(data)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                with self.assertRaisesRegex(
                    comment_reply_short.CommentReplyShortError,
                    "terminal checksum",
                ):
                    comment_reply_short.show_experiment(
                        self.spec, self.experiment_id
                    )

    def test_invalidated_result_is_covered_by_terminal_checksum(self) -> None:
        self._plan()
        self._start()
        invalidated = comment_reply_short.complete_experiment(
            self.spec,
            self.experiment_id,
            setup_changed=True,
            now=self.complete_now,
        )
        self.assertEqual(
            invalidated["terminal_sha256"],
            comment_reply_short._terminal_checksum(invalidated),
        )
        path = self._manifest_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["result"]["reason"] = "changed"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(
            comment_reply_short.CommentReplyShortError,
            "terminal checksum",
        ):
            comment_reply_short.show_experiment(self.spec, self.experiment_id)


if __name__ == "__main__":
    unittest.main()
