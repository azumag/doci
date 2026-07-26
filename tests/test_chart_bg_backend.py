"""chart_bg.select の CHART_BG_BACKEND 切替のテスト（ネットワーク不要）。

CHART_BG_BACKEND="codex" のとき llm.run_codex が min_web_fetches=0 で呼ばれること、
返った JSON から {query, media} のリストが正しく組み立てられ、n個に満たない場合は
theme でパディングされることを確認する。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import chart_bg, config


class SelectCodexBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_backend = config.CHART_BG_BACKEND
        config.CHART_BG_BACKEND = "codex"

    def tearDown(self) -> None:
        config.CHART_BG_BACKEND = self._orig_backend

    def test_uses_run_codex_with_min_web_fetches_zero(self) -> None:
        raw = json.dumps(
            {
                "backgrounds": [
                    {"query": "old library shelves", "media": "image"},
                    {"query": "city traffic at night", "media": "video"},
                ]
            }
        )
        spec = {"type": "stat", "value": "42", "caption": "テスト値"}
        with mock.patch.object(chart_bg.llm, "run_codex", return_value=raw) as run_codex_mock:
            result = chart_bg.select(spec, "テストテーマ")

        run_codex_mock.assert_called_once()
        _, kwargs = run_codex_mock.call_args
        # 位置引数(prompt, model)＋キーワード引数(timeout, min_web_fetches)で呼ばれる想定。
        args = run_codex_mock.call_args.args
        self.assertEqual(args[1], config.CODEX_MODEL)
        self.assertEqual(kwargs.get("min_web_fetches"), 0)
        self.assertEqual(kwargs.get("timeout"), 180)
        # stat は1個必要なので、返った先頭のみ採用される。
        self.assertEqual(result, [{"query": "old library shelves", "media": "image"}])

    def test_pads_with_theme_when_fewer_than_n(self) -> None:
        raw = json.dumps({"backgrounds": [{"query": "old clock tower", "media": "image"}]})
        spec = {
            "type": "timeline",
            "events": [
                {"year": "1990", "label": "出来事A"},
                {"year": "2000", "label": "出来事B"},
                {"year": "2010", "label": "出来事C"},
            ],
        }
        with mock.patch.object(chart_bg.llm, "run_codex", return_value=raw):
            result = chart_bg.select(spec, "テストテーマ")

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], {"query": "old clock tower", "media": "image"})
        self.assertEqual(result[1], {"query": "テストテーマ", "media": "image"})
        self.assertEqual(result[2], {"query": "テストテーマ", "media": "image"})


class AuxiliaryBackendDefaultTest(unittest.TestCase):
    def test_explicit_legacy_text_backend_keeps_auxiliary_compatibility(self) -> None:
        with mock.patch.object(config, "TEXT_BACKEND", "anthropic"):
            self.assertEqual(config._default_aux_backend(), "claude")
        with mock.patch.object(config, "TEXT_BACKEND", "claude_cli"):
            self.assertEqual(config._default_aux_backend(), "claude")

    def test_nonlegacy_text_backend_does_not_implicitly_select_claude(self) -> None:
        with mock.patch.object(config, "TEXT_BACKEND", "opencode_go"):
            self.assertEqual(config._default_aux_backend(), "opencode_go")
        with mock.patch.object(config, "TEXT_BACKEND", "opencode"):
            self.assertEqual(config._default_aux_backend(), "opencode")


class SelectOpenCodeBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_backend = config.CHART_BG_BACKEND
        config.CHART_BG_BACKEND = "opencode_go"

    def tearDown(self) -> None:
        config.CHART_BG_BACKEND = self._orig_backend

    def test_uses_opencode_go_without_calling_claude(self) -> None:
        raw = json.dumps({"backgrounds": [{"query": "old library shelves", "media": "image"}]})
        spec = {"type": "stat", "value": "42", "caption": "テスト値"}
        with (
            mock.patch("doci.ai_text._run_opencode_go", return_value=raw) as run_mock,
            mock.patch.object(chart_bg.llm, "run_claude") as claude_mock,
        ):
            result = chart_bg.select(spec, "テストテーマ")

        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[1], config.OPENCODE_GO_DEFAULT_MODEL)
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 120)
        claude_mock.assert_not_called()
        self.assertEqual(result, [{"query": "old library shelves", "media": "image"}])

    def test_uses_opencode_cli_without_calling_claude(self) -> None:
        config.CHART_BG_BACKEND = "opencode"
        raw = json.dumps({"backgrounds": [{"query": "old library shelves", "media": "image"}]})
        spec = {"type": "stat", "value": "42", "caption": "テスト値"}
        with (
            mock.patch("doci.ai_text._run_opencode", return_value=raw) as run_mock,
            mock.patch.object(chart_bg.llm, "run_claude") as claude_mock,
        ):
            result = chart_bg.select(spec, "テストテーマ")

        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[1], config.OPENCODE_MODEL or config.TEXT_MODEL)
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 120)
        claude_mock.assert_not_called()
        self.assertEqual(result, [{"query": "old library shelves", "media": "image"}])

    def test_agent_only_opencode_cli_does_not_inject_model(self) -> None:
        config.CHART_BG_BACKEND = "opencode"
        raw = json.dumps({"backgrounds": [{"query": "shelves", "media": "image"}]})
        with (
            mock.patch.object(config, "OPENCODE_MODEL", ""),
            mock.patch.object(config, "OPENCODE_AGENT", "custom-agent"),
            mock.patch("doci.ai_text._run_opencode", return_value=raw) as run_mock,
        ):
            chart_bg.select({"type": "stat", "value": "42", "caption": "値"}, "テーマ")

        self.assertEqual(run_mock.call_args.args[1], "")

    def test_unknown_backend_fails_closed_without_claude(self) -> None:
        config.CHART_BG_BACKEND = "opencode-go"
        with mock.patch.object(chart_bg.llm, "run_claude") as claude_mock:
            with self.assertRaisesRegex(ValueError, "未対応のCHART_BG_BACKEND"):
                chart_bg.select({"type": "stat", "value": "42", "caption": "テスト値"}, "テーマ")
        claude_mock.assert_not_called()

    def test_ensure_rejects_unknown_backend_before_fetch(self) -> None:
        config.CHART_BG_BACKEND = "opencode-go"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            chart_bg, "_fetch_one", return_value={"query": "テーマ", "media": "image", "path": None}
        ) as fetch_mock:
            with self.assertRaisesRegex(ValueError, "未対応のCHART_BG_BACKEND"):
                chart_bg.ensure(
                    {"type": "stat", "value": "42", "caption": "値"},
                    "テーマ",
                    Path(tmp),
                    0,
                )

        fetch_mock.assert_not_called()

    def test_ensure_rejects_unknown_backend_for_timeline_before_fetch(self) -> None:
        config.CHART_BG_BACKEND = "opencode-go"
        spec = {
            "type": "timeline",
            "events": [
                {"year": "1990", "label": "A"},
                {"year": "2000", "label": "B"},
                {"year": "2010", "label": "C"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            chart_bg, "_fetch_one", side_effect=lambda item, _out: {**item, "path": None}
        ) as fetch_mock:
            with self.assertRaisesRegex(ValueError, "未対応のCHART_BG_BACKEND"):
                chart_bg.ensure(spec, "テーマ", Path(tmp), 0)

        fetch_mock.assert_not_called()

    def test_legacy_claude_model_is_replaced_for_opencode_go(self) -> None:
        raw = json.dumps({"backgrounds": [{"query": "shelves", "media": "image"}]})
        with (
            mock.patch.object(config, "TEXT_MODEL", "claude-opus-4-8"),
            mock.patch("doci.ai_text._run_opencode_go", return_value=raw) as run_mock,
        ):
            chart_bg.select({"type": "stat", "value": "42", "caption": "値"}, "テーマ")

        self.assertEqual(run_mock.call_args.args[1], config.OPENCODE_GO_DEFAULT_MODEL)

    def test_explicit_claude_backend_uses_legacy_model_when_text_default_is_qwen(self) -> None:
        config.CHART_BG_BACKEND = "claude"
        raw = json.dumps({"backgrounds": [{"query": "shelves", "media": "image"}]})
        with (
            mock.patch.object(config, "TEXT_MODEL", config.OPENCODE_GO_DEFAULT_MODEL),
            mock.patch.object(config, "LEGACY_CLAUDE_MODEL", "claude-opus-4-8"),
            mock.patch.object(chart_bg.llm, "run_claude", return_value=raw) as run_mock,
        ):
            chart_bg.select({"type": "stat", "value": "42", "caption": "値"}, "テーマ")

        self.assertEqual(run_mock.call_args.args[1], "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
