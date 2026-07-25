from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from doci import history


class TopicCooldownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            history_file=self.root / "history.jsonl",
        )
        self.now = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)

    def _append(self, row: dict) -> None:
        self.spec.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.spec.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_rejects_substantially_similar_published_topic_and_records_reason(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(days=2)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "title": "クリック率10%でも失敗？",
                "video_id": "published-id",
                "status": "published",
                "topic": "CTRが高くても伸びない理由と冒頭30秒の設計",
            }
        )

        with self.assertRaises(history.TopicCooldownSkip) as raised:
            history.reserve_topic(
                self.spec,
                "analytics",
                "クリック率が高いのに伸びない理由と冒頭30秒の設計",
                cooldown_days=30,
                now=self.now,
            )

        self.assertIn("過去30日以内", raised.exception.reason)
        rows = [
            json.loads(line)
            for line in self.spec.history_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[-1]["status"], "skipped")
        self.assertEqual(rows[-1]["skip_reason"], raised.exception.reason)
        self.assertGreaterEqual(rows[-1]["similarity"], 0.55)

    def test_legacy_history_uses_research_topic_from_script(self) -> None:
        workdir = self.root / "old-run"
        workdir.mkdir()
        (workdir / "script.json").write_text(
            json.dumps(
                {
                    "_research": {
                        "topic": "タイトルとサムネイルの約束を冒頭30秒で回収する構成設計"
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._append(
            {
                "ts": (self.now - timedelta(days=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "title": "公式が明かす冒頭30秒の罠",
                "video_id": "legacy-id",
                "workdir": str(workdir),
            }
        )

        with self.assertRaises(history.TopicCooldownSkip):
            history.reserve_topic(
                self.spec,
                "video",
                "タイトルとサムネイルの約束を冒頭30秒で回収する構成設計",
                cooldown_days=30,
                reserve=False,
                now=self.now,
            )

        # dry-run照合は履歴を変更しない。
        self.assertEqual(
            len(self.spec.history_file.read_text(encoding="utf-8").splitlines()), 1
        )

    def test_allows_distinct_topic_and_atomically_queues_it(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(days=3)).isoformat(),
                "channel": "youtube-growth",
                "corner": "analytics",
                "title": "冒頭30秒の設計",
                "video_id": "published-id",
                "status": "published",
                "topic": "CTRが高くても伸びない理由と冒頭30秒の設計",
            }
        )

        reservation = history.reserve_topic(
            self.spec,
            "shorts",
            "ショートから通常動画へ送る関連動画リンクの使い方",
            cooldown_days=30,
            now=self.now,
        )

        self.assertIsNotNone(reservation)
        queued = json.loads(
            self.spec.history_file.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["reservation_id"], reservation)

        with self.assertRaises(history.TopicCooldownSkip) as raised:
            history.reserve_topic(
                self.spec,
                "video",
                "ショートから通常動画へ送る関連動画リンクの使い方",
                cooldown_days=30,
                now=self.now + timedelta(minutes=1),
            )
        self.assertEqual(raised.exception.match.source, "キュー済み")

    def test_allows_same_topic_after_configured_window(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(days=31)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "title": "古い動画",
                "video_id": "old-id",
                "status": "published",
                "topic": "一か月前の題材",
            }
        )

        reservation = history.reserve_topic(
            self.spec,
            "video",
            "一か月前の題材",
            cooldown_days=30,
            now=self.now,
        )

        self.assertIsNotNone(reservation)

    def test_cancelled_or_generated_reservation_no_longer_blocks_topic(self) -> None:
        for final_status in ("cancelled", "generated"):
            with self.subTest(final_status=final_status):
                self.spec.history_file.unlink(missing_ok=True)
                reservation = history.reserve_topic(
                    self.spec,
                    "video",
                    "同じ題材",
                    cooldown_days=30,
                    now=self.now,
                )
                self._append(
                    {
                        "ts": (self.now + timedelta(minutes=1)).isoformat(),
                        "channel": "youtube-growth",
                        "corner": "video",
                        "title": "生成済み" if final_status == "generated" else "",
                        "video_id": None,
                        "status": final_status,
                        "topic": "同じ題材",
                        "reservation_id": reservation,
                    }
                )

                next_reservation = history.reserve_topic(
                    self.spec,
                    "video",
                    "同じ題材",
                    cooldown_days=30,
                    now=self.now + timedelta(minutes=2),
                )
                self.assertIsNotNone(next_reservation)

    def test_published_reservation_continues_to_block_topic(self) -> None:
        reservation = history.reserve_topic(
            self.spec,
            "video",
            "公開する題材",
            cooldown_days=30,
            now=self.now,
        )
        self._append(
            {
                "ts": (self.now + timedelta(minutes=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "title": "公開動画",
                "video_id": "video-id",
                "status": "published",
                "topic": "公開する題材",
                "reservation_id": reservation,
            }
        )

        with self.assertRaises(history.TopicCooldownSkip):
            history.reserve_topic(
                self.spec,
                "video",
                "公開する題材",
                cooldown_days=30,
                now=self.now + timedelta(minutes=2),
            )

    def test_semantic_aliases_and_boilerplate_avoid_known_false_results(self) -> None:
        self.assertGreaterEqual(
            history.topic_similarity(
                "視聴維持率を上げる",
                "冒頭30秒を改善して平均視聴時間を伸ばす",
            ),
            0.55,
        )
        self.assertLess(
            history.topic_similarity(
                "初心者向けYouTubeサムネイル",
                "初心者向けYouTubeアナリティクス",
            ),
            0.55,
        )
        self.assertLess(
            history.topic_similarity(
                "サムネイルの文字を読みやすくする配色",
                "サムネイルに人物写真を使う効果",
            ),
            0.55,
        )
        self.assertLess(
            history.topic_similarity(
                "登録者への通知",
                "登録者が解除する理由",
            ),
            0.55,
        )

    def test_rotation_ignores_queue_skip_and_cancel_events(self) -> None:
        self._append(
            {
                "ts": self.now.isoformat(),
                "channel": "youtube-growth",
                "corner": "shorts",
                "title": "完成",
                "status": "published",
                "video_id": "id",
            }
        )
        for index, status in enumerate(("queued", "skipped", "cancelled"), start=1):
            self._append(
                {
                    "ts": (self.now + timedelta(minutes=index)).isoformat(),
                    "channel": "youtube-growth",
                    "corner": "analytics",
                    "title": "",
                    "status": status,
                    "topic": "未完了",
                }
            )

        self.assertEqual(history.last_corner(self.spec), "shorts")
        self.assertEqual(history.last_run(self.spec)["title"], "完成")


if __name__ == "__main__":
    unittest.main()
