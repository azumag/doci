from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import config, history, output_cleanup, topic_ledger


class TopicLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.alpha = SimpleNamespace(id="alpha")
        self.beta = SimpleNamespace(id="beta")
        self.patcher_output = patch.object(config, "OUTPUT", self.output)
        self.patcher_output.start()
        self.addCleanup(self.patcher_output.stop)
        self.patcher_channels = patch.object(
            topic_ledger.channel,
            "discover",
            return_value=[],
        )
        self.patcher_channels.start()
        self.addCleanup(self.patcher_channels.stop)

    def test_cross_channel_reservation_blocks_second_channel(self) -> None:
        first = topic_ledger.reserve(
            self.alpha,
            "a",
            "視聴維持率を改善する冒頭設計",
            cooldown_days=30,
            now=self.now,
        )
        self.assertIsNotNone(first)

        with self.assertRaises(history.TopicCooldownSkip) as raised:
            topic_ledger.reserve(
                self.beta,
                "b",
                "冒頭設計で視聴維持率を改善する方法",
                cooldown_days=30,
                now=self.now + timedelta(minutes=1),
            )

        self.assertIn("共通台帳(alpha/キュー済み)", raised.exception.reason)
        rows = [
            json.loads(line)
            for line in topic_ledger.ledger_path().read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([row["status"] for row in rows], ["queued", "skipped"])

    def test_semantic_check_receives_candidates_from_all_channels(self) -> None:
        first = topic_ledger.reserve(
            self.alpha,
            "a",
            "江戸幕府が鎖国を選んだ外交事情",
            cooldown_days=30,
            now=self.now,
        )
        self.assertIsNotNone(first)
        calls: list[list[str]] = []

        def semantic_check(
            _topic: str,
            candidates: list[str],
        ) -> history.TopicMatch | None:
            calls.append(candidates)
            return history.TopicMatch(
                topic="江戸幕府が鎖国を選んだ外交事情",
                ts="",
                similarity=0.91,
                source="LLM判定",
            )

        with self.assertRaises(history.TopicCooldownSkip) as raised:
            topic_ledger.reserve(
                self.beta,
                "b",
                "徳川政権が海外との接触を制限した背景",
                cooldown_days=30,
                semantic_check=semantic_check,
                now=self.now + timedelta(minutes=1),
            )

        self.assertEqual(calls, [["江戸幕府が鎖国を選んだ外交事情"]])
        self.assertEqual(raised.exception.match.source, "LLM判定")

    def test_semantic_rechecks_after_a_concurrent_reservation(self) -> None:
        calls: list[list[str]] = []

        def semantic_check(
            _topic: str,
            candidates: list[str],
        ) -> history.TopicMatch | None:
            calls.append(candidates)
            if len(calls) == 1:
                inner = topic_ledger.reserve(
                    self.alpha,
                    "a",
                    "意味的に重なる並行予約",
                    cooldown_days=30,
                    now=self.now + timedelta(minutes=1),
                )
                self.assertIsNotNone(inner)
                return None
            return history.TopicMatch(
                topic="意味的に重なる並行予約",
                ts="",
                similarity=0.94,
                source="LLM判定",
            )

        with self.assertRaises(history.TopicCooldownSkip):
            topic_ledger.reserve(
                self.beta,
                "b",
                "表現を変えた並行予約",
                cooldown_days=30,
                semantic_check=semantic_check,
                now=self.now,
            )

        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("意味的に重なる並行予約", calls[-1])

    def test_daily_limit_rechecks_after_a_concurrent_reservation(self) -> None:
        limited = SimpleNamespace(
            id="limited-race",
            history_file=self.output / "limited-race" / "history.jsonl",
            pipeline={"max_uploads_per_day": 1},
        )
        calls = 0

        def semantic_check(
            _topic: str,
            _candidates: list[str],
        ) -> history.TopicMatch | None:
            nonlocal calls
            calls += 1
            inner = topic_ledger.reserve(
                limited,
                "a",
                "先に入った並行投稿",
                cooldown_days=30,
                now=self.now + timedelta(minutes=1),
            )
            self.assertIsNotNone(inner)
            return None

        with self.assertRaises(topic_ledger.DailyUploadLimitSkip):
            topic_ledger.reserve(
                limited,
                "b",
                "後から入る投稿",
                cooldown_days=30,
                semantic_check=semantic_check,
                now=self.now,
            )
        self.assertEqual(calls, 1)

    def test_legacy_channel_history_is_read_without_rewriting_it(self) -> None:
        legacy_path = self.output / "beta" / "history.jsonl"
        legacy_path.parent.mkdir()
        legacy_path.write_text(
            json.dumps(
                {
                    "ts": (self.now - timedelta(days=2)).isoformat(),
                    "channel": "beta",
                    "corner": "b",
                    "status": "published",
                    "video_id": "legacy-video",
                    "topic": "クリック率と冒頭離脱を同時に改善する方法",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        legacy_spec = SimpleNamespace(id="beta", history_file=legacy_path)
        with (
            patch.object(topic_ledger.channel, "discover", return_value=["beta"]),
            patch.object(topic_ledger.channel, "load", return_value=legacy_spec),
        ):
            with self.assertRaises(history.TopicCooldownSkip) as raised:
                topic_ledger.reserve(
                    self.alpha,
                    "a",
                    "冒頭離脱を減らしてクリック率を改善する方法",
                    cooldown_days=30,
                    now=self.now,
                )

        self.assertIn("既存履歴(beta/公開済み)", raised.exception.reason)
        self.assertEqual(len(legacy_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_explicit_opposing_view_allows_new_angle(self) -> None:
        original = "計画経済が物不足を生んだ理由"
        original_metadata = {
            "angle": "制度設計の失敗から不足が生まれた過程",
            "audience": "歴史に関心のある視聴者",
            "format": "制度",
            "source": "research",
            "comparison_key": "制度設計の制約",
        }
        reservation = topic_ledger.reserve(
            self.alpha,
            "a",
            original,
            cooldown_days=30,
            metadata=original_metadata,
            now=self.now,
        )
        assert reservation is not None
        topic_ledger.complete(
            self.alpha,
            "a",
            original,
            reservation,
            status="published",
            metadata=original_metadata,
            video_id="video-1",
            now=self.now + timedelta(seconds=30),
        )

        continuation_metadata = {
            **original_metadata,
            "novelty_type": "opposing_view",
            "parent_topic": original,
            "novelty_reason": "同じ制度を支持者側の合理性から検証する",
            "angle": "不足を生んだ制約ではなく配分の優先順位を比較する",
            "novelty_axis": "stance",
            "viewpoint": "制度を支持する側の合理性",
            "comparison_key": "制度を支持する側の合理性",
        }
        continuation = topic_ledger.reserve(
            self.beta,
            "b",
            original,
            cooldown_days=30,
            metadata=continuation_metadata,
            semantic_check=lambda topic, recent: history.TopicMatch(
                topic=recent[0], ts="", similarity=0.91, source="LLM判定"
            ),
            now=self.now + timedelta(minutes=1),
        )
        self.assertIsNotNone(continuation)
        self.assertEqual(continuation_metadata["parent_topic_id"], "video-1")

    def test_same_angle_cannot_claim_to_be_a_sequel(self) -> None:
        original = "冒頭離脱を減らす構成設計"
        metadata = {
            "angle": "視聴維持率の冒頭グラフから離脱点を特定する",
            "viewpoint": "視聴者行動を診断する立場",
            "comparison_key": "冒頭グラフの離脱点",
        }
        reservation = topic_ledger.reserve(
            self.alpha,
            "a",
            original,
            cooldown_days=30,
            metadata=metadata,
            now=self.now,
        )
        assert reservation is not None
        topic_ledger.complete(
            self.alpha,
            "a",
            original,
            reservation,
            status="published",
            metadata=metadata,
            video_id="video-1",
            now=self.now + timedelta(seconds=30),
        )

        with self.assertRaises(history.TopicCooldownSkip):
            topic_ledger.reserve(
                self.beta,
                "b",
                original,
                cooldown_days=30,
                metadata={
                    **metadata,
                    "novelty_type": "sequel",
                    "parent_topic": original,
                    "novelty_reason": "続編としてもう一度説明する",
                    "novelty_axis": "case",
                    "comparison_key": "次の動画の冒頭グラフ",
                },
                now=self.now + timedelta(minutes=1),
        )

    def test_dry_run_only_checks_and_does_not_write_ledger(self) -> None:
        result = topic_ledger.reserve(
            self.alpha,
            "a",
            "新しい題材",
            cooldown_days=30,
            reserve=False,
            now=self.now,
        )
        self.assertIsNone(result)
        self.assertFalse(topic_ledger.ledger_path().exists())

    def test_cancelled_reservation_is_removed_from_active_candidates(self) -> None:
        reservation = topic_ledger.reserve(
            self.alpha,
            "a",
            "取消後に再利用できる題材",
            cooldown_days=30,
            now=self.now,
        )
        assert reservation is not None
        topic_ledger.cancel(
            self.alpha,
            "a",
            "取消後に再利用できる題材",
            reservation,
            "制作失敗",
            metadata={"canonical_theme": "具体的なテーマ"},
            now=self.now + timedelta(seconds=30),
        )
        rows = [
            json.loads(line)
            for line in topic_ledger.ledger_path().read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[-1]["status"], "cancelled")
        self.assertEqual(rows[-1]["cancel_reason"], "制作失敗")
        next_reservation = topic_ledger.reserve(
            self.beta,
            "b",
            "取消後に再利用できる題材",
            cooldown_days=30,
            now=self.now + timedelta(minutes=1),
        )
        self.assertIsNotNone(next_reservation)

    def test_queued_parent_cannot_be_bypassed_as_a_continuation(self) -> None:
        parent = "配給制度が物不足を生んだ理由"
        parent_metadata = {
            "canonical_theme": "配給制度と物不足",
            "angle": "制度設計の失敗から不足が生まれた過程",
            "viewpoint": "制度設計を批判的に検証する立場",
            "comparison_key": "制度設計の制約",
        }
        reservation = topic_ledger.reserve(
            self.alpha,
            "a",
            parent,
            cooldown_days=30,
            metadata=parent_metadata,
            now=self.now,
        )
        assert reservation is not None
        with self.assertRaises(history.TopicCooldownSkip):
            topic_ledger.reserve(
                self.beta,
                "b",
                parent,
                cooldown_days=30,
                metadata={
                    **parent_metadata,
                    "novelty_type": "opposing_view",
                    "parent_topic": parent,
                    "novelty_reason": "同じ制度を支持者側の合理性から検証する",
                    "angle": "不足ではなく配分の優先順位を比較する",
                "novelty_axis": "stance",
                "viewpoint": "制度を支持する側の合理性",
                "comparison_key": "制度を支持する側の合理性",
                },
                now=self.now + timedelta(minutes=1),
            )

    def test_publishing_reservation_remains_fail_closed_after_owner_exits(self) -> None:
        topic = "投稿結果不明でも再利用させない題材"
        reservation = topic_ledger.reserve(
            self.alpha,
            "a",
            topic,
            cooldown_days=30,
            now=self.now,
        )
        assert reservation is not None
        topic_ledger.mark_publishing(
            self.alpha,
            "a",
            topic,
            reservation,
        )
        with self.assertRaises(history.TopicCooldownSkip):
            topic_ledger.reserve(
                self.beta,
                "b",
                topic,
                cooldown_days=30,
                now=self.now + timedelta(hours=25),
            )

    def test_old_publishing_reservation_remains_fail_closed(self) -> None:
        topic = "31日以上経過しても投稿結果不明の題材"
        reservation = topic_ledger.reserve(
            self.alpha,
            "a",
            topic,
            cooldown_days=30,
            now=self.now,
        )
        assert reservation is not None
        topic_ledger.mark_publishing(self.alpha, "a", topic, reservation)
        with self.assertRaises(history.TopicCooldownSkip):
            topic_ledger.reserve(
                self.beta,
                "b",
                topic,
                cooldown_days=30,
                now=self.now + timedelta(days=31),
            )

    def _prepare_publishing_reservation(
        self, topic: str
    ) -> tuple[SimpleNamespace, str, str]:
        local_spec = SimpleNamespace(
            id="alpha",
            output_dir=self.output / "alpha",
            history_file=self.output / "alpha" / "history.jsonl",
        )
        ledger_reservation = topic_ledger.reserve(
            self.alpha,
            "video",
            topic,
            cooldown_days=30,
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
        workdir = local_spec.output_dir / "2026-08-02_video_120000"
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "script.json").write_text(
            json.dumps(
                {
                    "title": "復旧対象",
                    "description": "description",
                    "tags": [],
                    "narration": "復元できるナレーション",
                    "scenes": [{"visual_prompt": "new image"}],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (workdir / "video.mp4").write_bytes(b"video")
        history.mark_topic_publishing(
            local_spec,
            "video",
            topic,
            local_reservation,
            topic_ledger_reservation_id=ledger_reservation,
            workdir=workdir,
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
        workdir = local_spec.output_dir / "2026-08-02_video_120000"

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
        self.assertEqual(local_rows[-1]["workdir"], str(workdir))
        cleanup = output_cleanup.cleanup_uploaded_outputs(local_spec, apply=True)
        self.assertEqual(cleanup["workdirs"], 1)
        self.assertFalse((workdir / "video.mp4").exists())
        self.assertTrue((workdir / "script.json").exists())

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

    def test_invalid_terminal_row_does_not_hide_publishing(self) -> None:
        topic_ledger.ledger_path().write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "ts": self.now.isoformat(),
                            "channel": "alpha",
                            "corner": "a",
                            "topic": "時刻不正な終端行の題材",
                            "status": "publishing",
                            "reservation_id": "publishing-id",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "ts": "not-a-timestamp",
                            "channel": "alpha",
                            "corner": "a",
                            "topic": "時刻不正な終端行の題材",
                            "status": "cancelled",
                            "reservation_id": "publishing-id",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "ts": (self.now + timedelta(days=32)).isoformat(),
                            "channel": "alpha",
                            "corner": "a",
                            "topic": "時刻不正な終端行の題材",
                            "status": "cancelled",
                            "reservation_id": "publishing-id",
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(history.TopicCooldownSkip):
            topic_ledger.reserve(
                self.beta,
                "b",
                "時刻不正な終端行の題材",
                cooldown_days=30,
                now=self.now + timedelta(days=31),
            )

    def test_future_publishing_row_remains_fail_closed(self) -> None:
        topic_ledger.ledger_path().write_text(
            json.dumps(
                {
                    "ts": (self.now + timedelta(hours=1)).isoformat(),
                    "channel": "alpha",
                    "corner": "a",
                    "topic": "未来時刻でも投稿中の題材",
                    "status": "publishing",
                    "reservation_id": "future-publishing",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(history.TopicCooldownSkip):
            topic_ledger.reserve(
                self.beta,
                "b",
                "未来時刻でも投稿中の題材",
                cooldown_days=30,
                now=self.now,
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
            cooldown_days=0,
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
            cooldown_days=0,
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
                cooldown_days=30,
                now=self.now,
            )

    def test_stale_queued_reservation_does_not_block_after_lease(self) -> None:
        topic_ledger.ledger_path().write_text(
            json.dumps(
                {
                    "ts": (self.now - timedelta(hours=25)).isoformat(),
                    "channel": "alpha",
                    "corner": "a",
                    "topic": "期限切れ予約の題材",
                    "status": "queued",
                    "reservation_id": "stale",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        reservation = topic_ledger.reserve(
            self.beta,
            "b",
            "期限切れ予約の題材",
            cooldown_days=30,
            now=self.now,
        )
        self.assertIsNotNone(reservation)

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
            cooldown_days=0,
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
                cooldown_days=0,
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
            cooldown_days=0,
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
            cooldown_days=0,
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
