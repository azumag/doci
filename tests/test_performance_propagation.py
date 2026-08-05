"""issue #77: 実績施策のチャンネル横展開のテスト。

有効性判定(`_experiment_result`)、仮説の永続化
(`decision_hypothesis`/`reserve_performance_decision`/`complete_performance_evaluation`)、
横展開の候補生成(`_cross_channel_candidate`/`build_decision`統合)を検証する。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import history, performance


def _make_spec(spec_id: str, root: Path, pipeline: dict | None = None) -> SimpleNamespace:
    pipeline = pipeline if pipeline is not None else {}
    return SimpleNamespace(
        id=spec_id,
        output_dir=root,
        history_file=root / "history.jsonl",
        publish=SimpleNamespace(
            youtube=SimpleNamespace(
                token=root / "token.json",
                client_secret=root / "client.json",
            )
        ),
        pipeline=pipeline,
        pipeline_get=pipeline.get,
    )


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
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


class HypothesisPersistenceTest(unittest.TestCase):
    """予約時のhypothesis保存と、評価完了時の復元・result記録を検証する。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = _make_spec("youtube-growth", Path(self.tmp.name))

    def test_hypothesis_and_result_recorded_on_evaluated_row(self) -> None:
        hypothesis = {
            "positive_traits": ["chart:present"],
            "negative_traits": [],
            "metric": "youtube_analytics_api_v2.average_view_percentage",
            "format_cohort": "duration:60_to_179s|tier:long_short",
            "source": "local",
            "origin": None,
        }
        application_id = history.reserve_performance_decision(
            self.spec, "video", "dec-1", hypothesis=hypothesis
        )
        self.assertIsNotNone(application_id)
        history.apply_performance_decision(
            self.spec, "video", "dec-1", str(application_id), "vid-1"
        )
        applied_row = history.active_performance_experiment(self.spec, "video")
        result = {
            "effective": True,
            "metric": hypothesis["metric"],
            "score": 90,
            "peer_median": 25,
            "peers": 4,
            "reason": "",
        }

        history.complete_performance_evaluation(self.spec, applied_row, result=result)

        rows = [
            json.loads(line)
            for line in self.spec.history_file.read_text(encoding="utf-8").splitlines()
        ]
        evaluated = next(
            row for row in rows if row.get("status") == "performance_evaluated"
        )
        self.assertEqual(evaluated["performance_hypothesis"], hypothesis)
        self.assertEqual(evaluated["performance_result"], result)

    def test_evaluated_row_without_hypothesis_stays_backward_compatible(self) -> None:
        application_id = history.reserve_performance_decision(
            self.spec, "video", "dec-2"
        )
        history.apply_performance_decision(
            self.spec, "video", "dec-2", str(application_id), "vid-2"
        )
        applied_row = history.active_performance_experiment(self.spec, "video")

        history.complete_performance_evaluation(self.spec, applied_row)

        rows = [
            json.loads(line)
            for line in self.spec.history_file.read_text(encoding="utf-8").splitlines()
        ]
        evaluated = next(
            row for row in rows if row.get("status") == "performance_evaluated"
        )
        self.assertNotIn("performance_hypothesis", evaluated)
        self.assertNotIn("performance_result", evaluated)


