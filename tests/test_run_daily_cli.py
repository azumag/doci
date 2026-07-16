from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from doci import run_daily


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
