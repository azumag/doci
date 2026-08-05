"""issue #39起点、issue #92でcycle単位に一般化: feedback issueの重複防止・週次
レート制御・排他ロック・GitHub I/Oという「機構層」のテスト。

`doci.feedback_issues` は候補(candidate)の中身（何を仮説にするか）を一切解釈
しないため、ここでは`{fingerprint, hypothesis_keys, title, body}`を持つ汎用
candidateフィクスチャで機構だけを検証する。「decisionから候補を組み立てる」
ロジックのテストは `doci.performance_report` 側にある。

gh呼び出しは全て feedback_issues._run_gh をモックする。重複検索は
`gh issue list --label feedback --state all` (即時反映) を使うため、
`_search_response` は `gh issue list --json number,url,state,body,createdAt` と
同じ「配列」形式を返す（Search APIの `{total_count, items}` 形式ではない）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import config, feedback_issues


def _search_response(items: list[dict]) -> str:
    return json.dumps(items)


def _issue_row(
    *, number: int, body: str, state: str, created_at: str | None = None
) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/azumag/doci/issues/{number}",
        "state": state,
        "body": body,
        "createdAt": created_at or datetime.now(timezone.utc).isoformat(),
    }


class FeedbackIssuesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        pipeline = {"feedback_repository": "azumag/doci"}
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=self.root,
            pipeline=pipeline,
            pipeline_get=pipeline.get,
        )

    # --- fixtures ---

    def _candidate(
        self,
        *,
        fp: str = "a" * 16,
        hypothesis_keys: list[str] | None = None,
        title: str = "[feedback] youtube-growth 実績レポート",
        extra_body: str = "本文",
    ) -> dict:
        keys = hypothesis_keys if hypothesis_keys is not None else ["shorts|metric|chart:present"]
        markers = "\n".join(
            [
                feedback_issues.feedback_marker(fp),
                feedback_issues.channel_marker(self.spec.id),
                *[feedback_issues.hypothesis_marker(key) for key in keys],
            ]
        )
        return {
            "fingerprint": fp,
            "hypothesis_keys": keys,
            "title": title,
            "body": f"{markers}\n{extra_body}",
        }

    def _write_history_row(self, **overrides) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 2,
            "feedback_id": "fb-0000000000000000",
            "fingerprint": "0000000000000000",
            "channel": self.spec.id,
            "hypothesis_keys": ["shorts|metric|other"],
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

    # 1. 同一candidate再実行でissue作成は1回のみ

    def test_same_candidate_twice_creates_single_issue(self) -> None:
        candidate = self._candidate()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/501",
            ]
            first = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertEqual(first["created"]["number"], 501)
        self.assertEqual(mock_run_gh.call_count, 2)

        with patch.object(feedback_issues, "_run_gh") as mock_run_gh2:
            second = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        mock_run_gh2.assert_not_called()
        self.assertEqual(second["skip_reason"], "local_created")

    # 2. 重複判定がopen/closed両方のissueに効く

    def test_duplicate_check_matches_open_and_closed_issue(self) -> None:
        candidate = self._candidate(fp="b" * 16)
        for state in ("open", "closed"):
            with self.subTest(state=state):
                with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
                    mock_run_gh.side_effect = [
                        _search_response(
                            [_issue_row(number=42, body=candidate["body"], state=state.upper())]
                        )
                    ]
                    result = feedback_issues.submit_candidate(
                        self.spec, candidate, apply=True
                    )
                self.assertEqual(result["skip_reason"], "duplicate_remote")
                self.assertEqual(result["existing_issue"]["number"], 42)
                self.assertEqual(mock_run_gh.call_count, 1)
                feedback_issues._history_path(self.spec).unlink()

    # 3. dry-runは外部状態を一切変更しない

    def test_dry_run_changes_no_state(self) -> None:
        candidate = self._candidate()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=False)

        mock_run_gh.assert_not_called()
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["candidate"], candidate)
        self.assertFalse(feedback_issues._history_path(self.spec).exists())
        self.assertFalse(feedback_issues._lock_path(self.spec).exists())

    # 4. no_repository

    def test_no_repository_skips_without_gh_call(self) -> None:
        spec = SimpleNamespace(id="no-repo", output_dir=self.root, pipeline={}, pipeline_get={}.get)
        candidate = self._candidate()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.submit_candidate(spec, candidate, apply=True)
        mock_run_gh.assert_not_called()
        self.assertEqual(result["skip_reason"], "no_repository")

    # 5. 週次上限（ローカル、7日以内のみ集計）

    def test_apply_respects_weekly_limit(self) -> None:
        now = datetime.now(timezone.utc)
        for i in range(3):
            self._write_history_row(
                fingerprint=f"aaaaaaaaaaaaaaa{i}",
                hypothesis_keys=[f"other|metric|trait{i}"],
                ts=(now - timedelta(days=1)).isoformat(),
            )
        candidate = self._candidate(fp="c" * 16)
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        mock_run_gh.assert_not_called()
        self.assertEqual(result["skip_reason"], "weekly_limit_reached")

    def test_weekly_limit_ignores_rows_older_than_seven_days(self) -> None:
        now = datetime.now(timezone.utc)
        for i in range(3):
            self._write_history_row(
                fingerprint=f"bbbbbbbbbbbbbbb{i}",
                hypothesis_keys=[f"other|metric|trait{i}"],
                ts=(now - timedelta(days=8)).isoformat(),
            )
        candidate = self._candidate(fp="d" * 16)
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/900",
            ]
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertIsNotNone(result["created"])

    def test_naive_timestamp_in_history_does_not_crash(self) -> None:
        # tzオフセットの無いISO文字列はfromisoformatの解析自体は成功するため、
        # 比較時のTypeErrorも安全にskipされ、apply全体がクラッシュしないことを確認する。
        self._write_history_row(
            fingerprint="cccccccccccccccc",
            hypothesis_keys=["other|metric|trait"],
            ts="2026-08-01T12:00:00",
        )
        candidate = self._candidate(fp="e" * 16)
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/901",
            ]
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertIsNotNone(result["created"])

    def test_weekly_limit_is_scoped_per_channel(self) -> None:
        """複数channelが同一repositoryを共有する構成で、他channelの発行は
        このchannelの週次枠を食い潰さない。"""
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        other_channel_rows = [
            _issue_row(
                number=200 + i,
                body=(
                    f"{feedback_issues.feedback_marker('f' * 15 + str(i))}\n"
                    f"{feedback_issues.channel_marker('other-channel')}\n本文"
                ),
                state="OPEN",
                created_at=recent,
            )
            for i in range(config.FEEDBACK_ISSUES_MAX_PER_WEEK)
        ]
        candidate = self._candidate(fp="1" * 16)
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response(other_channel_rows),
                "https://github.com/azumag/doci/issues/902",
            ]
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertIsNotNone(result["created"])

    # 6. 実行あたり上限

    def test_apply_respects_per_run_limit(self) -> None:
        candidate = self._candidate()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.submit_candidate(
                self.spec, candidate, apply=True, max_issues=0
            )
        mock_run_gh.assert_not_called()
        self.assertEqual(result["skip_reason"], "run_limit_reached")

    # 7. create失敗後の再実行で二重作成しない

    def test_rerun_after_create_failure_no_double_create(self) -> None:
        candidate = self._candidate(fp="2" * 16)
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response([]),
                RuntimeError("GitHub操作に失敗しました (rc=1): network error"),
            ]
            with self.assertRaises(RuntimeError):
                feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertEqual(mock_run_gh.call_count, 2)

        records = feedback_issues._read_records(self.spec)
        self.assertEqual(records[-1]["status"], "creating")

        with patch.object(feedback_issues, "_run_gh") as mock_run_gh2:
            mock_run_gh2.side_effect = [
                _search_response([_issue_row(number=77, body=candidate["body"], state="OPEN")])
            ]
            second = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertEqual(second["skip_reason"], "duplicate_remote")
        self.assertEqual(mock_run_gh2.call_count, 1)
        create_calls = [
            call
            for call in mock_run_gh2.call_args_list
            if call.args[0][:2] == ["issue", "create"]
        ]
        self.assertEqual(create_calls, [])

    # 8. search結果の不整合で安全側停止

    def test_search_overflow_aborts_creation(self) -> None:
        candidate = self._candidate()
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
                feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertEqual(mock_run_gh.call_count, 1)

    # 9. 同一仮説のcooldown内はfingerprintが違ってもskip（ローカル）

    def test_duplicate_hypothesis_cooldown_local(self) -> None:
        key = "shorts|metric|chart:present"
        self._write_history_row(
            fingerprint="ffffffffffffffff",
            hypothesis_keys=[key],
            ts=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        candidate = self._candidate(fp="3" * 16, hypothesis_keys=[key])
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        mock_run_gh.assert_not_called()
        self.assertEqual(result["skip_reason"], "duplicate_hypothesis")

    # 10. ローカル履歴が空(ephemeralなCI等を想定)でも、同一仮説のissueが
    #     cooldown内にremoteへ存在すれば重複作成しない

    def test_duplicate_hypothesis_detected_remotely_without_local_history(self) -> None:
        key = "shorts|metric|chart:present"
        # fingerprintは対象動画集合が変わるため一致しないが、仮説(hypothesis)は同じ、
        # というシナリオ(次サイクルでtop/bottom video groupが入れ替わった想定)。
        other_body = "\n".join(
            [
                feedback_issues.feedback_marker("a" * 16),
                feedback_issues.channel_marker(self.spec.id),
                feedback_issues.hypothesis_marker(key),
                "本文",
            ]
        )
        candidate = self._candidate(fp="4" * 16, hypothesis_keys=[key])
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response(
                    [_issue_row(number=88, body=other_body, state="OPEN", created_at=recent)]
                )
            ]
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertEqual(result["skip_reason"], "duplicate_hypothesis_remote")
        self.assertEqual(result["existing_issue"]["number"], 88)
        self.assertEqual(mock_run_gh.call_count, 1)

    def test_hypothesis_marker_older_than_cooldown_does_not_block_creation(self) -> None:
        key = "shorts|metric|chart:present"
        other_body = "\n".join(
            [
                feedback_issues.feedback_marker("a" * 16),
                feedback_issues.channel_marker(self.spec.id),
                feedback_issues.hypothesis_marker(key),
                "本文",
            ]
        )
        candidate = self._candidate(fp="5" * 16, hypothesis_keys=[key])
        old = (
            datetime.now(timezone.utc)
            - timedelta(days=config.FEEDBACK_ISSUES_HYPOTHESIS_COOLDOWN_DAYS + 10)
        ).isoformat()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response(
                    [_issue_row(number=89, body=other_body, state="CLOSED", created_at=old)]
                ),
                "https://github.com/azumag/doci/issues/902",
            ]
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertIsNotNone(result["created"])

    # 11. ローカル履歴が空(ephemeralなCI等)でも、remote側のcreatedAtだけで
    #     週次上限を検出できる

    def test_weekly_limit_enforced_remotely_without_local_history(self) -> None:
        candidate = self._candidate(fp="6" * 16)
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        remote_rows = [
            _issue_row(
                number=100 + i,
                body=(
                    f"{feedback_issues.feedback_marker('0123456789abcdef'[i] * 16)}\n"
                    f"{feedback_issues.channel_marker(self.spec.id)}\n本文"
                ),
                state="OPEN",
                created_at=recent,
            )
            for i in range(config.FEEDBACK_ISSUES_MAX_PER_WEEK)
        ]
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [_search_response(remote_rows)]
            result = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertEqual(result["skip_reason"], "weekly_limit_reached")
        self.assertEqual(mock_run_gh.call_count, 1)

    # 12. duplicate_hypothesis_remoteによるローカル記録は恒久ブロックにならない

    def test_hypothesis_remote_duplicate_does_not_permanently_block_locally(self) -> None:
        key = "shorts|metric|chart:present"
        other_body = "\n".join(
            [
                feedback_issues.feedback_marker("b" * 16),
                feedback_issues.channel_marker(self.spec.id),
                feedback_issues.hypothesis_marker(key),
                "本文",
            ]
        )
        candidate = self._candidate(fp="7" * 16, hypothesis_keys=[key])
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh:
            mock_run_gh.side_effect = [
                _search_response(
                    [_issue_row(number=90, body=other_body, state="OPEN", created_at=recent)]
                )
            ]
            first = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertEqual(first["skip_reason"], "duplicate_hypothesis_remote")

        records = feedback_issues._read_records(self.spec)
        self.assertEqual(records[-1]["status"], "duplicate_hypothesis")

        # "duplicate_hypothesis" は _local_terminal_record の対象
        # ("created"/"duplicate") に含まれないため、次回実行は再度remoteを
        # 確認できる(cooldown経過を想定しremoteが空を返すケース)。
        with patch.object(feedback_issues, "_run_gh") as mock_run_gh2:
            mock_run_gh2.side_effect = [
                _search_response([]),
                "https://github.com/azumag/doci/issues/903",
            ]
            second = feedback_issues.submit_candidate(self.spec, candidate, apply=True)
        self.assertIsNotNone(second["created"])
        self.assertEqual(mock_run_gh2.call_count, 2)

    # 13. recent_hypothesis_keys: 'created'状態・cooldown内の行だけを拾う

    def test_recent_hypothesis_keys_filters_status_and_cooldown(self) -> None:
        now = datetime.now(timezone.utc)
        self._write_history_row(
            fingerprint="8" * 16,
            hypothesis_keys=["shorts|metric|chart:present"],
            status="created",
            ts=(now - timedelta(days=1)).isoformat(),
        )
        self._write_history_row(
            fingerprint="9" * 16,
            hypothesis_keys=["video|metric|scenes:5_to_8"],
            status="duplicate",
            ts=(now - timedelta(days=1)).isoformat(),
        )
        self._write_history_row(
            fingerprint="0" * 16,
            hypothesis_keys=["analytics|metric|duration:under_60s"],
            status="created",
            ts=(now - timedelta(days=config.FEEDBACK_ISSUES_HYPOTHESIS_COOLDOWN_DAYS + 5)).isoformat(),
        )
        keys = feedback_issues.recent_hypothesis_keys(self.spec, now=now)
        self.assertEqual(keys, {"shorts|metric|chart:present"})

    # 14. hypothesis_hashは決定的で、キーごとに異なる

    def test_hypothesis_hash_is_deterministic_and_distinct(self) -> None:
        h1 = feedback_issues.hypothesis_hash("youtube-growth|shorts|metric|chart:present")
        h2 = feedback_issues.hypothesis_hash("youtube-growth|shorts|metric|chart:present")
        h3 = feedback_issues.hypothesis_hash("youtube-growth|video|metric|chart:present")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)


if __name__ == "__main__":
    unittest.main()