class CrossChannelPropagationTest(unittest.TestCase):
    """`_cross_channel_candidate`/`build_decision`統合の横展開テスト。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = _make_spec(
            "channel-a", root / "channel-a", {"performance_feedback": True}
        )
        self.dest = _make_spec(
            "channel-b",
            root / "channel-b",
            {
                "performance_feedback": True,
                "performance_import_from": ["channel-a"],
            },
        )
        # ローカルで仮説が立たないよう、比較可能な動画が最低数(8)を下回るsnapshot。
        self.insufficient_snapshot = {
            "collected_at": "2026-07-26T00:00:00+00:00",
            "videos": [],
        }

    def _source_evaluated_row(
        self,
        *,
        trait: str = "chart:present",
        effective: bool = True,
        hypothesis_source: str = "local",
        ts: str = "2026-07-20T00:00:00+00:00",
        application_id: str = "source-app-1",
    ) -> dict:
        return {
            "ts": ts,
            "channel": "channel-a",
            "corner": "video",
            "video_id": "a-video-1",
            "status": "performance_evaluated",
            "performance_decision_id": "source-dec-1",
            "performance_application_id": application_id,
            "performance_hypothesis": {
                "positive_traits": [trait],
                "negative_traits": [],
                "metric": "youtube_analytics_api_v2.average_view_percentage",
                "format_cohort": "duration:60_to_179s|tier:long_short",
                "source": hypothesis_source,
                "origin": None,
            },
            "performance_result": {
                "effective": effective,
                "metric": "youtube_analytics_api_v2.average_view_percentage",
                "score": 90,
                "peer_median": 25,
                "peers": 4,
                "reason": "",
            },
        }

    def test_imports_effective_experiment_when_local_data_insufficient(self) -> None:
        _write_rows(
            self.source.history_file, [self._source_evaluated_row()]
        )

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "active")
        self.assertEqual(decision["source"], "cross_channel")
        self.assertEqual(decision["positive_traits"], ["chart:present"])
        self.assertEqual(decision["origin"]["channel"], "channel-a")
        self.assertEqual(decision["origin"]["application_id"], "source-app-1")
        self.assertNotIn("a-video-1", decision["guidance"])
        # decision_idはsnapshot非依存で、同じ入力なら再現可能。
        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            again = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )
        self.assertEqual(again["decision_id"], decision["decision_id"])

    def test_no_import_without_opt_in(self) -> None:
        dest_no_opt_in = _make_spec(
            "channel-c", Path(self.tmp.name) / "channel-c", {"performance_feedback": True}
        )
        _write_rows(self.source.history_file, [self._source_evaluated_row()])

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                dest_no_opt_in, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "insufficient_data")

    def test_ineffective_source_experiment_is_not_imported(self) -> None:
        _write_rows(
            self.source.history_file,
            [self._source_evaluated_row(effective=False)],
        )

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "insufficient_data")

    def test_stale_experiment_beyond_max_age_is_not_imported(self) -> None:
        old_ts = (
            datetime.now(timezone.utc) - timedelta(days=91)
        ).isoformat()
        _write_rows(
            self.source.history_file,
            [self._source_evaluated_row(ts=old_ts)],
        )

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "insufficient_data")

    def test_grandchild_propagation_is_blocked(self) -> None:
        _write_rows(
            self.source.history_file,
            [self._source_evaluated_row(hypothesis_source="cross_channel")],
        )

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "insufficient_data")

    def test_multi_trait_source_experiment_is_not_imported(self) -> None:
        row = self._source_evaluated_row()
        row["performance_hypothesis"]["negative_traits"] = ["scenes:1_to_4"]
        _write_rows(self.source.history_file, [row])

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "insufficient_data")

    def test_already_tested_trait_is_skipped(self) -> None:
        _write_rows(self.source.history_file, [self._source_evaluated_row()])
        # 展開先で同じtraitを過去に自前で試行済み。
        _write_rows(
            self.dest.history_file,
            [
                {
                    "ts": "2026-07-01T00:00:00+00:00",
                    "channel": "channel-b",
                    "corner": "video",
                    "video_id": "b-video-0",
                    "status": "performance_evaluated",
                    "performance_decision_id": "dest-dec-0",
                    "performance_application_id": "dest-app-0",
                    "performance_hypothesis": {
                        "positive_traits": ["chart:present"],
                        "negative_traits": [],
                        "metric": "m",
                        "format_cohort": "fc",
                        "source": "local",
                        "origin": None,
                    },
                    "performance_result": {"effective": False},
                }
            ],
        )

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "insufficient_data")

    def test_in_flight_experiment_blocks_reimport_to_another_corner(self) -> None:
        """自己レビュー指摘: 評価完了前(applied/published)の実験も
        tested_traits/used_originsに反映され、別cornerへの重複輸入を防ぐ。"""
        _write_rows(self.source.history_file, [self._source_evaluated_row()])

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )
        self.assertEqual(decision["status"], "active")

        application_id = history.reserve_performance_decision(
            self.dest,
            "video",
            decision["decision_id"],
            hypothesis=performance.decision_hypothesis(decision),
        )
        self.assertIsNotNone(application_id)
        # 評価完了前(performance_applied)へ遷移。この行自体はhypothesisを持たない。
        history.apply_performance_decision(
            self.dest, "video", decision["decision_id"], str(application_id), "b-video-1"
        )

        # 別corner("news")が同じorigin/traitを重複輸入しようとしても拒否される。
        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            other_corner = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="news"
            )
        self.assertEqual(other_corner["status"], "insufficient_data")

    def test_end_to_end_local_experiment_propagates_via_real_pipeline(self) -> None:
        """PRレビュー指摘の検証: 展開元の仮説を手書き辞書ではなく、実際の
        build_decision/decision_hypothesis/reserve_performance_decision/
        complete_performance_evaluationのパイプラインを通して生成し、
        それが横展開されることを確認する(hand-authoredな辞書だけでは
        `decision_hypothesis`の`source`デフォルト等の回帰を検出できないため)。
        """
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
                    "analytics": {
                        "views": 100,
                        "average_view_percentage": 40 + index * 5,
                    },
                }
            )
        snapshot = {"collected_at": "2026-07-26T00:00:00+00:00", "videos": videos}

        local_decision = performance.build_decision(
            self.source, snapshot, corner_key="video"
        )
        self.assertEqual(local_decision["status"], "active")
        self.assertEqual(local_decision["positive_traits"], ["chart:present"])

        application_id = history.reserve_performance_decision(
            self.source,
            "video",
            local_decision["decision_id"],
            hypothesis=performance.decision_hypothesis(local_decision),
        )
        self.assertIsNotNone(application_id)
        history.apply_performance_decision(
            self.source,
            "video",
            local_decision["decision_id"],
            str(application_id),
            "id-7",
        )

        # 評価閾値に到達したsnapshotを与え、実際の_experiment_resultに
        # 有効性を判定させる(id-7が最高スコアなのでeffective=True)。
        evaluated_snapshot = json.loads(json.dumps(snapshot))
        evaluated_snapshot["collected_at"] = "2026-07-26T06:00:00+00:00"
        evaluated = performance.build_decision(
            self.source, evaluated_snapshot, corner_key="video"
        )
        self.assertEqual(evaluated["status"], "active")  # 次の実験へ解禁済み
        self.assertIsNone(
            history.active_performance_experiment(self.source, "video")
        )

        rows = [
            json.loads(line)
            for line in self.source.history_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        evaluated_row = next(
            row for row in rows if row.get("status") == "performance_evaluated"
        )
        self.assertTrue(evaluated_row["performance_result"]["effective"])
        self.assertEqual(evaluated_row["performance_hypothesis"]["source"], "local")

        # 展開先が、実パイプラインで生成された展開元の実験を輸入できる。
        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            imported = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )
        self.assertEqual(imported["status"], "active")
        self.assertEqual(imported["source"], "cross_channel")
        self.assertEqual(imported["positive_traits"], ["chart:present"])
        self.assertEqual(imported["origin"]["application_id"], application_id)

    def test_cancelled_import_can_be_reoffered_with_same_decision_id(self) -> None:
        """PRレビュー指摘の検証: cross_channelのdecision_idは決定的ハッシュだが、
        予約がcancelされた場合は(ローカル仮説と同様)同じdecision_idで
        再提示できる。`_performance_decision_used_rows`は最新行が
        `performance_cancelled`のapplication_idを使用済みに数えないため、
        恒久ブロックは発生しない。"""
        _write_rows(self.source.history_file, [self._source_evaluated_row()])

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )
        self.assertEqual(decision["status"], "active")

        application_id = history.reserve_performance_decision(
            self.dest,
            "video",
            decision["decision_id"],
            hypothesis=performance.decision_hypothesis(decision),
        )
        self.assertIsNotNone(application_id)
        history.cancel_performance_decision(
            self.dest,
            "video",
            decision["decision_id"],
            str(application_id),
            "generation failed",
        )

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            reoffered = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )
        self.assertEqual(reoffered["status"], "active")
        self.assertEqual(reoffered["decision_id"], decision["decision_id"])

        retry_application_id = history.reserve_performance_decision(
            self.dest,
            "video",
            reoffered["decision_id"],
            hypothesis=performance.decision_hypothesis(reoffered),
        )
        self.assertIsNotNone(retry_application_id)
        self.assertNotEqual(retry_application_id, application_id)

    def test_imports_when_local_signal_is_insufficient(self) -> None:
        """ローカルで比較可能な動画は十分でも、上位・下位を分ける単一
        traitがない(insufficient_signal)場合にも横展開が発動する。"""
        _write_rows(self.source.history_file, [self._source_evaluated_row()])
        videos = [
            {
                "video_id": f"id-{index}",
                "corner": "video",
                "format_traits": [
                    "tier:long_short",
                    "duration:60_to_179s",
                    "chart:present",
                ],
                "analytics": {
                    "views": 100,
                    "average_view_percentage": 40 + index * 5,
                },
            }
            for index in range(8)
        ]
        local_snapshot = {"collected_at": "2026-07-26T00:00:00+00:00", "videos": videos}

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, local_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "active")
        self.assertEqual(decision["source"], "cross_channel")

    def test_already_imported_origin_is_not_reoffered_after_evaluation(self) -> None:
        _write_rows(self.source.history_file, [self._source_evaluated_row()])

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )
        self.assertEqual(decision["status"], "active")

        application_id = history.reserve_performance_decision(
            self.dest,
            "video",
            decision["decision_id"],
            hypothesis=performance.decision_hypothesis(decision),
        )
        self.assertIsNotNone(application_id)
        history.apply_performance_decision(
            self.dest, "video", decision["decision_id"], str(application_id), "b-video-1"
        )
        applied_row = history.active_performance_experiment(self.dest, "video")
        history.complete_performance_evaluation(
            self.dest,
            applied_row,
            result={"effective": False, "metric": "m", "score": 1, "peer_median": 5, "peers": 4, "reason": ""},
        )

        # 同じorigin experimentは既に消費済みなので再提示されない。
        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            after = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )
        self.assertEqual(after["status"], "insufficient_data")

    def test_local_hypothesis_is_preferred_over_cross_channel(self) -> None:
        _write_rows(self.source.history_file, [self._source_evaluated_row()])
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
                    "analytics": {
                        "views": 100,
                        "average_view_percentage": 40 + index * 5,
                    },
                }
            )
        local_snapshot = {"collected_at": "2026-07-26T00:00:00+00:00", "videos": videos}

        with patch.object(
            performance.channel, "load", side_effect=lambda cid: self.source
        ):
            decision = performance.build_decision(
                self.dest, local_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "active")
        self.assertNotEqual(decision.get("source"), "cross_channel")

    def test_broken_source_channel_load_is_skipped_without_raising(self) -> None:
        with patch.object(
            performance.channel, "load", side_effect=RuntimeError("boom")
        ):
            decision = performance.build_decision(
                self.dest, self.insufficient_snapshot, corner_key="video"
            )

        self.assertEqual(decision["status"], "insufficient_data")


class FeedbackIssueCrossChannelSkipTest(unittest.TestCase):
    """cross_channel decisionはfeedback issue化されないことを検証する。"""

    def test_build_candidate_skips_cross_channel_decision(self) -> None:
        from doci import feedback_issues

        decision = {
            "status": "active",
            "source": "cross_channel",
            "channel": "channel-b",
            "corner": "video",
        }
        candidate, skip_reason = feedback_issues.build_candidate(
            SimpleNamespace(id="channel-b"), decision, corner_key="video"
        )
        self.assertIsNone(candidate)
        self.assertEqual(skip_reason, "cross_channel_decision")


if __name__ == "__main__":
    unittest.main()
