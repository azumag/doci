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

    def test_explicit_opposing_view_allows_new_angle_in_channel_history(self) -> None:
        original = "計画経済が物不足を生んだ理由"
        original_metadata = {
            "canonical_theme": "計画経済の配分制度と物不足",
            "angle": "制度設計の失敗から不足が生まれた過程",
            "audience": "歴史に関心のある視聴者",
            "viewpoint": "制度設計を批判的に検証する立場",
            "comparison_key": "制度設計の制約",
        }
        reservation = history.reserve_topic(
            self.spec,
            "video",
            original,
            cooldown_days=30,
            metadata=original_metadata,
            now=self.now,
        )
        assert reservation is not None
        self._append(
            {
                "ts": (self.now + timedelta(minutes=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "status": "published",
                "video_id": "video-id",
                "topic": original,
                "reservation_id": reservation,
                "topic_metadata": original_metadata,
            }
        )

        continuation = history.reserve_topic(
            self.spec,
            "analytics",
            original,
            cooldown_days=30,
            metadata={
                **original_metadata,
                "novelty_type": "opposing_view",
                "parent_topic": original,
                "novelty_reason": "同じ制度を支持者側の合理性から検証する",
                "angle": "不足を生んだ制約ではなく配分の優先順位を比較する",
                "novelty_axis": "stance",
                "viewpoint": "制度を支持する側の合理性",
                "comparison_key": "制度を支持する側の合理性",
            },
            now=self.now + timedelta(minutes=2),
        )
        self.assertIsNotNone(continuation)

    def test_generic_canonical_theme_does_not_block_distinct_topic(self) -> None:
        first = history.reserve_topic(
            self.spec,
            "video",
            "冒頭離脱を減らす構成設計",
            cooldown_days=30,
            metadata={
                "canonical_theme": "YouTubeチャンネル運用の改善",
                "angle": "冒頭30秒の離脱点を確認する",
            },
            now=self.now,
        )
        assert first is not None
        self._append(
            {
                "ts": (self.now + timedelta(minutes=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "status": "published",
                "video_id": "video-id",
                "topic": "冒頭離脱を減らす構成設計",
                "reservation_id": first,
                "topic_metadata": {
                    "canonical_theme": "YouTubeチャンネル運用の改善",
                    "angle": "冒頭30秒の離脱点を確認する",
                },
            }
        )

        second = history.reserve_topic(
            self.spec,
            "analytics",
            "サムネイルの文字を読みやすくする配色",
            cooldown_days=30,
            metadata={"canonical_theme": "YouTubeチャンネル運用の改善"},
            now=self.now + timedelta(minutes=2),
        )
        self.assertIsNotNone(second)

    def test_specific_canonical_theme_does_not_block_unrelated_topic_alone(self) -> None:
        first = history.reserve_topic(
            self.spec,
            "video",
            "江戸幕府が鎖国を選んだ外交事情",
            cooldown_days=30,
            metadata={
                "canonical_theme": "江戸幕府の外交制限と海上交易",
                "angle": "外交判断の背景を一次資料でたどる",
            },
            now=self.now,
        )
        assert first is not None
        self._append(
            {
                "ts": (self.now + timedelta(minutes=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "status": "published",
                "video_id": "video-id",
                "topic": "江戸幕府が鎖国を選んだ外交事情",
                "reservation_id": first,
                "topic_metadata": {
                    "canonical_theme": "江戸幕府の外交制限と海上交易",
                    "angle": "外交判断の背景を一次資料でたどる",
                },
            }
        )

        second = history.reserve_topic(
            self.spec,
            "analytics",
            "明治政府が鉄道敷設を急いだ財政事情",
            cooldown_days=30,
            metadata={
                "canonical_theme": "江戸幕府の外交制限と海上交易",
                "angle": "公債発行と都市交通の整備を比較する",
            },
            now=self.now + timedelta(minutes=2),
        )
        self.assertIsNotNone(second)

    def test_stale_channel_reservation_does_not_block_after_lease(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(hours=25)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "status": "queued",
                "topic": "期限切れ予約の題材",
                "reservation_id": "stale",
            }
        )
        reservation = history.reserve_topic(
            self.spec,
            "analytics",
            "期限切れ予約の題材",
            cooldown_days=30,
            now=self.now,
        )
        self.assertIsNotNone(reservation)

    def test_generic_angle_does_not_block_distinct_topics(self) -> None:
        first = history.reserve_topic(
            self.spec,
            "video",
            "企業が市場から撤退した理由",
            cooldown_days=30,
            metadata={
                "canonical_theme": "経済",
                "angle": "成功の秘訣を解説する",
            },
            now=self.now,
        )
        assert first is not None
        self._append(
            {
                "ts": (self.now + timedelta(minutes=1)).isoformat(),
                "channel": "youtube-growth",
                "corner": "video",
                "status": "published",
                "video_id": "video-id",
                "topic": "企業が市場から撤退した理由",
                "reservation_id": first,
                "topic_metadata": {
                    "canonical_theme": "経済",
                    "angle": "成功の秘訣を解説する",
                },
            }
        )
        second = history.reserve_topic(
            self.spec,
            "analytics",
            "国家が価格統制を導入した背景",
            cooldown_days=30,
            metadata={
                "canonical_theme": "経済",
                "angle": "成功の秘訣を解説する",
            },
            now=self.now + timedelta(minutes=2),
        )
        self.assertIsNotNone(second)

    def test_publishing_channel_reservation_remains_fail_closed(self) -> None:
        topic = "投稿結果不明でも再利用させない題材"
        reservation = history.reserve_topic(
            self.spec,
            "video",
            topic,
            cooldown_days=30,
            now=self.now,
        )
        assert reservation is not None
        history.mark_topic_publishing(
            self.spec,
            "video",
            topic,
            reservation,
        )
        with self.assertRaises(history.TopicCooldownSkip):
            history.reserve_topic(
                self.spec,
                "analytics",
                topic,
                cooldown_days=30,
                now=datetime.now(timezone.utc) + timedelta(days=31),
            )

    def test_semantic_aliases_and_boilerplate_avoid_known_false_results(self) -> None:
        self.assertGreaterEqual(
            history.topic_similarity(
                "ソ連の配給制度と食料不足",
                "計画経済における日用品の供給不足",
            ),
            0.55,
        )
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
        self.assertLess(
            history.topic_similarity(
                "日本の教育格差はなぜ広がるのか",
                "アメリカの男女賃金格差の歴史",
            ),
            0.55,
        )

    def test_shared_canonical_theme_does_not_promote_distinct_topics(self) -> None:
        similarity = history.topic_match_similarity(
            "日本の教育格差はなぜ広がるのか",
            {
                "canonical_theme": "社会の格差構造",
                "angle": "学校選択が地域の学習機会を分ける仕組み",
            },
            "アメリカの男女賃金格差の歴史",
            {
                "topic": "アメリカの男女賃金格差の歴史",
                "status": "published",
                "topic_metadata": {
                    "canonical_theme": "社会の格差構造",
                    "angle": "職種と昇進制度が生涯賃金を分ける仕組み",
                },
            },
        )
        self.assertLess(similarity, 0.55)

    def test_similar_angle_alone_does_not_promote_distinct_topics(self) -> None:
        similarity = history.topic_match_similarity(
            "日本の教育格差はなぜ広がるのか",
            {
                "canonical_theme": "学校選択が地域の学習機会を分ける制度",
                "angle": "制度が格差を固定する仕組み",
            },
            "アメリカの男女賃金格差の歴史",
            {
                "topic": "アメリカの男女賃金格差の歴史",
                "status": "published",
                "topic_metadata": {
                    "canonical_theme": "職種と昇進制度が生涯賃金を分ける仕組み",
                    "angle": "制度が格差を固定する仕組み",
                },
            },
        )
        self.assertLess(similarity, 0.55)

    def test_invalid_or_missing_novelty_is_not_treated_as_new(self) -> None:
        self.assertEqual(
            history.topic_metadata("題材", {})["novelty_type"],
            "unknown",
        )
        self.assertEqual(
            history.topic_metadata("題材", {"novelty_type": "invented"})[
                "novelty_type"
            ],
            "unknown",
        )

    def test_youtube_creator_audience_is_saved_as_continuation_audience(self) -> None:
        metadata = history.topic_metadata(
            "YouTube制作者向けの題材",
            {"youtube_creator_audience": "初めて投稿するYouTube制作者"},
        )
        self.assertEqual(metadata["audience"], "初めて投稿するYouTube制作者")

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
