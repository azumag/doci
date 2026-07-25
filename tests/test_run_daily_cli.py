from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from doci import history, run_daily


class RunDailyCliTest(unittest.TestCase):
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
        }

        def fail_once(*args):
            args[-1].update(state)
            raise RuntimeError("tts failed")

        with (
            patch.object(run_daily, "_run_once", side_effect=fail_once),
            patch.object(run_daily.history, "cancel_topic") as cancel_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "tts failed"):
                run_daily.run(spec, "2026-07-17", "video", True, 0)

        cancel_mock.assert_called_once()
        self.assertEqual(cancel_mock.call_args.args[3], "reservation")

    def test_run_keeps_queue_when_external_publish_already_succeeded(self) -> None:
        spec = SimpleNamespace(id="alpha")

        def fail_after_publish(*args):
            args[-1].update(
                {
                    "spec": spec,
                    "corner": "video",
                    "topic": "公開済み題材",
                    "reservation_id": "reservation",
                    "external_published": True,
                }
            )
            raise OSError("history write failed")

        with (
            patch.object(run_daily, "_run_once", side_effect=fail_after_publish),
            patch.object(run_daily.history, "cancel_topic") as cancel_mock,
        ):
            with self.assertRaisesRegex(OSError, "history write failed"):
                run_daily.run(spec, "2026-07-17", "video", True, 0)

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
