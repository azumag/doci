"""plan.make_plan の PLAN_BACKEND 切替のテスト（ネットワーク不要）。

PLAN_BACKEND="codex" のとき llm.run_codex が min_web_fetches=0 / timeout=240 で
呼ばれること、beats の不足検証、charts の型フィルタ＋id採番が機能することを確認する。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from doci import config, plan
from doci.channel import CornerSpec


_CORNER = CornerSpec(
    key="communism",
    label="共産主義ネタ",
    persona_path=Path("persona_chinese.md"),
    corner_path=Path("corner_communism.md"),
    voice_key="chinese_ai",
)


class MakePlanCodexBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_backend = config.PLAN_BACKEND
        config.PLAN_BACKEND = "codex"

    def tearDown(self) -> None:
        config.PLAN_BACKEND = self._orig_backend

    def test_uses_run_codex_with_min_web_fetches_zero(self) -> None:
        raw = json.dumps(
            {
                "topic": "テスト題材",
                "beats": [
                    {"role": "起", "gist": "起の要点"},
                    {"role": "承", "gist": "承の要点"},
                    {"role": "転", "gist": "転の要点"},
                    {"role": "結", "gist": "結の要点"},
                ],
                "charts": [],
            }
        )
        with mock.patch.object(plan.llm, "run_codex", return_value=raw) as run_codex_mock:
            result = plan.make_plan(_CORNER, None)

        run_codex_mock.assert_called_once()
        args = run_codex_mock.call_args.args
        kwargs = run_codex_mock.call_args.kwargs
        self.assertEqual(args[1], config.CODEX_MODEL)
        self.assertEqual(kwargs.get("min_web_fetches"), 0)
        self.assertEqual(kwargs.get("timeout"), 240)
        self.assertEqual(result["topic"], "テスト題材")
        self.assertEqual(len(result["beats"]), 4)

    def test_charts_type_filter_and_id_numbering(self) -> None:
        raw = json.dumps(
            {
                "topic": "テスト題材",
                "beats": [
                    {"role": "起", "gist": "a"},
                    {"role": "承", "gist": "b"},
                    {"role": "転", "gist": "c"},
                    {"role": "結", "gist": "d"},
                ],
                "charts": [
                    {"place": "起", "type": "stat", "value": "1000時間", "caption": "..."},
                    {"place": "承", "type": "invalid_type", "title": "除外されるべき図表"},
                    {"place": "転", "type": "compare", "items": []},
                ],
            }
        )
        with mock.patch.object(plan.llm, "run_codex", return_value=raw):
            result = plan.make_plan(_CORNER, None)

        charts = result["charts"]
        # invalid_type は除外され、残り2件のみに 0 始まりの id が振られる。
        self.assertEqual(len(charts), 2)
        self.assertEqual(charts[0]["type"], "stat")
        self.assertEqual(charts[0]["id"], 0)
        self.assertEqual(charts[1]["type"], "compare")
        self.assertEqual(charts[1]["id"], 1)

    def test_retries_then_raises_when_beats_insufficient(self) -> None:
        bad_raw = json.dumps({"topic": "テスト題材", "beats": [{"role": "起", "gist": "a"}], "charts": []})
        with mock.patch.object(
            plan.llm, "run_codex", side_effect=[bad_raw, bad_raw]
        ) as run_codex_mock:
            with self.assertRaises(ValueError):
                plan.make_plan(_CORNER, None)

        self.assertEqual(run_codex_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
