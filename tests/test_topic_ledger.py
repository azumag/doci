from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import config, history, topic_ledger


class TopicLedgerTest(unittest.TestCase):
    """topic_ledgerはpipeline.max_uploads_per_dayのJST日次実投稿枠と、外部投稿結果が
    確定するまでの安全な状態遷移(queued→publishing→published/cancelled)だけを扱う。
    題材内容そのものの跨ぎ照合は行わない(チャンネル間でテーマは十分に異なるため)。
    そちらはチャンネル別history.reserve_topic()が担う(tests/test_topic_cooldown.py、
    tests/test_semantic_topic_duplicate.py参照)。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        # 既定で余裕のある日次枠を持たせ、日次枠の上限そのものを検証するテストは
        # 個別にpipeline.max_uploads_per_dayを指定したSimpleNamespaceを使う。
        self.alpha = SimpleNamespace(id="alpha", pipeline={"max_uploads_per_day": 10})
        self.beta = SimpleNamespace(id="beta", pipeline={"max_uploads_per_day": 10})
        self.patcher_output = patch.object(config, "OUTPUT", self.output)
        self.patcher_output.start()
        self.addCleanup(self.patcher_output.stop)

    def test_no_daily_limit_short_circuits_without_touching_the_ledger(self) -> None:
        no_limit = SimpleNamespace(id="unlimited")
        result = topic_ledger.reserve(
            no_limit,
            "a",
            "枠設定が無いチャンネルの題材",
            now=self.now,
        )
        self.assertIsNone(result)
        self.assertFalse(topic_ledger.ledger_path().exists())

    def test_dry_run_only_checks_and_does_not_write_ledger(self) -> None:
        result = topic_ledger.reserve(
            self.alpha,
            "a",
            "新しい題材",
            reserve=False,
            now=self.now,
        )
        self.assertIsNone(result)
        self.assertFalse(topic_ledger.ledger_path().exists())

    def test_reserve_writes_a_queued_row_with_daily_metadata(self) -> None:
        reservation = topic_ledger.reserve(
            self.alpha,
            "a",
            "新しい題材",
            now=self.now,
        )
        self.assertIsNotNone(reservation)
        rows = [
            json.loads(line)
            for line in topic_ledger.ledger_path().read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[-1]["status"], "queued")
        self.assertEqual(rows[-1]["reservation_id"], reservation)
        self.assertEqual(rows[-1]["daily_upload_limit"], 10)
        self.assertEqual(rows[-1]["daily_upload_day"], "2026-08-01")

    def _prepare_publishing_reservation(
        self, topic: str
    ) -> tuple[SimpleNamespace, str, str]:
        local_spec = SimpleNamespace(
            id="alpha",
            history_file=self.output / "alpha" / "history.jsonl",
        )
        ledger_reservation = topic_ledger.reserve(
            self.alpha,
            "video",
            topic,
            now=self.now,
        )
        assert ledger_reservation is not None
        local_reservation = history.reserve_topic(
            local_spec,
            "video",
            topic,
            cooldown_days=30,
            now=self.now,
            topic_ledger_reservation_id=ledger_reservation,
        )
        assert local_reservation is not None
        history.mark_topic_publishing(
            local_spec,
            "video",
            topic,
            local_reservation,
            topic_ledger_reservation_id=ledger_reservation,
        )
        topic_ledger.mark_publishing(
            self.alpha,
            "video",
            topic,
            ledger_reservation,
        )
        return local_spec, ledger_reservation, local_reservation

    def test_recover_publishing_cancelled_updates_both_ledgers(self) -> None:
        topic = "外部未投稿を確認して再利用できる題材"
        local_spec, ledger_reservation, local_reservation = (
            self._prepare_publishing_reservation(topic)
        )

        with patch.object(
            topic_ledger.channel,
            "load",
            return_value=local_spec,
        ):
            result = topic_ledger.recover_publishing(
                ledger_reservation,
                status="cancelled",
                reason="YouTube Studioで投稿なしを確認",
            )

        self.assertTrue(result["local_history_recovered"])
        self.assertEqual(result["status"], "cancelled")
        ledger_rows = [
            json.loads(line)
            for line in topic_ledger.ledger_path().read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(ledger_rows[-1]["status"], "cancelled")
        local_rows = history._read_path(local_spec.history_file)
        self.assertEqual(local_rows[-1]["status"], "cancelled")
        self.assertEqual(local_rows[-1]["reservation_id"], local_reservation)
        self.assertEqual(
            local_rows[-1]["topic_ledger_reservation_id"], ledger_reservation
        )

    def test_recover_publishing_published_records_confirmed_video(self) -> None:
        topic = "外部投稿済みを確認して題材を確定する復旧"
        local_spec, ledger_reservation, _local_reservation = (
            self._prepare_publishing_reservation(topic)
        )

        with patch.object(
            topic_ledger.channel,
            "load",
            return_value=local_spec,
        ):
            result = topic_ledger.recover_publishing(
                ledger_reservation,
                status="published",
                video_id="confirmed-video",
                reason="YouTube Studioで投稿済みを確認",
            )

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["video_id"], "confirmed-video")
        ledger_rows = [
            json.loads(line)
            for line in topic_ledger.ledger_path().read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(ledger_rows[-1]["status"], "published")
        self.assertEqual(ledger_rows[-1]["video_id"], "confirmed-video")
        local_rows = history._read_path(local_spec.history_file)
        self.assertEqual(local_rows[-1]["status"], "published")
        self.assertEqual(local_rows[-1]["video_id"], "confirmed-video")

    def test_recover_publishing_is_idempotent_and_rejects_conflicts(self) -> None:
        topic = "同じ外部投稿確認を二重実行しない復旧"
        local_spec, ledger_reservation, _local_reservation = (
            self._prepare_publishing_reservation(topic)
        )

        with patch.object(topic_ledger.channel, "load", return_value=local_spec):
            first = topic_ledger.recover_publishing(
                ledger_reservation,
                status="published",
                video_id="confirmed-video",
            )
            rows_after_first = topic_ledger.ledger_path().read_text(
                encoding="utf-8"
            ).splitlines()
            second = topic_ledger.recover_publishing(
                ledger_reservation,
                status="published",
                video_id="confirmed-video",
            )

        self.assertEqual(first["status"], "published")
        self.assertTrue(second["idempotent"])
        self.assertEqual(
            len(topic_ledger.ledger_path().read_text(encoding="utf-8").splitlines()),
            len(rows_after_first),
        )
        with self.assertRaisesRegex(ValueError, "video_idが異なります"):
            topic_ledger.recover_publishing(
                ledger_reservation,
                status="published",
                video_id="different-video",
            )
        with self.assertRaises(ValueError):
            topic_ledger.recover_publishing(
                ledger_reservation,
                status="cancelled",
            )

    def test_shared_reservation_counts_once_against_daily_limit(self) -> None:
        limited = SimpleNamespace(
            id="limited-shared",
            history_file=self.output / "limited-shared" / "history.jsonl",
            pipeline={"max_uploads_per_day": 2},
        )
        first = topic_ledger.reserve(
            limited,
            "a",
            "共通IDで一つに数える投稿",
            now=self.now,
        )
        assert first is not None
        local_reservation = history.reserve_topic(
            limited,
            "a",
            "共通IDで一つに数える投稿",
            cooldown_days=30,
            now=self.now,
            topic_ledger_reservation_id=first,
        )
        self.assertIsNotNone(local_reservation)
        second = topic_ledger.reserve(
            limited,
            "b",
            "同日の二つ目の投稿",
            now=self.now + timedelta(minutes=1),
        )
        self.assertIsNotNone(second)
        with self.assertRaises(topic_ledger.DailyUploadLimitSkip):
            topic_ledger.ensure_daily_capacity(
                limited,
                now=self.now + timedelta(minutes=2),
            )

    def test_malformed_ledger_fails_closed(self) -> None:
        topic_ledger.ledger_path().write_text(
            '{"status":"queued"}\n{"status":"queued"',
            encoding="utf-8",
        )
        with self.assertRaises(topic_ledger.TopicLedgerCorruptError):
            topic_ledger.reserve(
                self.alpha,
                "a",
                "新しい題材",
                now=self.now,
            )

    def test_daily_upload_limit_is_atomic_and_resets_on_jst_next_day(self) -> None:
        limited_dir = self.output / "limited"
        limited = SimpleNamespace(
            id="limited",
            history_file=limited_dir / "history.jsonl",
            pipeline={"max_uploads_per_day": 1},
        )

        first = topic_ledger.reserve(
            limited,
            "a",
            "当日の一つ目の実投稿",
            now=self.now,
        )
        self.assertIsNotNone(first)

        with self.assertRaises(topic_ledger.DailyUploadLimitSkip):
            topic_ledger.ensure_daily_capacity(
                limited,
                now=self.now + timedelta(minutes=30),
            )

        with self.assertRaises(topic_ledger.DailyUploadLimitSkip):
            topic_ledger.reserve(
                limited,
                "b",
                "当日の二つ目の実投稿",
                now=self.now + timedelta(hours=1),
            )

        topic_ledger.cancel(
            limited,
            "a",
            "当日の一つ目の実投稿",
            first,
            "制作失敗",
            now=self.now + timedelta(hours=2),
        )
        next_day = topic_ledger.reserve(
            limited,
            "b",
            "翌日の実投稿",
            now=self.now + timedelta(days=1),
        )
        self.assertIsNotNone(next_day)

    def test_active_queue_crossing_jst_midnight_still_consumes_next_slot(self) -> None:
        limited = SimpleNamespace(
            id="boundary",
            history_file=self.output / "boundary" / "history.jsonl",
            pipeline={"max_uploads_per_day": 1},
        )
        before_midnight = datetime(2026, 8, 1, 14, 59, tzinfo=timezone.utc)
        first = topic_ledger.reserve(
            limited,
            "a",
            "日付変更前から継続する投稿",
            now=before_midnight,
        )
        assert first is not None

        after_midnight = datetime(2026, 8, 1, 15, 1, tzinfo=timezone.utc)
        with self.assertRaises(topic_ledger.DailyUploadLimitSkip):
            topic_ledger.ensure_daily_capacity(limited, now=after_midnight)

        topic_ledger.cancel(
            limited,
            "a",
            "日付変更前から継続する投稿",
            first,
            "制作失敗",
            now=after_midnight,
        )
        topic_ledger.ensure_daily_capacity(limited, now=after_midnight)

    def test_published_event_crossing_jst_midnight_consumes_publication_slot_day(self) -> None:
        limited = SimpleNamespace(
            id="terminal-boundary",
            history_file=self.output / "terminal-boundary" / "history.jsonl",
            pipeline={"max_uploads_per_day": 1},
        )
        before_midnight = datetime(2026, 8, 1, 14, 59, tzinfo=timezone.utc)
        after_midnight = datetime(2026, 8, 1, 15, 1, tzinfo=timezone.utc)
        reservation_id = "cross-day-reservation"
        topic_ledger.ledger_path().write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "ts": before_midnight.isoformat(),
                            "channel": limited.id,
                            "status": "queued",
                            "reservation_id": reservation_id,
                            "topic": "日付をまたいで確定する投稿",
                            "daily_upload_day": "2026-08-01",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "ts": after_midnight.isoformat(),
                            "channel": limited.id,
                            "status": "published",
                            "reservation_id": reservation_id,
                            "video_id": "cross-day-video",
                            "topic": "日付をまたいで確定する投稿",
                            "daily_upload_day": "2026-08-02",
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(topic_ledger.DailyUploadLimitSkip):
            topic_ledger.ensure_daily_capacity(limited, now=after_midnight)
        topic_ledger.ensure_daily_capacity(
            limited,
            now=after_midnight + timedelta(days=1),
        )


if __name__ == "__main__":
    unittest.main()
