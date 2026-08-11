"""issue #92: 実績フィードバックの自動適用撤去→3日毎チャンネル別issue化のテスト。

対象: `doci.performance_report` のtrait出現検知・実験状態遷移・issue本文組み立て・
オーケストレーション（interval gate/all-channels隔離/dry-run無副作用）。
`performance._experiment_result`（有効性判定の純関数）もここで検証する
（旧 tests/test_performance_propagation.py から移設）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import (
    channel,
    config,
    feedback_issues,
    history,
    performance,
    performance_report,
)


def _video(video_id: str, avg: float, *, corner: str = "video") -> dict:
    return {
        "video_id": video_id,
        "corner": corner,
        "format_traits": ["tier:long_short", "duration:60_to_179s", "chart:present"],
        "analytics": {"views": 100, "average_view_percentage": avg},
    }


class ExperimentResultTest(unittest.TestCase):
    """`_experiment_result`: 有効性判定の単体テスト。"""

    def test_effective_when_score_exceeds_peer_median(self) -> None:
        videos = [_video(f"peer-{i}", avg) for i, avg in enumerate([10, 20, 30, 40])]
        videos.append(_video("applied", 90))
        snapshot = {"collected_at": "2026-07-26T00:00:00+00:00", "videos": videos}

        result = performance._experiment_result(snapshot, "video", "applied")

        self.assertTrue(result["effective"])
        self.assertEqual(result["peers"], 4)
        self.assertEqual(result["peer_median"], 25)
        self.assertEqual(result["reason"], "")

    def test_not_effective_when_score_below_peer_median(self) -> None:
        videos = [_video(f"peer-{i}", avg) for i, avg in enumerate([60, 70, 80, 90])]
        videos.append(_video("applied", 10))
        snapshot = {"collected_at": "2026-07-26T00:00:00+00:00", "videos": videos}

        result = performance._experiment_result(snapshot, "video", "applied")

        self.assertFalse(result["effective"])
        self.assertEqual(result["peer_median"], 75)

    def test_fails_closed_when_peers_insufficient(self) -> None:
        videos = [_video(f"peer-{i}", avg) for i, avg in enumerate([10, 20, 30])]
        videos.append(_video("applied", 90))
        snapshot = {"collected_at": "2026-07-26T00:00:00+00:00", "videos": videos}

        result = performance._experiment_result(snapshot, "video", "applied")

        self.assertFalse(result["effective"])
        self.assertEqual(result["reason"], "insufficient_comparison")
        self.assertEqual(result["peers"], 3)

    def test_fails_closed_when_applied_video_missing_from_cohort(self) -> None:
        videos = [_video(f"peer-{i}", avg) for i, avg in enumerate([10, 20, 30, 40])]
        snapshot = {"collected_at": "2026-07-26T00:00:00+00:00", "videos": videos}

        result = performance._experiment_result(snapshot, "video", "missing-video")

        self.assertFalse(result["effective"])
        self.assertEqual(result["reason"], "insufficient_comparison")
        self.assertEqual(result["peers"], 0)


def _experiment(
    *,
    corner: str = "video",
    trait: str = "chart:present",
    direction: str = "positive",
    proposed_at: str = "2026-07-01T00:00:00+00:00",
    **overrides,
) -> dict:
    row = {
        "experiment_id": "px-abc",
        "channel": "youtube-growth",
        "corner": corner,
        "status": "proposed",
        "direction": direction,
        "trait": trait,
        "metric": "youtube_analytics_api_v2.average_view_percentage",
        "format_cohort": "duration:60_to_179s|tier:long_short",
        "decision_id": "dec-1",
        "proposed_at": proposed_at,
    }
    row.update(overrides)
    return row


class DetectAppliedVideoTest(unittest.TestCase):
    def test_positive_trait_present_is_detected(self) -> None:
        experiment = _experiment(direction="positive", trait="chart:present")
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "published_at": "2026-07-02T00:00:00+00:00",
                    "format_traits": ["chart:present", "tier:long_short"],
                }
            ]
        }
        applied = performance_report._detect_applied_video(experiment, snapshot)
        self.assertIsNotNone(applied)
        self.assertEqual(applied["video_id"], "v1")

    def test_positive_trait_absent_is_not_detected(self) -> None:
        experiment = _experiment(direction="positive", trait="chart:present")
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "published_at": "2026-07-02T00:00:00+00:00",
                    "format_traits": ["chart:absent", "tier:long_short"],
                }
            ]
        }
        self.assertIsNone(performance_report._detect_applied_video(experiment, snapshot))

    def test_negative_trait_with_family_present_is_detected(self) -> None:
        experiment = _experiment(direction="negative", trait="chart:present")
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "published_at": "2026-07-02T00:00:00+00:00",
                    "format_traits": ["chart:absent", "tier:long_short"],
                }
            ]
        }
        applied = performance_report._detect_applied_video(experiment, snapshot)
        self.assertIsNotNone(applied)
        self.assertEqual(applied["video_id"], "v1")

    def test_negative_trait_without_family_is_not_a_false_positive(self) -> None:
        """script.json欠落等でchart系traitがまるごと無い動画を誤って
        「chart:absentを選んだ」とは判定しない（familyガード）。"""
        experiment = _experiment(direction="negative", trait="chart:present")
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "published_at": "2026-07-02T00:00:00+00:00",
                    "format_traits": ["tier:long_short"],
                }
            ]
        }
        self.assertIsNone(performance_report._detect_applied_video(experiment, snapshot))

    def test_videos_before_proposed_at_are_ignored(self) -> None:
        experiment = _experiment(
            direction="positive",
            trait="chart:present",
            proposed_at="2026-07-05T00:00:00+00:00",
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "before",
                    "corner": "video",
                    "published_at": "2026-07-01T00:00:00+00:00",
                    "format_traits": ["chart:present"],
                }
            ]
        }
        self.assertIsNone(performance_report._detect_applied_video(experiment, snapshot))

    def test_earliest_published_candidate_is_returned(self) -> None:
        experiment = _experiment(direction="positive", trait="chart:present")
        snapshot = {
            "videos": [
                {
                    "video_id": "later",
                    "corner": "video",
                    "published_at": "2026-07-10T00:00:00+00:00",
                    "format_traits": ["chart:present"],
                },
                {
                    "video_id": "earlier",
                    "corner": "video",
                    "published_at": "2026-07-03T00:00:00+00:00",
                    "format_traits": ["chart:present"],
                },
            ]
        }
        applied = performance_report._detect_applied_video(experiment, snapshot)
        self.assertEqual(applied["video_id"], "earlier")

    def test_other_corner_videos_are_ignored(self) -> None:
        experiment = _experiment(corner="video", direction="positive", trait="chart:present")
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "shorts",
                    "published_at": "2026-07-02T00:00:00+00:00",
                    "format_traits": ["chart:present"],
                }
            ]
        }
        self.assertIsNone(performance_report._detect_applied_video(experiment, snapshot))


class ProgressExperimentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = SimpleNamespace(id="youtube-growth", output_dir=self.root)

    def _write_experiments(self, rows: list[dict]) -> None:
        path = performance_report._experiments_path(self.spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_proposed_without_matching_video_stays_pending(self) -> None:
        self._write_experiments([_experiment()])
        snapshot = {"videos": []}
        by_corner = performance_report._progress_experiments(
            self.spec, snapshot, datetime(2026, 7, 10, tzinfo=timezone.utc), apply=True
        )
        self.assertEqual(by_corner, {})
        rows = performance_report._read_experiments(self.spec)
        self.assertEqual(rows[-1]["status"], "proposed")

    def test_proposed_becomes_applied_then_evaluated_once_threshold_reached(self) -> None:
        self._write_experiments([_experiment()])
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)

        # 1st pass: 適用動画は見つかるが指標閾値未到達 → "applied"止まり、報告対象外。
        under_threshold_snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "published_at": "2026-07-02T00:00:00+00:00",
                    "format_traits": ["chart:present", "tier:long_short", "duration:60_to_179s"],
                    "privacy_status": "unlisted",
                    "data_api": {"views": 5},
                    "analytics": None,
                }
            ]
        }
        by_corner = performance_report._progress_experiments(
            self.spec, under_threshold_snapshot, now, apply=True
        )
        self.assertEqual(by_corner, {})
        rows = performance_report._read_experiments(self.spec)
        self.assertEqual(rows[-1]["status"], "applied")
        self.assertEqual(rows[-1]["video_id"], "v1")

        # 2nd pass: 指標が十分に育った → "evaluated"へ遷移し、報告対象になる。
        ready_snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "format_traits": ["chart:present", "tier:long_short", "duration:60_to_179s"],
                    "analytics": {"views": 100, "average_view_percentage": 80},
                },
                *[
                    {
                        "video_id": f"peer-{i}",
                        "corner": "video",
                        "format_traits": ["chart:absent", "tier:long_short", "duration:60_to_179s"],
                        "analytics": {"views": 100, "average_view_percentage": avg},
                    }
                    for i, avg in enumerate([10, 20, 30, 40])
                ],
            ]
        }
        by_corner = performance_report._progress_experiments(
            self.spec, ready_snapshot, now, apply=True
        )
        self.assertIn("video", by_corner)
        self.assertEqual(len(by_corner["video"]), 1)
        self.assertTrue(by_corner["video"][0]["result"]["effective"])
        rows = performance_report._read_experiments(self.spec)
        self.assertEqual(rows[-1]["status"], "evaluated")

    def test_unreported_evaluated_row_persists_across_calls(self) -> None:
        evaluated = _experiment(
            status="evaluated",
            video_id="v1",
            result={"effective": True, "reason": ""},
        )
        self._write_experiments([evaluated])
        by_corner = performance_report._progress_experiments(
            self.spec, {"videos": []}, datetime(2026, 7, 10, tzinfo=timezone.utc), apply=True
        )
        self.assertIn("video", by_corner)
        self.assertEqual(by_corner["video"][0]["status"], "evaluated")

    def test_reported_row_is_not_returned_again(self) -> None:
        reported = _experiment(
            status="reported",
            video_id="v1",
            result={"effective": True, "reason": ""},
            report_issue_number=42,
        )
        self._write_experiments([reported])
        by_corner = performance_report._progress_experiments(
            self.spec, {"videos": []}, datetime(2026, 7, 10, tzinfo=timezone.utc), apply=True
        )
        self.assertEqual(by_corner, {})

    def test_expired_after_max_age_without_detection(self) -> None:
        old_proposal = _experiment(proposed_at="2026-01-01T00:00:00+00:00")
        self._write_experiments([old_proposal])
        far_future = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=config.PERFORMANCE_EXPERIMENT_MAX_AGE_DAYS + 1
        )
        by_corner = performance_report._progress_experiments(
            self.spec, {"videos": []}, far_future, apply=True
        )
        self.assertEqual(by_corner, {})
        rows = performance_report._read_experiments(self.spec)
        self.assertEqual(rows[-1]["status"], "expired")

    def test_applied_also_expires_after_max_age_without_evaluation_data(self) -> None:
        """指標が育たない(Analytics未許可・非公開のまま等)まま`applied`に
        無期限で滞留すると「前回提案の効果検証」が永久に空欄になるため、
        `applied`にも`proposed`と同じmax_ageを適用する。"""
        stalled_applied = _experiment(
            status="applied",
            proposed_at="2026-01-01T00:00:00+00:00",
            video_id="v1",
            video_published_at="2026-01-02T00:00:00+00:00",
        )
        self._write_experiments([stalled_applied])
        far_future = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=config.PERFORMANCE_EXPERIMENT_MAX_AGE_DAYS + 1
        )
        # 指標が全く育っていないsnapshot(has_evaluation_result=False)を渡す。
        by_corner = performance_report._progress_experiments(
            self.spec,
            {"videos": [{"video_id": "v1", "corner": "video", "analytics": None, "data_api": {"views": 0}}]},
            far_future,
            apply=True,
        )
        self.assertEqual(by_corner, {})
        rows = performance_report._read_experiments(self.spec)
        self.assertEqual(rows[-1]["status"], "expired")

    def test_dry_run_does_not_persist_any_state_transition(self) -> None:
        """issue #92レビュー指摘: 以前はapplyに関わらず無条件でJSONLへ
        追記しており、dry-runで実行しただけでexpired等の終端状態へ
        進んでしまう副作用があった。"""
        self._write_experiments([_experiment(proposed_at="2026-01-01T00:00:00+00:00")])
        before = performance_report._read_experiments(self.spec)
        far_future = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=config.PERFORMANCE_EXPERIMENT_MAX_AGE_DAYS + 1
        )
        by_corner = performance_report._progress_experiments(
            self.spec, {"videos": []}, far_future, apply=False
        )
        self.assertEqual(by_corner, {})
        after = performance_report._read_experiments(self.spec)
        self.assertEqual(before, after)  # 1行も追記されていない


class CornerSectionAndCandidateTest(unittest.TestCase):
    def _decision(self, **overrides) -> dict:
        decision = {
            "decision_id": "dec-1",
            "status": "active",
            "reason": "",
            "metric": "youtube_analytics_api_v2.average_view_percentage",
            "format_cohort": "duration:60_to_179s|tier:long_short",
            "eligible_video_ids": [f"v{i}" for i in range(8)],
            "top_video_ids": ["v6", "v7"],
            "bottom_video_ids": ["v0", "v1"],
            "positive_traits": ["chart:present"],
            "negative_traits": [],
        }
        decision.update(overrides)
        return decision

    def test_active_decision_produces_proposal_when_not_in_cooldown(self) -> None:
        spec = SimpleNamespace(id="youtube-growth")
        section = performance_report.build_corner_section(
            spec, "video", self._decision(), [], set()
        )
        self.assertIsNotNone(section["proposal"])
        self.assertEqual(section["proposal"]["trait"], "chart:present")
        self.assertEqual(section["proposal"]["direction"], "positive")

    def test_empty_channel_page_signal_preserves_legacy_fingerprint(self) -> None:
        spec = SimpleNamespace(id="youtube-growth")
        section = performance_report.build_corner_section(
            spec, "video", self._decision(), [], set()
        )

        self.assertEqual(
            performance_report.fingerprint(spec.id, [section]),
            "ebb25a910ce9bc88",
        )
        self.assertEqual(
            performance_report.fingerprint(
                spec.id, [section], channel_page_signals={}
            ),
            "ebb25a910ce9bc88",
        )

    def test_cooldown_suppresses_proposal_but_keeps_investigation(self) -> None:
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision()
        key = performance_report.hypothesis_key(spec.id, "video", decision)
        section = performance_report.build_corner_section(
            spec, "video", decision, [], {key}
        )
        self.assertIsNone(section["proposal"])
        body_hyp_text = performance_report._hypothesis_text(decision, section)
        self.assertIn("cooldown", body_hyp_text)
        investigation = performance_report._investigation_text(decision)
        self.assertIn(decision["metric"], investigation)

    def test_insufficient_data_produces_no_proposal(self) -> None:
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(spec, "video", decision, [], set())
        self.assertIsNone(section["proposal"])

    def test_build_cycle_candidate_none_when_no_content(self) -> None:
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(spec, "video", decision, [], set())
        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc)
        )
        self.assertIsNone(candidate)

    def test_cycle_body_cap_preserves_markers_and_guardrails(self) -> None:
        body = performance_report._bounded_cycle_body(
            ["<!-- marker -->", "x" * 70_000],
            ["", "## ガードレール", "", "- 一度に試す変数は1つ"],
        )

        self.assertLessEqual(
            len(body), performance_report._CYCLE_BODY_MAX_CHARS
        )
        self.assertTrue(body.startswith("<!-- marker -->"))
        self.assertIn("## 表示上限", body)
        self.assertIn("## ガードレール", body)
        self.assertTrue(body.endswith("- 一度に試す変数は1つ\n"))

    def test_cycle_body_cap_does_not_split_markdown_block(self) -> None:
        oversized_inline_code = "`" + ("x" * 70_000) + "`"
        body = performance_report._bounded_cycle_body(
            ["<!-- marker -->", oversized_inline_code, "- 後続ブロック"],
            ["", "## ガードレール", "", "- 一度に試す変数は1つ"],
        )

        self.assertNotIn("`", body)
        self.assertNotIn("後続ブロック", body)
        self.assertIn("## 表示上限", body)
        self.assertIn("## ガードレール", body)

    def test_build_cycle_candidate_includes_gap_discovery_without_proposal(self) -> None:
        """issue #164 (Sol review指摘6): 形式仮説が無くても、gap動画の
        検索発見データがsnapshotにあればレポート候補を生成する。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "shorts", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "gap-1",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                    "analytics": {
                        "views": 100,
                        "traffic_sources": {"YT_SEARCH": 40},
                        "search_terms": [{"term": "ネタ切れ 解消", "views": 40}],
                    },
                }
            ]
        }

        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc), snapshot
        )

        self.assertIsNotNone(candidate)
        self.assertIn("検索発見（Discovery）", candidate["body"])

    def test_build_cycle_candidate_includes_retention_moments_without_proposal(self) -> None:
        """issue #149 (Sol review指摘): 形式仮説・gap動画が無くても、matching
        cornerに明瞭な維持率の山/谷があればレポート候補を生成する。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "video", decision, [], set()
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "data_api": {"duration": "PT1M"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.2, "watch_ratio": 0.85},
                            {"elapsed_ratio": 0.3, "watch_ratio": 0.40},  # dip
                            {"elapsed_ratio": 0.5, "watch_ratio": 0.85},
                            {"elapsed_ratio": 0.6, "watch_ratio": 0.40},  # dip
                            {"elapsed_ratio": 0.7, "watch_ratio": 0.80},
                            {"elapsed_ratio": 1.0, "watch_ratio": 0.50},
                        ]
                    },
                }
            ],
        }

        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc), snapshot
        )

        self.assertIsNotNone(candidate)
        self.assertIn("維持率カーブの山/谷", candidate["body"])

    def test_build_cycle_candidate_includes_subscribed_status_difference(self) -> None:
        """issue #128: 他の仮説が無くても、標本条件を満たす購読状態別の
        明瞭な差があればレポート候補を生成する。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(
            status="insufficient_data", reason="比較可能な動画が2本"
        )
        section = performance_report.build_corner_section(
            spec, "video", decision, [], set()
        )
        subscribed = []
        unsubscribed = []
        for index in range(1, 7):
            subscribed.append(
                {
                    "elapsed_ratio": index / 10,
                    "watch_ratio": 0.9 - index * 0.02,
                    "segment_impressions": 30,
                }
            )
            unsubscribed.append(
                {
                    "elapsed_ratio": index / 10,
                    "watch_ratio": 0.9 - index * 0.06,
                    "segment_impressions": 40,
                }
            )
        snapshot = {
            "traffic_sources": {"available": True},
            "retention_by_subscribed_status": {
                "available": True,
                "queried_video_ids": ["segmented-1"],
            },
            "videos": [
                {
                    "video_id": "segmented-1",
                    "corner": "video",
                    "data_api": {"duration": "PT100S"},
                    "analytics": {
                        "traffic_sources": {"YT_SEARCH": 40},
                        "retention_by_subscribed_status": {
                            "SUBSCRIBED": subscribed,
                            "UNSUBSCRIBED": unsubscribed,
                        },
                    },
                }
            ],
        }

        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertIsNotNone(candidate)
        self.assertIn("購読状態別の維持率と流入元", candidate["body"])
        self.assertIn("リピーター/新規視聴者ではありません", candidate["body"])

    def test_build_cycle_candidate_includes_monotonic_opening_drop(self) -> None:
        """issue #142: 山/谷が無い単調低下でも、冒頭シグナルから候補を作る。"""
        spec = SimpleNamespace(id="ideology")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "capitalism", decision, [], set()
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "opening-drop",
                    "corner": "capitalism",
                    "data_api": {"duration": "PT100S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                            {"elapsed_ratio": 0.1, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.2, "watch_ratio": 0.82},
                            {"elapsed_ratio": 0.3, "watch_ratio": 0.70},
                            {"elapsed_ratio": 0.5, "watch_ratio": 0.60},
                            {"elapsed_ratio": 1.0, "watch_ratio": 0.40},
                        ]
                    },
                }
            ],
        }

        self.assertEqual(
            performance.retention_moments(
                snapshot["videos"][0]["analytics"]["retention_curve"]
            ),
            [],
        )
        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertIsNotNone(candidate)
        self.assertIn("冒頭30秒の維持率と次の1本", candidate["body"])
        self.assertIn("冒頭フックだけを変更", candidate["body"])

    def test_build_cycle_candidate_includes_thumbnail_opening_slope_for_ideology(
        self,
    ) -> None:
        """issue #125: source cornerに限定せず、30秒→中盤低下だけで候補化する。"""
        spec = SimpleNamespace(id="ideology")
        decision = self._decision(
            status="insufficient_data", reason="比較可能な動画が2本"
        )
        section = performance_report.build_corner_section(
            spec, "capitalism", decision, [], set()
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "script.json").write_text(
                json.dumps(
                    {
                        "title": "資本主義が約束した自由",
                        "_tts_timing": {
                            "duration_seconds": 120.0,
                            "segments": [
                                {
                                    "text": "自由という約束から始めます。",
                                    "start_seconds": 0.0,
                                    "end_seconds": 12.0,
                                },
                                {
                                    "text": "その代償を確認します。",
                                    "start_seconds": 12.0,
                                    "end_seconds": 28.0,
                                },
                            ],
                        },
                        "_thumbnail_provenance": {
                            "display_text": "資本主義が約束した自由",
                            "render_status": "rendered",
                            "youtube_set_status": "set",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            snapshot = {
                "retention_curve": {"available": True},
                "videos": [
                    {
                        "video_id": "ideology-mid-drop",
                        "title": "資本主義が約束した自由",
                        "corner": "capitalism",
                        "workdir": tmp,
                        "history_ts": "2026-07-25T00:00:00+00:00",
                        "data_api": {"duration": "PT2M"},
                        "analytics": {
                            "retention_curve": [
                                {"elapsed_ratio": 0.00, "watch_ratio": 0.95},
                                {"elapsed_ratio": 0.10, "watch_ratio": 0.94},
                                {"elapsed_ratio": 0.20, "watch_ratio": 0.93},
                                {"elapsed_ratio": 0.25, "watch_ratio": 0.92},
                                {"elapsed_ratio": 0.30, "watch_ratio": 0.90},
                                {"elapsed_ratio": 0.35, "watch_ratio": 0.88},
                                {"elapsed_ratio": 0.40, "watch_ratio": 0.86},
                                {"elapsed_ratio": 0.45, "watch_ratio": 0.84},
                                {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
                                {"elapsed_ratio": 1.00, "watch_ratio": 0.80},
                            ]
                        },
                    }
                ],
            }

            opening = performance_report._opening_signal_for_row(
                snapshot["videos"][0]
            )
            self.assertIsNotNone(opening)
            self.assertFalse(opening["actionable"])
            self.assertEqual(
                performance.retention_moments(
                    snapshot["videos"][0]["analytics"]["retention_curve"]
                ),
                [],
            )
            candidate = performance_report.build_cycle_candidate(
                spec,
                [section],
                datetime(2026, 7, 26, tzinfo=timezone.utc),
                snapshot,
            )

        self.assertIsNotNone(candidate)
        self.assertIn("サムネの約束・合成入力文と30秒→中盤の傾き", candidate["body"])
        self.assertIn("生成時サムネ描画文字", candidate["body"])
        self.assertIn("自由という約束から始めます", candidate["body"])
        self.assertIn("一方だけを手動変更", candidate["body"])

    def test_thumbnail_slope_without_comparison_evidence_is_not_a_candidate(
        self,
    ) -> None:
        spec = SimpleNamespace(id="ideology")
        decision = self._decision(
            status="insufficient_data", reason="比較可能な動画が2本"
        )
        section = performance_report.build_corner_section(
            spec, "capitalism", decision, [], set()
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "no-script-evidence",
                    "corner": "capitalism",
                    "data_api": {"duration": "PT2M"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.00, "watch_ratio": 0.95},
                            {"elapsed_ratio": 0.10, "watch_ratio": 0.94},
                            {"elapsed_ratio": 0.20, "watch_ratio": 0.93},
                            {"elapsed_ratio": 0.25, "watch_ratio": 0.92},
                            {"elapsed_ratio": 0.30, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.35, "watch_ratio": 0.88},
                            {"elapsed_ratio": 0.40, "watch_ratio": 0.86},
                            {"elapsed_ratio": 0.45, "watch_ratio": 0.84},
                            {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
                            {"elapsed_ratio": 1.00, "watch_ratio": 0.80},
                        ]
                    },
                }
            ],
        }

        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertIsNone(candidate)

    def test_build_cycle_candidate_includes_first_three_drop_for_short_tier(self) -> None:
        """issue #127: corner名に依存せず、実際のShort tierの冒頭3秒低下だけで
        レポート候補を生成する。"""
        spec = SimpleNamespace(id="ideology")
        decision = self._decision(
            status="insufficient_data", reason="比較可能な動画が2本"
        )
        section = performance_report.build_corner_section(
            spec, "capitalism", decision, [], set()
        )
        curve = [
            {"elapsed_ratio": 0.01, "watch_ratio": 0.95},  # 0.6秒
            {"elapsed_ratio": 0.03, "watch_ratio": 0.91},  # 1.8秒
            {"elapsed_ratio": 0.05, "watch_ratio": 0.87},  # 3.0秒
            {"elapsed_ratio": 0.10, "watch_ratio": 0.91},
            {"elapsed_ratio": 0.20, "watch_ratio": 0.93},
            {"elapsed_ratio": 0.50, "watch_ratio": 0.95},
            {"elapsed_ratio": 1.00, "watch_ratio": 0.90},
        ]
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "ideology-short",
                    "corner": "capitalism",
                    "format_traits": ["tier:short", "duration:60_to_179s"],
                    "data_api": {"duration": "PT60S"},
                    "analytics": {"retention_curve": curve},
                }
            ],
        }

        opening = performance_report._opening_signal_for_row(snapshot["videos"][0])
        self.assertIsNotNone(opening)
        self.assertFalse(opening["actionable"])
        self.assertEqual(performance.retention_moments(curve), [])

        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertIsNotNone(candidate)
        self.assertIn("Shorts冒頭3秒の維持率と削る情報", candidate["body"])
        self.assertIn("不要な情報を1つだけ削る", candidate["body"])

    def test_build_cycle_candidate_ignores_first_three_drop_for_longform(self) -> None:
        """issue #127: 同じ短い低下でも、履歴tierがlongformなら対象外。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(
            status="insufficient_data", reason="比較可能な動画が2本"
        )
        section = performance_report.build_corner_section(
            spec, "video", decision, [], set()
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "regular-video",
                    "corner": "video",
                    "format_traits": ["tier:longform"],
                    "data_api": {"duration": "PT60S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.01, "watch_ratio": 0.95},
                            {"elapsed_ratio": 0.03, "watch_ratio": 0.91},
                            {"elapsed_ratio": 0.05, "watch_ratio": 0.87},
                            {"elapsed_ratio": 0.10, "watch_ratio": 0.91},
                            {"elapsed_ratio": 0.20, "watch_ratio": 0.93},
                            {"elapsed_ratio": 0.50, "watch_ratio": 0.95},
                            {"elapsed_ratio": 1.00, "watch_ratio": 0.90},
                        ]
                    },
                }
            ],
        }

        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertIsNone(candidate)

    def test_build_cycle_candidate_ignores_flat_retention_curve(self) -> None:
        """issue #149: 山/谷の無い平坦なカーブでは無内容issueを作らない。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "video", decision, [], set()
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": i / 10, "watch_ratio": 0.70}
                            for i in range(11)
                        ]
                    },
                }
            ],
        }

        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc), snapshot
        )

        self.assertIsNone(candidate)

    def test_build_cycle_candidate_includes_share_over_one_percent(self) -> None:
        """issue #144: 形式仮説・gap動画・維持率が無くても、shorts cornerの
        動画で共有率が1%を超え、構造（format_traits）が記録されていれば
        報告候補を生成する。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "shorts", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "s1",
                    "corner": "shorts",
                    "format_traits": ["tier:short", "scenes:1_to_4"],
                    "share_30d": {"views": 500, "shares": 8},
                }
            ]
        }
        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )
        self.assertIsNotNone(candidate)
        self.assertIn("共有率と共有される動画の構造", candidate["body"])

    def test_build_cycle_candidate_ignores_low_share_rate(self) -> None:
        """issue #144: 共有率1%以下だけでは候補を作らない（無内容issue防止）。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "shorts", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "s2",
                    "corner": "shorts",
                    "format_traits": ["tier:short"],
                    "share_30d": {"views": 1000, "shares": 3},
                }
            ]
        }
        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )
        self.assertIsNone(candidate)

    def test_build_cycle_candidate_ignores_share_without_traits(self) -> None:
        """issue #144 (Sol review指摘): 1%超でも構造（format_traits）が
        未記録なら次の企画の材料にならないため候補にしない。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "shorts", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "s1",
                    "corner": "shorts",
                    "share_30d": {"views": 500, "shares": 8},
                }
            ]
        }
        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )
        self.assertIsNone(candidate)

    def test_build_cycle_candidate_ignores_non_shorts_share(self) -> None:
        """issue #144 (Sol review指摘): video/analytics cornerの共有率1%超だけでは
        shorts専用施策の候補を作らない。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "video", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "format_traits": ["tier:short"],
                    "share_30d": {"views": 100, "shares": 8},
                }
            ]
        }
        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )
        self.assertIsNone(candidate)

    def test_build_cycle_candidate_without_snapshot_keeps_legacy_behavior(self) -> None:
        """issue #164: snapshot未指定の呼び出しは従来どおりsection内容のみで
        判定する（非gapの通常ケースでNoneを維持）。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "shorts", decision, [], set()
        )

        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc)
        )

        self.assertIsNone(candidate)

    def test_build_cycle_candidate_ignores_non_gap_search_data(self) -> None:
        """issue #164 (Sol review指摘6回目): 通常動画（gap_queryなし）に
        検索流入・検索語句があっても、形式仮説・未報告評価が無ければ候補は
        None（無内容issueの作成を防ぐ）。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "shorts", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "normal-1",
                    "corner": "shorts",
                    "analytics": {
                        "views": 100,
                        "traffic_sources": {"YT_SEARCH": 40},
                        "search_terms": [{"term": "通常動画", "views": 40}],
                    },
                }
            ]
        }

        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc), snapshot
        )

        self.assertIsNone(candidate)

    def test_build_cycle_candidate_gap_video_without_search_terms_still_reports(self) -> None:
        """issue #164: gap_query付き動画は検索語句が取得不可でも候補を生成し、
        取得不可と表示する（0や「なし」と断定しない）。"""
        spec = SimpleNamespace(id="youtube-growth")
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "shorts", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "gap-1",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                    "analytics": {"views": 10},
                }
            ]
        }

        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc), snapshot
        )

        self.assertIsNotNone(candidate)
        self.assertIn("取得できませんでした", candidate["body"])

    def test_build_cycle_candidate_ignores_gap_video_with_missing_corner_section(self) -> None:
        """issue #164 (Claude review指摘): gap_query付きでも、そのcornerが
        sectionsに存在しない動画は候補判定に含めない（無内容issueの防止）。"""
        spec = SimpleNamespace(id="youtube-growth")
        # sectionsはvideo cornerのみ（shorts sectionが存在しない）。
        decision = self._decision(status="insufficient_data", reason="比較可能な動画が2本")
        section = performance_report.build_corner_section(
            spec, "video", decision, [], set()
        )
        snapshot = {
            "videos": [
                {
                    "video_id": "gap-shorts",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                    "analytics": {
                        "views": 100,
                        "traffic_sources": {"YT_SEARCH": 40},
                        "search_terms": [{"term": "ネタ切れ 解消", "views": 40}],
                    },
                }
            ]
        }

        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc), snapshot
        )

        self.assertIsNone(candidate)

    def test_build_cycle_candidate_aggregates_multiple_corners(self) -> None:
        spec = SimpleNamespace(id="youtube-growth")
        active_section = performance_report.build_corner_section(
            spec, "video", self._decision(), [], set()
        )
        idle_section = performance_report.build_corner_section(
            spec,
            "shorts",
            self._decision(status="insufficient_data", reason="標本不足"),
            [],
            set(),
        )
        candidate = performance_report.build_cycle_candidate(
            spec, [active_section, idle_section], datetime(2026, 7, 26, tzinfo=timezone.utc)
        )
        self.assertIsNotNone(candidate)
        self.assertIn("corner: video", candidate["body"])
        self.assertIn("corner: shorts", candidate["body"])
        self.assertEqual(len(candidate["hypothesis_keys"]), 1)

    def test_evaluation_section_reports_effective_result(self) -> None:
        spec = SimpleNamespace(id="youtube-growth")
        evaluations = [
            _experiment(
                status="evaluated",
                video_id="v9",
                result={"effective": True, "score": 80, "peer_median": 40, "reason": ""},
            )
        ]
        section = performance_report.build_corner_section(
            spec,
            "video",
            self._decision(status="insufficient_data", reason="標本不足"),
            evaluations,
            set(),
        )
        candidate = performance_report.build_cycle_candidate(
            spec, [section], datetime(2026, 7, 26, tzinfo=timezone.utc)
        )
        self.assertIsNotNone(candidate)
        self.assertIn("v9", candidate["body"])
        self.assertIn("effective", candidate["body"])

    def test_discovery_satisfaction_text_separates_search_from_retention(self) -> None:
        """issue #164: 検索発見（Discovery）と視聴後評価（Satisfaction）を
        別セクションで表示し、取得できない指標は推測で補わない。"""
        snapshot = {
            "videos": [
                {
                    "video_id": "gap-1",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "コンテンツギャップ"},
                    "analytics": {
                        "views": 100,
                        "average_view_percentage": 60.0,
                        "traffic_sources": {"YT_SEARCH": 40},
                        "search_terms": [
                            {"term": "コンテンツギャップ", "views": 25},
                            {"term": "ネタ切れ", "views": 15},
                        ],
                    },
                },
                {
                    "video_id": "gap-2",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "ネタ切れ"},
                    "analytics": {
                        "views": 10,
                        "traffic_sources": {},
                        "search_terms": [],
                    },
                },
            ]
        }

        text = performance_report._discovery_satisfaction_text(snapshot, "shorts")

        self.assertIn("### 検索発見（Discovery）", text)
        self.assertIn("### 視聴後評価（Satisfaction）", text)
        self.assertIn("YouTube検索からの視聴 40 回（全体の 40.0%）", text)
        self.assertIn("「コンテンツギャップ」(25回)", text)
        self.assertIn("平均視聴維持率 60.0%", text)
        # 取得できない動画は0や「なし」と断定しない。
        self.assertIn("YouTube検索からの流入を取得できませんでした", text)
        self.assertIn("維持率を取得できませんでした", text)

    def test_discovery_text_excludes_non_gap_videos(self) -> None:
        """issue #164 (Sol review指摘6回目): 通常動画（gap_queryなし）は
        Discovery/Satisfactionの評価対象にしない。"""
        snapshot = {
            "videos": [
                {
                    "video_id": "normal-1",
                    "corner": "shorts",
                    "analytics": {
                        "views": 100,
                        "traffic_sources": {"YT_SEARCH": 40},
                        "search_terms": [{"term": "通常動画", "views": 40}],
                    },
                },
                {
                    "video_id": "gap-1",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                    "analytics": {
                        "views": 50,
                        "traffic_sources": {"YT_SEARCH": 10},
                        "search_terms": [{"term": "ネタ切れ 解消", "views": 10}],
                    },
                },
            ]
        }

        text = performance_report._discovery_satisfaction_text(snapshot, "shorts")

        self.assertIn("`gap-1`", text)
        self.assertNotIn("`normal-1`", text)

    def test_discovery_satisfaction_text_without_snapshot_is_fail_closed(self) -> None:
        text = performance_report._discovery_satisfaction_text(None, "shorts")
        self.assertIn("snapshot未取得", text)

    def test_discovery_satisfaction_text_ignores_other_corner(self) -> None:
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "topic_metadata": {"gap_query": "語句"},
                    "analytics": {"views": 5},
                }
            ]
        }
        text = performance_report._discovery_satisfaction_text(snapshot, "shorts")
        self.assertIn("このcornerの動画がsnapshotにありません", text)

    def test_gap_match_status_distinguishes_matched_not_confirmed_and_missing(self) -> None:
        """issue #164 (Sol review指摘3): gap_queryと実検索語句の対応を
        完全一致・未確認（上位内非一致）・判定不能に区別する。"""
        self.assertEqual(
            performance_report._gap_match_status(
                "ネタ切れ 解消",
                [{"term": "ネタ切れ 解消", "views": 5}],
            ),
            "matched",
        )
        self.assertEqual(
            performance_report._gap_match_status(
                "ネタ切れ 解消",
                [{"term": "猫 かわいい", "views": 5}],
            ),
            "not_confirmed",
        )
        self.assertEqual(
            performance_report._gap_match_status("", [{"term": "猫", "views": 5}]),
            "not_evaluated",
        )
        self.assertEqual(
            performance_report._gap_match_status("ネタ切れ", []),
            "not_evaluated",
        )

    def test_discovery_text_reports_gap_query_match_status(self) -> None:
        """issue #164: レポート本文にgap_queryとの一致/不一致を明示する。"""
        snapshot = {
            "videos": [
                {
                    "video_id": "gap-a",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                    "analytics": {
                        "views": 100,
                        "average_view_percentage": 60.0,
                        "traffic_sources": {"YT_SEARCH": 40},
                        "search_terms": [
                            {"term": "ネタ切れ 解消", "views": 25},
                            {"term": "コンテンツギャップ", "views": 15},
                        ],
                    },
                },
                {
                    "video_id": "gap-b",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "猫 かわいい"},
                    "analytics": {
                        "views": 80,
                        "average_view_percentage": 50.0,
                        "traffic_sources": {"YT_SEARCH": 20},
                        "search_terms": [{"term": "別の語句", "views": 20}],
                    },
                },
            ]
        }

        text = performance_report._discovery_satisfaction_text(snapshot, "shorts")

        self.assertIn("狙った検索語「ネタ切れ 解消」と完全一致", text)
        self.assertIn("取得できた上位語句に「猫 かわいい」の完全一致なし", text)

    def test_discovery_text_shows_search_terms_when_traffic_unavailable(self) -> None:
        """issue #164 (Claude review指摘): video_traffic_sources のバッチ取得が
        失敗（traffic_sources が空）しても、video_search_terms が成功していれば
        検索語句とgap一致判定を表示する。流入回数を0や「なし」と断定しない。"""
        snapshot = {
            "videos": [
                {
                    "video_id": "gap-a",
                    "corner": "shorts",
                    "topic_metadata": {"gap_query": "ネタ切れ 解消"},
                    "analytics": {
                        "views": 100,
                        "average_view_percentage": 60.0,
                        "traffic_sources": {},
                        "search_terms": [{"term": "ネタ切れ 解消", "views": 25}],
                    },
                }
            ]
        }

        text = performance_report._discovery_satisfaction_text(snapshot, "shorts")

        self.assertIn("流入回数は取得できませんでした", text)
        self.assertIn("検索語句は取得済み", text)
        self.assertIn("「ネタ切れ 解消」(25回)", text)
        self.assertIn("狙った検索語「ネタ切れ 解消」と完全一致", text)
        self.assertNotIn("流入を取得できませんでした", text)


