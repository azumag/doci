"""issue #106: 過去動画のnarrationからviewer_actionを抽出するbackfillのテスト。

対象: doci.tactic_backfill の 抽出・JSONL保存・レジューム・スキップ判定。
LLM呼び出し(run_codex/_run_opencode_go)は全てモックする。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import tactic_backfill


class TacticBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=self.root,
            history_file=self.root / "history.jsonl",
            pipeline={},
            pipeline_get=lambda k, d=None: d,
        )
        self.now = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)

    # --- fixtures ---

    def _write_history_row(
        self,
        *,
        video_id: str,
        status: str = "published",
        narration: str = "視聴後にYouTube Studioでオートダビングを有効にして確認する操作です。",
        workdir: str | None = None,
        viewer_action: str = "",
        title: str = "テスト動画",
        ts: str | None = None,
    ) -> Path | None:
        if workdir:
            wd = Path(workdir)
            wd.mkdir(parents=True, exist_ok=True)
            (wd / "script.json").write_text(
                json.dumps({"narration": narration}), encoding="utf-8"
            )
        row = {
            "ts": ts or self.now.isoformat(),
            "channel": self.spec.id,
            "corner": "shorts",
            "title": title,
            "video_id": video_id,
            "status": status,
            "topic": "テスト題材",
        }
        if workdir:
            row["workdir"] = workdir
        if viewer_action:
            row["topic_metadata"] = {"viewer_action": viewer_action}
        self.spec.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.spec.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
        return wd if workdir else None

    def _read_backfill_rows(self) -> list[dict]:
        path = tactic_backfill._backfill_path(self.spec)
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]

    def _mock_extract(self, result: dict | None = None, error: Exception | None = None):
        def side_effect(narration, *, backend, timeout):
            if error is not None:
                raise error
            return result or {"viewer_action": "施策", "youtube_creator_problem": "課題"}

        return patch.object(tactic_backfill, "_extract", side_effect=side_effect)

    # --- 1. 抽出と保存 ---

    def test_extracts_and_appends_rows(self) -> None:
        wd = self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        with self._mock_extract(
            {"viewer_action": "オートダビングを有効化する", "youtube_creator_problem": "課題"}
        ):
            result = tactic_backfill.run(self.spec)

        self.assertEqual(len(result["processed"]), 1)
        self.assertEqual(result["processed"][0]["video_id"], "v1")
        rows = self._read_backfill_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["video_id"], "v1")
        self.assertEqual(row["status"], "extracted")
        self.assertEqual(row["viewer_action"], "オートダビングを有効化する")
        self.assertEqual(row["youtube_creator_problem"], "課題")
        self.assertEqual(row["narration_len"], len("視聴後にYouTube Studioでオートダビングを有効にして確認する操作です。"))

    def test_empty_viewer_action_recorded_as_empty(self) -> None:
        self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        with self._mock_extract(
            {"viewer_action": "", "youtube_creator_problem": ""}
        ):
            result = tactic_backfill.run(self.spec)

        self.assertEqual(result["processed"][0]["status"], "empty")
        rows = self._read_backfill_rows()
        self.assertEqual(rows[0]["status"], "empty")
        self.assertEqual(rows[0]["viewer_action"], "")

    def test_llm_error_recorded_as_error_and_does_not_stop_batch(self) -> None:
        self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        self._write_history_row(video_id="v2", workdir=str(self.root / "wd2"))
        with self._mock_extract(error=RuntimeError("LLM失敗")):
            result = tactic_backfill.run(self.spec)

        self.assertEqual(len(result["processed"]), 0)
        self.assertEqual(len(result["errors"]), 2)
        rows = self._read_backfill_rows()
        self.assertEqual({r["status"] for r in rows}, {"error"})
        self.assertIn("LLM失敗", rows[0]["error"])

    # --- 2. 冪等・レジューム ---

    def test_extracted_video_skipped_on_rerun(self) -> None:
        self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        with self._mock_extract():
            tactic_backfill.run(self.spec)

        with self._mock_extract() as mock_extract:
            result = tactic_backfill.run(self.spec)
        mock_extract.assert_not_called()
        self.assertEqual(len(result["processed"]), 0)

    def test_error_video_skipped_unless_retry_errors(self) -> None:
        self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        with self._mock_extract(error=RuntimeError("一時失敗")):
            tactic_backfill.run(self.spec)

        with self._mock_extract(error=RuntimeError("まだ失敗")) as mock_extract:
            result = tactic_backfill.run(self.spec)
        mock_extract.assert_not_called()
        self.assertEqual(len(result["processed"]), 0)

        with self._mock_extract(
            {"viewer_action": "復旧", "youtube_creator_problem": ""}
        ):
            result = tactic_backfill.run(self.spec, retry_errors=True)
        self.assertEqual(result["processed"][0]["status"], "extracted")

    def test_only_video_id_limits_scope(self) -> None:
        self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        self._write_history_row(video_id="v2", workdir=str(self.root / "wd2"))
        with self._mock_extract() as mock_extract:
            result = tactic_backfill.run(self.spec, only_video_ids=["v2"])
        mock_extract.assert_called_once()
        self.assertEqual(result["processed"][0]["video_id"], "v2")

    def test_limit_stops_after_n(self) -> None:
        for i in range(3):
            self._write_history_row(
                video_id=f"v{i}", workdir=str(self.root / f"wd{i}")
            )
        with self._mock_extract() as mock_extract:
            result = tactic_backfill.run(self.spec, limit=2)
        self.assertEqual(mock_extract.call_count, 2)
        self.assertEqual(len(result["processed"]), 2)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "limit_reached")

    # --- 3. スキップ判定 ---

    def test_already_extracted_video_skipped(self) -> None:
        self._write_history_row(
            video_id="v1",
            workdir=str(self.root / "wd1"),
            viewer_action="既存の施策",
        )
        with self._mock_extract() as mock_extract:
            result = tactic_backfill.run(self.spec)
        mock_extract.assert_not_called()
        self.assertEqual(len(result["processed"]), 0)

    def test_non_published_and_workdir_missing_skipped(self) -> None:
        self._write_history_row(video_id="q1", status="queued")
        self._write_history_row(video_id="p1")  # workdir無し → narration取れず
        with self._mock_extract() as mock_extract:
            result = tactic_backfill.run(self.spec)
        mock_extract.assert_not_called()
        # p1はnarration欠落としてempty記録される
        self.assertEqual(len(result["errors"]), 1)
        rows = self._read_backfill_rows()
        self.assertEqual(rows[0]["status"], "empty")

    # --- 4. プロンプト整合 ---

    def test_prompt_contains_viewer_action_definition(self) -> None:
        self.assertIn("viewer_action", tactic_backfill._EXTRACT_PROMPT)
        self.assertIn("youtube_creator_problem", tactic_backfill._EXTRACT_PROMPT)
        self.assertIn("空文字", tactic_backfill._EXTRACT_PROMPT)
        self.assertIn("{narration}", tactic_backfill._EXTRACT_PROMPT)

    def test_extract_uses_codex_without_web_fetch(self) -> None:
        from doci import llm

        with patch.object(llm, "run_codex", return_value='{"viewer_action": "X", "youtube_creator_problem": "Y"}') as mock_codex:
            result = tactic_backfill._extract(
                "ナレーション本文", backend="codex", timeout=120
            )
        mock_codex.assert_called_once()
        _, kwargs = mock_codex.call_args
        self.assertEqual(kwargs["min_web_fetches"], 0)
        self.assertEqual(result["viewer_action"], "X")

    def test_extract_rejects_unknown_backend(self) -> None:
        with self.assertRaises(RuntimeError):
            tactic_backfill._extract("本文", backend="claude", timeout=120)

    def test_run_rejects_unsupported_backend_before_writing_rows(self) -> None:
        """未対応バックエンドは一括拒否され、error行を書き残さない。"""
        self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        with self.assertRaisesRegex(RuntimeError, "未対応のバックフィル抽出バックエンド"):
            tactic_backfill.run(self.spec, backend="claude")
        self.assertEqual(self._read_backfill_rows(), [])

    def test_only_video_id_reprocesses_empty_row(self) -> None:
        """--onlyで明示指定された動画はempty済みでも再抽出できる。"""
        self._write_history_row(video_id="v1", workdir=str(self.root / "wd1"))
        with self._mock_extract({"viewer_action": "", "youtube_creator_problem": ""}):
            tactic_backfill.run(self.spec)
        self.assertEqual(self._read_backfill_rows()[0]["status"], "empty")

        with self._mock_extract(
            {"viewer_action": "再抽出施策", "youtube_creator_problem": ""}
        ) as mock_extract:
            result = tactic_backfill.run(self.spec, only_video_ids=["v1"])
        mock_extract.assert_called_once()
        self.assertEqual(result["processed"][0]["status"], "extracted")
        rows = self._read_backfill_rows()
        self.assertEqual(rows[-1]["viewer_action"], "再抽出施策")


if __name__ == "__main__":
    unittest.main()
