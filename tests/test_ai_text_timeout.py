"""台本執筆タイムアウト設定のテスト（ネットワーク不要）。"""
from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from doci import ai_text, config


class WriteTimeoutTest(unittest.TestCase):
    def test_zero_disables_opencode_cli_timeout(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(ai_text.subprocess, "run", return_value=completed) as run_mock,
        ):
            ai_text._run_opencode("prompt", "opencode-go/qwen3.7-plus", "")

        self.assertIsNone(run_mock.call_args.kwargs["timeout"])

    def test_opencode_go_stream_returns_text_without_thinking(self) -> None:
        events = b"".join(
            [
                b'data:{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"secret reasoning"}}\n',
                b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"title\\":"}}\n',
                b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"\\"ok\\"}"}}\n',
                b'data:{"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n',
            ]
        )

        class FakeResponse(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=FakeResponse(events)
            ) as urlopen_mock,
        ):
            result = ai_text._run_opencode_go(
                "prompt",
                "opencode-go/qwen3.7-plus",
                timeout=17,
            )

        self.assertEqual(result, '{"title":"ok"}')
        self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 17)
        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.headers["X-api-key"], "test-key")
        self.assertEqual(request.headers["User-agent"], "doci/1.0")

    def test_opencode_go_stream_has_a_whole_response_deadline(self) -> None:
        class SlowResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                def delayed_lines():
                    time.sleep(0.03)
                    yield b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"late"}}\n'

                return delayed_lines()

            def close(self):
                return None

        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=SlowResponse()
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "時間上限"):
                ai_text._run_opencode_go("prompt", "opencode-go/qwen3.7-plus", timeout=0.001)

    def test_opencode_go_default_uses_write_timeout(self) -> None:
        class FakeResponse(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        events = b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n'
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(config, "WRITE_LLM_IDLE_TIMEOUT", 300),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=FakeResponse(events)
            ) as urlopen_mock,
        ):
            self.assertEqual(ai_text._run_opencode_go("prompt", "opencode-go/qwen3.7-plus"), "ok")

        self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 300)

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

    def test_opencode_agent_remains_selectable_when_model_is_empty(self) -> None:
        with (
            mock.patch.object(config, "TEXT_BACKEND", "opencode"),
            mock.patch.object(config, "TEXT_MODEL", "legacy-model"),
            mock.patch.object(config, "OPENCODE_MODEL", ""),
            mock.patch.object(config, "OPENCODE_AGENT", "custom-agent"),
            mock.patch.object(ai_text, "_run_opencode", return_value="{}") as run_mock,
        ):
            self.assertEqual(ai_text._dispatch("prompt"), "{}")

        run_mock.assert_called_once_with("prompt", "", "custom-agent")

    def test_zero_disables_explicit_claude_timeout(self) -> None:
        with (
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(ai_text.llm, "run_claude", return_value="{}") as run_mock,
        ):
            ai_text._run_claude_cli("prompt", "claude-sonnet-5")

        self.assertIsNone(run_mock.call_args.kwargs["timeout"])

    def test_legacy_claude_path_does_not_receive_opencode_model_default(self) -> None:
        with (
            mock.patch.object(config, "LEGACY_CLAUDE_MODEL", "claude-opus-4-8"),
            mock.patch.object(ai_text.llm, "run_claude", return_value="{}") as run_mock,
        ):
            ai_text._run_claude_cli("prompt", "opencode-go/qwen3.7-plus")

        self.assertEqual(run_mock.call_args.args[1], "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