class ChannelPageShareTest(unittest.TestCase):
    """issue #122: YT_CHANNEL割合を最新動画の次施策へ接続する。"""

    def _row(
        self,
        video_id: str,
        published_at: str,
        channel_views: object,
        *,
        total_views: object = 100,
        corner: str = "capitalism",
        traits: list[str] | None = None,
    ) -> dict:
        published_day = datetime.fromisoformat(published_at).date()
        start_day = published_day + timedelta(days=1)
        end_day = start_day + timedelta(
            days=performance.CHANNEL_PAGE_SHARE_WINDOW_DAYS - 1
        )
        channel_page_share = {
            "status": (
                "channel_page_unavailable"
                if channel_views is None
                else "available"
            ),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "window_days": performance.CHANNEL_PAGE_SHARE_WINDOW_DAYS,
            "data_through_date": "2026-07-31",
            "views": total_views,
            "channel_page_views": channel_views,
        }
        return {
            "video_id": video_id,
            "corner": corner,
            "published_at": published_at,
            "history_ts": published_at,
            "format_traits": traits
            or ["tier:longform", "duration:180s_or_more"],
            "channel_page_share": channel_page_share,
        }

    def _snapshot(self, videos: list[dict]) -> dict:
        grouped: dict[str, list[dict]] = {}
        for row in videos:
            grouped.setdefault(str(row.get("corner") or ""), []).append(row)
        corner_plans: dict[str, dict] = {}
        for corner, corner_rows in grouped.items():
            ranked: list[tuple[float, str, dict]] = []
            unknown = False
            for row in corner_rows:
                published = history._parse_ts(
                    row.get("published_at") or row.get("history_ts")
                )
                video_id = str(row.get("video_id") or "")
                if published is None or not video_id:
                    unknown = True
                    break
                ranked.append((published.timestamp(), video_id, row))
            if unknown:
                corner_plans[corner] = {
                    "status": "unknown_timestamp",
                    "latest_video_id": "",
                    "cohort": "",
                    "peer_video_ids": [],
                    "missing_detail_video_ids": [],
                }
                continue
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            latest = ranked[0][2]
            latest_id = str(latest.get("video_id") or "")
            cohort = performance._format_cohort(latest)
            if not cohort:
                corner_plans[corner] = {
                    "status": "cohort_unavailable",
                    "latest_video_id": latest_id,
                    "cohort": "",
                    "peer_video_ids": [],
                    "missing_detail_video_ids": [],
                }
                continue
            peers = [
                video_id
                for _published, video_id, row in ranked[1:]
                if performance._format_cohort(row) == cohort
            ][: performance.CHANNEL_PAGE_SHARE_MAX_PEERS]
            corner_plans[corner] = {
                "status": "ready",
                "latest_video_id": latest_id,
                "cohort": cohort,
                "peer_video_ids": peers,
                "missing_detail_video_ids": [],
            }
        return {
            "channel_page_share": {
                "available": True,
                "window_days": performance.CHANNEL_PAGE_SHARE_WINDOW_DAYS,
                "min_total_views": performance.CHANNEL_PAGE_SHARE_MIN_VIEWS,
                "data_through_date": "2026-07-31",
                "corners": corner_plans,
            },
            "videos": videos,
        }

    def _comparison_rows(
        self,
        *,
        latest_views: int = 10,
        corner: str = "capitalism",
    ) -> list[dict]:
        return [
            self._row(
                "peer-20",
                "2026-07-20T00:00:00+00:00",
                20,
                corner=corner,
            ),
            self._row(
                "peer-30",
                "2026-07-21T00:00:00+00:00",
                30,
                corner=corner,
            ),
            self._row(
                "peer-40",
                "2026-07-22T00:00:00+00:00",
                40,
                corner=corner,
            ),
            self._row(
                "latest",
                "2026-07-25T00:00:00+00:00",
                latest_views,
                corner=corner,
            ),
        ]

    def _section(self, spec: SimpleNamespace, corner: str) -> dict:
        decision = {
            "decision_id": "dec-channel-page",
            "status": "insufficient_data",
            "reason": "比較可能な動画が2本",
            "metric": "youtube_analytics_api_v2.average_view_percentage",
            "format_cohort": "",
            "eligible_video_ids": [],
            "top_video_ids": [],
            "bottom_video_ids": [],
            "positive_traits": [],
            "negative_traits": [],
        }
        return performance_report.build_corner_section(
            spec, corner, decision, [], set()
        )

    def test_latest_share_uses_same_cohort_peers_and_proposes_one_change(
        self,
    ) -> None:
        rows = self._comparison_rows()
        rows.extend(
            [
                self._row(
                    "different-cohort",
                    "2026-07-23T00:00:00+00:00",
                    90,
                    traits=["tier:short", "duration:under_60s"],
                ),
                self._row(
                    "different-corner",
                    "2026-07-24T00:00:00+00:00",
                    95,
                    corner="communism",
                ),
            ]
        )
        snapshot = self._snapshot(rows)

        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )

        self.assertEqual(analysis["status"], "compared")
        self.assertTrue(analysis["actionable"])
        self.assertEqual(analysis["video_id"], "latest")
        self.assertEqual(analysis["peer_count"], 3)
        self.assertAlmostEqual(analysis["peer_median_ratio"], 0.30)
        self.assertAlmostEqual(analysis["difference_points"], -20.0)
        self.assertEqual(analysis["top_peer"]["video_id"], "peer-40")
        self.assertIn("チャンネルページ流入割合: 10.0%", text)
        self.assertIn("直近peer 3本の中央値 30.0%", text)
        self.assertIn("差 -20.0ポイント", text)
        self.assertIn("peer上位 `peer-40`（40.0%）", text)
        self.assertIn("StudioのYT_CHANNEL詳細", text)
        self.assertIn("比較条件を揃えられない場合は変更しない", text)
        self.assertIn("タイトルまたはサムネイルの一方だけ", text)
        self.assertNotIn("different-cohort", text)
        self.assertNotIn("different-corner", text)

    def test_higher_latest_preserves_packaging(self) -> None:
        snapshot = self._snapshot(self._comparison_rows(latest_views=50))

        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )

        self.assertTrue(analysis["actionable"])
        self.assertAlmostEqual(analysis["difference_points"], 20.0)
        self.assertIn("組み合わせをこの指標だけでは変更せず", text)
        self.assertNotIn("タイトルまたはサムネイルの一方だけを手動で変えて", text)

    def test_exact_five_point_boundary_is_actionable(self) -> None:
        rows = [
            self._row(
                f"peer-{index}",
                f"2026-07-{20 + index:02d}T00:00:00+00:00",
                30,
            )
            for index in range(3)
        ]
        rows.append(
            self._row("latest", "2026-07-25T00:00:00+00:00", 25)
        )

        analysis = performance_report._channel_page_share_analysis(
            self._snapshot(rows), "capitalism"
        )

        self.assertAlmostEqual(analysis["difference_points"], -5.0)
        self.assertTrue(analysis["actionable"])

    def test_twenty_views_one_count_boundary_is_held(self) -> None:
        rows = [
            self._row(
                f"peer-{index}",
                f"2026-07-{20 + index:02d}T00:00:00+00:00",
                6,
                total_views=20,
            )
            for index in range(3)
        ]
        rows.append(
            self._row(
                "latest",
                "2026-07-25T00:00:00+00:00",
                5,
                total_views=20,
            )
        )

        analysis = performance_report._channel_page_share_analysis(
            self._snapshot(rows), "capitalism"
        )
        text = performance_report._channel_page_share_text(
            self._snapshot(rows), "capitalism"
        )

        self.assertEqual(analysis["status"], "latest_metric_unavailable")
        self.assertFalse(analysis["actionable"])
        self.assertIn("全viewsが100未満", text)

    def test_latest_incomplete_seven_day_window_is_held(self) -> None:
        rows = self._comparison_rows()
        rows[-1]["channel_page_share"].update(
            {
                "status": "window_incomplete",
                "views": None,
                "channel_page_views": None,
                "data_through_date": "2026-07-29",
            }
        )

        analysis = performance_report._channel_page_share_analysis(
            self._snapshot(rows), "capitalism"
        )
        text = performance_report._channel_page_share_text(
            self._snapshot(rows), "capitalism"
        )

        self.assertEqual(analysis["status"], "latest_window_incomplete")
        self.assertFalse(analysis["actionable"])
        self.assertIn("期間が揃うまで評価しません", text)
        self.assertIn("`2026-07-29`", text)

    def test_subthreshold_difference_does_not_change_or_create_candidate(
        self,
    ) -> None:
        spec = SimpleNamespace(id="ideology")
        rows = self._comparison_rows(latest_views=33)
        snapshot = self._snapshot(rows)
        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )
        candidate = performance_report.build_cycle_candidate(
            spec,
            [self._section(spec, "capitalism")],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertFalse(analysis["actionable"])
        self.assertIn("5.0ポイント未満", text)
        self.assertIn("この指標からは変更しません", text)
        self.assertIsNone(candidate)

    def test_latest_missing_metric_does_not_fallback_to_old_video(self) -> None:
        rows = self._comparison_rows()
        rows[-1] = self._row(
            "latest",
            "2026-07-25T00:00:00+00:00",
            None,
        )
        snapshot = self._snapshot(rows)

        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )

        self.assertEqual(analysis["status"], "latest_metric_unavailable")
        self.assertFalse(analysis["actionable"])
        self.assertIn("`latest`（このcornerの最新動画）", text)
        self.assertIn("旧動画へfallbackせず", text)
        self.assertIn("欠落を0とみなしません", text)
        self.assertNotIn("`peer-40`", text)

    def test_latest_missing_data_api_detail_does_not_promote_old_video(
        self,
    ) -> None:
        snapshot = self._snapshot(self._comparison_rows())
        snapshot["channel_page_share"]["corners"]["capitalism"] = {
            "status": "latest_detail_unavailable",
            "latest_video_id": "history-latest-missing",
            "cohort": "duration:180s_or_more|tier:longform",
            "peer_video_ids": [],
            "missing_detail_video_ids": ["history-latest-missing"],
        }

        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )

        self.assertEqual(analysis["status"], "latest_detail_unavailable")
        self.assertFalse(analysis["actionable"])
        self.assertIn("`history-latest-missing`", text)
        self.assertIn("旧動画を最新へ繰り上げず", text)
        self.assertNotIn("10.0%", text)

    def test_unknown_timestamp_does_not_guess_latest(self) -> None:
        rows = self._comparison_rows()
        rows[0]["published_at"] = ""
        rows[0]["history_ts"] = ""

        analysis = performance_report._channel_page_share_analysis(
            self._snapshot(rows), "capitalism"
        )
        text = performance_report._channel_page_share_text(
            self._snapshot(rows), "capitalism"
        )

        self.assertEqual(analysis["status"], "unknown_latest")
        self.assertIn("最新動画を推測せず", text)

    def test_insufficient_peer_and_missing_cohort_are_fail_closed(self) -> None:
        rows = self._comparison_rows()[-2:]
        analysis = performance_report._channel_page_share_analysis(
            self._snapshot(rows), "capitalism"
        )
        text = performance_report._channel_page_share_text(
            self._snapshot(rows), "capitalism"
        )
        self.assertEqual(analysis["status"], "insufficient_peers")
        self.assertIn("有効peer 1本 / 必要 3本", text)

        rows[-1]["format_traits"] = ["tier:longform"]
        no_cohort = performance_report._channel_page_share_analysis(
            self._snapshot(rows), "capitalism"
        )
        no_cohort_text = performance_report._channel_page_share_text(
            self._snapshot(rows), "capitalism"
        )
        self.assertEqual(no_cohort["status"], "cohort_unavailable")
        self.assertIn("尺/tierを確認できない", no_cohort_text)

    def test_missing_recent_peer_does_not_fallback_to_older_valid_peer(
        self,
    ) -> None:
        rows = [
            self._row(
                "older-valid",
                "2026-07-16T00:00:00+00:00",
                90,
            ),
            self._row(
                "window-valid-0",
                "2026-07-17T00:00:00+00:00",
                20,
            ),
            self._row(
                "recent-missing",
                "2026-07-18T00:00:00+00:00",
                None,
            ),
            self._row(
                "recent-valid-1",
                "2026-07-19T00:00:00+00:00",
                30,
            ),
            self._row(
                "recent-valid-2",
                "2026-07-20T00:00:00+00:00",
                40,
            ),
            self._row(
                "recent-valid-3",
                "2026-07-21T00:00:00+00:00",
                50,
            ),
            self._row(
                "latest",
                "2026-07-25T00:00:00+00:00",
                10,
            ),
        ]
        snapshot = self._snapshot(rows)

        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )

        self.assertEqual(analysis["status"], "peer_metric_unavailable")
        self.assertFalse(analysis["actionable"])
        self.assertEqual(analysis["peer_count"], 5)
        self.assertEqual(analysis["missing_peer_count"], 1)
        self.assertIn("欠落peerを除外したり", text)
        self.assertIn("古い動画へ置換したりせず", text)
        self.assertIn("selection bias", text)
        self.assertNotIn("peer上位", text)
        self.assertNotIn("90.0%", text)

    def test_missing_peer_data_api_detail_keeps_fixed_peer_id(self) -> None:
        rows = self._comparison_rows()
        rows.insert(
            0,
            self._row(
                "older-valid",
                "2026-07-19T00:00:00+00:00",
                90,
            ),
        )
        snapshot = self._snapshot(rows)
        snapshot["channel_page_share"]["corners"]["capitalism"][
            "peer_video_ids"
        ] = ["peer-40", "peer-30", "missing-detail"]

        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )

        self.assertEqual(analysis["status"], "peer_metric_unavailable")
        self.assertFalse(analysis["actionable"])
        self.assertEqual(analysis["peer_count"], 3)
        self.assertEqual(analysis["missing_peer_count"], 1)
        self.assertIn("selection bias", text)
        self.assertNotIn("90.0%", text)

    def test_invalid_counts_are_rejected_but_explicit_zero_is_valid(self) -> None:
        base = self._row(
            "v", "2026-07-25T00:00:00+00:00", 0, total_views=100
        )
        metrics = performance_report._channel_page_share_metrics(base)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["share_ratio"], 0.0)

        cases = [
            self._row("v", "2026-07-25T00:00:00+00:00", None),
            self._row("v", "2026-07-25T00:00:00+00:00", 21, total_views=20),
            self._row("v", "2026-07-25T00:00:00+00:00", True),
            self._row("v", "2026-07-25T00:00:00+00:00", 2.5),
            self._row("v", "2026-07-25T00:00:00+00:00", 2, total_views=19),
            self._row("v", "2026-07-25T00:00:00+00:00", 2, total_views=float("nan")),
        ]
        for row in cases:
            with self.subTest(row=row):
                self.assertIsNone(
                    performance_report._channel_page_share_metrics(row)
                )

    def test_dedicated_readback_failure_is_reported_without_using_row_data(
        self,
    ) -> None:
        snapshot = self._snapshot(self._comparison_rows())
        snapshot["channel_page_share"].update(
            {"available": False, "reason": "quota exceeded"}
        )

        analysis = performance_report._channel_page_share_analysis(
            snapshot, "capitalism"
        )
        text = performance_report._channel_page_share_text(
            snapshot, "capitalism"
        )

        self.assertEqual(analysis["status"], "channel_page_unavailable")
        self.assertIn("quota exceeded", text)
        self.assertIn("推測で補いません", text)
        self.assertNotIn("10.0%", text)

    def test_actionable_signal_creates_candidate_for_non_source_corner(
        self,
    ) -> None:
        spec = SimpleNamespace(id="ideology")
        snapshot = self._snapshot(self._comparison_rows())

        candidate = performance_report.build_cycle_candidate(
            spec,
            [self._section(spec, "capitalism")],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertIsNotNone(candidate)
        self.assertIn("チャンネルページ流入割合と次の1本", candidate["body"])
        self.assertIn("`latest`（このcornerの最新動画）", candidate["body"])
        self.assertIn("自分または他チャンネルのページ", candidate["body"])
        self.assertIn("CTR・維持率とも別指標", candidate["body"])

    def test_actionable_signal_changes_fingerprint_with_latest_video(self) -> None:
        spec = SimpleNamespace(id="ideology")
        section = self._section(spec, "capitalism")
        first = self._snapshot(self._comparison_rows())
        second_rows = self._comparison_rows()
        second_rows[-1]["video_id"] = "latest-next"
        second = self._snapshot(second_rows)
        corrected_counts = self._snapshot(
            self._comparison_rows(latest_views=11)
        )
        changed_peer_rows = self._comparison_rows()
        changed_peer_rows[0]["video_id"] = "peer-replaced"
        changed_peer = self._snapshot(changed_peer_rows)

        first_candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            first,
        )
        same_candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            first,
        )
        second_candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            second,
        )
        corrected_candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            corrected_counts,
        )
        changed_peer_candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            changed_peer,
        )

        self.assertIsNotNone(first_candidate)
        self.assertIsNotNone(same_candidate)
        self.assertIsNotNone(second_candidate)
        self.assertIsNotNone(corrected_candidate)
        self.assertIsNotNone(changed_peer_candidate)
        self.assertEqual(
            first_candidate["fingerprint"], same_candidate["fingerprint"]
        )
        self.assertNotEqual(
            first_candidate["fingerprint"], second_candidate["fingerprint"]
        )
        self.assertEqual(
            first_candidate["fingerprint"],
            corrected_candidate["fingerprint"],
        )
        self.assertNotEqual(
            first_candidate["fingerprint"],
            changed_peer_candidate["fingerprint"],
        )


class RunChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        pipeline = {
            "performance_feedback": True,
            "feedback_repository": "azumag/doci",
        }
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=self.root,
            corners={"shorts": None, "video": None},
            pipeline=pipeline,
            pipeline_get=pipeline.get,
        )

    def _snapshot(self) -> dict:
        return {"collected_at": "2026-07-26T00:00:00+00:00", "videos": []}

    def test_disabled_channel_is_skipped(self) -> None:
        spec = SimpleNamespace(
            id="x",
            output_dir=self.root,
            pipeline={},
            pipeline_get={}.get,
        )
        result = performance_report.run_channel(spec, apply=True)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "performance_feedback_disabled")

    def test_no_repository_is_skipped(self) -> None:
        pipeline = {"performance_feedback": True}
        spec = SimpleNamespace(
            id="x", output_dir=self.root, pipeline=pipeline, pipeline_get=pipeline.get
        )
        result = performance_report.run_channel(spec, apply=True)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_repository")

    def test_interval_gate_skips_recent_apply_run(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        performance_report._save_state(
            self.spec, {"last_run_at": (now - timedelta(hours=1)).isoformat()}
        )
        with patch.object(performance, "sync") as sync_mock:
            result = performance_report.run_channel(self.spec, now=now, apply=True)
        sync_mock.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "interval_not_elapsed")

    def test_interval_gate_does_not_block_dry_run(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        performance_report._save_state(
            self.spec, {"last_run_at": (now - timedelta(hours=1)).isoformat()}
        )
        with patch.object(performance, "sync", return_value=self._snapshot()):
            result = performance_report.run_channel(self.spec, now=now, apply=False)
        self.assertNotEqual(result.get("reason"), "interval_not_elapsed")

    def test_dry_run_creates_no_experiment_or_state_files(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        with (
            patch.object(performance, "sync", return_value=self._snapshot()),
            patch.object(feedback_issues, "submit_candidate") as submit_mock,
        ):
            performance_report.run_channel(self.spec, now=now, apply=False)
        submit_mock.assert_not_called()  # no_content: insufficient_data止まりで候補自体が作られない
        self.assertFalse(performance_report._experiments_path(self.spec).exists())
        self.assertFalse(performance_report._state_path(self.spec).exists())

    def test_apply_records_proposed_experiments_after_issue_created(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        videos = []
        for index in range(8):
            upper = index >= 6
            videos.append(
                {
                    "video_id": f"id-{index}",
                    "corner": "video",
                    "format_traits": (
                        ["tier:long_short", "duration:60_to_179s", "chart:present"]
                        if upper
                        else ["tier:long_short", "duration:60_to_179s", "chart:absent"]
                    ),
                    "privacy_status": "unlisted",
                    "data_api": {"views": 100},
                    "analytics": {"views": 100, "average_view_percentage": 40 + index * 5},
                }
            )
        snapshot = {"collected_at": now.isoformat(), "videos": videos}
        created = {"number": 123, "url": "https://github.com/azumag/doci/issues/123"}
        with (
            patch.object(performance, "sync", return_value=snapshot),
            patch.object(
                feedback_issues,
                "submit_candidate",
                return_value={"mode": "apply", "created": created, "skip_reason": ""},
            ),
        ):
            result = performance_report.run_channel(self.spec, now=now, apply=True)

        self.assertEqual(result["status"], "submitted")
        rows = performance_report._read_experiments(self.spec)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "proposed")
        self.assertEqual(rows[0]["corner"], "video")
        self.assertEqual(rows[0]["trait"], "chart:present")
        state = performance_report._load_state(self.spec)
        self.assertEqual(state["last_run_at"], now.isoformat())

    def test_apply_does_not_record_proposal_when_issue_not_created(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        videos = [
            {
                "video_id": f"id-{index}",
                "corner": "video",
                "format_traits": (
                    ["tier:long_short", "duration:60_to_179s", "chart:present"]
                    if index >= 6
                    else ["tier:long_short", "duration:60_to_179s", "chart:absent"]
                ),
                "privacy_status": "unlisted",
                "data_api": {"views": 100},
                "analytics": {"views": 100, "average_view_percentage": 40 + index * 5},
            }
            for index in range(8)
        ]
        snapshot = {"collected_at": now.isoformat(), "videos": videos}
        with (
            patch.object(performance, "sync", return_value=snapshot),
            patch.object(
                feedback_issues,
                "submit_candidate",
                return_value={"mode": "apply", "created": None, "skip_reason": "weekly_limit_reached"},
            ),
        ):
            result = performance_report.run_channel(self.spec, now=now, apply=True)
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(performance_report._experiments_path(self.spec).exists())

    def test_dry_run_status_is_distinct_from_submitted(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        videos = [
            {
                "video_id": f"id-{index}",
                "corner": "video",
                "format_traits": (
                    ["tier:long_short", "duration:60_to_179s", "chart:present"]
                    if index >= 6
                    else ["tier:long_short", "duration:60_to_179s", "chart:absent"]
                ),
                "privacy_status": "unlisted",
                "data_api": {"views": 100},
                "analytics": {"views": 100, "average_view_percentage": 40 + index * 5},
            }
            for index in range(8)
        ]
        snapshot = {"collected_at": now.isoformat(), "videos": videos}
        with patch.object(performance, "sync", return_value=snapshot):
            result = performance_report.run_channel(self.spec, now=now, apply=False)
        self.assertEqual(result["status"], "dry_run")
        self.assertIn("body", result["submission"]["candidate"])


class RunAllTest(unittest.TestCase):
    def test_one_channel_failure_does_not_stop_others(self) -> None:
        specs = {
            "alpha": SimpleNamespace(
                id="alpha",
                output_dir=Path("/tmp/doesnotmatter-alpha"),
                corners={},
                pipeline={},
                pipeline_get={}.get,
            ),
        }

        def fake_load(channel_id):
            if channel_id == "broken":
                raise ValueError("bad config")
            return specs[channel_id]

        with (
            patch.object(channel, "discover", return_value=["alpha", "broken"]),
            patch.object(channel, "load", side_effect=fake_load),
        ):
            summary, exit_code = performance_report.run_all(apply=False)

        self.assertEqual(exit_code, 0)
        statuses = {row["channel"]: row["status"] for row in summary["channels"]}
        self.assertEqual(statuses["broken"], "error")
        self.assertEqual(statuses["alpha"], "skipped")  # performance_feedback未設定


class ShareRateTest(unittest.TestCase):
    """issue #144: 共有率（shares/views）と1%超動画の構造表示。"""

    def test_share_text_lists_rate_and_over_one_percent_structure(self) -> None:
        snapshot = {
            "videos": [
                {
                    "video_id": "s1",
                    "corner": "shorts",
                    "format_traits": ["tier:short", "duration:under_60s", "scenes:1_to_4"],
                    "share_30d": {"views": 500, "shares": 8},
                },
                {
                    "video_id": "s2",
                    "corner": "shorts",
                    "format_traits": ["tier:short"],
                    "share_30d": {"views": 1000, "shares": 3},
                },
            ]
        }
        text = performance_report._share_text(snapshot, "shorts")
        self.assertIn("共有率 1.600%（共有 8 / 再生 500）", text)
        self.assertIn("共有率1%超の動画の構造", text)
        self.assertIn("`s1`", text)
        self.assertIn("構造: tier:short, duration:under_60s, scenes:1_to_4", text)
        # 1%以下の動画は件数要約に留める。
        self.assertIn("他 1 本は共有率1%以下", text)
        self.assertIn("再生数だけの評価を避けるための補助指標", text)

    def test_share_text_prefers_trait_videos_in_limit(self) -> None:
        """issue #144 (Sol review指摘): 表示上限内では構造付き（format_traits
        あり）動画を最優先し、候補化の根拠となった動画を必ず本文へ出す。"""
        videos = []
        for index in range(21):
            videos.append(
                {
                    "video_id": f"no-trait-{index}",
                    "corner": "shorts",
                    "share_30d": {"views": 100, "shares": 5},
                }
            )
        # 末尾に構造付き1本（候補化の根拠）。
        videos.append(
            {
                "video_id": "with-trait",
                "corner": "shorts",
                "format_traits": ["tier:short", "scenes:1_to_4"],
                "share_30d": {"views": 100, "shares": 5},
            }
        )
        text = performance_report._share_text({"videos": videos}, "shorts")
        self.assertIn("`with-trait`", text)
        self.assertIn("構造: tier:short, scenes:1_to_4", text)

    def test_share_text_summarizes_no_trait_videos(self) -> None:
        """issue #144 (Sol review指摘): 構造未記録の1%超動画だけの場合は
        個別ID・構造見出しを出さず、件数要約に留める。"""
        snapshot = {
            "videos": [
                {
                    "video_id": "no-trait",
                    "corner": "shorts",
                    "share_30d": {"views": 100, "shares": 5},
                }
            ]
        }
        text = performance_report._share_text(snapshot, "shorts")
        self.assertIn("構造未記録の共有率1%超: 1 本", text)
        self.assertNotIn("`no-trait`", text)
        self.assertNotIn("共有率1%超の動画の構造", text)

    def test_share_text_shows_up_to_five_below_one_percent(self) -> None:
        """issue #144 (Sol review指摘): 1%超の動画が無い場合、1%以下の動画を
        最大5本まで参考表示する（1本なら個別表示、6本なら5本＋要約）。"""
        one = {
            "videos": [
                {
                    "video_id": "low-1",
                    "corner": "shorts",
                    "share_30d": {"views": 1000, "shares": 3},
                }
            ]
        }
        text_one = performance_report._share_text(one, "shorts")
        self.assertIn("`low-1`", text_one)
        self.assertIn("共有率 0.300%", text_one)

        six_videos = {
            "videos": [
                {
                    "video_id": f"low-{index}",
                    "corner": "shorts",
                    "share_30d": {"views": 1000, "shares": 3},
                }
                for index in range(6)
            ]
        }
        text_six = performance_report._share_text(six_videos, "shorts")
        for index in range(5):
            self.assertIn(f"`low-{index}`", text_six)
        self.assertNotIn("`low-5`", text_six)
        self.assertIn("他 1 本は共有率1%以下", text_six)

    def test_share_text_does_not_list_below_when_no_trait_over_exists(self) -> None:
        """issue #144 (Sol review指摘): 構造未記録の1%超動画が存在する場合は
        1%以下動画の参考表示を出さず、件数要約に留める。"""
        videos = [
            {
                "video_id": "no-trait-over",
                "corner": "shorts",
                "share_30d": {"views": 100, "shares": 5},
            }
        ]
        videos.extend(
            {
                "video_id": f"low-{index}",
                "corner": "shorts",
                "share_30d": {"views": 1000, "shares": 3},
            }
            for index in range(6)
        )
        text = performance_report._share_text({"videos": videos}, "shorts")
        self.assertIn("構造未記録の共有率1%超: 1 本", text)
        self.assertNotIn("`low-0`", text)
        self.assertIn("他 6 本は共有率1%以下", text)

    def test_share_text_is_fail_closed_when_missing(self) -> None:
        snapshot = {
            "videos": [
                {"video_id": "v1", "corner": "shorts"}
            ]
        }
        text = performance_report._share_text(snapshot, "shorts")
        self.assertIn("算出できませんでした", text)
        self.assertIn("推測で補いません", text)

    def test_share_text_skips_zero_views(self) -> None:
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "shorts",
                    "share_30d": {"views": 0, "shares": 5},
                }
            ]
        }
        text = performance_report._share_text(snapshot, "shorts")
        self.assertIn("算出できませんでした", text)

    def test_share_text_without_snapshot_is_fail_closed(self) -> None:
        text = performance_report._share_text(None, "shorts")
        self.assertIn("snapshot未取得", text)

    def test_share_text_ignores_non_shorts_corner(self) -> None:
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "share_30d": {"views": 100, "shares": 2},
                }
            ]
        }
        # video cornerの共有率は対象外（shortsのみ）。
        text = performance_report._share_text(snapshot, "video")
        self.assertIn("shorts のみ対象", text)

    def test_share_metrics_rejects_invalid_values(self) -> None:
        self.assertIsNone(performance_report._share_metrics({}))
        self.assertIsNone(
            performance_report._share_metrics(
                {"share_30d": {"views": 100, "shares": None}}
            )
        )
        self.assertIsNone(
            performance_report._share_metrics(
                {"share_30d": {"views": "unknown", "shares": 1}}
            )
        )
        self.assertIsNone(
            performance_report._share_metrics(
                {"share_30d": {"views": 100, "shares": -1}}
            )
        )
        self.assertIsNone(
            performance_report._share_metrics(
                {"share_30d": {"views": True, "shares": 1}}
            )
        )
        self.assertIsNone(
            performance_report._share_metrics(
                {"share_30d": {"views": 100.9, "shares": 2.9}}
            )
        )
        self.assertEqual(
            performance_report._share_metrics(
                {"share_30d": {"views": 100, "shares": 2}}
            ),
            (2, 100),
        )

    def test_share_one_percent_boundary(self) -> None:
        # 1%ちょうど（shares*100 == views）は超えない。
        exactly = {"share_30d": {"views": 200, "shares": 2}}
        self.assertFalse(performance_report._is_share_over_one_percent(exactly))
        # 直上（shares*100 > views）は超える。
        just_over = {"share_30d": {"views": 200, "shares": 3}}
        self.assertTrue(performance_report._is_share_over_one_percent(just_over))
        # 直下（shares*100 < views）は超えない。
        just_under = {"share_30d": {"views": 200, "shares": 1}}
        self.assertFalse(performance_report._is_share_over_one_percent(just_under))


