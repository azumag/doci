from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from doci import history


class PerformanceEvalWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            history_file=self.root / "history.jsonl",
        )
        self.now = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)

    def _append(self, row: dict) -> None:
        self.spec.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.spec.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_no_history_never_raises(self) -> None:
        info = history.ensure_corner_eval_capacity(
            self.spec, "video", 72, now=self.now
        )
        self.assertFalse(info["active"])
        self.assertIsNone(info["elapsed_hours"])

    def test_active_experiment_within_window_raises(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(hours=10)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": "id-1",
                "status": "performance_applied",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        with self.assertRaises(history.PerformanceEvalWindowSkip) as raised:
            history.ensure_corner_eval_capacity(
                self.spec, "video", 72, now=self.now
            )
        exc = raised.exception
        self.assertEqual(exc.corner, "video")
        self.assertEqual(exc.application_id, "app-1")
        self.assertEqual(exc.video_id, "id-1")
        self.assertAlmostEqual(exc.elapsed_hours, 10.0, places=3)
        self.assertIn("評価期間72時間内", exc.reason)

    def test_active_experiment_outside_window_does_not_raise(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(hours=100)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": "id-1",
                "status": "performance_applied",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        info = history.ensure_corner_eval_capacity(
            self.spec, "video", 72, now=self.now
        )
        self.assertTrue(info["active"])
        self.assertAlmostEqual(info["elapsed_hours"], 100.0, places=3)

    def test_cancelled_experiment_is_not_active(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(hours=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": None,
                "status": "performance_queued",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        self._append(
            {
                "ts": (self.now - timedelta(minutes=30)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": "",
                "status": "performance_cancelled",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        info = history.ensure_corner_eval_capacity(
            self.spec, "video", 72, now=self.now
        )
        self.assertFalse(info["active"])

    def test_evaluated_experiment_is_not_active(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(hours=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": "id-1",
                "status": "performance_applied",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        self._append(
            {
                "ts": (self.now - timedelta(minutes=30)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": "id-1",
                "status": "performance_evaluated",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        info = history.ensure_corner_eval_capacity(
            self.spec, "video", 72, now=self.now
        )
        self.assertFalse(info["active"])

    def test_broken_ts_on_active_row_never_raises(self) -> None:
        self._append(
            {
                "ts": "not-a-timestamp",
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": "id-1",
                "status": "performance_applied",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        info = history.ensure_corner_eval_capacity(
            self.spec, "video", 72, now=self.now
        )
        self.assertTrue(info["active"])
        self.assertIsNone(info["elapsed_hours"])

    def test_future_ts_fails_closed(self) -> None:
        self._append(
            {
                "ts": (self.now + timedelta(hours=5)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "video_id": "id-1",
                "status": "performance_applied",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        with self.assertRaises(history.PerformanceEvalWindowSkip) as raised:
            history.ensure_corner_eval_capacity(
                self.spec, "video", 72, now=self.now
            )
        self.assertLess(raised.exception.elapsed_hours, 0)

    def test_different_corner_is_unaffected(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(hours=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "shorts",
                "video_id": "id-1",
                "status": "performance_applied",
                "performance_decision_id": "dec-1",
                "performance_application_id": "app-1",
            }
        )
        info = history.ensure_corner_eval_capacity(
            self.spec, "video", 72, now=self.now
        )
        self.assertFalse(info["active"])

    def test_experiment_elapsed_hours_parses_z_suffix(self) -> None:
        row = {"ts": (self.now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")}
        elapsed = history.experiment_elapsed_hours(row, now=self.now)
        self.assertAlmostEqual(elapsed, 3.0, places=3)

    def test_experiment_elapsed_hours_returns_none_for_garbage(self) -> None:
        self.assertIsNone(
            history.experiment_elapsed_hours({"ts": "garbage"}, now=self.now)
        )
        self.assertIsNone(history.experiment_elapsed_hours({}, now=self.now))


if __name__ == "__main__":
    unittest.main()
