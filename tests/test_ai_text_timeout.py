"""台本執筆タイムアウト設定のテスト（ネットワーク不要）。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import ai_text, config


class WriteTimeoutTest(unittest.TestCase):
    def test_zero_disables_opencode_timeout(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(ai_text.subprocess, "run", return_value=completed) as run_mock,
        ):
            ai_text._run_opencode("prompt", "opencode-go/qwen3.7-plus", "")

        self.assertIsNone(run_mock.call_args.kwargs["timeout"])

    def test_positive_value_is_kept(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 900),
            mock.patch.object(ai_text.subprocess, "run", return_value=completed) as run_mock,
        ):
            ai_text._run_opencode("prompt", "opencode-go/qwen3.7-plus", "")

        self.assertEqual(run_mock.call_args.kwargs["timeout"], 900)

    def test_zero_disables_claude_fallback_timeout(self) -> None:
        with (
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(ai_text.llm, "run_claude", return_value="{}") as run_mock,
        ):
            ai_text._run_claude_cli("prompt", "claude-sonnet-5")

        self.assertIsNone(run_mock.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
