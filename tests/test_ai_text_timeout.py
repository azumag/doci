"""台本執筆タイムアウト設定のテスト（ネットワーク不要）。"""
from __future__ import annotations

import socket
import json
import subprocess
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import ai_text, config


class WriteTimeoutTest(unittest.TestCase):
    def test_opencode_go_rejects_other_provider_qualified_models(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "opencode-go/<model>"):
            ai_text._opencode_go_model("minimax/m3")
        with self.assertRaisesRegex(RuntimeError, "Claudeモデル"):
            ai_text._opencode_go_model("opencode-go/claude-opus-4-8")
        self.assertEqual(
            ai_text._opencode_go_model("opencode-go/qwen3.7-plus"),
            "opencode-go/qwen3.7-plus",
        )

    def test_opencode_cli_rejects_legacy_claude_models(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Claudeモデル"):
            ai_text._opencode_cli_aux_model("claude-opus-4-8", explicit=True)
        with (
            mock.patch.object(config, "OPENCODE_MODEL", ""),
            mock.patch.object(config, "OPENCODE_AGENT", ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "Claudeモデル"):
                ai_text._opencode_cli_model("opencode-go/claude-opus-4-8")

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

    def test_explicit_unlimited_opencode_cli_timeout_is_not_write_timeout(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 17),
            mock.patch.object(ai_text.subprocess, "run", return_value=completed) as run_mock,
        ):
            ai_text._run_opencode(
                "prompt", "opencode-go/qwen3.7-plus", "", timeout=None
            )

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
        # issue #153: OpenAI互換エンドポイント(/chat/completions + Bearer)へ統一した
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(request.headers["User-agent"], "doci/1.0")
        self.assertTrue(request.full_url.endswith("/chat/completions"))

    def test_opencode_go_stream_processes_unterminated_final_sse_line(self) -> None:
        class FakeResponse(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        events = (
            b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}'
            b'\ndata:{"type":"message_delta","delta":{"stop_reason":"end_turn"}}'
        )
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=FakeResponse(events)
            ),
        ):
            self.assertEqual(
                ai_text._run_opencode_go(
                    "prompt", "opencode-go/qwen3.7-plus", timeout=17
                ),
                "ok",
            )

    def test_opencode_go_stream_reassembles_split_sse_chunks(self) -> None:
        class ChunkedResponse:
            def __init__(self):
                self.chunks = [
                    b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"o',
                    b'k"}}\ndata:{"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
                ]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def __iter__(self):
                return iter(())

            def read1(self, _amount=4096):
                if not self.chunks:
                    raise StopIteration
                return self.chunks.pop(0)

            def close(self):
                return None

        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=ChunkedResponse()
            ),
        ):
            self.assertEqual(
                ai_text._run_opencode_go(
                    "prompt", "opencode-go/qwen3.7-plus", timeout=17
                ),
                "ok",
            )

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

    def test_opencode_go_trickling_bytes_cannot_extend_whole_deadline(self) -> None:
        class TrickleResponse:
            def __init__(self):
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def __iter__(self):
                return iter(())

            def read(self, _amount=1):
                time.sleep(0.001)
                return b"d" if not self.closed else b""

            def close(self):
                self.closed = True

        response = TrickleResponse()
        started = time.monotonic()
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=response
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "時間上限"):
                ai_text._run_opencode_go(
                    "prompt", "opencode-go/qwen3.7-plus", timeout=0.01
                )

        self.assertLess(time.monotonic() - started, 0.2)

    def test_anthropic_response_has_a_whole_response_deadline(self) -> None:
        class SlowResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _amount=-1):
                time.sleep(0.03)
                return b"{}"

        with (
            mock.patch.object(config, "get", return_value="test-key"),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=SlowResponse()
            ),
        ):
            with self.assertRaisesRegex(TimeoutError, "時間上限"):
                ai_text._run_anthropic("prompt", "claude-sonnet-4-6", timeout=0.001)

    def test_anthropic_connection_uses_the_remaining_deadline(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = [b'{"content": []}', b'']
        with (
            mock.patch.object(config, "get", return_value="test-key"),
            mock.patch.object(ai_text.urllib.request, "urlopen", return_value=response) as urlopen_mock,
        ):
            ai_text._run_anthropic("prompt", "claude-sonnet-4-6", timeout=17)

        self.assertLessEqual(urlopen_mock.call_args.kwargs["timeout"], 17)

    def test_opencode_go_read_fallback_closes_response_and_socket(self) -> None:
        class Closable:
            def __init__(self):
                self.close_mock = mock.Mock()

            def close(self):
                self.close_mock()

        class SlowResponse:
            def __init__(self):
                self.sock = Closable()
                self.raw = Closable()
                self.fp = SimpleNamespace(raw=self.raw)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def __iter__(self):
                def delayed_lines():
                    time.sleep(0.05)
                    yield b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"late"}}\n'

                return delayed_lines()

            def close(self):
                self.sock.close()

        response = SlowResponse()
        # The nested response path used by expire_stream must include the raw socket.
        response.fp.raw._sock = response.sock
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=response
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "時間上限"):
                ai_text._run_opencode_go("prompt", "opencode-go/qwen3.7-plus", timeout=0.01)

        self.assertTrue(response.sock.close_mock.called)

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

    def test_explicit_unlimited_opencode_go_timeout_keeps_idle_guard(self) -> None:
        class FakeResponse(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        events = b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n'
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 17),
            mock.patch.object(config, "WRITE_LLM_IDLE_TIMEOUT", 7),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=FakeResponse(events)
            ) as urlopen_mock,
        ):
            self.assertEqual(
                ai_text._run_opencode_go("prompt", "opencode-go/qwen3.7-plus", timeout=None),
                "ok",
            )

        self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 7)

    def test_opencode_go_keeps_idle_guard_when_both_limits_are_zero(self) -> None:
        class FakeResponse(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        events = b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n'
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(config, "WRITE_LLM_IDLE_TIMEOUT", 0),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=FakeResponse(events)
            ) as urlopen_mock,
        ):
            self.assertEqual(
                ai_text._run_opencode_go("prompt", "opencode-go/qwen3.7-plus"), "ok"
            )

        self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 300)

    def test_opencode_go_sets_socket_timeout_before_each_stream_read(self) -> None:
        class FakeSocket:
            def __init__(self):
                self.values = []

            def settimeout(self, value):
                self.values.append(value)

        class FakeResponse:
            def __init__(self, events, sock):
                self.events = events
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                return iter(self.events)

        sock = FakeSocket()
        response = FakeResponse(
            [b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n'],
            sock,
        )
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(config, "WRITE_LLM_IDLE_TIMEOUT", 7),
            mock.patch.object(ai_text.urllib.request, "urlopen", return_value=response),
        ):
            self.assertEqual(
                ai_text._run_opencode_go("prompt", "opencode-go/qwen3.7-plus", timeout=100), "ok"
            )

        self.assertGreaterEqual(len(sock.values), 1)
        self.assertTrue(all(value == 7 for value in sock.values))

    def test_opencode_go_socket_timeout_is_reported_as_idle_timeout(self) -> None:
        class FakeSocket:
            def settimeout(self, _value):
                return None

        class TimeoutResponse:
            def __init__(self):
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=FakeSocket()))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                def timed_lines():
                    raise socket.timeout("idle")
                    yield b""

                return timed_lines()

        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(config, "WRITE_LLM_IDLE_TIMEOUT", 300),
            mock.patch.object(
                ai_text.urllib.request, "urlopen", return_value=TimeoutResponse()
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "無通信タイムアウト"):
                ai_text._run_opencode_go("prompt", "opencode-go/qwen3.7-plus")

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

    def test_bare_opencode_go_model_is_sent_as_model_id(self) -> None:
        class FakeResponse(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        events = b'data:{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n'
        with (
            mock.patch.object(config, "OPENCODE_GO_API_KEY", "test-key"),
            mock.patch.object(ai_text.urllib.request, "urlopen", return_value=FakeResponse(events)) as urlopen_mock,
        ):
            self.assertEqual(ai_text._run_opencode_go("prompt", "qwen3.7-plus", timeout=7), "ok")

        request_body = json.loads(urlopen_mock.call_args.args[0].data)
        self.assertEqual(request_body["model"], "qwen3.7-plus")

    def test_opencode_go_openai_format_chunks_are_parsed(self) -> None:
        """issue #153: OpenAI互換のSSEチャンク(choices[].delta.content)をパースする。"""
        events = b"".join(
            [
                b'data:{"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"{\\"title\\":"},"finish_reason":null}]}\n',
                b'data:{"id":"2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"\\"ok\\"}"},"finish_reason":null}]}\n',
                b'data:{"id":"3","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
                b"data:[DONE]\n",
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
            ),
        ):
            result = ai_text._run_opencode_go(
                "prompt", "opencode-go/kimi-k3", timeout=17
            )

        self.assertEqual(result, '{"title":"ok"}')

    def test_opencode_go_openai_finish_length_raises_max_tokens(self) -> None:
        """issue #153: finish_reason=length は max_tokens 到達として失敗させる。"""
        events = (
            b'data:{"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":"length"}]}\n'
            b"data:[DONE]\n"
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
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "max_tokens"):
                ai_text._run_opencode_go(
                    "prompt", "opencode-go/kimi-k3", timeout=17
                )

    def test_opencode_go_think_tags_are_stripped(self) -> None:
        """issue #153: reasoning(<think>...</think>)は本文から除去される。"""
        events = (
            b'data:{"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"<think>internal reasoning</think>"},"finish_reason":null}]}\n'
            b'data:{"id":"2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n'
            b"data:[DONE]\n"
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
            ),
        ):
            result = ai_text._run_opencode_go(
                "prompt", "opencode-go/minimax-m3", timeout=17
            )

        self.assertEqual(result, "ok")

    def test_strip_think_tags_handles_case_and_attributes(self) -> None:
        """大文字・属性付きタグでも内容ごと除去される (Claudeレビュー指摘)。"""
        samples = [
            ("<think>inner</think>OK", "OK"),  # 基本
            ("<Think>inner</Think>OK", "OK"),  # 大文字
            ('<think budget="1000">inner</think>OK', "OK"),  # 属性付き
            ("<think>unterminated reasoning...", ""),  # 閉じタグなし→reasoning断片は切り落とし
            ("<Think>inner</think>OK", "OK"),  # 開き大文字・閉じ小文字
            ("<thinks>not a think tag</thinks>OK", "<thinks>not a think tag</thinks>OK"),  # 誤マッチしない
            ('本文中の<think>リテラル', '本文中の'),  # 未終端は以降を切り落とし
        ]
        for sample, expected in samples:
            with self.subTest(sample=sample):
                self.assertEqual(ai_text._strip_think_tags(sample), expected)

    def test_zero_disables_explicit_claude_timeout(self) -> None:
        with (
            mock.patch.object(config, "WRITE_LLM_TIMEOUT", 0),
            mock.patch.object(ai_text.llm, "run_claude", return_value="{}") as run_mock,
        ):
            ai_text._run_claude_cli("prompt", "claude-sonnet-5")

        self.assertIsNone(run_mock.call_args.kwargs["timeout"])

    def test_codex_text_backend_uses_codex_model_without_web_fetch_requirement(
        self,
    ) -> None:
        with (
            mock.patch.object(config, "TEXT_BACKEND", "codex"),
            mock.patch.object(config, "CODEX_MODEL", "gpt-5.6-luna"),
            mock.patch.object(ai_text.llm, "run_codex", return_value="{}") as run_mock,
        ):
            self.assertEqual(ai_text._dispatch("prompt", timeout=17), "{}")

        run_mock.assert_called_once_with(
            "prompt", "gpt-5.6-luna", timeout=17, min_web_fetches=0
        )

    def test_codex_text_backend_defaults_to_unlimited_timeout_when_unset(self) -> None:
        # run_codexの既定timeout=600に暗黙で丸め込まれず、他バックエンド同様
        # timeout未指定=無制限（timeout=Noneを明示）になることを確認する。
        with (
            mock.patch.object(config, "TEXT_BACKEND", "codex"),
            mock.patch.object(config, "CODEX_MODEL", "gpt-5.6-luna"),
            mock.patch.object(ai_text.llm, "run_codex", return_value="{}") as run_mock,
        ):
            self.assertEqual(ai_text._dispatch("prompt"), "{}")

        run_mock.assert_called_once_with(
            "prompt", "gpt-5.6-luna", timeout=None, min_web_fetches=0
        )

    def test_legacy_claude_path_does_not_receive_opencode_model_default(self) -> None:
        with (
            mock.patch.object(config, "LEGACY_CLAUDE_MODEL", "claude-opus-4-8"),
            mock.patch.object(ai_text.llm, "run_claude", return_value="{}") as run_mock,
        ):
            ai_text._run_claude_cli("prompt", "opencode-go/qwen3.7-plus")

        self.assertEqual(run_mock.call_args.args[1], "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
