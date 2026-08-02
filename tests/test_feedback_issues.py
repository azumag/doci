"""issue #39: Analytics decisionからfeedback issueを安全に生成するコマンドのテスト。

対象: doci.feedback_issues の fingerprint/build_candidate/run。
gh呼び出しは全て feedback_issues._run_gh をモックする。重複検索は
`gh issue list --label feedback --state all` (即時反映) を使うため、
`_search_response` は `gh issue list --json number,url,state,body` と同じ
「配列」形式を返す（Search APIの `{total_count, items}` 形式ではない）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import feedback_issues, performance


def _search_response(items: list[dict]) -> str:
    return json.dumps(items)


def _issue_row(*, number: int, body: str, state: str) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/azumag/doci/issues/{number}",
        "state": state,
        "body": body,
    }


class FeedbackIssuesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=self.root,
            history_file=self.root / "history.jsonl",
            publish=SimpleNamespace(
                youtube=SimpleNamespace(
                    review=SimpleNamespace(repository="azumag/doci"),
                )
            ),
        )

    # --- fixtures ---

    def _decision(self, **overrides) -> dict:
        decision = {
            "schema_version": 1,
            "decision_id": "decisionid00001a",
            "channel": self.spec.id,
            "corner": "shorts",
            "snapshot_at": "2026-08-01T00:00:00+00:00",
            "metric": "youtube_data_api_v3.views_per_day",
            "format_cohort": "duration:60_to_179s|tier:short",
            "eligible_video_ids": [f"vid{i}" for i in range(8)],
            "min_samples": 8,
            "min_group_size": 2,
            "min_trait_support": 2,
            "source_status": {
                "data_api": {"available": True, "source": "youtube_data_api_v3"},
                "analytics": {"available": True},
            },
            "guardrails": ["相関を因果と断定しない"],
            "status": "active",
            "reason": "同一corner・同一尺・同一tier cohortの相対上位・下位群から単一の形式仮説を作成",
            "top_video_ids": ["vid0", "vid1"],
            "bottom_video_ids": ["vid6", "vid7"],
            "positive_traits": ["thumbnail:face_closeup"],
            "negative_traits": [],
            "guidance": "実験仮説として使う",
        }
        decision.update(overrides)
        return decision

    def _write_decision(self, decision: dict) -> None:
        path = performance._decision_path(self.spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")

    def _write_snapshot(self, collected_at: str) -> None:
        path = performance._snapshot_path(self.spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"collected_at": collected_at}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _write_active_setup(self, **overrides) -> dict:
        decision = self._decision(**overrides)
        self._write_decision(decision)
        self._write_snapshot(decision["snapshot_at"])
        return decision

    def _write_history_row(self, **overrides) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
            "feedback_id": "fb-0000000000000000",
            "fingerprint": "0000000000000000",
            "decision_id": "d",
            "source_snapshot_at": "s",
            "hypothesis_key": "shorts|m|t",
            "issue_number": 1,
            "issue_url": "https://github.com/azumag/doci/issues/1",
            "status": "created",
            "reason": "",
        }
        row.update(overrides)
        path = feedback_issues._history_path(self.spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 1. 同一decision再実行でissue作成は1回のみ

    def test_same_snapshot_twice_creates_single_issue(self) -> None:
        self._write_active_setup()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/501",
            ]
            first = feedback_issues.run(self.spec, apply=True)
        self.assertEqual(first["created"]["number"], 501)
        self.assertEqual(mock_run_gh.call_count, 2)

        with patch.object(feedback_issues, "_run_gh") as mock_run_gh2:
            second = feedback_issues.run(self.spec, apply=True)
        mock_run_gh2.assert_not_called()
        self.assertEqual(second["skip_reason"], "local_created")

    # 2. 重複判定がopen/closed両方のissueに効く

    def test_duplicate_check_matches_open_and_closed_issue(self) -> None:
        decision = self._write_active_setup()
        fp = feedback_issues.fingerprint(decision)
        for state in ("open", "closed"):
            with self.subTest(state=state):
                body = f"<!-- doci-feedback:{fp} -->\n本文"
                with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
                    mock_run_gh.side_effect = [
                        _search_response(
                            [_issue_row(number=42, body=body, state=state.upper())]
                        )
                    ]
                    result = feedback_issues.run(self.spec, apply=True)
                self.assertEqual(result["skip_reason"], "duplicate_remote")
                self.assertEqual(result["existing_issue"]["number"], 42)
                self.assertEqual(mock_run_gh.call_count, 1)
                # 次のsubTestのためにローカル履歴のduplicate行をクリアしない
                # (再実行してもlocal fast-pathでなくremote検索へ到達することを
                #  確認したいので、履歴ファイルを削除して独立させる)
                feedback_issues._history_path(self.spec).unlink()

    # 3. snapshot_atのみ違う同一仮説はfingerprintが変わらず新規issueを作らない

    def test_unchanged_hypothesis_reuses_fingerprint(self) -> None:
        d1 = self._decision(snapshot_at="2026-08-01T00:00:00+00:00")
        d2 = self._decision(
            snapshot_at="2026-08-08T00:00:00+00:00",
            decision_id="decisionid00002b",
        )
        self.assertEqual(feedback_issues.fingerprint(d1), feedback_issues.fingerprint(d2))

        self._write_decision(d1)
        self._write_snapshot(d1["snapshot_at"])
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/700",
            ]
            first = feedback_issues.run(self.spec, apply=True)
        self.assertIsNotNone(first["created"])

        self._write_decision(d2)
        self._write_snapshot(d2["snapshot_at"])
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh2:
            second = feedback_issues.run(self.spec, apply=True)
        mock_run_gh2.assert_not_called()
        self.assertEqual(second["skip_reason"], "local_created")

    # 4. Analytics不足時はgh不呼び出し

    def test_no_gh_call_when_analytics_unavailable(self) -> None:
        self._write_active_setup(
            source_status={
                "data_api": {"available": True, "source": "x"},
                "analytics": {"available": False, "reason": "未認証"},
            }
        )
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.run(self.spec, apply=True)
        mock_run_gh.assert_not_called()
        self.assertIsNone(result["candidate"])
        self.assertEqual(result["skip_reason"], "analytics_unavailable")

    # 5. active以外のstatusではgh不呼び出し

    def test_no_gh_call_when_status_not_active(self) -> None:
        for status in (
            "insufficient_data",
            "insufficient_signal",
            "waiting_for_publish",
            "waiting_for_result",
            "waiting",
        ):
            with self.subTest(status=status):
                self._write_active_setup(status=status)
                with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
                    result = feedback_issues.run(self.spec, apply=True)
                mock_run_gh.assert_not_called()
                self.assertIsNone(result["candidate"])
                self.assertEqual(result["skip_reason"], status)

    # 6. dry-runは外部状態を一切変更しない

    def test_dry_run_changes_no_state(self) -> None:
        self._write_active_setup()
        decision_path = performance._decision_path(self.spec)
        snapshot_path = performance._snapshot_path(self.spec)
        before_decision = decision_path.read_bytes()
        before_snapshot = snapshot_path.read_bytes()
        before_mtime = decision_path.stat().st_mtime_ns

        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.run(self.spec, apply=False)

        mock_run_gh.assert_not_called()
        self.assertEqual(result["mode"], "dry-run")
        self.assertIn("doci-feedback:", result["candidate"]["body"])
        self.assertFalse(feedback_issues._history_path(self.spec).exists())
        self.assertFalse(feedback_issues._lock_path(self.spec).exists())
        self.assertEqual(decision_path.read_bytes(), before_decision)
        self.assertEqual(snapshot_path.read_bytes(), before_snapshot)
        self.assertEqual(decision_path.stat().st_mtime_ns, before_mtime)

    # 7. 週次上限

    def test_apply_respects_weekly_limit(self) -> None:
        now = datetime.now(timezone.utc)
        for i in range(3):
            self._write_history_row(
                fingerprint=f"aaaaaaaaaaaaaaa{i}",
                hypothesis_key=f"other|metric|trait{i}",
                ts=(now - timedelta(days=1)).isoformat(),
            )
        self._write_active_setup()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.run(self.spec, apply=True)
        mock_run_gh.assert_not_called()
        self.assertEqual(result["skip_reason"], "weekly_limit_reached")

    def test_weekly_limit_ignores_rows_older_than_seven_days(self) -> None:
        now = datetime.now(timezone.utc)
        for i in range(3):
            self._write_history_row(
                fingerprint=f"bbbbbbbbbbbbbbb{i}",
                hypothesis_key=f"other|metric|trait{i}",
                ts=(now - timedelta(days=8)).isoformat(),
            )
        self._write_active_setup()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/900",
            ]
            result = feedback_issues.run(self.spec, apply=True)
        self.assertIsNotNone(result["created"])

    def test_naive_timestamp_in_history_does_not_crash(self) -> None:
        # tzオフセットの無いISO文字列はfromisoformatの解析自体は成功するため、
        # 比較時のTypeErrorも安全にskipされ、apply全体がクラッシュしないことを確認する。
        self._write_history_row(
            fingerprint="cccccccccccccccc",
            hypothesis_key="other|metric|trait",
            ts="2026-08-01T12:00:00",
        )
        self._write_active_setup()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/901",
            ]
            result = feedback_issues.run(self.spec, apply=True)
        self.assertIsNotNone(result["created"])

    # 8. 実行あたり上限

    def test_apply_respects_per_run_limit(self) -> None:
        self._write_active_setup()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.run(self.spec, apply=True, max_issues=0)
        mock_run_gh.assert_not_called()
        self.assertEqual(result["skip_reason"], "run_limit_reached")

    # 9. create失敗後の再実行で二重作成しない

    def test_rerun_after_create_failure_no_double_create(self) -> None:
        decision = self._write_active_setup()
        fp = feedback_issues.fingerprint(decision)
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                RuntimeError("GitHub操作に失敗しました (rc=1): network error"),
            ]
            with self.assertRaises(RuntimeError):
                feedback_issues.run(self.spec, apply=True)
        self.assertEqual(mock_run_gh.call_count, 2)

        records = feedback_issues._read_records(self.spec)
        self.assertEqual(records[-1]["status"], "creating")

        body = f"<!-- doci-feedback:{fp} -->\n本文"
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh2:
            mock_run_gh2.side_effect = [
                _search_response([_issue_row(number=77, body=body, state="OPEN")])
            ]
            second = feedback_issues.run(self.spec, apply=True)
        self.assertEqual(second["skip_reason"], "duplicate_remote")
        self.assertEqual(mock_run_gh2.call_count, 1)
        create_calls = [
            call
            for call in mock_run_gh2.call_args_list
            if call.args[0][:2] == ["issue", "create"]
        ]
        self.assertEqual(create_calls, [])

    # 10. issue本文から根拠を追跡できる

    def test_body_traceability(self) -> None:
        decision = self._decision(
            top_video_ids=["topA", "topB"],
            bottom_video_ids=["botA", "botB"],
        )
        fp = feedback_issues.fingerprint(decision)
        body = feedback_issues._issue_body(decision, fp)
        match = feedback_issues._FEEDBACK_MARKER_RE.search(body)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), fp)
        for video_id in ("topA", "topB", "botA", "botB"):
            self.assertIn(video_id, body)
        self.assertIn(decision["metric"], body)
        self.assertIn(decision["decision_id"], body)
        self.assertIn(fp, body)  # 可視テキストにも記載(GitHub検索対象)

    # 11. fingerprintの感度

    def test_fingerprint_sensitivity(self) -> None:
        base = self._decision()
        same_snapshot_only = self._decision(
            snapshot_at="2099-01-01T00:00:00+00:00",
            decision_id="different-decision-id",
        )
        different_videos = self._decision(top_video_ids=["other0", "other1"])
        different_trait = self._decision(positive_traits=["thumbnail:text_overlay"])

        self.assertEqual(
            feedback_issues.fingerprint(base),
            feedback_issues.fingerprint(same_snapshot_only),
        )
        self.assertNotEqual(
            feedback_issues.fingerprint(base),
            feedback_issues.fingerprint(different_videos),
        )
        self.assertNotEqual(
            feedback_issues.fingerprint(base),
            feedback_issues.fingerprint(different_trait),
        )

    # 12. decisionとperformance.jsonlの鮮度不一致でskip

    def test_stale_decision_skipped(self) -> None:
        decision = self._decision(snapshot_at="2026-08-01T00:00:00+00:00")
        self._write_decision(decision)
        self._write_snapshot("2026-08-02T00:00:00+00:00")
        candidate, reason = feedback_issues.build_candidate(self.spec, decision)
        self.assertIsNone(candidate)
        self.assertEqual(reason, "stale_decision")

    # 13. search結果の不整合で安全側停止

    def test_search_overflow_aborts_creation(self) -> None:
        self._write_active_setup()
        with (
            patch.object(feedback_issues, "_ISSUE_LIST_LIMIT", 2),
            patch.object(feedback_issues, "_run_gh") as mock_run_gh,
        ):
            mock_run_gh.return_value = _search_response(
                [
                    _issue_row(number=1, body="関係ない issue", state="OPEN"),
                    _issue_row(number=2, body="別の issue", state="CLOSED"),
                ]
            )
            with self.assertRaises(RuntimeError):
                feedback_issues.run(self.spec, apply=True)
        self.assertEqual(mock_run_gh.call_count, 1)

    # 14. 同一仮説のcooldown内はfingerprintが違ってもskip

    def test_duplicate_hypothesis_cooldown(self) -> None:
        decision = self._write_active_setup()
        hypothesis_key = feedback_issues._hypothesis_key(decision)
        self._write_history_row(
            fingerprint="ffffffffffffffff",
            hypothesis_key=hypothesis_key,
            ts=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.run(self.spec, apply=True)
        mock_run_gh.assert_not_called()
        self.assertEqual(result["skip_reason"], "duplicate_hypothesis")


if __name__ == "__main__":
    unittest.main()
