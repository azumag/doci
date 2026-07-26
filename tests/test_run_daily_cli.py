from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from doci import config, history, run_daily, youtube_review


class RunDailyCliTest(unittest.TestCase):
    def test_review_issues_are_checked_only_for_real_upload_runs(self) -> None:
        spec = SimpleNamespace(
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    review=SimpleNamespace(enabled=True),
                )
            )
        )
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(config, "PUBLISH_DRY_RUN", False),
            patch.object(
                youtube_review,
                "reconcile",
                return_value=[],
            ) as reconcile_mock,
        ):
            run_daily._reconcile_youtube_review(spec, True)
            run_daily._reconcile_youtube_review(spec, False)

        reconcile_mock.assert_called_once_with(spec)

    def test_review_issues_do_not_mutate_during_global_dry_run(self) -> None:
        spec = SimpleNamespace(
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    review=SimpleNamespace(enabled=True),
                )
            )
        )
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(config, "PUBLISH_DRY_RUN", True),
            patch.object(youtube_review, "reconcile") as reconcile_mock,
        ):
            run_daily._reconcile_youtube_review(spec, True)

        reconcile_mock.assert_not_called()

    def test_reconcile_all_checks_each_enabled_channel_without_generation(self) -> None:
        enabled = SimpleNamespace(
            id="enabled",
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    review=SimpleNamespace(enabled=True),
                )
            ),
        )
        disabled = SimpleNamespace(
            id="disabled",
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    review=SimpleNamespace(enabled=False),
                )
            ),
        )
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(config, "PUBLISH_DRY_RUN", False),
            patch.object(
                run_daily.channel,
                "discover",
                return_value=["enabled", "disabled"],
            ),
            patch.object(
                run_daily.channel,
                "load",
                side_effect=[enabled, disabled],
            ),
            patch.object(
                youtube_review,
                "reconcile_result",
                return_value=youtube_review.ReconcileResult(("checked",)),
            ) as reconcile_mock,
            patch.object(youtube_review, "save_retry_plan") as save_plan_mock,
            patch.object(run_daily, "run") as generate_mock,
        ):
            summary, exit_code = run_daily._reconcile_all_youtube_reviews()

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["channels"][0]["events"], ["checked"])
        reconcile_mock.assert_called_once_with(enabled)
        save_plan_mock.assert_called_once_with(enabled, "", ())
        generate_mock.assert_not_called()

    def test_reconcile_all_propagates_individual_failures(self) -> None:
        enabled = SimpleNamespace(
            id="enabled",
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    review=SimpleNamespace(enabled=True),
                )
            ),
        )
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(config, "PUBLISH_DRY_RUN", False),
            patch.object(run_daily.channel, "discover", return_value=["enabled"]),
            patch.object(run_daily.channel, "load", return_value=enabled),
            patch.object(
                youtube_review,
                "reconcile_result",
                return_value=youtube_review.ReconcileResult(
                    ("動画 broken123: 確認処理失敗 RuntimeError: boom",),
                    failed_count=1,
                    failed_video_ids=("broken123",),
                ),
            ),
            patch.object(youtube_review, "save_retry_plan") as save_plan_mock,
        ):
            summary, exit_code = run_daily._reconcile_all_youtube_reviews()

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["channels"][0]["status"], "error")
        self.assertEqual(summary["channels"][0]["failed_count"], 1)
        self.assertEqual(
            summary["channels"][0]["failed_video_ids"],
            ["broken123"],
        )
        save_plan_mock.assert_called_once_with(
            enabled,
            "",
            ("broken123",),
        )

    def test_channel_retry_uses_only_failed_video_ids_from_same_cycle(self) -> None:
        spec = SimpleNamespace(
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    review=SimpleNamespace(enabled=True),
                )
            )
        )
        outcome = youtube_review.ReconcileResult(
            ("動画 broken123: 確認処理失敗 RuntimeError: boom",),
            failed_count=1,
            failed_video_ids=("broken123",),
        )
        with (
            patch.dict(os.environ, {"DOCI_REVIEW_CYCLE_ID": "cron-123"}),
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(config, "PUBLISH_DRY_RUN", False),
            patch.object(
                youtube_review,
                "load_retry_plan",
                return_value=("broken123",),
            ) as load_plan_mock,
            patch.object(
                youtube_review,
                "reconcile_result",
                return_value=outcome,
            ) as result_mock,
            patch.object(youtube_review, "reconcile") as full_reconcile_mock,
            patch.object(
                youtube_review,
                "save_retry_plan",
            ) as save_plan_mock,
        ):
            run_daily._reconcile_youtube_review(spec, True)

        load_plan_mock.assert_called_once_with(spec, "cron-123")
        result_mock.assert_called_once_with(
            spec,
            only_video_ids={"broken123"},
        )
        full_reconcile_mock.assert_not_called()
        save_plan_mock.assert_called_once_with(
            spec,
            "cron-123",
            ("broken123",),
        )

    def test_reconcile_all_is_read_only_during_dry_run(self) -> None:
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(config, "PUBLISH_DRY_RUN", True),
            patch.object(run_daily.channel, "discover") as discover_mock,
            patch.object(youtube_review, "reconcile") as reconcile_mock,
        ):
            summary, exit_code = run_daily._reconcile_all_youtube_reviews()

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["status"], "skipped")
        discover_mock.assert_not_called()
        reconcile_mock.assert_not_called()

    def test_all_channels_continues_after_failure_and_succeeds_if_one_finishes(self) -> None:
        specs = {
            "alpha": SimpleNamespace(id="alpha"),
            "broken": SimpleNamespace(id="broken"),
            "beta": SimpleNamespace(id="beta"),
        }
        calls: list[str] = []

        def fake_run(spec, *_args, **_kwargs):
            calls.append(spec.id)
            if spec.id == "broken":
                raise RuntimeError("intentional failure")
            return {"channel": spec.id, "title": f"Title {spec.id}"}

        with (
            patch.object(
                run_daily.channel,
                "discover",
                return_value=["alpha", "broken", "beta"],
            ),
            patch.object(
                run_daily.channel,
                "load",
                side_effect=lambda channel_id: specs[channel_id],
            ),
            patch.object(run_daily, "run", side_effect=fake_run),
        ):
            summary, exit_code = run_daily._run_all_channels(
                "2026-07-17", do_upload=False, video_scenes=0
            )

        self.assertEqual(calls, ["alpha", "broken", "beta"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["succeeded"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(
            [item["status"] for item in summary["channels"]],
            ["ok", "error", "ok"],
        )

    def test_all_channels_returns_nonzero_only_when_all_fail(self) -> None:
        with (
            patch.object(run_daily.channel, "discover", return_value=["a", "b"]),
            patch.object(
                run_daily.channel,
                "load",
                side_effect=lambda channel_id: SimpleNamespace(id=channel_id),
            ),
            patch.object(run_daily, "run", side_effect=RuntimeError("failed")),
        ):
            summary, exit_code = run_daily._run_all_channels(
                "2026-07-17", do_upload=False, video_scenes=0
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(summary["failed"], 2)

    def test_all_channels_treats_topic_cooldown_as_normal_skip(self) -> None:
        match = history.TopicMatch(
            topic="既存テーマ",
            ts="2026-07-01T00:00:00+00:00",
            similarity=0.9,
            source="公開済み",
        )
        skip = history.TopicCooldownSkip("重複テーマ", match, 30)
        with (
            patch.object(run_daily.channel, "discover", return_value=["alpha"]),
            patch.object(
                run_daily.channel,
                "load",
                return_value=SimpleNamespace(id="alpha"),
            ),
            patch.object(run_daily, "run", side_effect=skip),
        ):
            summary, exit_code = run_daily._run_all_channels(
                "2026-07-17", do_upload=True, video_scenes=0
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["channels"][0]["status"], "skipped")
        self.assertIn("過去30日以内", summary["channels"][0]["reason"])

    def test_run_cancels_active_reservation_when_production_fails(self) -> None:
        spec = SimpleNamespace(id="alpha")
        state = {
            "spec": spec,
            "corner": "video",
            "topic": "失敗した題材",
            "reservation_id": "reservation",
            "performance_spec": spec,
            "performance_corner": "video",
            "performance_decision_id": "decision",
            "performance_application_id": "application",
        }

        def fail_once(*args):
            args[-1].update(state)
            raise RuntimeError("tts failed")

        with (
            patch.object(run_daily, "_run_once", side_effect=fail_once),
            patch.object(run_daily.history, "cancel_topic") as cancel_mock,
            patch.object(
                run_daily.history,
                "cancel_performance_decision",
            ) as cancel_performance_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "tts failed"):
                run_daily.run(spec, "2026-07-17", "video", True, 0)

        cancel_mock.assert_called_once()
        self.assertEqual(cancel_mock.call_args.args[3], "reservation")
        cancel_performance_mock.assert_called_once()
        self.assertEqual(
            cancel_performance_mock.call_args.args[3],
            "application",
        )

    def test_run_keeps_queue_when_external_publish_already_succeeded(self) -> None:
        spec = SimpleNamespace(id="alpha")

        def fail_after_publish(*args):
            args[-1].update(
                {
                    "spec": spec,
                    "corner": "video",
                    "topic": "公開済み題材",
                    "reservation_id": "reservation",
                    "performance_spec": spec,
                    "performance_corner": "video",
                    "performance_decision_id": "decision",
                    "performance_application_id": "application",
                    "external_published": True,
                }
            )
            raise OSError("history write failed")

        with (
            patch.object(run_daily, "_run_once", side_effect=fail_after_publish),
            patch.object(run_daily.history, "cancel_topic") as cancel_mock,
            patch.object(
                run_daily.history,
                "cancel_performance_decision",
            ) as cancel_performance_mock,
        ):
            with self.assertRaisesRegex(OSError, "history write failed"):
                run_daily.run(spec, "2026-07-17", "video", True, 0)

        cancel_mock.assert_not_called()
        cancel_performance_mock.assert_not_called()

    def test_performance_publish_marks_external_before_history_write(self) -> None:
        spec = SimpleNamespace(id="alpha")
        state = {"performance_application_id": "application"}

        with (
            patch.object(
                run_daily.history,
                "apply_performance_decision",
                side_effect=OSError("history write failed"),
            ),
            patch.object(
                run_daily.history,
                "cancel_performance_decision",
            ) as cancel_mock,
        ):
            with self.assertRaisesRegex(OSError, "history write failed"):
                run_daily._finalize_performance_application(
                    spec,
                    "video",
                    "decision",
                    "application",
                    "youtube-video",
                    state,
                )

        self.assertTrue(state["external_published"])
        cancel_mock.assert_not_called()

    def test_list_channels_includes_last_run_and_isolates_bad_config(self) -> None:
        alpha = SimpleNamespace(id="alpha", name="Alpha")

        def fake_load(channel_id):
            if channel_id == "broken":
                raise ValueError("bad config")
            return alpha

        with (
            patch.object(
                run_daily.channel, "discover", return_value=["alpha", "broken"]
            ),
            patch.object(run_daily.channel, "load", side_effect=fake_load),
            patch.object(
                run_daily.history,
                "last_run",
                return_value={"ts": "2026-07-17T00:00:00Z", "title": "Latest"},
            ),
        ):
            rows = run_daily._list_channels()

        self.assertEqual(rows[0]["last_run"]["title"], "Latest")
        self.assertEqual(rows[1]["status"], "error")

    def test_main_list_channels_does_not_require_default_channel(self) -> None:
        with (
            patch("sys.argv", ["doci.run_daily", "--list-channels"]),
            patch.object(
                run_daily,
                "_list_channels",
                return_value=[{"channel": "alpha", "last_run": None}],
            ),
            patch.object(run_daily.channel, "default_channel") as default_mock,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            exit_code = run_daily.main()

        self.assertEqual(exit_code, 0)
        default_mock.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())[0]["channel"], "alpha")


if __name__ == "__main__":
    unittest.main()
