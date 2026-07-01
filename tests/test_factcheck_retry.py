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
            result = factcheck.verify_and_correct("検証対象のナレーション原文")

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


if __name__ == "__main__":
    unittest.main()
