"""chart_bg.select の CHART_BG_BACKEND 切替のテスト（ネットワーク不要）。

CHART_BG_BACKEND="codex" のとき llm.run_codex が min_web_fetches=0 で呼ばれること、
返った JSON から {query, media} のリストが正しく組み立てられ、n個に満たない場合は
theme でパディングされることを確認する。
"""
from __future__ import annotations

import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
