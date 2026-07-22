from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config
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


if __name__ == "__main__":
    unittest.main()
