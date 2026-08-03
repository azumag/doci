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

    def test_queued_experiment_without_video_id_never_raises(self) -> None:
        """投稿結果unknownで保留されたqueued行（video未確定）には、動画が
        存在せず保護対象が無いため評価期間ゲートを適用しない
        （復旧まで生成自体が恒久停止するのを防ぐ）。"""
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
        info = history.ensure_corner_eval_capacity(
            self.spec, "video", 72, now=self.now
        )
        self.assertTrue(info["active"])
        self.assertIsNone(info["video_id"])

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


class PerformanceApplicationRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            history_file=self.root / "history.jsonl",
        )

    def _queue(self, application_id: str = "app-1", corner: str = "video") -> None:
        self.spec.history_file.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": "2026-08-01T00:00:00+00:00",
            "channel": self.spec.id,
            "corner": corner,
            "video_id": None,
            "status": "performance_queued",
            "performance_decision_id": "dec-1",
            "performance_application_id": application_id,
        }
        with self.spec.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _rows(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.spec.history_file.read_text(encoding="utf-8").splitlines()
        ]

    def test_cancelled_recovery_frees_the_corner(self) -> None:
        self._queue()

        result = history.recover_performance_application(
            self.spec,
            "app-1",
            status="cancelled",
            reason="タイムアウト後にYouTube Studioで未投稿を確認",
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["idempotent"])
        self.assertIsNone(history.active_performance_experiment(self.spec, "video"))
        self.assertEqual(self._rows()[-1]["status"], "performance_cancelled")

    def test_published_recovery_records_confirmed_video(self) -> None:
        self._queue()

        result = history.recover_performance_application(
            self.spec,
            "app-1",
            status="published",
            video_id="confirmed-video",
        )

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["video_id"], "confirmed-video")
        rows = self._rows()
        self.assertEqual(rows[-1]["status"], "performance_applied")
        self.assertEqual(rows[-1]["video_id"], "confirmed-video")

    def test_recovery_is_idempotent_and_rejects_conflicting_video_id(self) -> None:
        self._queue()

        first = history.recover_performance_application(
            self.spec, "app-1", status="published", video_id="confirmed-video"
        )
        rows_after_first = self._rows()
        second = history.recover_performance_application(
            self.spec, "app-1", status="published", video_id="confirmed-video"
        )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(self._rows()), len(rows_after_first))
        with self.assertRaisesRegex(ValueError, "video_idが異なります"):
            history.recover_performance_application(
                self.spec, "app-1", status="published", video_id="different-video"
            )

    def test_recovery_rejects_already_cancelled_as_published(self) -> None:
        self._queue()
        history.recover_performance_application(self.spec, "app-1", status="cancelled")

        with self.assertRaises(ValueError):
            history.recover_performance_application(
                self.spec, "app-1", status="published", video_id="late-video"
            )

    def test_recovery_raises_for_unknown_application_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "見つかりません"):
            history.recover_performance_application(
                self.spec, "does-not-exist", status="cancelled"
            )

    def test_published_recovery_requires_video_id(self) -> None:
        self._queue()
        with self.assertRaisesRegex(ValueError, "video_idが必要"):
            history.recover_performance_application(
                self.spec, "app-1", status="published"
            )

    def test_cancelled_recovery_rejects_video_id(self) -> None:
        self._queue()
        with self.assertRaisesRegex(ValueError, "video_idは指定できません"):
            history.recover_performance_application(
                self.spec, "app-1", status="cancelled", video_id="unexpected"
            )


if __name__ == "__main__":
    unittest.main()