class RetentionCurveAnalysisTest(unittest.TestCase):
    """issue #127/#142/#149: 冒頭低下と山/谷の分析・レポート表示。"""

    def test_opening_retention_signal_detects_monotonic_drop_and_scene(self) -> None:
        """山/谷のない単調低下でも、冒頭30秒の累計・最大区間低下を返す。"""
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
            {"elapsed_ratio": 0.1, "watch_ratio": 0.92},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.70},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.50},
        ]
        script = {
            "scenes": [
                {"caption": "導入"},
                {"caption": "問題提起"},
                {"caption": "展開"},
                {"caption": "結び"},
            ]
        }

        signal = performance.opening_retention_signal(curve, "PT100S", script)

        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])
        self.assertAlmostEqual(signal["cumulative_drop_ratio"], 0.25)
        self.assertAlmostEqual(signal["largest_step_drop_ratio"], 0.20)
        self.assertEqual(signal["drop_from_seconds"], 20.0)
        self.assertEqual(signal["drop_to_seconds"], 30.0)
        self.assertEqual(signal["scene_index"], 1)
        self.assertEqual(signal["scene_caption"], "問題提起")

    def test_opening_retention_signal_ignores_drop_after_30_seconds(self) -> None:
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.92},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.4, "watch_ratio": 0.50},
        ]

        signal = performance.opening_retention_signal(curve, "PT100S")

        self.assertIsNotNone(signal)
        self.assertFalse(signal["actionable"])
        self.assertAlmostEqual(signal["cumulative_drop_ratio"], 0.05)
        self.assertAlmostEqual(signal["largest_step_drop_ratio"], 0.03)
        self.assertEqual(signal["end_seconds"], 30.0)

    def test_opening_retention_signal_uses_full_short_video(self) -> None:
        signal = performance.opening_retention_signal(
            [
                {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                {"elapsed_ratio": 0.5, "watch_ratio": 0.88},
                {"elapsed_ratio": 1.0, "watch_ratio": 0.75},
            ],
            "PT20S",
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["window_seconds"], 20.0)
        self.assertEqual(signal["end_seconds"], 20.0)
        self.assertTrue(signal["actionable"])

    def test_opening_retention_signal_includes_exact_threshold(self) -> None:
        signal = performance.opening_retention_signal(
            [
                {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                {"elapsed_ratio": 0.3, "watch_ratio": 0.87},
            ],
            "PT100S",
        )

        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])

    def test_opening_retention_signal_rejects_conflicting_duplicate_points(self) -> None:
        """同一時点の異値重複は、位置や入力順に関わらずfail-closedにする。"""
        base = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
            {"elapsed_ratio": 0.15, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.75},
        ]
        for index, point in enumerate(base):
            conflicting = {
                "elapsed_ratio": point["elapsed_ratio"],
                "watch_ratio": point["watch_ratio"] - 0.20,
            }
            for duplicate_first in (False, True):
                curve = list(base)
                insert_at = index if duplicate_first else index + 1
                curve.insert(insert_at, conflicting)
                with self.subTest(index=index, duplicate_first=duplicate_first):
                    self.assertIsNone(
                        performance.opening_retention_signal(curve, "PT100S")
                    )

    def test_opening_retention_signal_merges_identical_duplicate_points(self) -> None:
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
            {"elapsed_ratio": 0.15, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.15, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.75},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.75},
        ]

        signal = performance.opening_retention_signal(curve, "PT100S")

        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])
        self.assertAlmostEqual(signal["cumulative_drop_ratio"], 0.20)

    def test_opening_retention_signal_is_fail_closed(self) -> None:
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.70},
        ]
        self.assertIsNone(performance.opening_retention_signal(curve, None))
        self.assertIsNone(performance.opening_retention_signal(curve, "PT0S"))
        self.assertIsNone(
            performance.opening_retention_signal(
                [{"elapsed_ratio": 0.0, "watch_ratio": 0.95}], "PT1M"
            )
        )
        self.assertIsNone(
            performance.opening_retention_signal(curve, "PT1M", window_seconds=0)
        )

    def test_retention_flat_regions_detects_long_flat_segment(self) -> None:
        """issue #117: 変化量が小さいまま続く区間を平坦区間として検出する。"""
        curve = [
            {"elapsed_ratio": 0.00, "watch_ratio": 0.95},
            {"elapsed_ratio": 0.10, "watch_ratio": 0.60},  # 冒頭の急落
            {"elapsed_ratio": 0.20, "watch_ratio": 0.58},
            {"elapsed_ratio": 0.35, "watch_ratio": 0.59},
            {"elapsed_ratio": 0.50, "watch_ratio": 0.58},
            {"elapsed_ratio": 0.65, "watch_ratio": 0.57},
            {"elapsed_ratio": 0.80, "watch_ratio": 0.56},
            {"elapsed_ratio": 0.90, "watch_ratio": 0.30},  # 終盤の急落
            {"elapsed_ratio": 1.00, "watch_ratio": 0.29},
        ]

        regions = performance.retention_flat_regions(curve)

        self.assertEqual(len(regions), 1)
        region = regions[0]
        self.assertAlmostEqual(region["start_ratio"], 0.10)
        self.assertAlmostEqual(region["end_ratio"], 0.80)
        self.assertAlmostEqual(region["span_ratio"], 0.70)
        self.assertAlmostEqual(region["avg_watch_ratio"], 0.58, places=3)

    def test_retention_flat_regions_ignores_short_or_noisy_segments(self) -> None:
        """issue #117: 幅25%未満の区間と、急な変化をまたぐ区間は対象外。"""
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.1, "watch_ratio": 0.89},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.88},  # 短い平坦区間
            {"elapsed_ratio": 0.3, "watch_ratio": 0.50},
            {"elapsed_ratio": 0.4, "watch_ratio": 0.55},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.52},
            {"elapsed_ratio": 0.6, "watch_ratio": 0.51},
            {"elapsed_ratio": 0.7, "watch_ratio": 0.90},  # 大きな変化
            {"elapsed_ratio": 0.8, "watch_ratio": 0.91},
            {"elapsed_ratio": 0.9, "watch_ratio": 0.92},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.93},
        ]

        regions = performance.retention_flat_regions(curve)

        # 0.3→0.7は中間で0.52→0.51→0.90と大きな変化があるため平坦区間にならない。
        # 0.7→1.0は幅0.3で平坦だが、0.6→0.7が大きな変化なので別区間として検出される。
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0]["start_ratio"], 0.7)
        self.assertAlmostEqual(regions[0]["end_ratio"], 1.0)

    def test_retention_flat_regions_exact_threshold_is_flat(self) -> None:
        """issue #117: 変化量がちょうどmax_slopeの点列も平坦区間として扱う。"""
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.80},
            {"elapsed_ratio": 0.25, "watch_ratio": 0.77},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.74},
            {"elapsed_ratio": 0.75, "watch_ratio": 0.71},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.68},
        ]

        regions = performance.retention_flat_regions(curve)

        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0]["span_ratio"], 1.0)

    def test_retention_flat_regions_is_fail_closed(self) -> None:
        """issue #117: 無効データ・矛盾重複・点不足は空を返す。"""
        self.assertEqual(performance.retention_flat_regions([]), [])
        self.assertEqual(
            performance.retention_flat_regions(
                [{"elapsed_ratio": 0.0, "watch_ratio": 0.9}]
            ),
            [],
        )
        self.assertEqual(
            performance.retention_flat_regions(
                [
                    {"elapsed_ratio": 0.0, "watch_ratio": "bad"},
                    {"elapsed_ratio": 0.5, "watch_ratio": 0.9},
                    {"elapsed_ratio": 1.0, "watch_ratio": 0.9},
                ]
            ),
            [],
        )
        conflicting = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.9},
            {"elapsed_ratio": 0.0, "watch_ratio": 0.5},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.9},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.9},
        ]
        self.assertEqual(
            performance.retention_flat_regions(conflicting), []
        )

    def test_retention_flat_region_scenes_annotates_scene(self) -> None:
        """issue #117: 平坦区間の中間点をシーンへ照合して秒位置を返す。"""
        regions = [
            {
                "start_ratio": 0.2,
                "end_ratio": 0.8,
                "span_ratio": 0.6,
                "start_watch_ratio": 0.58,
                "end_watch_ratio": 0.56,
                "avg_watch_ratio": 0.57,
            }
        ]
        script = {
            "scenes": [
                {"caption": "導入"},
                {"caption": "展開"},
                {"caption": "結び"},
            ]
        }

        annotated = performance.retention_flat_region_scenes(
            regions, script, "PT100S"
        )

        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0]["start_seconds"], 20.0)
        self.assertEqual(annotated[0]["end_seconds"], 80.0)
        self.assertEqual(annotated[0]["scene_index"], 1)
        self.assertEqual(annotated[0]["scene_caption"], "展開")

    def test_retention_flat_region_text_reports_longest_region(self) -> None:
        """issue #117: snapshotから最長の平坦区間を秒位置とシーンで表示する。"""
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "v-flat",
                    "corner": "video",
                    "workdir": "missing",
                    "data_api": {"duration": "PT100S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.9},
                            {"elapsed_ratio": 0.1, "watch_ratio": 0.5},
                            {"elapsed_ratio": 0.3, "watch_ratio": 0.49},
                            {"elapsed_ratio": 0.5, "watch_ratio": 0.48},
                            {"elapsed_ratio": 0.7, "watch_ratio": 0.47},
                            {"elapsed_ratio": 0.9, "watch_ratio": 0.46},
                            {"elapsed_ratio": 1.0, "watch_ratio": 0.2},
                        ]
                    },
                }
            ],
        }

        text = performance_report._retention_flat_region_text(snapshot, "video")

        self.assertIn("v-flat", text)
        self.assertIn("最長の平坦区間", text)
        self.assertIn("約10.0〜90.0秒", text)
        self.assertIn("平坦区間の長さだけで良し悪しは判定せず", text)

    def test_retention_flat_region_text_no_region_message(self) -> None:
        """issue #117: 平坦区間なし・取得不可・他cornerは分離表示する。"""
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "v-steep",
                    "corner": "shorts",
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": i / 10, "watch_ratio": 0.9 - i * 0.05}
                            for i in range(11)
                        ]
                    },
                }
            ],
        }

        shorts_text = performance_report._retention_flat_region_text(
            snapshot, "shorts"
        )
        video_text = performance_report._retention_flat_region_text(
            snapshot, "video"
        )

        self.assertIn("明瞭な平坦区間を検出しませんでした", shorts_text)
        self.assertIn("このcornerの動画がsnapshotにありません", video_text)

    def test_opening_to_midpoint_signal_uses_real_points_and_slope(self) -> None:
        """issue #125: 30秒から50%地点内の端点だけで変化と傾きを算出する。"""
        signal = performance.opening_to_midpoint_retention_signal(
            [
                {"elapsed_ratio": 0.20, "watch_ratio": 0.94},  # 24秒: 対象外
                {"elapsed_ratio": 0.25, "watch_ratio": 0.92},  # 30秒
                {"elapsed_ratio": 0.30, "watch_ratio": 0.90},  # 36秒
                {"elapsed_ratio": 0.35, "watch_ratio": 0.88},  # 42秒
                {"elapsed_ratio": 0.40, "watch_ratio": 0.86},  # 48秒
                {"elapsed_ratio": 0.45, "watch_ratio": 0.84},  # 54秒
                {"elapsed_ratio": 0.50, "watch_ratio": 0.82},  # 60秒
                {"elapsed_ratio": 0.75, "watch_ratio": 0.40},  # 対象外
            ],
            "PT2M",
        )

        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])
        self.assertEqual(signal["start_seconds"], 30.0)
        self.assertEqual(signal["end_seconds"], 60.0)
        self.assertEqual(signal["observed_points"], 6)
        self.assertAlmostEqual(signal["decline_ratio"], 0.10)
        self.assertAlmostEqual(signal["slope_ratio_per_10_seconds"], -1 / 30)
        self.assertAlmostEqual(signal["coverage_ratio"], 1.0)
        self.assertEqual(signal["max_observed_gap_seconds"], 6.0)

    def test_opening_to_midpoint_signal_includes_exact_threshold(self) -> None:
        signal = performance.opening_to_midpoint_retention_signal(
            [
                {"elapsed_ratio": 0.25, "watch_ratio": 0.90},
                {"elapsed_ratio": 0.3125, "watch_ratio": 0.88},
                {"elapsed_ratio": 0.375, "watch_ratio": 0.86},
                {"elapsed_ratio": 0.4375, "watch_ratio": 0.84},
                {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
            ],
            "PT2M",
        )

        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])
        self.assertAlmostEqual(signal["decline_ratio"], 0.08)

    def test_opening_to_midpoint_signal_is_fail_closed(self) -> None:
        curve = [
            {"elapsed_ratio": 0.50, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.50, "watch_ratio": 0.70},
        ]
        self.assertIsNone(
            performance.opening_to_midpoint_retention_signal(curve, "PT60S")
        )
        self.assertIsNone(
            performance.opening_to_midpoint_retention_signal(curve, None)
        )
        self.assertIsNone(
            performance.opening_to_midpoint_retention_signal(curve, "PT2M")
        )
        self.assertIsNone(
            performance.opening_to_midpoint_retention_signal(
                [{"elapsed_ratio": 0.25, "watch_ratio": 0.90}], "PT2M"
            )
        )

    def test_opening_to_midpoint_signal_rejects_sparse_or_missing_coverage(
        self,
    ) -> None:
        cases = {
            "middle_only": [
                {"elapsed_ratio": 0.49, "watch_ratio": 0.90},
                {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
            ],
            "missing_start": [
                {"elapsed_ratio": 0.35, "watch_ratio": 0.90},
                {"elapsed_ratio": 0.40, "watch_ratio": 0.87},
                {"elapsed_ratio": 0.45, "watch_ratio": 0.84},
                {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
            ],
            "missing_end": [
                {"elapsed_ratio": 0.25, "watch_ratio": 0.90},
                {"elapsed_ratio": 0.30, "watch_ratio": 0.87},
                {"elapsed_ratio": 0.35, "watch_ratio": 0.84},
                {"elapsed_ratio": 0.40, "watch_ratio": 0.82},
            ],
            "large_middle_gap": [
                {"elapsed_ratio": 0.25, "watch_ratio": 0.90},
                {"elapsed_ratio": 0.30, "watch_ratio": 0.88},
                {"elapsed_ratio": 0.45, "watch_ratio": 0.84},
                {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
            ],
        }
        for name, curve in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(
                    performance.opening_to_midpoint_retention_signal(
                        curve, "PT2M"
                    )
                )

    def test_opening_to_midpoint_signal_rejects_any_invalid_curve_point(
        self,
    ) -> None:
        valid = [
            {"elapsed_ratio": 0.25, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.30, "watch_ratio": 0.88},
            {"elapsed_ratio": 0.35, "watch_ratio": 0.86},
            {"elapsed_ratio": 0.40, "watch_ratio": 0.84},
            {"elapsed_ratio": 0.45, "watch_ratio": 0.83},
            {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
        ]
        invalid_points = [
            {"elapsed_ratio": 0.375, "watch_ratio": "bad"},
            {"elapsed_ratio": 0.375, "watch_ratio": float("nan")},
            {"elapsed_ratio": 0.375, "watch_ratio": -0.1},
            {"elapsed_ratio": "bad", "watch_ratio": 0.85},
        ]
        for invalid in invalid_points:
            with self.subTest(invalid=invalid):
                self.assertIsNone(
                    performance.opening_to_midpoint_retention_signal(
                        [*valid, invalid], "PT2M"
                    )
                )

    def test_opening_to_midpoint_signal_merges_only_identical_duplicates(
        self,
    ) -> None:
        base = [
            {"elapsed_ratio": 0.25, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.30, "watch_ratio": 0.88},
            {"elapsed_ratio": 0.35, "watch_ratio": 0.86},
            {"elapsed_ratio": 0.40, "watch_ratio": 0.84},
            {"elapsed_ratio": 0.45, "watch_ratio": 0.83},
            {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
        ]
        identical = [*base, dict(base[2])]
        signal = performance.opening_to_midpoint_retention_signal(
            identical, "PT2M"
        )
        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])

        conflicting = [
            *base,
            {"elapsed_ratio": 0.35, "watch_ratio": 0.50},
        ]
        self.assertIsNone(
            performance.opening_to_midpoint_retention_signal(
                conflicting, "PT2M"
            )
        )

    def test_tts_opening_excerpt_uses_actual_segments_and_marks_boundary(self) -> None:
        excerpt = performance_report._tts_opening_excerpt(
            {
                "_tts_timing": {
                    "duration_seconds": 70.0,
                    "segments": [
                        {
                            "text": "最初の文です。",
                            "start_seconds": 0.0,
                            "end_seconds": 20.0,
                        },
                        {
                            "text": "境界をまたぐ文です。",
                            "start_seconds": 20.0,
                            "end_seconds": 35.0,
                        },
                        {
                            "text": "中盤の文です。",
                            "start_seconds": 35.0,
                            "end_seconds": 50.0,
                        },
                    ],
                }
            }
        )

        self.assertEqual(
            excerpt["text"], "最初の文です。 境界をまたぐ文です。"
        )
        self.assertTrue(excerpt["crosses_boundary"])
        self.assertFalse(excerpt["truncated"])

    def test_tts_opening_excerpt_marks_truncated_input(self) -> None:
        long_text = "あ" * 450
        excerpt = performance_report._tts_opening_excerpt(
            {
                "_tts_timing": {
                    "duration_seconds": 40.0,
                    "segments": [
                        {
                            "text": long_text,
                            "start_seconds": 0.0,
                            "end_seconds": 35.0,
                        }
                    ],
                }
            }
        )

        self.assertEqual(
            len(excerpt["text"]),
            performance_report._THUMBNAIL_OPENING_EXCERPT_LIMIT,
        )
        self.assertTrue(excerpt["crosses_boundary"])
        self.assertTrue(excerpt["truncated"])

    def test_tts_opening_excerpt_rejects_invalid_timing(self) -> None:
        self.assertIsNone(performance_report._tts_opening_excerpt({}))
        self.assertIsNone(
            performance_report._tts_opening_excerpt(
                {
                    "_tts_timing": {
                        "duration_seconds": 60,
                        "segments": [
                            {
                                "text": "後の文",
                                "start_seconds": 10,
                                "end_seconds": 20,
                            },
                            {
                                "text": "重なる文",
                                "start_seconds": 15,
                                "end_seconds": 25,
                            },
                        ],
                    }
                }
            )
        )

    def test_thumbnail_opening_evidence_requires_all_provenance(self) -> None:
        def write_script(payload: dict) -> dict:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            Path(tmp.name, "script.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            return {"workdir": tmp.name}

        timing = {
            "duration_seconds": 60,
            "segments": [
                {"text": "冒頭です。", "start_seconds": 0, "end_seconds": 10}
            ],
        }
        valid = {
            "title": "生成タイトル",
            "_tts_timing": timing,
            "_thumbnail_provenance": {
                "display_text": "描画文字",
                "render_status": "rendered",
                "youtube_set_status": "set",
            },
        }
        evidence = performance_report._thumbnail_opening_evidence_for_row(
            write_script(valid)
        )
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["display_text"], "描画文字")

        variants = {
            "missing_title": {**valid, "title": ""},
            "render_failed": {
                **valid,
                "_thumbnail_provenance": {
                    **valid["_thumbnail_provenance"],
                    "render_status": "failed",
                },
            },
            "set_failed": {
                **valid,
                "_thumbnail_provenance": {
                    **valid["_thumbnail_provenance"],
                    "youtube_set_status": "failed",
                },
            },
            "missing_timing": {key: value for key, value in valid.items() if key != "_tts_timing"},
        }
        for name, payload in variants.items():
            with self.subTest(name=name):
                self.assertFalse(
                    performance_report._thumbnail_opening_evidence_for_row(
                        write_script(payload)
                    )["available"]
                )
        self.assertFalse(
            performance_report._thumbnail_opening_evidence_for_row(
                {"title": "Data APIだけの現在タイトル"}
            )["available"]
        )

    def test_safe_markdown_inline_disables_mentions_html_and_links(self) -> None:
        safe = performance_report._safe_markdown_inline(
            "@user @org/team <details> [label](https://example.com) `code`"
        )

        self.assertNotIn("@user", safe)
        self.assertNotIn("@org/team", safe)
        self.assertIn("@\u200buser", safe)
        self.assertIn("&lt;details&gt;", safe)
        self.assertIn(r"\[label\]\(https://example.com\)", safe)
        self.assertIn(r"\`code\`", safe)

    def test_short_tier_detection_is_fail_closed(self) -> None:
        self.assertEqual(
            performance_report._youtube_short_tier(
                {"format_traits": ["tier:short", "duration:60_to_179s"]}
            ),
            "short",
        )
        self.assertEqual(
            performance_report._youtube_short_tier(
                {"format_traits": ["tier:long_short"]}
            ),
            "long_short",
        )
        self.assertEqual(
            performance_report._youtube_short_tier(
                {"format_traits": ["tier:longform"]}
            ),
            "",
        )
        self.assertEqual(
            performance_report._youtube_short_tier(
                {"format_traits": ("tier:short",)}
            ),
            "",
        )

    def test_shorts_first_three_signal_uses_only_observed_points_within_window(
        self,
    ) -> None:
        row = {
            "format_traits": ["tier:long_short"],
            "data_api": {"duration": "PT60S"},
            "analytics": {
                "retention_curve": [
                    {"elapsed_ratio": 0.01, "watch_ratio": 0.95},  # 0.6秒
                    {"elapsed_ratio": 0.03, "watch_ratio": 0.91},  # 1.8秒
                    {"elapsed_ratio": 0.05, "watch_ratio": 0.87},  # 3.0秒
                    {"elapsed_ratio": 0.10, "watch_ratio": 0.40},  # 対象外
                ]
            },
        }

        signal = performance_report._shorts_first_three_signal_for_row(row)

        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])
        self.assertEqual(signal["window_seconds"], 3.0)
        self.assertEqual(signal["start_seconds"], 0.6)
        self.assertEqual(signal["end_seconds"], 3.0)
        self.assertAlmostEqual(signal["cumulative_drop_ratio"], 0.08)
        self.assertAlmostEqual(signal["largest_step_drop_ratio"], 0.04)

    def test_shorts_first_three_text_proposes_one_manual_information_removal(
        self,
    ) -> None:
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "short-opening",
                    "corner": "capitalism",
                    "format_traits": ["tier:short", "duration:60_to_179s"],
                    "workdir": str(Path(self._workdir())),
                    "history_ts": "2026-08-11T00:00:00+00:00",
                    "data_api": {"duration": "PT60S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.01, "watch_ratio": 0.96},
                            {"elapsed_ratio": 0.03, "watch_ratio": 0.91},
                            {"elapsed_ratio": 0.05, "watch_ratio": 0.84},
                            {"elapsed_ratio": 0.10, "watch_ratio": 0.80},
                        ]
                    },
                },
                {
                    "video_id": "not-queried",
                    "corner": "capitalism",
                    "format_traits": ["tier:long_short"],
                    "data_api": {"duration": "PT90S"},
                    "analytics": {"views": 100},
                },
                {
                    "video_id": "regular-video",
                    "corner": "capitalism",
                    "format_traits": ["tier:longform"],
                    "data_api": {"duration": "PT4M"},
                    "analytics": {},
                },
            ],
        }

        text = performance_report._shorts_first_three_retention_text(
            snapshot, "capitalism"
        )

        self.assertIn("Shorts対象 2本", text)
        self.assertIn("冒頭3秒低下シグナル 1本", text)
        self.assertIn("Analytics未評価 1本", text)
        self.assertIn("`short-opening`（tier=short）", text)
        self.assertIn("シーン・均等割近似: 導入", text)
        self.assertIn("上記一覧の先頭", text)
        self.assertIn("検証対象に固定", text)
        self.assertIn("不要な情報を1つだけ削る", text)
        self.assertIn("他の中心変数を固定", text)
        self.assertIn("運用者が手動", text)
        self.assertIn("スワイプアウト率", text)
        self.assertIn("離脱人数そのものではありません", text)
        self.assertIn("シーン位置は動画長をscene数で均等割した近似", text)
        self.assertNotIn("`regular-video`", text)

    def test_shorts_first_three_text_withholds_when_points_are_insufficient(
        self,
    ) -> None:
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                {
                    "video_id": "sparse-short",
                    "corner": "analytics",
                    "format_traits": ["tier:long_short"],
                    "data_api": {"duration": "PT180S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.01, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.02, "watch_ratio": 0.60},
                        ]
                    },
                }
            ],
        }

        text = performance_report._shorts_first_three_retention_text(
            snapshot, "analytics"
        )

        self.assertIn("分析可能な維持率カーブがありません", text)
        self.assertIn("有効な観測点が2点未満", text)
        self.assertNotIn("次の1本:", text)

    def test_shorts_first_three_text_prioritises_recent_then_larger_drop(
        self,
    ) -> None:
        def row(video_id: str, timestamp: str, end_ratio: float) -> dict:
            return {
                "video_id": video_id,
                "corner": "communism",
                "format_traits": ["tier:short", "duration:60_to_179s"],
                "history_ts": timestamp,
                "data_api": {"duration": "PT60S"},
                "analytics": {
                    "retention_curve": [
                        {"elapsed_ratio": 0.01, "watch_ratio": 0.95},
                        {
                            "elapsed_ratio": 0.03,
                            "watch_ratio": (0.95 + end_ratio) / 2,
                        },
                        {"elapsed_ratio": 0.05, "watch_ratio": end_ratio},
                    ]
                },
            }

        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                row("older-strongest", "2026-08-09T00:00:00+00:00", 0.50),
                row("latest-mild", "2026-08-10T00:00:00+00:00", 0.87),
                row("latest-strong", "2026-08-10T00:00:00+00:00", 0.70),
            ],
        }

        text = performance_report._shorts_first_three_retention_text(
            snapshot, "communism"
        )

        self.assertLess(text.index("`latest-strong`"), text.index("`latest-mild`"))
        self.assertLess(text.index("`latest-mild`"), text.index("`older-strongest`"))
        self.assertIn("上記一覧の先頭（最新優先、同時刻なら低下が大きい動画）", text)

    def test_opening_retention_text_proposes_one_manual_hook_change(self) -> None:
        snapshot = {
            "videos": [
                {
                    "video_id": "v-opening",
                    "corner": "capitalism",
                    "workdir": str(Path(self._workdir())),
                    "data_api": {"duration": "PT100S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                            {"elapsed_ratio": 0.1, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.2, "watch_ratio": 0.80},
                            {"elapsed_ratio": 0.3, "watch_ratio": 0.65},
                        ]
                    },
                }
            ],
            "retention_curve": {"available": True},
        }

        text = performance_report._opening_retention_text(snapshot, "capitalism")

        self.assertIn("冒頭低下シグナル 1本", text)
        self.assertIn("最大区間低下", text)
        self.assertIn("次の1本", text)
        self.assertIn("冒頭フックだけを変更", text)
        self.assertIn("運用者が手動", text)
        self.assertIn("万能な合格ラインではありません", text)

    def test_opening_retention_text_prioritises_recent_then_larger_drop(self) -> None:
        videos = []
        for index in range(9):
            videos.append(
                {
                    "video_id": f"old-{index}",
                    "corner": "video",
                    "history_ts": f"2026-08-{index + 1:02d}T00:00:00+00:00",
                    "data_api": {"duration": "PT100S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                            {"elapsed_ratio": 0.3, "watch_ratio": 0.80},
                        ]
                    },
                }
            )
        videos += [
            {
                "video_id": "latest-mild",
                "corner": "video",
                "history_ts": "2026-08-10T00:00:00+00:00",
                "data_api": {"duration": "PT100S"},
                "analytics": {
                    "retention_curve": [
                        {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                        {"elapsed_ratio": 0.3, "watch_ratio": 0.80},
                    ]
                },
            },
            {
                "video_id": "latest-strong",
                "corner": "video",
                "history_ts": "2026-08-10T00:00:00+00:00",
                "data_api": {"duration": "PT100S"},
                "analytics": {
                    "retention_curve": [
                        {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                        {"elapsed_ratio": 0.3, "watch_ratio": 0.60},
                    ]
                },
            },
        ]
        snapshot = {
            "videos": videos,
            "retention_curve": {"available": True},
        }

        text = performance_report._opening_retention_text(snapshot, "video")

        self.assertIn("`latest-strong`", text)
        self.assertIn("`latest-mild`", text)
        self.assertLess(text.index("`latest-strong`"), text.index("`latest-mild`"))
        self.assertNotIn("`old-0`", text)
        self.assertIn("他にも1本", text)

    def test_thumbnail_opening_slope_text_prioritises_latest_then_decline(
        self,
    ) -> None:
        def row(video_id: str, published: str, midpoint_watch: float) -> dict:
            return {
                "video_id": video_id,
                "title": f"{video_id} title",
                "corner": "communism",
                "workdir": self._workdir(),
                "history_ts": published,
                "data_api": {"duration": "PT2M"},
                "analytics": {
                    "retention_curve": [
                        {"elapsed_ratio": 0.00, "watch_ratio": 0.95},
                        {"elapsed_ratio": 0.25, "watch_ratio": 0.92},
                        {"elapsed_ratio": 0.30, "watch_ratio": 0.90},
                        {"elapsed_ratio": 0.35, "watch_ratio": 0.88},
                        {"elapsed_ratio": 0.40, "watch_ratio": 0.86},
                        {"elapsed_ratio": 0.45, "watch_ratio": 0.84},
                        {"elapsed_ratio": 0.50, "watch_ratio": midpoint_watch},
                        {"elapsed_ratio": 1.00, "watch_ratio": midpoint_watch},
                    ]
                },
            }

        snapshot = {
            "retention_curve": {"available": True},
            "videos": [
                row("older-strongest", "2026-08-09T00:00:00+00:00", 0.62),
                row("latest-mild", "2026-08-10T00:00:00+00:00", 0.82),
                row("latest-strong", "2026-08-10T00:00:00+00:00", 0.72),
            ],
        }

        text = performance_report._thumbnail_opening_slope_text(
            snapshot, "communism"
        )

        self.assertLess(text.index("`latest-strong`"), text.index("`latest-mild`"))
        self.assertLess(text.index("`latest-mild`"), text.index("`older-strongest`"))
        self.assertIn("VOICEVOX合成入力文", text)
        self.assertIn("一方だけを手動変更", text)
        self.assertIn("サムネ背景画像の意味的一致", text)

    def test_thumbnail_opening_slope_text_withholds_for_short_video(self) -> None:
        text = performance_report._thumbnail_opening_slope_text(
            {
                "retention_curve": {"available": True},
                "videos": [
                    {
                        "video_id": "short",
                        "corner": "shorts",
                        "data_api": {"duration": "PT60S"},
                        "analytics": {
                            "retention_curve": [
                                {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                                {"elapsed_ratio": 0.5, "watch_ratio": 0.80},
                                {"elapsed_ratio": 1.0, "watch_ratio": 0.60},
                            ]
                        },
                    }
                ],
            },
            "shorts",
        )

        self.assertIn("分析可能な維持率カーブがありません", text)
        self.assertIn("判定材料不足 1本", text)
        self.assertIn("推測で補いません", text)

    def test_thumbnail_opening_slope_text_withholds_without_evidence(self) -> None:
        text = performance_report._thumbnail_opening_slope_text(
            {
                "retention_curve": {"available": True},
                "videos": [
                    {
                        "video_id": "legacy-no-timing",
                        "corner": "video",
                        "data_api": {"duration": "PT2M"},
                        "analytics": {
                            "retention_curve": [
                                {"elapsed_ratio": 0.25, "watch_ratio": 0.92},
                                {"elapsed_ratio": 0.30, "watch_ratio": 0.90},
                                {"elapsed_ratio": 0.35, "watch_ratio": 0.88},
                                {"elapsed_ratio": 0.40, "watch_ratio": 0.86},
                                {"elapsed_ratio": 0.45, "watch_ratio": 0.84},
                                {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
                            ]
                        },
                    }
                ],
            },
            "video",
        )

        self.assertIn("比較証拠不足 1本", text)
        self.assertIn("#125の変更提案には使いません", text)
        self.assertIn("#125の変更を提案しません", text)
        self.assertNotIn("- 次の1本:", text)

    def test_thumbnail_opening_slope_text_labels_truncated_synthesis_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "script.json").write_text(
                json.dumps(
                    {
                        "title": "生成タイトル",
                        "_tts_timing": {
                            "duration_seconds": 120,
                            "segments": [
                                {
                                    "text": "あ" * 450,
                                    "start_seconds": 0,
                                    "end_seconds": 35,
                                }
                            ],
                        },
                        "_thumbnail_provenance": {
                            "display_text": "描画文字",
                            "render_status": "rendered",
                            "youtube_set_status": "set",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            text = performance_report._thumbnail_opening_slope_text(
                {
                    "retention_curve": {"available": True},
                    "videos": [
                        {
                            "video_id": "long-opening",
                            "corner": "video",
                            "workdir": tmp,
                            "data_api": {"duration": "PT2M"},
                            "analytics": {
                                "retention_curve": [
                                    {"elapsed_ratio": 0.25, "watch_ratio": 0.92},
                                    {"elapsed_ratio": 0.30, "watch_ratio": 0.90},
                                    {"elapsed_ratio": 0.35, "watch_ratio": 0.88},
                                    {"elapsed_ratio": 0.40, "watch_ratio": 0.86},
                                    {"elapsed_ratio": 0.45, "watch_ratio": 0.84},
                                    {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
                                ]
                            },
                        }
                    ],
                },
                "video",
            )

        self.assertIn("30秒境界をまたぐ文を含む", text)
        self.assertIn("先頭400字の抜粋", text)
        self.assertNotIn("全文表示", text)

    def test_retention_moments_detects_spike_and_dip(self) -> None:
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.85},
            {"elapsed_ratio": 0.4, "watch_ratio": 0.40},  # dip
            {"elapsed_ratio": 0.6, "watch_ratio": 0.80},
            {"elapsed_ratio": 0.8, "watch_ratio": 0.90},  # spike
            {"elapsed_ratio": 1.0, "watch_ratio": 0.50},
        ]
        moments = performance.retention_moments(curve)
        kinds = {m["kind"] for m in moments}
        self.assertIn("dip", kinds)
        self.assertIn("spike", kinds)

    def test_retention_moments_uses_ratio_thresholds(self) -> None:
        """issue #149: 閾値は比率（0.08=8%ポイント）で判定する。"""
        at_threshold = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.88},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.40},  # dip（差0.48）
            {"elapsed_ratio": 0.7, "watch_ratio": 0.88},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.90},
        ]
        self.assertEqual(
            [m["kind"] for m in performance.retention_moments(at_threshold)],
            ["dip"],
        )
        below = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.89},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.83},  # 差0.06 < 0.08
            {"elapsed_ratio": 0.7, "watch_ratio": 0.89},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.90},
        ]
        self.assertEqual(performance.retention_moments(below), [])
        # 再視聴（1.0超）も有効な山として検出する
        rewound = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.5, "watch_ratio": 1.20},  # spike
            {"elapsed_ratio": 0.7, "watch_ratio": 0.90},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.90},
        ]
        self.assertEqual(
            [m["kind"] for m in performance.retention_moments(rewound)],
            ["spike"],
        )

    def test_retention_moments_detects_exact_threshold(self) -> None:
        """issue #149 (Sol review指摘): ちょうど0.08の差もspike/dipとして
        検出する（浮動小数誤差を考慮した >= / <= 判定）。"""
        spike = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.50},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.50},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.58},  # 前後とちょうど0.08
            {"elapsed_ratio": 0.7, "watch_ratio": 0.50},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.50},
        ]
        dip = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.58},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.58},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.50},  # 前後とちょうど0.08
            {"elapsed_ratio": 0.7, "watch_ratio": 0.58},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.58},
        ]
        self.assertEqual(
            [m["kind"] for m in performance.retention_moments(dip)],
            ["dip"],
        )
        self.assertEqual(
            [m["kind"] for m in performance.retention_moments(spike)],
            ["spike"],
        )

    def test_retention_moments_dip_requires_both_sides_over_threshold(self) -> None:
        """issue #149 (Sol review指摘): dipは前後両方との差が閾値以上の場合
        だけ検出する。片側だけの落差（左0.10・右0.01）では検出しない。"""
        one_sided = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.70},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.70},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.60},  # 前0.10・後0.01
            {"elapsed_ratio": 0.7, "watch_ratio": 0.61},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.70},
        ]
        self.assertEqual(performance.retention_moments(one_sided), [])

        both_sides = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.70},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.70},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.60},  # 前0.10・後0.10
            {"elapsed_ratio": 0.7, "watch_ratio": 0.70},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.70},
        ]
        self.assertEqual(
            [m["kind"] for m in performance.retention_moments(both_sides)],
            ["dip"],
        )

    def test_retention_curve_text_limits_headings_after_ten_moments(self) -> None:
        """issue #149 (Sol review指摘): 10件到達後は動画見出しを追加せず、
        省略通知を一度だけ表示する。"""
        videos = []
        for index in range(6):
            videos.append(
                {
                    "video_id": f"v{index}",
                    "corner": "video",
                    "data_api": {"duration": "PT1M"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.2, "watch_ratio": 0.85},
                            {"elapsed_ratio": 0.3, "watch_ratio": 0.40},  # dip
                            {"elapsed_ratio": 0.5, "watch_ratio": 0.85},
                            {"elapsed_ratio": 0.6, "watch_ratio": 0.40},  # dip
                            {"elapsed_ratio": 0.7, "watch_ratio": 0.80},
                            {"elapsed_ratio": 1.0, "watch_ratio": 0.50},
                        ]
                    },
                }
            )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": videos,
        }

        text = performance_report._retention_curve_text(snapshot, "video")

        # 各動画4モーメント × 6動画 = 24。10件まで表示（動画3件目の途中で
        # 到達するため、見出しは10モーメント分＝動画3件分以内）。
        self.assertLessEqual(text.count("維持率カーブの山/谷"), 10)
        self.assertEqual(text.count("先頭10件まで表示します"), 1)
        self.assertNotIn("- `v5`", text)

    def test_retention_moments_ignores_flat_and_short_curves(self) -> None:
        flat = [
            {"elapsed_ratio": i / 10, "watch_ratio": 0.70} for i in range(11)
        ]
        self.assertEqual(performance.retention_moments(flat), [])
        self.assertEqual(performance.retention_moments(flat[:4]), [])
        self.assertEqual(performance.retention_moments([]), [])

    def test_retention_moment_scenes_annotates_with_caption(self) -> None:
        moments = [
            {
                "elapsed_ratio": 0.4,
                "watch_ratio": 0.40,
                "kind": "dip",
            }
        ]
        script = {
            "scenes": [
                {"caption": "導入"},
                {"caption": "展開"},
                {"caption": "結び"},
            ],
        }
        annotated = performance.retention_moment_scenes(moments, script, "PT100S")
        self.assertEqual(annotated[0]["scene_index"], 1)
        self.assertEqual(annotated[0]["scene_caption"], "展開")
        self.assertAlmostEqual(annotated[0]["elapsed_seconds"], 40.0, delta=1.0)

    def test_retention_moment_scenes_returns_moments_without_script(self) -> None:
        moments = [
            {"elapsed_ratio": 0.5, "watch_ratio": 0.50, "kind": "dip"}
        ]
        annotated = performance.retention_moment_scenes(moments, {}, "PT100S")
        self.assertEqual(annotated[0]["scene_index"], None)
        self.assertEqual(annotated[0]["scene_caption"], "")

    def test_retention_moment_scenes_unknown_duration_is_fail_closed(self) -> None:
        """issue #149: 動画長を取得できない場合は位置不明（秒を捏造しない）。"""
        moments = [
            {"elapsed_ratio": 0.5, "watch_ratio": 0.50, "kind": "dip"}
        ]
        annotated = performance.retention_moment_scenes(moments, {}, None)
        self.assertIsNone(annotated[0]["elapsed_seconds"])
        self.assertIsNone(annotated[0]["scene_index"])

    def test_iso8601_duration_seconds(self) -> None:
        self.assertEqual(performance._iso8601_duration_seconds("PT1M"), 60.0)
        self.assertEqual(performance._iso8601_duration_seconds("PT1M30S"), 90.0)
        self.assertEqual(performance._iso8601_duration_seconds("PT1H2M3S"), 3723.0)
        self.assertEqual(performance._iso8601_duration_seconds("PT30S"), 30.0)
        self.assertIsNone(performance._iso8601_duration_seconds("bad"))
        self.assertIsNone(performance._iso8601_duration_seconds(""))
        self.assertIsNone(performance._iso8601_duration_seconds(None))
        # 末尾単位欠落・順序不正・重複単位・日数付きは fail-closed
        self.assertIsNone(performance._iso8601_duration_seconds("PT1M30"))
        self.assertIsNone(performance._iso8601_duration_seconds("PT30S1M"))
        self.assertIsNone(performance._iso8601_duration_seconds("PT1M1M"))
        self.assertIsNone(performance._iso8601_duration_seconds("P1DT1H"))
        self.assertIsNone(performance._iso8601_duration_seconds("PT"))

    def test_retention_moment_scenes_uses_duration_not_average_watch_time(self) -> None:
        """issue #149: 秒位置は動画全長（ISO 8601）から算出する。"""
        moments = [
            {"elapsed_ratio": 0.8, "watch_ratio": 0.40, "kind": "dip"}
        ]
        annotated = performance.retention_moment_scenes(moments, {}, "PT1M")
        self.assertAlmostEqual(annotated[0]["elapsed_seconds"], 48.0, delta=1.0)

    def test_retention_curve_text_reports_moments_without_judging(self) -> None:
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "workdir": str(Path(self._workdir())),
                    "data_api": {"duration": "PT1M40S"},
                    "analytics": {
                        "retention_curve": [
                            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
                            {"elapsed_ratio": 0.2, "watch_ratio": 0.85},
                            {"elapsed_ratio": 0.4, "watch_ratio": 0.40},
                            {"elapsed_ratio": 0.6, "watch_ratio": 0.70},
                            {"elapsed_ratio": 0.8, "watch_ratio": 0.60},
                            {"elapsed_ratio": 1.0, "watch_ratio": 0.30},
                        ],
                    },
                }
            ],
            "retention_curve": {"available": True},
        }
        text = performance_report._retention_curve_text(snapshot, "video")
        self.assertIn("谷（dip）", text)
        self.assertIn("成功・失敗を判定しません", text)
        self.assertIn("該当箇所の内容と照合", text)

    def test_retention_curve_text_fail_closed_when_no_curve(self) -> None:
        snapshot = {
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "analytics": {"retention_curve": []},
                }
            ],
            "retention_curve": {"available": True},
        }
        text = performance_report._retention_curve_text(snapshot, "video")
        self.assertIn("取得できませんでした", text)
        self.assertIn("推測で補いません", text)

    def test_retention_curve_text_reports_global_failure(self) -> None:
        """issue #149: カーブAPI全体の失敗は「Shortsで返さない」ではなく
        取得失敗と明記する。"""
        snapshot = {
            "videos": [
                {"video_id": "v1", "corner": "video", "analytics": {}}
            ],
            "retention_curve": {
                "available": False,
                "reason": "quota exceeded",
            },
        }
        text = performance_report._retention_curve_text(snapshot, "video")
        self.assertIn("取得に失敗しました", text)
        self.assertIn("quota exceeded", text)

    def test_subscribed_status_retention_text_separates_traffic_and_segments(
        self,
    ) -> None:
        subscribed = []
        unsubscribed = []
        for index in range(1, 7):
            subscribed.append(
                {
                    "elapsed_ratio": index / 10,
                    "watch_ratio": 0.9 - index * 0.02,
                    "segment_impressions": 30,
                }
            )
            unsubscribed.append(
                {
                    "elapsed_ratio": index / 10,
                    "watch_ratio": 0.9 - index * 0.06,
                    "segment_impressions": 40,
                }
            )
        snapshot = {
            "traffic_sources": {"available": True},
            "retention_by_subscribed_status": {
                "available": True,
                "queried_video_ids": ["v1"],
            },
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "workdir": str(Path(self._workdir())),
                    "data_api": {"duration": "PT100S"},
                    "analytics": {
                        "traffic_sources": {
                            "YT_SEARCH": 40,
                            "RELATED_VIDEO": 20,
                        },
                        "retention_by_subscribed_status": {
                            "SUBSCRIBED": subscribed,
                            "UNSUBSCRIBED": unsubscribed,
                        },
                    },
                }
            ],
        }

        text = performance_report._subscribed_status_retention_text(
            snapshot, "video"
        )

        self.assertIn("流入元views（維持率とは結合しません）", text)
        self.assertIn("購読者", text)
        self.assertIn("非購読者", text)
        self.assertIn("次の1本", text)
        self.assertIn("リピーター/新規視聴者ではありません", text)
        self.assertNotIn("新規視聴者 50", text)

        snapshot["traffic_sources"] = {
            "available": False,
            "reason": "quota exceeded",
        }
        failed_traffic_text = (
            performance_report._subscribed_status_retention_text(
                snapshot, "video"
            )
        )
        self.assertIn("流入元views: 取得に失敗しました", failed_traffic_text)
        self.assertIn("quota exceeded", failed_traffic_text)
        self.assertIn("購読者", failed_traffic_text)
        self.assertNotIn("YT_SEARCH=40", failed_traffic_text)

        snapshot["traffic_sources"] = {"available": True}
        snapshot["videos"][0]["analytics"]["traffic_sources"] = {}
        missing_traffic_text = (
            performance_report._subscribed_status_retention_text(
                snapshot, "video"
            )
        )
        self.assertIn("内訳データが返らず未評価", missing_traffic_text)
        self.assertIn("0とはみなしません", missing_traffic_text)

    def test_subscribed_status_retention_text_withholds_low_sample(self) -> None:
        low_sample = [
            {
                "elapsed_ratio": index / 10,
                "watch_ratio": 0.8,
                "segment_impressions": 19,
            }
            for index in range(1, 7)
        ]
        snapshot = {
            "traffic_sources": {"available": True},
            "retention_by_subscribed_status": {
                "available": True,
                "queried_video_ids": ["v1"],
            },
            "videos": [
                {
                    "video_id": "v1",
                    "corner": "video",
                    "data_api": {"duration": "PT100S"},
                    "analytics": {
                        "retention_by_subscribed_status": {
                            "SUBSCRIBED": low_sample,
                            "UNSUBSCRIBED": low_sample,
                        }
                    },
                }
            ],
        }

        text = performance_report._subscribed_status_retention_text(
            snapshot, "video"
        )

        self.assertIn("判定材料不足", text)
        self.assertNotIn("次の1本: 差が大きい", text)

    def _workdir(self) -> str:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "script.json"
        path.write_text(
            json.dumps(
                {
                    "title": "生成時タイトル",
                    "narration": "あ" * 20 + "い" * 30,
                    "scenes": [{"caption": "導入"}, {"caption": "展開"}],
                    "_tts_timing": {
                        "duration_seconds": 100.0,
                        "segments": [
                            {
                                "text": "冒頭の合成入力文です。",
                                "start_seconds": 0.0,
                                "end_seconds": 12.0,
                            }
                        ],
                    },
                    "_thumbnail_provenance": {
                        "display_text": "生成時タイトル",
                        "render_status": "rendered",
                        "youtube_set_status": "set",
                    },
                }
            ),
            encoding="utf-8",
        )
        return tmp.name


