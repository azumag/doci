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

    def test_avoid_topics_are_included_in_prompt_when_given(self) -> None:
        raw = json.dumps(
            {
                "topic": "テスト題材",
                "beats": [
                    {"role": "起", "gist": "a"},
                    {"role": "承", "gist": "b"},
                    {"role": "転", "gist": "c"},
                    {"role": "結", "gist": "d"},
                ],
                "charts": [],
            }
        )
        with mock.patch.object(plan.llm, "run_codex", return_value=raw) as run_codex_mock:
            plan.make_plan(
                _CORNER, None, avoid_topics=["見えざる手の寓話", "成長という名の神様"]
            )

        prompt = run_codex_mock.call_args.args[0]
        self.assertIn("見えざる手の寓話", prompt)
        self.assertIn("成長という名の神様", prompt)
        self.assertIn("最近すでに扱った題材", prompt)

    def test_no_avoid_topics_leaves_prompt_unchanged(self) -> None:
        raw = json.dumps(
            {
                "topic": "テスト題材",
                "beats": [
                    {"role": "起", "gist": "a"},
                    {"role": "承", "gist": "b"},
                    {"role": "転", "gist": "c"},
                    {"role": "結", "gist": "d"},
                ],
                "charts": [],
            }
        )
        with mock.patch.object(plan.llm, "run_codex", return_value=raw) as run_codex_mock:
            plan.make_plan(_CORNER, None)

        prompt = run_codex_mock.call_args.args[0]
        self.assertNotIn("最近すでに扱った題材", prompt)


class MakePlanOpenCodeGoBackendTest(unittest.TestCase):
    def test_opencode_go_model_uses_direct_api_without_cli_state(self) -> None:
        raw = json.dumps(
            {
                "topic": "テスト題材",
                "beats": [
                    {"role": "起", "gist": "a"},
                    {"role": "承", "gist": "b"},
                    {"role": "転", "gist": "c"},
                    {"role": "結", "gist": "d"},
                ],
                "charts": [],
            }
        )
        with (
            mock.patch.object(config, "PLAN_BACKEND", "opencode"),
            mock.patch.object(config, "PLAN_MODEL", "opencode-go/minimax-m3"),
            mock.patch.object(
                plan.ai_text, "_run_opencode_go", return_value=raw
            ) as direct_mock,
            mock.patch.object(plan.ai_text, "_run_opencode") as cli_mock,
        ):
            result = plan.make_plan(_CORNER, None)

        direct_mock.assert_called_once()
        cli_mock.assert_not_called()
        self.assertEqual(result["topic"], "テスト題材")


if __name__ == "__main__":
    unittest.main()
