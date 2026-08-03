from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config, llm
from doci.llm import _ensure_codex_home, _parse_codex_events


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


class ParseCodexEventsTest(unittest.TestCase):
    def test_mixed_command_execution_and_agent_message(self) -> None:
        stdout = "\n".join(
            [
                _line(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "curl -s https://duckduckgo.com/html/?q=test",
                            "exit_code": 0,
                        },
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "最終回答です"},
                    }
                ),
                _line({"type": "turn.completed", "usage": {}}),
            ]
        )
        message, fetch_count = _parse_codex_events(stdout)
        self.assertEqual(message, "最終回答です")
        self.assertEqual(fetch_count, 1)

    def test_invalid_json_lines_are_skipped(self) -> None:
        stdout = "\n".join(
            [
                "not json at all {{{",
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "回答"},
                    }
                ),
                "",
                "   ",
            ]
        )
        message, fetch_count = _parse_codex_events(stdout)
        self.assertEqual(message, "回答")
        self.assertEqual(fetch_count, 0)

    def test_last_agent_message_wins(self) -> None:
        stdout = "\n".join(
            [
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "最初のメッセージ"},
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "途中のメッセージ"},
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "最後のメッセージ"},
                    }
                ),
            ]
        )
        message, _ = _parse_codex_events(stdout)
        self.assertEqual(message, "最後のメッセージ")

    def test_command_execution_without_curl_is_not_counted_as_fetch(self) -> None:
        stdout = "\n".join(
            [
                _line(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "ls -la /tmp",
                            "exit_code": 0,
                        },
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "echo hello",
                            "exit_code": 0,
                        },
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "回答"},
                    }
                ),
            ]
        )
        message, fetch_count = _parse_codex_events(stdout)
        self.assertEqual(message, "回答")
        self.assertEqual(fetch_count, 0)


class CodexProviderConfigTest(unittest.TestCase):
    def test_minimax_key_is_read_from_environment_not_written_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            secret = "test-secret-that-must-not-be-written"
            with (
                mock.patch.object(config, "CODEX_HOME", codex_home),
                mock.patch.object(config, "MINIMAX_API_KEY", secret),
                mock.patch.object(
                    config,
                    "CODEX_MINIMAX_BASE_URL",
                    "https://api.minimax.example/v1",
                ),
            ):
                _ensure_codex_home("MiniMax-M3")

            generated = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn(
                'env_http_headers = { Authorization = "DOCI_MINIMAX_AUTHORIZATION" }',
                generated,
            )
            self.assertNotIn("experimental_bearer_token", generated)
            self.assertNotIn(secret, generated)


class CodexDualProviderTest(unittest.TestCase):
    def _completed(self, text: str) -> subprocess.CompletedProcess:
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": text},
            }
        )
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def test_ensure_codex_home_returns_none_for_chatgpt_provider(self) -> None:
        with (
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "MINIMAX_API_KEY", ""),
        ):
            self.assertIsNone(_ensure_codex_home("gpt-5.6-luna"))

    def test_ensure_codex_home_still_builds_minimax_home_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "minimax"),
                mock.patch.object(config, "CODEX_HOME", codex_home),
                mock.patch.object(config, "MINIMAX_API_KEY", "secret"),
            ):
                home = _ensure_codex_home("MiniMax-M3")
            self.assertEqual(home, codex_home)
            self.assertTrue((codex_home / "config.toml").exists())

    def test_run_codex_chatgpt_provider_uses_real_codex_home_and_no_minimax_env(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "CODEX_REASONING_EFFORT", ""),
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.dict(llm.os.environ, {}, clear=True),
            mock.patch.object(
                llm.subprocess, "run", return_value=self._completed("ok")
            ) as run_mock,
        ):
            result = llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0)

        self.assertEqual(result, "ok")
        env = run_mock.call_args.kwargs["env"]
        self.assertNotIn("CODEX_HOME", env)
        self.assertNotIn("DOCI_MINIMAX_AUTHORIZATION", env)
        cmd = run_mock.call_args.args[0]
        self.assertIn("gpt-5.6-luna", cmd)
        self.assertIn("approval_policy=never", cmd)

    def test_run_codex_chatgpt_provider_does_not_require_minimax_key(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "MINIMAX_API_KEY", ""),
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(
                llm.subprocess, "run", return_value=self._completed("ok")
            ),
        ):
            self.assertEqual(
                llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0), "ok"
            )

    def test_run_codex_passes_explicit_reasoning_effort_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "CODEX_REASONING_EFFORT", "max"),
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(
                llm.subprocess, "run", return_value=self._completed("ok")
            ) as run_mock,
        ):
            llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0)

        cmd = run_mock.call_args.args[0]
        self.assertIn("model_reasoning_effort=max", cmd)

    def test_run_codex_minimax_provider_keeps_isolated_home_and_auth_env(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CODEX_PROVIDER", "minimax"),
            mock.patch.object(config, "CODEX_REASONING_EFFORT", ""),
            mock.patch.object(config, "CODEX_HOME", Path(tmp) / "codex-home"),
            mock.patch.object(config, "MINIMAX_API_KEY", "secret"),
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(
                llm.subprocess, "run", return_value=self._completed("ok")
            ) as run_mock,
        ):
            self.assertEqual(
                llm.run_codex("prompt", "MiniMax-M3", min_web_fetches=0), "ok"
            )

        env = run_mock.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], str(Path(tmp) / "codex-home"))
        self.assertEqual(env["DOCI_MINIMAX_AUTHORIZATION"], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
