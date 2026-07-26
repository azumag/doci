"""factcheck.verify_and_correct のリトライ挙動のテスト（ネットワーク不要）。

MiniMax-M3 等が長い日本語JSONのエスケープを崩し不正JSONを返す事象を再現し、
research.web_research と同様に SCRIPT_FACTCHECK_RETRIES 回まで再試行することを確認する。
"""
from __future__ import annotations

import unittest
from unittest import mock

from doci import config, factcheck


class VerifyAndCorrectRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        # 既定の claude バックエンドを明示し、run_claude 経由の呼び出しをモックする。
        self._orig_backend = config.FACTCHECK_BACKEND
        self._orig_retries = config.SCRIPT_FACTCHECK_RETRIES
        config.FACTCHECK_BACKEND = "claude"
        config.SCRIPT_FACTCHECK_RETRIES = 2

    def tearDown(self) -> None:
        config.FACTCHECK_BACKEND = self._orig_backend
        config.SCRIPT_FACTCHECK_RETRIES = self._orig_retries

    def test_succeeds_after_one_bad_json_then_retry(self) -> None:
        good_raw = '{"narration": "修正後の全文です。", "changed": true, "issues": []}'
        with mock.patch.object(
            factcheck.llm,
            "run_claude",
            side_effect=[
                "これはJSONではない不正な出力です",  # 1回目: JSON抽出でValueError
                good_raw,  # 2回目: 成功
            ],
        ) as run_claude_mock:
            result = factcheck.verify_and_correct(
                "検証対象のナレーション原文",
                research={"facts": [{"claim": "確認済み", "source_url": "https://example.org/source"}]},
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["narration"], "修正後の全文です。")
        self.assertEqual(run_claude_mock.call_count, 2)

    def test_raises_when_all_attempts_fail(self) -> None:
        with mock.patch.object(
            factcheck.llm,
            "run_claude",
            side_effect=["不正なJSONその1", "不正なJSONその2"],
        ) as run_claude_mock:
            with self.assertRaises(ValueError):
                factcheck.verify_and_correct("検証対象のナレーション原文")

        self.assertEqual(run_claude_mock.call_count, 2)

    def test_opencode_go_backend_does_not_call_claude(self) -> None:
        raw = '{"narration": "確認後の全文です。", "changed": false, "issues": []}'
        with (
            mock.patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            mock.patch("doci.ai_text._run_opencode_go", return_value=raw) as run_mock,
            mock.patch.object(factcheck.llm, "run_claude") as claude_mock,
        ):
            result = factcheck.verify_and_correct(
                "検証対象のナレーション原文",
                research={"facts": [{"claim": "確認済み", "source_url": "https://example.org/source"}]},
            )

        run_mock.assert_called_once()
        claude_mock.assert_not_called()
        self.assertEqual(result["narration"], "確認後の全文です。")

    def test_opencode_cli_backend_does_not_call_claude(self) -> None:
        raw = '{"narration": "確認後の全文です。", "changed": false, "issues": []}'
        with (
            mock.patch.object(config, "FACTCHECK_BACKEND", "opencode"),
            mock.patch("doci.ai_text._run_opencode", return_value=raw) as run_mock,
            mock.patch.object(factcheck.llm, "run_claude") as claude_mock,
        ):
            result = factcheck.verify_and_correct(
                "検証対象のナレーション原文",
                research={"facts": [{"claim": "確認済み", "source_url": "https://example.org/source"}]},
            )

        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.kwargs["timeout"], config.script_llm_timeout())
        claude_mock.assert_not_called()
        self.assertEqual(result["narration"], "確認後の全文です。")

    def test_explicit_legacy_claude_factcheck_keeps_opus_default(self) -> None:
        raw = '{"narration": "確認後の全文です。", "changed": false, "issues": []}'
        with (
            mock.patch.object(config, "FACTCHECK_MODEL", config.OPENCODE_GO_DEFAULT_MODEL),
            mock.patch.object(config, "LEGACY_CLAUDE_FACTCHECK_MODEL", "claude-opus-4-8"),
            mock.patch.object(factcheck.llm, "run_claude", return_value=raw) as run_mock,
        ):
            factcheck._attempt("prompt", "claude")

        self.assertEqual(run_mock.call_args.args[1], "claude-opus-4-8")

    def test_unknown_backend_fails_closed_without_claude(self) -> None:
        with (
            mock.patch.object(factcheck.llm, "run_claude") as claude_mock,
            mock.patch.object(config, "FACTCHECK_BACKEND", "opencode-go"),
        ):
            with self.assertRaisesRegex(ValueError, "未対応のFACTCHECK_BACKEND"):
                factcheck._attempt("prompt", "opencode-go")
        claude_mock.assert_not_called()

    def test_opencode_go_without_research_keeps_original(self) -> None:
        with (
            mock.patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            mock.patch("doci.ai_text._run_opencode_go") as run_mock,
            mock.patch.object(factcheck, "_log") as log_mock,
        ):
            result = factcheck.verify_and_correct("原文を維持する")

        run_mock.assert_not_called()
        self.assertIsNone(result)
        log_mock.assert_called_once_with(
            "OpenCodeファクトチェック: 検証済み資料がないため原文を維持"
            "（検証済み資料を取得できませんでした）"
        )

    def test_opencode_cli_without_research_keeps_original(self) -> None:
        with (
            mock.patch.object(config, "FACTCHECK_BACKEND", "opencode"),
            mock.patch("doci.ai_text._run_opencode") as run_mock,
            mock.patch.object(factcheck, "_log") as log_mock,
        ):
            result = factcheck.verify_and_correct("原文を維持する")

        run_mock.assert_not_called()
        self.assertIsNone(result)
        log_mock.assert_called_once_with(
            "OpenCodeファクトチェック: 検証済み資料がないため原文を維持"
            "（検証済み資料を取得できませんでした）"
        )


if __name__ == "__main__":
    unittest.main()
