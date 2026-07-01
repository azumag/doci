from __future__ import annotations

import json
import unittest

from doci.llm import _parse_codex_events


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


if __name__ == "__main__":
    unittest.main()
