"""比喩や言い回しを変えただけの言い換え重複に対するdedupe強化のテスト。

対象: history.py の ideology 向け概念タグ追加、cooldown_window_topics、
recent_titles の日数窓、reserve_topic の semantic_check 差し込み、
ai_text.check_semantic_duplicate。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import ai_text, history


class IdeologyConceptTagTest(unittest.TestCase):
    """youtube-growth専用語彙に依存しない意味的重複が、実データで検出できることを確認する。"""

    def test_reworded_communism_utopia_narrative_is_caught(self) -> None:
        a = "人類はなぜ「完璧な楽園」を信じてしまうのか？16億人が託した夢と現実"
        b = "なぜ人は「全員が同じ」を信じてしまうのか？16億人の夢と一夜の暴落"
        self.assertGreaterEqual(history.topic_similarity(a, b), 0.55)

    def test_distinct_capitalism_and_communism_topics_stay_below_threshold(self) -> None:
        capitalism = "GDPは現代の祈り？「成長」という名の神様を降りる日"
        communism = "「天国」を作るための「地獄」？理想が人を喰らう時、私たちは何を見るのか"
        self.assertLess(history.topic_similarity(capitalism, communism), 0.55)


class CooldownWindowTopicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = SimpleNamespace(
            id="ideology",
            history_file=Path(self.tmp.name) / "history.jsonl",
        )
        self.now = datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)

    def _append(self, row: dict) -> None:
        self.spec.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.spec.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_returns_recent_topics_newest_first_deduped_within_window(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(days=40)).isoformat(),
                "status": "published",
                "topic": "古すぎる題材",
            }
        )
        self._append(
            {
                "ts": (self.now - timedelta(days=5)).isoformat(),
                "status": "published",
                "topic": "少し前の題材",
            }
        )
        self._append(
            {
                "ts": (self.now - timedelta(days=1)).isoformat(),
                "status": "queued",
                "topic": "最新の題材",
            }
        )
        self._append(
            {
                "ts": self.now.isoformat(),
                "status": "queued",
                "topic": "少し前の題材",  # 重複トピックは1回だけ
            }
        )

        topics = history.cooldown_window_topics(
            self.spec, cooldown_days=30, now=self.now
        )

        self.assertEqual(topics, ["少し前の題材", "最新の題材"])

    def test_zero_cooldown_returns_empty(self) -> None:
        self.assertEqual(
            history.cooldown_window_topics(self.spec, cooldown_days=0), []
        )

    def test_recent_titles_can_be_scoped_to_cooldown_window(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(days=45)).isoformat(),
                "title": "窓の外の古いタイトル",
            }
        )
        self._append(
            {
                "ts": (self.now - timedelta(days=2)).isoformat(),
                "title": "窓の中の新しいタイトル",
            }
        )

        unscoped = history.recent_titles(self.spec, now=self.now)
        scoped = history.recent_titles(
            self.spec, cooldown_days=30, now=self.now
        )

        self.assertIn("窓の外の古いタイトル", unscoped)
        self.assertNotIn("窓の外の古いタイトル", scoped)
        self.assertIn("窓の中の新しいタイトル", scoped)


class ReserveTopicSemanticCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = SimpleNamespace(
            id="ideology",
            history_file=Path(self.tmp.name) / "history.jsonl",
        )
        self.now = datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)

    def _append(self, row: dict) -> None:
        self.spec.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.spec.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_semantic_check_only_runs_when_lexical_match_is_absent(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(days=1)).isoformat(),
                "status": "published",
                "topic": "既存の題材",
            }
        )
        calls: list[tuple[str, list[str]]] = []

        def semantic_check(topic: str, recent: list[str]):
            calls.append((topic, recent))
            return None

        # 語彙一致する題材なのでLLM判定を呼ばずに文字列照合だけでスキップする。
        with self.assertRaises(history.TopicCooldownSkip):
            history.reserve_topic(
                self.spec,
                "capitalism",
                "既存の題材",
                cooldown_days=30,
                now=self.now,
                semantic_check=semantic_check,
            )
        self.assertEqual(calls, [])

    def test_semantic_match_raises_skip_and_records_llm_source(self) -> None:
        self._append(
            {
                "ts": (self.now - timedelta(days=1)).isoformat(),
                "status": "published",
                "topic": "見えざる手が導く資本主義の光と影",
            }
        )

        def semantic_check(topic: str, recent: list[str]):
            self.assertEqual(recent, ["見えざる手が導く資本主義の光と影"])
            return history.TopicMatch(
                topic=recent[0], ts="", similarity=0.9, source="LLM判定"
            )

        with self.assertRaises(history.TopicCooldownSkip) as raised:
            history.reserve_topic(
                self.spec,
                "capitalism",
                "「見えない手」に導かれて、私たちはどこへ向かうのか？",
                cooldown_days=30,
                now=self.now,
                semantic_check=semantic_check,
            )

        self.assertEqual(raised.exception.match.source, "LLM判定")
        rows = [
            json.loads(line)
            for line in self.spec.history_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[-1]["status"], "skipped")
        self.assertAlmostEqual(rows[-1]["similarity"], 0.9)

    def test_semantic_check_miss_still_allows_reservation(self) -> None:
        reservation = history.reserve_topic(
            self.spec,
            "capitalism",
            "新しい題材",
            cooldown_days=30,
            now=self.now,
            semantic_check=lambda topic, recent: None,
        )
        self.assertIsNotNone(reservation)

    def test_semantic_match_for_explicit_opposing_view_allows_continuation(self) -> None:
        parent = "配給制度が不足を生んだ理由"
        self._append(
            {
                "ts": (self.now - timedelta(days=1)).isoformat(),
                "status": "published",
                "video_id": "parent-video",
                "topic": parent,
                "topic_metadata": {
                    "angle": "制度設計の制約から不足を検証する",
                    "comparison_key": "制度設計の制約",
                    "viewpoint": "制度を批判的に検証する立場",
                },
            }
        )

        metadata = {
            "novelty_type": "opposing_view",
            "parent_topic": parent,
            "parent_topic_id": "parent-video",
            "novelty_reason": "同じ制度を支持者側の合理性から検証する",
            "angle": "不足ではなく配分の優先順位を比較する",
            "novelty_axis": "stance",
            "viewpoint": "制度を支持する側の合理性",
            "comparison_key": "制度を支持する側の合理性",
        }
        reservation = history.reserve_topic(
            self.spec,
            "capitalism",
            parent,
            cooldown_days=30,
            metadata=metadata,
            semantic_check=lambda topic, recent: history.TopicMatch(
                topic=recent[0], ts="", similarity=0.9, source="LLM判定"
            ),
            now=self.now,
        )
        self.assertIsNotNone(reservation)


class CheckSemanticDuplicateTest(unittest.TestCase):
    def test_llm_flags_reworded_topic_as_duplicate(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps(
                {
                    "duplicate": True,
                    "matched_index": 2,
                    "confidence": 0.87,
                    "reason": "同じ主張の比喩違い",
                }
            ),
        ):
            result = ai_text.check_semantic_duplicate(
                "新しい候補", ["候補1", "候補2", "候補3"]
            )
        self.assertEqual(result, ("候補2", 0.87))

    def test_llm_says_not_duplicate_returns_none(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps({"duplicate": False}),
        ):
            result = ai_text.check_semantic_duplicate("新しい候補", ["候補1"])
        self.assertIsNone(result)

    def test_dispatch_failure_returns_none_instead_of_raising(self) -> None:
        with mock.patch.object(
            ai_text, "_dispatch", side_effect=RuntimeError("backend down")
        ):
            result = ai_text.check_semantic_duplicate("新しい候補", ["候補1"])
        self.assertIsNone(result)

    def test_out_of_range_matched_index_falls_back_to_first_candidate(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps({"duplicate": True, "matched_index": 99}),
        ):
            result = ai_text.check_semantic_duplicate("新しい候補", ["候補1", "候補2"])
        self.assertEqual(result, ("候補1", 1.0))

    def test_no_recent_topics_short_circuits_without_dispatch(self) -> None:
        with mock.patch.object(ai_text, "_dispatch") as dispatch:
            result = ai_text.check_semantic_duplicate("新しい候補", [])
        dispatch.assert_not_called()
        self.assertIsNone(result)

    def test_semantic_limit_keeps_newest_candidates(self) -> None:
        newest_first = [f"題材{i}" for i in range(30)]
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps(
                {"duplicate": True, "matched_index": 1, "confidence": 0.9}
            ),
        ) as dispatch:
            result = ai_text.check_semantic_duplicate(
                "新しい候補", newest_first
            )

        self.assertEqual(result, ("題材0", 0.9))
        prompt = dispatch.call_args.args[0]
        self.assertIn("1. 題材0", prompt)
        self.assertNotIn("題材29", prompt)


if __name__ == "__main__":
    unittest.main()
