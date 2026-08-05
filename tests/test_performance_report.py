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

from doci import channel, config, feedback_issues, performance, performance_report


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


if __name__ == "__main__":
    unittest.main()