class CommentIntentManualReviewTest(unittest.TestCase):
    """issue #123: 最新動画の実測離脱をStudioでの手動確認へ接続する。"""

    def _workdir(
        self,
        *,
        topic: str = "制度の誤解",
        angle: str = "原因と結果を分ける",
    ) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "script.json").write_text(
            json.dumps(
                {
                    "title": "制度の誤解をほどく",
                    "_research": {"topic": topic, "angle": angle},
                    "scenes": [
                        {"caption": "導入"},
                        {"caption": "原因"},
                        {"caption": "結果"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return tmp.name

    def _row(
        self,
        video_id: str,
        *,
        corner: str = "capitalism",
        published_at: str = "2026-07-25T00:00:00+00:00",
        history_ts: str | None = None,
        duration: str = "PT100S",
        curve: list[dict] | None = None,
        include_intent: bool = True,
    ) -> dict:
        if curve is None:
            curve = [
                {"elapsed_ratio": 0.0, "watch_ratio": 0.95},
                {"elapsed_ratio": 0.1, "watch_ratio": 0.91},
                {"elapsed_ratio": 0.2, "watch_ratio": 0.86},
                {"elapsed_ratio": 0.3, "watch_ratio": 0.70},
                {"elapsed_ratio": 0.5, "watch_ratio": 0.62},
                {"elapsed_ratio": 1.0, "watch_ratio": 0.50},
            ]
        return {
            "video_id": video_id,
            "corner": corner,
            "history_ts": published_at if history_ts is None else history_ts,
            "published_at": published_at,
            "workdir": self._workdir(topic=f"題材-{video_id}") if include_intent else "",
            "format_traits": ["tier:longform"],
            "data_api": {"duration": duration},
            "analytics": {"retention_curve": curve},
        }

    def test_manual_review_uses_latest_video_and_does_not_emit_comment_data(self) -> None:
        old = self._row("old", published_at="2026-07-20T00:00:00+00:00")
        latest = self._row("latest", published_at="2026-07-25T00:00:00+00:00")
        # 旧schema等に生コメントが紛れた入力でも、この節は参照・転載しない。
        latest["comment_sample"] = {
            "comments": [
                {"text": "氏名 山田太郎 mail@example.com @viewer 090-1234-5678"}
            ]
        }
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [old, latest],
        }

        text = performance_report._comment_intent_retention_text(
            snapshot, "capitalism"
        )

        self.assertIn("`latest`（このcornerの最新動画）", text)
        self.assertNotIn("`old`", text)
        self.assertIn("Studioでの確認", text)
        self.assertIn("コメント本文・件数・投稿者情報はdociへ入力・保存しない", text)
        self.assertIn("除外後の分類対象が3件未満", text)
        self.assertIn("分母は運営自身を除外した後の有効標本数", text)
        self.assertIn("どちらにも該当しなければ変更しない", text)
        for secret in ("山田太郎", "mail@example.com", "@viewer", "090-1234-5678"):
            self.assertNotIn(secret, text)

    def test_latest_video_without_curve_does_not_fallback_to_old_video(self) -> None:
        old = self._row("old", published_at="2026-07-20T00:00:00+00:00")
        latest = self._row(
            "latest",
            published_at="2026-07-25T00:00:00+00:00",
            curve=[],
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [old, latest],
        }

        text = performance_report._comment_intent_retention_text(
            snapshot, "capitalism"
        )

        self.assertIn("`latest`（このcornerの最新動画）", text)
        self.assertIn("旧動画へfallbackせず評価しません", text)
        self.assertNotIn("Studioでの確認", text)
        self.assertNotIn("`old`", text)

    def test_youtube_published_at_wins_when_history_order_is_reversed(self) -> None:
        youtube_older = self._row(
            "youtube-older",
            published_at="2026-07-20T00:00:00+00:00",
            history_ts="2026-07-26T00:00:00+00:00",
        )
        youtube_latest = self._row(
            "youtube-latest",
            published_at="2026-07-25T00:00:00+00:00",
            history_ts="2026-07-21T00:00:00+00:00",
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [youtube_latest, youtube_older],
        }

        text = performance_report._comment_intent_retention_text(
            snapshot, "capitalism"
        )

        self.assertIn("`youtube-latest`（このcornerの最新動画）", text)
        self.assertNotIn("`youtube-older`", text)

    def test_unknown_publication_time_does_not_guess_latest_video(self) -> None:
        known = self._row("known")
        unknown = self._row("unknown")
        unknown["history_ts"] = ""
        unknown["published_at"] = ""
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [known, unknown],
        }

        text = performance_report._comment_intent_retention_text(
            snapshot, "capitalism"
        )

        self.assertIn("公開時刻またはvideo_idを確認できない", text)
        self.assertIn("最新動画を推測せず", text)
        self.assertNotIn("Studioでの確認", text)

    def test_latest_video_failure_reason_is_shown_without_fallback(self) -> None:
        old = self._row("old", published_at="2026-07-20T00:00:00+00:00")
        latest = self._row(
            "latest",
            published_at="2026-07-25T00:00:00+00:00",
            curve=[],
        )
        snapshot = {
            "retention_curve": {
                "available": True,
                "failed_video_ids": ["latest"],
                "failures": {"latest": "privacy threshold"},
            },
            "videos": [old, latest],
        }

        text = performance_report._comment_intent_retention_text(
            snapshot, "capitalism"
        )

        self.assertIn("privacy threshold", text)
        self.assertIn("旧動画へfallbackせず評価しません", text)

    def test_missing_saved_intent_is_not_inferred_from_current_title(self) -> None:
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [self._row("latest", include_intent=False)],
        }

        text = performance_report._comment_intent_retention_text(
            snapshot, "capitalism"
        )

        self.assertIn("topic/angleが揃わない", text)
        self.assertIn("現在のタイトルから意図を推測せず", text)
        self.assertNotIn("Studioでの確認", text)

    def test_end_only_dip_is_not_an_early_or_midpoint_signal(self) -> None:
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.1, "watch_ratio": 0.89},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.89},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.89},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.89},
            {"elapsed_ratio": 0.7, "watch_ratio": 0.89},
            {"elapsed_ratio": 0.8, "watch_ratio": 0.60},
            {"elapsed_ratio": 0.9, "watch_ratio": 0.89},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.88},
        ]

        context = performance_report._comment_retention_context(
            self._row("end-dip", curve=curve)
        )

        self.assertFalse(context["actionable"])

    def test_exact_midpoint_dip_is_allowed_when_seconds_are_known(self) -> None:
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.62},
            {"elapsed_ratio": 0.1, "watch_ratio": 0.62},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.62},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.61},
            {"elapsed_ratio": 0.4, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.60},
            {"elapsed_ratio": 0.6, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.8, "watch_ratio": 0.88},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.86},
        ]

        context = performance_report._comment_retention_context(
            self._row("mid-dip", curve=curve)
        )

        self.assertTrue(context["actionable"])
        self.assertEqual(context["kind"], "retention_dip")
        self.assertEqual(context["location"], "約50.0秒")

    def test_midpoint_dip_without_duration_fails_closed(self) -> None:
        curve = [
            {"elapsed_ratio": 0.0, "watch_ratio": 0.62},
            {"elapsed_ratio": 0.1, "watch_ratio": 0.62},
            {"elapsed_ratio": 0.2, "watch_ratio": 0.62},
            {"elapsed_ratio": 0.3, "watch_ratio": 0.61},
            {"elapsed_ratio": 0.4, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.5, "watch_ratio": 0.60},
            {"elapsed_ratio": 0.6, "watch_ratio": 0.90},
            {"elapsed_ratio": 0.8, "watch_ratio": 0.88},
            {"elapsed_ratio": 1.0, "watch_ratio": 0.86},
        ]

        context = performance_report._comment_retention_context(
            self._row("unknown-duration", duration="", curve=curve)
        )

        self.assertFalse(context["actionable"])

    def test_cycle_body_applies_manual_review_to_non_source_corner(self) -> None:
        spec = SimpleNamespace(id="ideology")
        decision = {
            "decision_id": "dec-1",
            "status": "insufficient_data",
            "reason": "比較可能な動画が2本",
            "metric": "youtube_analytics_api_v2.average_view_percentage",
            "format_cohort": "",
            "eligible_video_ids": [],
            "top_video_ids": [],
            "bottom_video_ids": [],
            "positive_traits": [],
            "negative_traits": [],
        }
        section = performance_report.build_corner_section(
            spec, "capitalism", decision, [], set()
        )
        snapshot = {
            "retention_curve": {"available": True},
            "videos": [self._row("ideology-latest")],
        }

        candidate = performance_report.build_cycle_candidate(
            spec,
            [section],
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            snapshot,
        )

        self.assertIsNotNone(candidate)
        self.assertIn(
            "制作意図・視聴者コメントと序盤/中盤離脱", candidate["body"]
        )
        self.assertIn("`ideology-latest`（このcornerの最新動画）", candidate["body"])
        self.assertIn("Studioでの確認", candidate["body"])
        self.assertNotIn("commentThreads.list", candidate["body"])


if __name__ == "__main__":
    unittest.main()
