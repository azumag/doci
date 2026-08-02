"""issue #70: narrationの書き出しが動画をまたいで同型化するのを防ぐテスト。

対象: ai_text._opening_family、ai_text._check_opening_pattern、
ai_text.check_narration_opening_pattern_duplicate、
ai_text.generate（Layer2のretry統合）、
history.recent_narration_openings、corners.build_prompt、
run_daily._apply_narration_pattern_check。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import ai_text, channel, config, corners, history, run_daily


class OpeningFamilyTest(unittest.TestCase):
    # issue #70 実測サンプル: ideology channelの直近narration冒頭（ほぼ全て反語疑問）。
    REAL_RHETORICAL_WHY_SAMPLES = (
        "人類はなぜ、何度も同じ失敗を繰り返しながら、それでも「みんなが等しく幸せになる世界」を夢見てしまうのでしょうか",
        "人類はなぜ、何度裏切られても「誰も取り残されない世界」を夢見てしまうのでしょうか",
        "人間はなぜ、完璧な正義を、自分以外の誰かに託したくなるのでしょうか",
        "私たちはなぜ、自分の取り分を計算する前に、まず隣人の皿を気にしてしまうのでしょうか",
        "人はなぜ、自らの手で鎖を作っておきながら、その自由を愛してやまないのでしょうか",
        "自由を選んだはずの人間が、なぜ自らを枷（かせ）に変えてしまうのか",
        "人類はなぜ、奪い合いの仕組みを神聖なものと呼ぶのか",
    )

    def test_real_ideology_openings_classify_as_rhetorical_why(self) -> None:
        for sample in self.REAL_RHETORICAL_WHY_SAMPLES:
            with self.subTest(sample=sample):
                self.assertEqual(ai_text._opening_family(sample), "rhetorical_why")

    def test_opening_sentence_default_max_chars_does_not_truncate_before_ending(
        self,
    ) -> None:
        # 独立レビュー指摘: rhetorical_whyは文末アンカー($)必須のため、
        # _opening_sentenceのmax_chars既定値が短すぎると「でしょうか」が
        # 切り詰められ検出漏れになる。63文字（旧上限60を超える）の反語疑問が
        # 引き続き検出できることを確認する。
        long_opening = (
            "人類はなぜ、何度も何度も同じ過ちを繰り返しながら、"
            "それでもなお希望を捨てきれずに新しい理想の世界を夢見続けてしまうのでしょうか"
        )
        self.assertGreater(len(long_opening), 60)
        narration = long_opening + "。詳細な内容がここに続きます。"
        self.assertEqual(
            ai_text._opening_family(ai_text._opening_sentence(narration)),
            "rhetorical_why",
        )

    def test_next_video_directive_family(self) -> None:
        self.assertEqual(
            ai_text._opening_family("次のショート動画では「装飾」を一つ削ぎ落とします"),
            "next_video_directive",
        )
        self.assertEqual(
            ai_text._opening_family("次の動画では、尺を短くする実験をやめてください"),
            "next_video_directive",
        )

    def test_conclusion_first_family(self) -> None:
        self.assertEqual(
            ai_text._opening_family("結論から言うと、装飾を一つ削ぎ落とします"),
            "conclusion_first",
        )

    def test_scene_setting_opening_has_no_family(self) -> None:
        self.assertIsNone(
            ai_text._opening_family("誰もいない工場の音だけが、正しさを語っていました")
        )

    def test_declarative_naze_no_ka_clause_is_not_misclassified_as_rhetorical_why(
        self,
    ) -> None:
        # 「なぜ」+「のか」は反語疑問の型でなくても、活用として文中に自然に現れる
        # （のかを/のか特定/のかは等）。文末がでしょうか/のかで終わる場合だけを
        # rhetorical_whyとして扱う（独立レビューで検出された誤検出パターン）。
        non_templated = (
            "彼らがなぜあの選択をしたのかは今も謎です",
            "科学者たちは、なぜこの現象が起きるのかを長年研究してきました",
            "専門家ですら、なぜ事故が起きたのか特定できていません",
        )
        for sample in non_templated:
            with self.subTest(sample=sample):
                self.assertIsNone(ai_text._opening_family(sample))

    def test_opening_sentence_splits_at_first_terminal_punctuation(self) -> None:
        self.assertEqual(
            ai_text._opening_sentence("最初の一文です。ここは二文目です。"),
            "最初の一文です",
        )

    def test_opening_sentence_truncates_to_max_chars(self) -> None:
        long_sentence = "あ" * 100
        self.assertEqual(len(ai_text._opening_sentence(long_sentence, max_chars=10)), 10)


class CheckOpeningPatternTest(unittest.TestCase):
    def test_raises_when_family_matches_immediately_previous_opening(self) -> None:
        with self.assertRaises(ValueError):
            ai_text._check_opening_pattern(
                "人類はなぜ、また同じ過ちを繰り返すのでしょうか。",
                ["人類はなぜ、繰り返してしまうのでしょうか"],
            )

    def test_raises_when_family_share_in_window_exceeds_threshold(self) -> None:
        # 直前(recentの最後)はNone family にして、「直前と同一」分岐ではなく
        # 「直近window件中の偏り」分岐だけを単独で踏むようにする。
        recent = [
            "人類はなぜ、目を逸らすのでしょうか",  # rhetorical_why (1)
            "人類はなぜ、忘れてしまうのでしょうか",  # rhetorical_why (2)
            "誰もいない場所から始まる話です",  # None family（直前はこれ）
        ]
        with self.assertRaises(ValueError):
            ai_text._check_opening_pattern(
                "人類はなぜ、また信じてしまうのでしょうか。",
                recent,
                window=6,
                max_family_share=2,
            )

    def test_allows_when_family_share_is_below_threshold(self) -> None:
        # 直前と異なり、かつwindow内の出現数がmax_family_share未満なら許可される
        # （share>=thresholdの境界を挟んで区別できることを確認）。
        recent = [
            "人類はなぜ、目を逸らすのでしょうか",  # rhetorical_why (1のみ)
            "誰もいない場所から始まる話です",  # None family（直前はこれ）
        ]
        ai_text._check_opening_pattern(
            "人類はなぜ、また信じてしまうのでしょうか。",
            recent,
            window=6,
            max_family_share=2,
        )  # 例外が出なければOK

    def test_allows_different_family_from_previous(self) -> None:
        ai_text._check_opening_pattern(
            "誰もいない工場の音だけが、正しさを語っていました。",
            ["人類はなぜ、繰り返してしまうのでしょうか"],
        )  # 例外が出なければOK

    def test_allows_unclassifiable_opening_even_with_matching_history(self) -> None:
        ai_text._check_opening_pattern(
            "誰もいない工場の音だけが、正しさを語っていました。",
            ["誰もいない工場の音だけが、正しさを語っていました"],
        )

    def test_allows_when_no_history(self) -> None:
        ai_text._check_opening_pattern(
            "人類はなぜ、また信じてしまうのでしょうか。", []
        )

    def test_operates_on_bracket_containing_text_consistently(self) -> None:
        # 「」がまだ残っていても、なぜ〜でしょうか の判定自体は変わらない
        # （_validate内で括弧除去は既に完了している前提だが、判定関数自体は堅牢）。
        with self.assertRaises(ValueError):
            ai_text._check_opening_pattern(
                "「人類」はなぜ、また同じ過ちを繰り返すのでしょうか。",
                ["人類はなぜ、繰り返してしまうのでしょうか"],
            )


class CheckNarrationOpeningPatternDuplicateTest(unittest.TestCase):
    def test_llm_flags_matching_opening_as_pattern_duplicate(self) -> None:
        with mock.patch.object(
            ai_text,
            "_dispatch",
            return_value=json.dumps(
                {
                    "duplicate": True,
                    "matched_index": 1,
                    "overlapping_axes": ["opening_syntax", "rhetorical_move"],
                    "confidence": 0.75,
                    "reason": "同じ疑問文構造と反語の使い回し",
                }
            ),
        ):
            result = ai_text.check_narration_opening_pattern_duplicate(
                "新しい書き出し案", ["直近の書き出し1", "直近の書き出し2"]
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["matched_opening"], "直近の書き出し1")
        self.assertEqual(result["confidence"], 0.75)
        self.assertEqual(
            result["overlapping_axes"], ["opening_syntax", "rhetorical_move"]
        )

    def test_llm_says_not_duplicate_returns_none(self) -> None:
        with mock.patch.object(
            ai_text, "_dispatch", return_value=json.dumps({"duplicate": False})
        ):
            result = ai_text.check_narration_opening_pattern_duplicate(
                "新しい書き出し案", ["直近の書き出し1"]
            )
        self.assertIsNone(result)

    def test_dispatch_failure_returns_none_instead_of_raising(self) -> None:
        with mock.patch.object(
            ai_text, "_dispatch", side_effect=RuntimeError("backend down")
        ):
            result = ai_text.check_narration_opening_pattern_duplicate(
                "新しい書き出し案", ["直近の書き出し1"]
            )
        self.assertIsNone(result)

    def test_no_recent_openings_short_circuits_without_dispatch(self) -> None:
        with mock.patch.object(ai_text, "_dispatch") as dispatch:
            result = ai_text.check_narration_opening_pattern_duplicate(
                "新しい書き出し案", []
            )
        dispatch.assert_not_called()
        self.assertIsNone(result)


class ApplyNarrationPatternCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = SimpleNamespace(
            id="youtube-growth",
            pipeline={"narration_pattern_check": True},
        )
        self.spec.pipeline_get = lambda key, default=None: self.spec.pipeline.get(
            key, default
        )

    def test_disabled_by_default_when_pipeline_flag_missing(self) -> None:
        spec = SimpleNamespace(pipeline={}, pipeline_get=lambda key, default=None: default)
        script = {"narration": "新しい書き出し案です。詳細"}
        with mock.patch.object(
            ai_text, "check_narration_opening_pattern_duplicate"
        ) as check_mock:
            run_daily._apply_narration_pattern_check(spec, script, ["過去の書き出し"])
        check_mock.assert_not_called()
        self.assertNotIn("_narration_opening_check", script)

    def test_skipped_when_no_recent_openings(self) -> None:
        script = {"narration": "新しい書き出し案です。詳細"}
        with mock.patch.object(
            ai_text, "check_narration_opening_pattern_duplicate"
        ) as check_mock:
            run_daily._apply_narration_pattern_check(self.spec, script, [])
        check_mock.assert_not_called()
        self.assertNotIn("_narration_opening_check", script)

    def test_records_match_and_logs_when_duplicate_found(self) -> None:
        script = {"narration": "新しい書き出し案です。詳細"}
        match = {
            "matched_opening": "過去の書き出し",
            "confidence": 0.8,
            "overlapping_axes": ["opening_syntax", "subject_frame"],
            "reason": "同じ主語枠と構文",
        }
        with (
            mock.patch.object(
                ai_text,
                "check_narration_opening_pattern_duplicate",
                return_value=match,
            ) as check_mock,
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_narration_pattern_check(
                self.spec, script, ["過去の書き出し"]
            )
        check_mock.assert_called_once_with("新しい書き出し案です", ["過去の書き出し"])
        self.assertEqual(
            script["_narration_opening_check"], {"checked": True, "match": match}
        )
        log_mock.assert_called_once()
        self.assertIn("書き出し修辞パターン重複の疑い", log_mock.call_args.args[0])

    def test_records_no_match_without_logging(self) -> None:
        script = {"narration": "新しい書き出し案です。詳細"}
        with (
            mock.patch.object(
                ai_text, "check_narration_opening_pattern_duplicate", return_value=None
            ),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_narration_pattern_check(
                self.spec, script, ["過去の書き出し"]
            )
        self.assertEqual(
            script["_narration_opening_check"], {"checked": True, "match": None}
        )
        log_mock.assert_not_called()

    def test_check_failure_is_recorded_as_no_match_without_raising(self) -> None:
        script = {"narration": "新しい書き出し案です。詳細"}
        with (
            mock.patch.object(
                ai_text,
                "check_narration_opening_pattern_duplicate",
                side_effect=RuntimeError("backend down"),
            ),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_narration_pattern_check(
                self.spec, script, ["過去の書き出し"]
            )
        self.assertEqual(
            script["_narration_opening_check"], {"checked": True, "match": None}
        )
        log_mock.assert_called_once()
        self.assertIn("判定に失敗", log_mock.call_args.args[0])


class RecentNarrationOpeningsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="alpha", history_file=self.root / "history.jsonl"
        )

    def _append(self, row: dict) -> None:
        self.spec.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.spec.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _write_script(self, workdir: Path, narration: str) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "script.json").write_text(
            json.dumps({"narration": narration}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_filters_by_corner_and_orders_oldest_to_newest(self) -> None:
        rows = [
            ("a", "Aコーナーの一本目です。詳細"),
            ("b", "Bコーナーの一本目です。詳細"),
            ("a", "Aコーナーの二本目です。詳細"),
        ]
        for i, (corner, text) in enumerate(rows):
            wd = self.root / f"wd{i}"
            self._write_script(wd, text)
            self._append({"status": "published", "corner": corner, "workdir": str(wd)})
        result = history.recent_narration_openings(self.spec, "a")
        self.assertEqual(result, ["Aコーナーの一本目です", "Aコーナーの二本目です"])

    def test_no_corner_filter_returns_all_corners(self) -> None:
        for i, corner in enumerate(["a", "b"]):
            wd = self.root / f"wd{i}"
            self._write_script(wd, f"{corner}コーナーです。詳細")
            self._append({"status": "published", "corner": corner, "workdir": str(wd)})
        result = history.recent_narration_openings(self.spec, None)
        self.assertEqual(len(result), 2)

    def test_missing_workdir_and_script_json_are_skipped(self) -> None:
        self._append(
            {
                "status": "published",
                "corner": "a",
                "workdir": str(self.root / "does-not-exist"),
            }
        )
        self._append({"status": "published", "corner": "a"})
        self.assertEqual(history.recent_narration_openings(self.spec, "a"), [])

    def test_non_dict_script_json_is_skipped_without_raising(self) -> None:
        # script.jsonがnull/リスト/文字列等トップレベルdict以外にパースされる
        # 壊れ方をしても、その行だけスキップしAttributeErrorを送出してはならない。
        for payload in ("null", "[]", '"broken"', "42"):
            with self.subTest(payload=payload):
                wd = self.root / f"wd-{payload}"
                wd.mkdir(parents=True, exist_ok=True)
                (wd / "script.json").write_text(payload, encoding="utf-8")
                spec = SimpleNamespace(
                    id="alpha", history_file=self.root / f"{payload}.jsonl"
                )
                spec.history_file.parent.mkdir(parents=True, exist_ok=True)
                with spec.history_file.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {"status": "published", "corner": "a", "workdir": str(wd)}
                        )
                        + "\n"
                    )
                self.assertEqual(history.recent_narration_openings(spec, "a"), [])

    def test_limit_keeps_only_the_newest_items(self) -> None:
        for i in range(5):
            wd = self.root / f"wd{i}"
            self._write_script(wd, f"書き出しその{i}です。詳細")
            self._append({"status": "published", "corner": "a", "workdir": str(wd)})
        result = history.recent_narration_openings(self.spec, "a", limit=2)
        self.assertEqual(result, ["書き出しその3です", "書き出しその4です"])

    def test_stops_reading_script_json_once_limit_is_reached(self) -> None:
        # issue #70レビュー指摘: 新しい順に走査しlimit件集まった時点で打ち切ることで、
        # 投稿本数の多いチャンネルで不要なscript.json読み込みが起きないことを確認する。
        for i in range(5):
            wd = self.root / f"wd{i}"
            self._write_script(wd, f"書き出しその{i}です。詳細")
            self._append({"status": "published", "corner": "a", "workdir": str(wd)})
        with mock.patch.object(
            history, "_narration_opening", wraps=history._narration_opening
        ) as opening_mock:
            result = history.recent_narration_openings(self.spec, "a", limit=2)
        self.assertEqual(result, ["書き出しその3です", "書き出しその4です"])
        self.assertEqual(opening_mock.call_count, 2)

    def test_unpublished_rows_are_ignored(self) -> None:
        wd = self.root / "wd0"
        self._write_script(wd, "スキップされる書き出しです。詳細")
        self._append(
            {"status": "queued", "corner": "a", "workdir": str(wd)}
        )
        self.assertEqual(history.recent_narration_openings(self.spec, "a"), [])


class BuildPromptOpeningsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        prompts = root / "prompts"
        prompts.mkdir(parents=True)
        self.persona = prompts / "persona.md"
        self.persona.write_text("PERSONA", encoding="utf-8")
        self.corner_tpl = prompts / "corner.md"
        self.corner_tpl.write_text("CORNER {date} {past_topics}", encoding="utf-8")
        self.spec = SimpleNamespace(root=root)
        self.corner = SimpleNamespace(
            persona_path=self.persona, corner_path=self.corner_tpl
        )

    def test_recent_openings_appends_avoid_section(self) -> None:
        prompt = corners.build_prompt(
            self.spec,
            self.corner,
            "2026-08-03",
            [],
            recent_openings=["過去の書き出し文"],
        )
        self.assertIn("直近の書き出し", prompt)
        self.assertIn("過去の書き出し文", prompt)

    def test_empty_or_none_recent_openings_leaves_prompt_byte_identical(self) -> None:
        base = corners.build_prompt(self.spec, self.corner, "2026-08-03", [])
        with_empty = corners.build_prompt(
            self.spec, self.corner, "2026-08-03", [], recent_openings=[]
        )
        with_none = corners.build_prompt(
            self.spec, self.corner, "2026-08-03", [], recent_openings=None
        )
        self.assertEqual(base, with_empty)
        self.assertEqual(base, with_none)


class GenerateOpeningRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.channels_dir = Path(self.tmp.name) / "channels"
        self.output_dir = Path(self.tmp.name) / "output"

    def _make_spec(self, channel_id: str, *, narration_opening_guard: bool) -> channel.ChannelSpec:
        root = self.channels_dir / channel_id
        prompts = root / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "persona_a.md").write_text("PERSONA", encoding="utf-8")
        (prompts / "corner_a.md").write_text(
            "CORNER {date} {past_topics}", encoding="utf-8"
        )
        (root / "voices.json").write_text(
            json.dumps({"voice_a": {"voicevox_speaker": 1, "label": "A"}}),
            encoding="utf-8",
        )
        guard = "true" if narration_opening_guard else "false"
        (root / "channel.toml").write_text(
            f"""\
