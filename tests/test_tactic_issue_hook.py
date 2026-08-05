"""issue #90: 投稿完了直後のviewer_action検知フックのテスト。

対象: run_daily._apply_tactic_issue_detection。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from doci import run_daily, tactic_issues


def _spec(pipeline: dict) -> SimpleNamespace:
    spec = SimpleNamespace(id="youtube-growth", pipeline=pipeline)
    spec.pipeline_get = lambda key, default=None: spec.pipeline.get(key, default)
    return spec


class ApplyTacticIssueDetectionTest(unittest.TestCase):
    def test_flag_off_does_nothing(self) -> None:
        spec = _spec({})
        with mock.patch.object(tactic_issues, "run") as run_mock:
            run_daily._apply_tactic_issue_detection(spec, "vid123")
        run_mock.assert_not_called()

    def test_no_video_id_does_nothing(self) -> None:
        spec = _spec({"tactic_issues": True})
        with mock.patch.object(tactic_issues, "run") as run_mock:
            run_daily._apply_tactic_issue_detection(spec, "")
        run_mock.assert_not_called()

    def test_flag_on_calls_tactic_issues_run(self) -> None:
        spec = _spec({"tactic_issues": True})
        with mock.patch.object(
            tactic_issues,
            "run",
            return_value={"created": [{"issue": {"number": 1}}], "skipped": []},
        ) as run_mock:
            run_daily._apply_tactic_issue_detection(spec, "vid123")
        run_mock.assert_called_once_with(spec, apply=True)

    def test_failure_is_swallowed(self) -> None:
        spec = _spec({"tactic_issues": True})
        with (
            mock.patch.object(
                tactic_issues, "run", side_effect=RuntimeError("gh unavailable")
            ),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_tactic_issue_detection(spec, "vid123")
        self.assertTrue(
            any("施策issue作成失敗" in call.args[0] for call in log_mock.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
