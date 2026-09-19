"""読み上げ用表記と画面字幕用表記の分離テスト。"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import ai_text, compose, config, factcheck, llm, run_daily, voicevox


class SubtitleDisplayTest(unittest.TestCase):
    def test_validate_preserves_display_text_with_natural_spelling(self) -> None:
        script = {
            "title": "テスト",
            "description": "概要",
            "tags": [],
            "narration": "アップルを使います。次です。",
            "subtitle_narration": "Appleを使います。次です。",
            "scenes": [{}],
        }

        validated = ai_text._validate(script)

        self.assertEqual(validated["subtitle_narration"], "Appleを使います。次です。")

    def test_generated_validation_requires_display_text(self) -> None:
        script = {
            "title": "テスト",
            "description": "概要",
            "tags": [],
            "narration": "アップルを使います。",
            "scenes": [{}],
        }

        with self.assertRaisesRegex(ValueError, "subtitle_narration"):
            ai_text._validate(script, require_subtitle_narration=True)

    def test_alignment_accepts_same_sentence_boundaries(self) -> None:
        narration = voicevox.split_sentences("アップルを使います。次です。")

        self.assertEqual(
            voicevox.align_subtitle_sentences(
                narration,
                "Appleを使います。次です。",
            ),
            ["Appleを使います。", "次です。"],
        )

    def test_alignment_falls_back_when_sentence_boundaries_differ(self) -> None:
        narration = voicevox.split_sentences("アップルを使います。次です。")

        self.assertEqual(
            voicevox.align_subtitle_sentences(narration, "Appleを使います。"),
            [None, None],
        )

    def test_alignment_reuses_audio_comma_boundaries_when_display_is_shorter(self) -> None:
        narration = voicevox.split_sentences(
            ("カタカナ" * 16) + "、読み上げ用の長い説明です。"
        )

        self.assertEqual(
            voicevox.align_subtitle_sentences(
                narration,
                "Appleの短い説明、表示用の文です。",
            ),
            ["Appleの短い説明、", "表示用の文です。"],
        )

    def test_build_subtitles_prefers_display_text(self) -> None:
        segment = SimpleNamespace(
            text="アップルを使います。",
            subtitle_text="Appleを使います。",
            start=0.0,
            end=2.0,
        )

        subtitles = compose.build_subtitles([segment])

        self.assertEqual(subtitles, [("Appleを使います", 0.0, 2.0)])

    def test_build_subtitles_falls_back_for_legacy_segment(self) -> None:
        segment = SimpleNamespace(
            text="アップルを使います。",
            start=0.0,
            end=2.0,
        )

        subtitles = compose.build_subtitles([segment])

        self.assertEqual(subtitles, [("アップルを使います", 0.0, 2.0)])

    def test_tts_timing_keeps_display_text_as_optional_provenance(self) -> None:
        result = run_daily._tts_timing_payload(
            voicevox.TtsResult(
                wav_path=Path("test.wav"),
                duration=2.0,
                segments=[
                    voicevox.Segment(
                        "アップルを使います。",
                        0.0,
                        2.0,
                        subtitle_text="Appleを使います。",
                    )
                ],
            )
        )

        self.assertEqual(result["segments"][0]["text"], "アップルを使います。")
        self.assertEqual(
            result["segments"][0]["subtitle_text"], "Appleを使います。"
        )

    def test_factcheck_keeps_display_text_when_audit_is_unchanged(self) -> None:
        audit = {"changed": False, "issues": []}
        with (
            patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            patch.object(factcheck, "_attempt_audit", return_value=audit),
        ):
            result = factcheck.verify_and_correct(
                "アップルを使います。",
                research={
                    "facts": [
                        {
                            "claim": "確認済み",
                            "source_url": "https://example.org/source",
                        }
                    ]
                },
                subtitle_narration="Appleを使います。",
            )

        self.assertEqual(result["narration"], "アップルを使います。")
        self.assertEqual(result["subtitle_narration"], "Appleを使います。")

    def test_factcheck_rewrites_both_text_variants(self) -> None:
        audit = {
            "changed": True,
            "issues": [
                {
                    "before": "誤り",
                    "decision": "correct",
                    "verified_fact": "正しい",
                    "reason": "一次資料で確認",
                    "source_url": "https://example.org/source",
                    "replacement": "正しい",
                }
            ],
        }
        with (
            patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            patch.object(factcheck, "_attempt_audit", return_value=audit),
            patch.object(
                factcheck,
                "_attempt_rewrite",
                return_value=(
                    "アップルは正しいです。次です。",
                    "Appleは正しいです。次です。",
                ),
            ),
        ):
            result = factcheck.verify_and_correct(
                "アップルは誤りです。次です。",
                research={
                    "facts": [
                        {
                            "claim": "正しい",
                            "source_url": "https://example.org/source",
                        }
                    ]
                },
                subtitle_narration="Appleは誤りです。次です。",
            )

        self.assertEqual(result["narration"], "アップルは正しいです。次です。")
        self.assertEqual(
            result["subtitle_narration"], "Appleは正しいです。次です。"
        )

    def test_subtitle_factcheck_rewrite_rejects_stale_display_claim(self) -> None:
        audit = {
            "before": "誤り",
            "decision": "correct",
            "replacement": "正しい",
        }

        with self.assertRaises(factcheck.SubtitleRewriteValidationError):
            factcheck._validate_subtitle_rewrite(
                "Appleは誤りです。",
                "Appleは誤りです。",
                original_narration="アップルは誤りです。",
                rewritten_narration="アップルは正しいです。",
                audit=[audit],
            )

    def test_subtitle_factcheck_rejects_empty_correct_replacement(self) -> None:
        with self.assertRaisesRegex(
            factcheck.SubtitleRewriteValidationError, "置換形"
        ):
            factcheck._validate_subtitle_rewrite(
                "Appleは誤りです。",
                "Appleは正しいです。",
                original_narration="アップルは誤りです。",
                rewritten_narration="アップルは正しいです。",
                audit=[
                    {
                        "before": "誤り",
                        "decision": "correct",
                        "replacement": "",
                    }
                ],
            )

    def test_single_stage_factcheck_falls_back_without_actionable_audit(self) -> None:
        raw = (
            '{"narration":"アップルは正しいです。",'
            '"subtitle_narration":"Appleは間違いです。",'
            '"changed":true,"issues":[]}'
        )
        with patch.object(llm, "run_codex", return_value=raw):
            result = factcheck._attempt(
                "prompt",
                "codex",
                require_subtitle_narration=True,
                subtitle_narration="Appleは誤りです。",
                original_narration="アップルは誤りです。",
            )

        self.assertEqual(result["subtitle_narration"], result["narration"])

    def test_single_stage_factcheck_sanitizes_display_text(self) -> None:
        raw = (
            '{"narration":"アップルは正しいです。",'
            '"subtitle_narration":"Appleは正しい\\u200bです。",'
            '"changed":true,"issues":[{"before":"誤り",'
            '"decision":"correct","replacement":"正しい"}]}'
        )
        with patch.object(factcheck.llm, "run_codex", return_value=raw):
            result = factcheck._attempt(
                "prompt",
                "codex",
                require_subtitle_narration=True,
                subtitle_narration="Appleは誤りです。",
                original_narration="アップルは誤りです。",
            )

        self.assertEqual(result["subtitle_narration"], "Appleは正しいです。")

    def test_factcheck_prompt_renders_single_brace_output_schema(self) -> None:
        raw = (
            '{"narration":"アップルを使います。",'
            '"subtitle_narration":"Appleを使います。",'
            '"changed":false,"issues":[]}'
        )
        with patch.object(
            config, "FACTCHECK_BACKEND", "codex"
        ), patch.object(factcheck.llm, "run_codex", return_value=raw) as run_codex:
            result = factcheck.verify_and_correct(
                "アップルを使います。",
                subtitle_narration="Appleを使います。",
            )

        prompt = run_codex.call_args.args[0]
        self.assertIn('{"narration": "修正後の最終ナレーション全文"', prompt)
        self.assertIn(
            '"subtitle_narration": "同じ修正を反映した画面表示用全文"', prompt
        )
        self.assertNotIn('{{"narration"', prompt)
        self.assertEqual(result["subtitle_narration"], "Appleを使います。")

    def test_subtitle_factcheck_accepts_synced_remove_audit(self) -> None:
        result = factcheck._validate_subtitle_rewrite(
            "Appleは誤りを含みます。",
            "Appleです。",
            original_narration="アップルは誤りを含みます。",
            rewritten_narration="アップルです。",
            audit=[
                {
                    "before": "誤りを含みます",
                    "decision": "remove",
                    "replacement": "",
                }
            ],
        )

        self.assertEqual(result, "Appleです。")


if __name__ == "__main__":
    unittest.main()