[channel]
id = "{channel_id}"
name = "{channel_id}"
rotation = ["a"]

[corners.a]
label = "A"
persona = "prompts/persona_a.md"
corner = "prompts/corner_a.md"
voice = "voice_a"

[pipeline]
research = false
factcheck = false
plan = false
narration_opening_guard = {guard}
""",
            encoding="utf-8",
        )
        with mock.patch.object(config, "OUTPUT", self.output_dir):
            return channel.load(channel_id, channels_dir=self.channels_dir)

    def _script_json(self, narration: str, title: str = "タイトル") -> str:
        return json.dumps(
            {
                "title": title,
                "description": "概要",
                "tags": ["a"],
                "narration": narration,
                "scenes": [{}],
            },
            ensure_ascii=False,
        )

    def test_retry_avoids_repeating_the_immediately_previous_opening_family(self) -> None:
        spec = self._make_spec("guarded", narration_opening_guard=True)
        corner = spec.corners["a"]
        recent = ["人類はなぜ、繰り返してしまうのでしょうか"]
        responses = [
            self._script_json("人類はなぜ、また同じ過ちを繰り返すのでしょうか。詳細です。"),
            self._script_json("誰もいない工場の音だけが、正しさを語っていました。詳細です。"),
        ]
        with mock.patch.object(
            ai_text, "_dispatch", side_effect=responses
        ) as dispatch:
            script = ai_text.generate(
                spec, corner, "2026-08-03", [], recent_openings=recent
            )
        self.assertEqual(dispatch.call_count, 2)
        self.assertIn(
            "直前の書き出しは使用禁止", dispatch.call_args_list[1].args[0]
        )
        self.assertEqual(
            script["_opening_guard"], {"accepted_with_violation": False}
        )
        self.assertTrue(script["narration"].startswith("誰もいない工場の音"))

    def test_exhausted_retries_accept_last_draft_with_violation_recorded(self) -> None:
        spec = self._make_spec("guarded2", narration_opening_guard=True)
        corner = spec.corners["a"]
        recent = ["人類はなぜ、繰り返してしまうのでしょうか"]
        violating = self._script_json(
            "人類はなぜ、また同じ過ちを繰り返すのでしょうか。詳細です。"
        )
        with mock.patch.object(ai_text, "_dispatch", return_value=violating) as dispatch:
            script = ai_text.generate(
                spec, corner, "2026-08-03", [], recent_openings=recent
            )
        self.assertEqual(dispatch.call_count, config.SCRIPT_DRAFT_RETRIES)
        self.assertTrue(script["_opening_guard"]["accepted_with_violation"])
        self.assertIn("narration", script)

    def test_guard_disabled_by_default_leaves_output_unaffected(self) -> None:
        spec = self._make_spec("unguarded", narration_opening_guard=False)
        corner = spec.corners["a"]
        recent = ["人類はなぜ、繰り返してしまうのでしょうか"]
        violating = self._script_json(
            "人類はなぜ、また同じ過ちを繰り返すのでしょうか。詳細です。"
        )
        with mock.patch.object(ai_text, "_dispatch", return_value=violating) as dispatch:
            script = ai_text.generate(
                spec, corner, "2026-08-03", [], recent_openings=recent
            )
        dispatch.assert_called_once()
        self.assertNotIn("_opening_guard", script)
        # フラグOFFのチャンネルでは、recent_openingsが渡されていてもプロンプトへ
        # 動的注入されてはならない（Layer1もLayer2と同じくopt-inでなければならない）。
        # 動的注入セクションの見出しと、実データ由来の書き出し文そのもの（静的な
        # output_rules.mdの文言とは違い、これが出れば注入が起きた確実な証拠）の
        # 両方が無いことを確認する。
        prompt = dispatch.call_args.args[0]
        self.assertNotIn("これらと同じ型で始めない", prompt)
        self.assertNotIn(recent[0], prompt)

    def test_time_budget_exhaustion_accepts_fallback_instead_of_raising(self) -> None:
        # SCRIPT_DRAFT_TOTAL_TIMEOUT切れは、書き出しガード導入前は常にRuntimeErrorだった。
        # 既に違反ありの有効なfallback_scriptがある場合は、時間切れでも動画を丸ごと
        # 落とさず違反ありのまま採用する（致命的raiseにはしない）。
        spec = self._make_spec("guarded3", narration_opening_guard=True)
        corner = spec.corners["a"]
        recent = ["人類はなぜ、繰り返してしまうのでしょうか"]
        violating = self._script_json(
            "人類はなぜ、また同じ過ちを繰り返すのでしょうか。詳細です。"
        )
        with (
            mock.patch.object(config, "SCRIPT_DRAFT_TOTAL_TIMEOUT", 100),
            mock.patch.object(ai_text, "_monotonic", side_effect=[0, 10, 150]),
            mock.patch.object(
                ai_text, "_dispatch", return_value=violating
            ) as dispatch,
        ):
            script = ai_text.generate(
                spec, corner, "2026-08-03", [], recent_openings=recent
            )
        dispatch.assert_called_once()
        self.assertTrue(script["_opening_guard"]["accepted_with_violation"])


if __name__ == "__main__":
    unittest.main()
