from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import (
    ai_text,
    channel,
    config,
    corners,
    history,
    publish,
    run_daily,
    topic_ledger,
    voicevox,
    youtube_review,
)


class ChannelPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.channels_dir = self.root / "channels"
        self.output_dir = self.root / "output"

    def _make_spec(
        self,
        channel_id: str,
        *,
        output_rules: str | None = None,
        output_rules_addendum: str | None = None,
        pipeline: str = "",
    ) -> channel.ChannelSpec:
        root = self.channels_dir / channel_id
        prompts = root / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "persona_a.md").write_text(
            f"PERSONA-{channel_id}", encoding="utf-8"
        )
        (prompts / "corner_a.md").write_text(
            "CORNER-A {date} {past_topics}", encoding="utf-8"
        )
        (prompts / "persona_b.md").write_text("PERSONA-B", encoding="utf-8")
        (prompts / "corner_b.md").write_text("CORNER-B", encoding="utf-8")
        if output_rules is not None:
            (prompts / "output_rules.md").write_text(output_rules, encoding="utf-8")
        if output_rules_addendum is not None:
            (prompts / "output_rules_addendum.md").write_text(
                output_rules_addendum,
                encoding="utf-8",
            )
        (root / "voices.json").write_text(
            json.dumps(
                {
                    "voice_a": {"voicevox_speaker": 41, "label": "Voice A"},
                    "voice_b": {"voicevox_speaker": 42, "label": "Voice B"},
                }
            ),
            encoding="utf-8",
        )
        (root / "channel.toml").write_text(
            f'''\
[channel]
id = "{channel_id}"
name = "{channel_id}"
rotation = ["a", "b"]

[corners.a]
label = "A"
persona = "prompts/persona_a.md"
corner = "prompts/corner_a.md"
voice = "voice_a"

[corners.b]
label = "B"
persona = "prompts/persona_b.md"
corner = "prompts/corner_b.md"
voice = "voice_b"

{pipeline}
''',
            encoding="utf-8",
        )
        with patch.object(config, "OUTPUT", self.output_dir):
            return channel.load(channel_id, channels_dir=self.channels_dir)

    def test_rotation_and_channel_voice_resolution(self) -> None:
        spec = self._make_spec("alpha")

        self.assertEqual(corners.pick_corner(spec, None).key, "a")
        self.assertEqual(corners.pick_corner(spec, "a").key, "b")
        self.assertEqual(corners.pick_corner(spec, "b").key, "a")
        self.assertEqual(spec.voice_for("a").speaker, 41)
        self.assertEqual(spec.voice_for("b").speaker, 42)

    def test_build_prompt_uses_channel_prompts_and_common_rules(self) -> None:
        spec = self._make_spec("alpha")
        common_rules = (config.PROMPTS / "output_rules.md").read_text(encoding="utf-8")

        prompt = corners.build_prompt(
            spec,
            spec.corners["a"],
            "2026-07-16",
            ["既出テーマ"],
        )

        self.assertIn("PERSONA-alpha", prompt)
        self.assertIn("CORNER-A 2026-07-16 既出テーマ", prompt)
        self.assertIn(common_rules, prompt)

    def test_missing_addendum_preserves_legacy_prompt_bytes(self) -> None:
        spec = self._make_spec("alpha")
        persona = spec.corners["a"].persona_path.read_text(encoding="utf-8")
        rules = (config.PROMPTS / "output_rules.md").read_text(encoding="utf-8")
        corner = "CORNER-A 2026-07-16 既出テーマ"

        prompt = corners.build_prompt(
            spec,
            spec.corners["a"],
            "2026-07-16",
            ["既出テーマ"],
        )

        self.assertEqual(prompt, f"{persona}\n\n{rules}\n\n{corner}\n")

    def test_channel_output_rules_addendum_follows_common_rules(self) -> None:
        spec = self._make_spec(
            "alpha",
            output_rules_addendum="CHANNEL-ADDENDUM",
        )
        common_rules = (config.PROMPTS / "output_rules.md").read_text(encoding="utf-8")

        prompt = corners.build_prompt(spec, spec.corners["a"], "2026-07-16", [])

        self.assertIn(
            f"{common_rules}\n\nCHANNEL-ADDENDUM\n\nCORNER-A",
            prompt,
        )

    def test_channel_output_rules_override_common_rules(self) -> None:
        spec = self._make_spec("alpha", output_rules="CHANNEL-RULES-ONLY")

        prompt = corners.build_prompt(spec, spec.corners["a"], "2026-07-16", [])

        self.assertIn("CHANNEL-RULES-ONLY", prompt)
        self.assertNotIn(
            (config.PROMPTS / "output_rules.md").read_text(encoding="utf-8"),
            prompt,
        )

    def test_youtube_growth_addendum_is_scoped_to_its_channel(self) -> None:
        youtube_growth = channel.load("youtube-growth")
        ideology = channel.load("ideology")
        local_rules = (
            "冒頭の二〜四文を短い「結論と次の一手」にします",
            "共通ルールの「つかみは問いから」を適用しません",
            "次に試す行動、測る数字、または判断基準を宣言的な文で示して締めます",
            "「あなたはどう思いますか」「どうでしょうか」",
        )

        for corner in youtube_growth.corners.values():
            with self.subTest(corner=corner.key):
                prompt = corners.build_prompt(
                    youtube_growth,
                    corner,
                    "2026-07-26",
                    [],
                )
                for local_rule in local_rules:
                    self.assertIn(local_rule, prompt)
                    self.assertGreater(
                        prompt.index(local_rule),
                        prompt.index("つかみは問いから"),
                    )

        ideology_corner = ideology.corners["communism"]
        ideology_prompt = corners.build_prompt(
            ideology,
            ideology_corner,
            "2026-07-26",
            [],
        )
        ideology_persona = ideology_corner.persona_path.read_text(encoding="utf-8")
        common_rules = (config.PROMPTS / "output_rules.md").read_text(encoding="utf-8")
        ideology_corner_body = ideology_corner.corner_path.read_text(
            encoding="utf-8"
        ).replace("{date}", "2026-07-26").replace(
            "{past_topics}",
            "（まだありません）",
        )
        for local_rule in local_rules:
            self.assertNotIn(local_rule, ideology_prompt)
        self.assertEqual(
            ideology_prompt,
            f"{ideology_persona}\n\n{common_rules}\n\n{ideology_corner_body}\n",
        )

    def test_history_is_isolated_by_channel(self) -> None:
        alpha = self._make_spec("alpha")
        beta = self._make_spec("beta")
        with patch.object(config, "OUTPUT", self.output_dir):
            history.record(
                alpha,
                "a",
                "Alpha title",
                extra={"description": "Alpha angle\nmore"},
            )
            history.record(beta, "b", "Beta title")

            self.assertEqual(history.last_corner(alpha), "a")
            self.assertEqual(history.last_corner(beta), "b")
            self.assertEqual(
                history.recent_topics(alpha),
                ["Alpha title（Alpha angle）"],
            )
            self.assertEqual(history.recent_titles(alpha), ["Alpha title"])
            self.assertEqual(history.recent_topics(beta), ["Beta title"])
            self.assertEqual(history.recent_titles(beta), ["Beta title"])
            self.assertNotEqual(alpha.history_file, beta.history_file)
            alpha_row = json.loads(alpha.history_file.read_text(encoding="utf-8"))
            beta_row = json.loads(beta.history_file.read_text(encoding="utf-8"))

        self.assertEqual(alpha_row["channel"], "alpha")
        self.assertEqual(beta_row["channel"], "beta")

    def test_generate_honors_pipeline_switches_and_records_channel(self) -> None:
        spec = self._make_spec(
            "alpha",
            pipeline="""\
[pipeline]
research = false
plan = false
factcheck = false
""",
        )
        raw = json.dumps(
            {
                "title": "Title",
                "description": "Description",
                "tags": [],
                "narration": "本題から始まるナレーションです。",
                "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
            }
        )
        with (
            patch.object(config, "SCRIPT_RESEARCH", True),
            patch.object(config, "SCRIPT_PLAN", True),
            patch.object(config, "SCRIPT_FACTCHECK", True),
            patch.object(ai_text, "_dispatch", return_value=raw) as dispatch_mock,
        ):
            guarded_topics: list[str] = []
            script = ai_text.generate(
                spec,
                spec.corners["a"],
                "2026-07-16",
                [],
                topic_guard=guarded_topics.append,
                performance_decision={
                    "decision_id": "decision-1",
                    "guidance": "retention形式を1変数だけ試す",
                },
            )

        self.assertEqual(script["_channel"], "alpha")
        self.assertEqual(script["_corner"], "a")
        self.assertEqual(script["_speaker"], 41)
        self.assertEqual(guarded_topics, ["Title Description"])
        self.assertEqual(
            script["_performance_feedback"]["decision_id"], "decision-1"
        )
        self.assertIn(
            "retention形式を1変数だけ試す",
            dispatch_mock.call_args.args[0],
        )

    def test_generate_does_not_fallback_to_claude_after_primary_failure(self) -> None:
        spec = self._make_spec(
            "alpha",
            pipeline="""\
[pipeline]
research = false
plan = false
factcheck = false
""",
        )
        with (
            patch.object(config, "TEXT_BACKEND", "opencode_go"),
            patch.object(config, "SCRIPT_DRAFT_RETRIES", 1),
            patch.object(config, "WRITE_LLM_TIMEOUT", 900),
            patch.object(config, "SCRIPT_DRAFT_TOTAL_TIMEOUT", 2700),
            patch.object(
                ai_text, "_dispatch", side_effect=RuntimeError("backend unavailable")
            ) as dispatch_mock,
            patch.object(ai_text, "_run_claude_cli") as claude_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "執筆が規定回数で揃いませんでした"):
                ai_text.generate(spec, spec.corners["a"], "2026-07-26", [])

        claude_mock.assert_not_called()
        self.assertLessEqual(dispatch_mock.call_args.kwargs["timeout"], 900)

    def test_generate_stops_retrying_after_draft_total_budget(self) -> None:
        spec = self._make_spec(
            "draft-budget",
            pipeline="""\
[pipeline]
research = false
plan = false
factcheck = false
""",
        )

        def fail_after_budget(_prompt, timeout=None):
            raise RuntimeError("backend unavailable")

        clock = [100.0]

        def monotonic() -> float:
            current = clock[0]
            clock[0] += 0.5
            return current

        with (
            patch.object(config, "TEXT_BACKEND", "opencode_go"),
            patch.object(config, "SCRIPT_DRAFT_RETRIES", 3),
            patch.object(config, "WRITE_LLM_TIMEOUT", 900),
            patch.object(config, "SCRIPT_DRAFT_TOTAL_TIMEOUT", 1),
            patch.object(
                ai_text,
                "_monotonic",
                side_effect=monotonic,
            ),
            patch.object(ai_text, "_dispatch", side_effect=fail_after_budget) as dispatch_mock,
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"執筆段全体の時間上限.*backend unavailable"
            ):
                ai_text.generate(spec, spec.corners["a"], "2026-07-26", [])

        self.assertEqual(dispatch_mock.call_count, 1)

    def test_factcheck_only_opencode_go_fetches_research_materials(self) -> None:
        spec = self._make_spec(
            "factcheck-only",
            pipeline="""\
[pipeline]
research = false
plan = false
factcheck = true
""",
        )
        raw = json.dumps(
            {
                "title": "Title",
                "description": "Description",
                "tags": [],
                "narration": "本題から始まるナレーションです。",
                "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
            }
        )
        research_data = {
            "topic": "確認用の題材",
            "facts": [{"claim": "確認済み", "source_url": "https://support.google.com/youtube/help"}],
        }
        corrected = {
            "narration": "確認済みのナレーションです。",
            "changed": True,
            "issues": [],
        }
        with (
            patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            patch.object(config, "SCRIPT_FACTCHECK", False),
            patch("doci.research.web_research", return_value=research_data) as research_mock,
            patch("doci.factcheck.verify_and_correct", return_value=corrected) as factcheck_mock,
            patch.object(ai_text, "_dispatch", return_value=raw),
        ):
            script = ai_text.generate(spec, spec.corners["a"], "2026-07-26", [])

        research_mock.assert_called_once()
        self.assertFalse(research_mock.call_args.kwargs["require_youtube_examples"])
        factcheck_mock.assert_called_once_with("本題から始まるナレーションです。", research_data)
        self.assertEqual(script["narration"], "確認済みのナレーションです。")

    def test_failed_research_is_not_repeated_for_factcheck(self) -> None:
        spec = self._make_spec(
            "research-failed-factcheck",
            pipeline="""\
[pipeline]
research = true
plan = false
factcheck = true
""",
        )
        raw = json.dumps(
            {
                "title": "Title",
                "description": "Description",
                "tags": [],
                "narration": "本題から始まるナレーションです。",
                "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
            }
        )
        with (
            patch.object(config, "RESEARCH_BACKEND", "opencode_go"),
            patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            patch("doci.research.web_research", return_value=None) as research_mock,
            patch("doci.factcheck.verify_and_correct", return_value=None),
            patch.object(ai_text, "_dispatch", return_value=raw),
        ):
            ai_text.generate(spec, spec.corners["a"], "2026-07-26", [])

        research_mock.assert_called_once()

    def test_factcheck_required_sources_does_not_reraise_transient_error(self) -> None:
        spec = self._make_spec(
            "factcheck-transient-error",
            pipeline="""\
[pipeline]
research = false
plan = false
factcheck = true
""",
        )
        raw = json.dumps(
            {
                "title": "Title",
                "description": "Description",
                "tags": [],
                "narration": "本題から始まるナレーションです。",
                "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
            }
        )
        with (
            patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            patch.object(config, "SCRIPT_FACTCHECK_RESEARCH", False),
            patch.object(config, "SCRIPT_FACTCHECK_REQUIRE_SOURCES", True),
            patch(
                "doci.factcheck.verify_and_correct",
                side_effect=ValueError("一時的なJSON不良"),
            ),
            patch.object(ai_text, "_dispatch", return_value=raw),
        ):
            script = ai_text.generate(spec, spec.corners["a"], "2026-07-26", [])

        self.assertEqual(script["narration"], "本題から始まるナレーションです。")

    def test_factcheck_research_can_be_disabled_independently(self) -> None:
        spec = self._make_spec(
            "factcheck-no-research",
            pipeline="""\
[pipeline]
research = false
plan = false
factcheck = true
""",
        )
        raw = json.dumps(
            {
                "title": "Title",
                "description": "Description",
                "tags": [],
                "narration": "本題から始まるナレーションです。",
                "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
            }
        )
        corrected = {
            "narration": "原文を確認しました。",
            "changed": False,
            "issues": [],
        }
        with (
            patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
            patch.object(config, "SCRIPT_FACTCHECK", False),
            patch.object(config, "SCRIPT_FACTCHECK_RESEARCH", False),
            patch("doci.research.web_research") as research_mock,
            patch("doci.factcheck.verify_and_correct", return_value=corrected),
            patch.object(ai_text, "_dispatch", return_value=raw),
        ):
            ai_text.generate(spec, spec.corners["a"], "2026-07-26", [])

        research_mock.assert_not_called()

    def test_generate_guards_researched_topic_before_drafting(self) -> None:
        spec = self._make_spec(
            "alpha",
            pipeline="""\
[pipeline]
research = true
plan = false
factcheck = false
""",
        )
        guarded_topics: list[str] = []

        def reject(topic: str) -> None:
            guarded_topics.append(topic)
            raise RuntimeError("duplicate topic")

        with (
            patch(
                "doci.research.web_research",
                return_value={
                    "topic": "既存と重複する題材",
                    "facts": [{"claim": "fact", "source_url": "https://example.com"}],
                },
            ) as research_mock,
            patch.object(ai_text, "_dispatch") as dispatch_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "duplicate topic"):
                ai_text.generate(
                    spec,
                    spec.corners["a"],
                    "2026-07-16",
                    [],
                    topic_guard=reject,
                )

        self.assertEqual(guarded_topics, ["既存と重複する題材"])
        self.assertTrue(research_mock.call_args.kwargs["require_structured_novelty"])
        dispatch_mock.assert_not_called()

    def test_generate_preserves_theme_review_research_fields(self) -> None:
        spec = self._make_spec(
            "review-research",
            pipeline="""\
[pipeline]
research = true
plan = false
factcheck = false
""",
        )
        research = {
            "topic": "YouTubeショートの冒頭離脱を改善する",
            "angle": "視聴維持率を使って冒頭を比較する",
            "youtube_creator_audience": "YouTube制作者",
            "youtube_creator_problem": "YouTubeの視聴維持率が冒頭で落ちる課題",
            "viewer_action": "YouTube Studioで視聴維持率を確認して冒頭を編集する",
            "theme_fit": "clear",
            "theme_fit_reason": "YouTube Studioの視聴維持率改善を扱うため",
            "facts": [
                {
                    "claim": "確認対象の事実",
                    "source_url": "https://example.com/source",
                }
            ],
        }
        raw = json.dumps(
            {
                "title": "YouTubeショートの冒頭離脱を直す",
                "description": "YouTubeの視聴維持率を改善します。",
                "tags": [],
                "narration": "YouTube Studioの視聴維持率を見て冒頭を編集します。",
                "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
            }
        )
        with (
            patch("doci.research.web_research", return_value=research),
            patch.object(ai_text, "_dispatch", return_value=raw),
        ):
            script = ai_text.generate(
                spec,
                spec.corners["a"],
                "2026-07-26",
                [],
            )

        self.assertEqual(script["_research"], research)
        for key in (
            "youtube_creator_audience",
            "youtube_creator_problem",
            "viewer_action",
            "theme_fit",
            "theme_fit_reason",
        ):
            self.assertEqual(script["_research"][key], research[key])

    def _run_review_pipeline(
        self,
        spec: channel.ChannelSpec,
        script: dict,
        video_id: str,
        *,
        publish_dry_run: bool = False,
        publish_unknown: bool = False,
        publish_results: list[publish.PublishResult] | None = None,
        generate_side_effect=None,
    ):
        def fake_fetch_image(_prompt, out_path, **_kwargs):
            Path(out_path).write_bytes(b"image")
            return Path(out_path)

        def fake_compose(_scenes, _wav, _duration, out_path, **_kwargs):
            Path(out_path).write_bytes(b"video")
            return Path(out_path)

        def fake_thumbnail(_title, out_path, **_kwargs):
            Path(out_path).write_bytes(b"thumbnail")
            return Path(out_path)

        uploaded = publish.PublishResult(
            "youtube",
            "unknown"
            if publish_unknown
            else "dry_run"
            if publish_dry_run
            else "ok",
            url=f"https://youtu.be/{video_id}",
            id=None if publish_dry_run or publish_unknown else video_id,
            detail="投稿結果不明" if publish_unknown else "",
        )
        result_rows = publish_results if publish_results is not None else [uploaded]
        with (
            patch.dict(os.environ, {"DOCI_REVIEW_RECONCILED": ""}),
            patch.object(config, "OUTPUT", self.output_dir),
            patch.object(config, "PUBLISH_DRY_RUN", publish_dry_run),
            patch.object(
                run_daily,
                "_reconcile_youtube_review",
            ) as reconcile_mock,
            patch.object(
                ai_text,
                "generate",
                side_effect=generate_side_effect,
                return_value=script,
            ),
            patch.object(
                run_daily.voicevox,
                "synthesize",
                return_value=voicevox.TtsResult(
                    wav_path=self.root / "fake.wav",
                    duration=10.0,
                    segments=[],
                ),
            ),
            patch.object(run_daily.assets, "fetch_video", side_effect=fake_fetch_image),
            patch.object(run_daily.assets, "fetch_image", side_effect=fake_fetch_image),
            patch.object(run_daily.compose, "compose", side_effect=fake_compose),
            patch("doci.thumbnail.render", side_effect=fake_thumbnail),
            patch("doci.thumbnail.to_16x9", side_effect=fake_thumbnail),
            patch("doci.publish.publish", return_value=result_rows) as publish_mock,
            patch.object(topic_ledger, "recent_topics", return_value=[]),
            patch.object(topic_ledger, "reserve", wraps=topic_ledger.reserve) as ledger_reserve_mock,
            patch.object(history, "reserve_topic", wraps=history.reserve_topic) as history_reserve_mock,
            patch.object(youtube_review, "queue_pending") as queue_mock,
            patch.object(
                youtube_review,
                "ensure_issue",
                return_value=SimpleNamespace(
                    number=42,
                    url="https://github.com/owner/repo/issues/42",
                ),
            ) as ensure_mock,
        ):
            result = run_daily.run(
                spec,
                "2026-07-26",
                "a",
                do_upload=True,
                video_scenes=0,
            )

        return (
            result,
            reconcile_mock,
            publish_mock,
            queue_mock,
            ensure_mock,
            ledger_reserve_mock,
            history_reserve_mock,
        )

    def _review_script(self, *, viewer_action: str) -> dict:
        return {
            "title": "YouTubeショートの冒頭離脱を直す",
            "description": "YouTubeの視聴維持率を改善します。",
            "tags": [],
            "narration": "YouTube Studioの視聴維持率を見て冒頭を編集します。",
            "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
            "_research": {
                "topic": "YouTubeショートの冒頭離脱を改善する",
                "angle": "視聴維持率を使って冒頭を比較する",
                "youtube_creator_audience": "YouTube制作者",
                "youtube_creator_problem": "YouTubeの視聴維持率が冒頭で落ちる課題",
                "viewer_action": viewer_action,
                "theme_fit": "clear",
                "theme_fit_reason": "YouTube Studioの視聴維持率改善を扱うため",
            },
        }

    def _review_spec(
        self,
        channel_id: str,
        *,
        require_approval: bool = False,
    ) -> channel.ChannelSpec:
        approval_line = "require_approval = true\n" if require_approval else ""
        return self._make_spec(
            channel_id,
            pipeline=f"""\
[publish]
platforms = ["youtube"]

[publish.youtube]
privacy = "unlisted"
client_secret = "client.json"
token = "token.json"

[publish.youtube.review]
enabled = true
{approval_line}repository = "owner/repo"
publish_label = "公開承認"
hold_label = "保留"
keep_unlisted_label = "限定公開で保持"

[pipeline]
research = false
plan = false
factcheck = false
""",
        )

    def test_run_daily_routes_missing_action_to_unlisted_issue_workflow(self) -> None:
        spec = self._review_spec("review-unlisted")
        script = self._review_script(viewer_action="")

        result, reconcile_mock, publish_mock, queue_mock, ensure_mock, _, _ = (
            self._run_review_pipeline(
                spec,
                script,
                "unlisted123",
            )
        )

        reconcile_mock.assert_called_once_with(spec, True)
        self.assertEqual(
            publish_mock.call_args.kwargs["youtube_privacy"],
            "unlisted",
        )
        queue_mock.assert_called_once()
        self.assertEqual(queue_mock.call_args.args[:3], (spec, "unlisted123", script["title"]))
        self.assertFalse(queue_mock.call_args.args[3].eligible_for_public)
        ensure_mock.assert_called_once_with(spec, "unlisted123")
        self.assertEqual(
            result["youtube_review_issue"],
            "https://github.com/owner/repo/issues/42",
        )

    def test_run_daily_routes_complete_theme_fields_directly_to_public(self) -> None:
        spec = self._review_spec("review-public")
        script = self._review_script(
            viewer_action="YouTube Studioで視聴維持率を確認して冒頭を編集する"
        )

        result, reconcile_mock, publish_mock, queue_mock, ensure_mock, _, _ = (
            self._run_review_pipeline(
                spec,
                script,
                "public123",
            )
        )

        reconcile_mock.assert_called_once_with(spec, True)
        self.assertEqual(
            publish_mock.call_args.kwargs["youtube_privacy"],
            "public",
        )
        queue_mock.assert_not_called()
        ensure_mock.assert_not_called()
        self.assertEqual(result["youtube_privacy"], "public")
        self.assertIsNone(result["youtube_review_issue"])

    def test_run_daily_require_approval_keeps_clear_theme_unlisted(self) -> None:
        spec = self._review_spec("review-approval", require_approval=True)
        script = self._review_script(
            viewer_action="YouTube Studioで視聴維持率を確認して冒頭を編集する"
        )

        result, _, publish_mock, queue_mock, ensure_mock, _, _ = (
            self._run_review_pipeline(
                spec,
                script,
                "approval123",
            )
        )

        self.assertEqual(
            publish_mock.call_args.kwargs["youtube_privacy"],
            "unlisted",
        )
        queue_mock.assert_called_once()
        self.assertTrue(queue_mock.call_args.args[3].eligible_for_public)
        ensure_mock.assert_called_once_with(spec, "approval123")
        self.assertEqual(
            result["youtube_review_issue"],
            "https://github.com/owner/repo/issues/42",
        )

    def test_global_publish_dry_run_does_not_queue_topic_reservations(self) -> None:
        spec = self._review_spec("review-dry-run")
        script = self._review_script(
            viewer_action="YouTube Studioで視聴維持率を確認して冒頭を編集する"
        )

        def generate_and_guard(*_args, **kwargs):
            kwargs["topic_metadata_guard"](script["_research"])
            kwargs["topic_guard"](script["_research"]["topic"])
            return script

        (
            result,
            reconcile_mock,
            publish_mock,
            queue_mock,
            ensure_mock,
            ledger_reserve_mock,
            history_reserve_mock,
        ) = self._run_review_pipeline(
            spec,
            script,
            "dry123",
            publish_dry_run=True,
            generate_side_effect=generate_and_guard,
        )

        reconcile_mock.assert_called_once_with(spec, False)
        publish_mock.assert_called_once()
        queue_mock.assert_not_called()
        ensure_mock.assert_not_called()
        self.assertFalse(ledger_reserve_mock.call_args.kwargs["reserve"])
        self.assertFalse(history_reserve_mock.call_args.kwargs["reserve"])
        self.assertFalse((self.output_dir / "topic_ledger.jsonl").exists())
        with patch.object(config, "OUTPUT", self.output_dir):
            rows = [
                json.loads(line)
                for line in spec.history_file.read_text(encoding="utf-8").splitlines()
            ]
        self.assertNotIn("queued", [row.get("status") for row in rows])
        self.assertNotIn("publishing", [row.get("status") for row in rows])
        self.assertEqual(result["video_id"], None)

    def test_unknown_publish_keeps_topic_in_publishing_state(self) -> None:
        spec = self._review_spec("review-unknown")
        script = self._review_script(
            viewer_action="YouTube Studioで視聴維持率を確認して冒頭を編集する"
        )

        def generate_and_guard(*_args, **kwargs):
            kwargs["topic_metadata_guard"](script["_research"])
            kwargs["topic_guard"](script["_research"]["topic"])
            return script

        result, _, _, _, _, _, _ = self._run_review_pipeline(
            spec,
            script,
            "unknown123",
            publish_unknown=True,
            generate_side_effect=generate_and_guard,
        )

        with patch.object(config, "OUTPUT", self.output_dir):
            ledger_rows = [
                json.loads(line)
                for line in (self.output_dir / "topic_ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            history_rows = [
                json.loads(line)
                for line in spec.history_file.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(result["video_id"], None)
        self.assertEqual(ledger_rows[-1]["status"], "publishing")
        self.assertEqual(history_rows[-1]["status"], "publishing")
        self.assertEqual(
            ledger_rows[-1]["reservation_id"],
            history_rows[-1]["topic_ledger_reservation_id"],
        )

    def test_mixed_publish_results_keep_topic_in_publishing_state(self) -> None:
        spec = self._review_spec("review-mixed-results")
        script = self._review_script(
            viewer_action="YouTube Studioで視聴維持率を確認して冒頭を編集する"
        )

        def generate_and_guard(*_args, **kwargs):
            kwargs["topic_metadata_guard"](script["_research"])
            kwargs["topic_guard"](script["_research"]["topic"])
            return script

        result, _, _, _, _, _, _ = self._run_review_pipeline(
            spec,
            script,
            "mixed123",
            publish_results=[
                publish.PublishResult("youtube", "ok", id="mixed123"),
                publish.PublishResult(
                    "tiktok", "unknown", detail="投稿結果不明: timeout"
                ),
            ],
            generate_side_effect=generate_and_guard,
        )

        with patch.object(config, "OUTPUT", self.output_dir):
            ledger_rows = [
                json.loads(line)
                for line in (self.output_dir / "topic_ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            history_rows = [
                json.loads(line)
                for line in spec.history_file.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(result["video_id"], "mixed123")
        self.assertEqual(ledger_rows[-1]["status"], "publishing")
        self.assertEqual(history_rows[-1]["status"], "publishing")
        self.assertEqual(
            [item["status"] for item in ledger_rows[-1]["publish_results"]],
            ["ok", "unknown"],
        )

    def test_run_daily_scopes_workdir_voice_and_history_to_channel(self) -> None:
        spec = self._make_spec("alpha")
        script = {
            "title": "Title",
            "description": "Description",
            "tags": [],
            "narration": "本題です。",
            "scenes": [{"caption": "Scene", "visual_prompt": "Image"}],
        }

        def fake_fetch_image(_prompt, out_path, **_kwargs):
            Path(out_path).write_bytes(b"image")
            return Path(out_path)

        def fake_compose(_scenes, _wav, _duration, out_path, **_kwargs):
            Path(out_path).write_bytes(b"video")
            return Path(out_path)

        def fake_thumbnail(_title, out_path, **_kwargs):
            Path(out_path).write_bytes(b"thumbnail")
            return Path(out_path)

        with (
            patch.object(config, "OUTPUT", self.output_dir),
            patch.object(ai_text, "generate", return_value=script) as generate_mock,
            patch.object(
                run_daily.voicevox,
                "synthesize",
                return_value=voicevox.TtsResult(
                    wav_path=self.root / "fake.wav",
                    duration=10.0,
                    segments=[],
                ),
            ) as synthesize_mock,
            patch.object(run_daily.assets, "fetch_image", side_effect=fake_fetch_image),
            patch.object(
                run_daily.compose, "compose", side_effect=fake_compose
            ) as compose_mock,
            patch(
                "doci.thumbnail.render", side_effect=fake_thumbnail
            ) as thumbnail_mock,
            patch("doci.thumbnail.to_16x9", side_effect=fake_thumbnail),
            patch("doci.publish.publish", return_value=[]) as publish_mock,
        ):
            result = run_daily.run(
                spec,
                "2026-07-16",
                "a",
                do_upload=True,
                video_scenes=0,
            )

            workdir = Path(result["video"]).parent
            self.assertEqual(workdir.parent, self.output_dir / "alpha")
            self.assertEqual(history.last_corner(spec), "a")
            row = json.loads(spec.history_file.read_text(encoding="utf-8"))

        self.assertEqual(result["channel"], "alpha")
        self.assertEqual(row["channel"], "alpha")
        self.assertEqual(generate_mock.call_args.args[:3], (spec, spec.corners["a"], "2026-07-16"))
        self.assertEqual(synthesize_mock.call_args.args[1], 41)
        self.assertIs(compose_mock.call_args.kwargs["style"], spec.style)
        self.assertIs(
            thumbnail_mock.call_args.kwargs["style"], spec.style.thumbnail
        )
        self.assertIs(publish_mock.call_args.kwargs["spec"], spec)
        self.assertTrue(workdir.name.startswith("2026-07-16_a_"))

    def test_run_daily_keeps_channel_spec_style_with_chart_scene(self) -> None:
        # 図表シーンを含む場合、run() 内のローカル変数 spec (chart_bg.ensure の戻り値)
        # が run() 引数の spec: ChannelSpec を上書きしないことを確認する回帰テスト。
        spec = self._make_spec("alpha")
        chart_payload = {"type": "bar", "title": "T"}
        script = {
            "title": "Title",
            "description": "Description",
            "tags": [],
            "narration": "本題です。",
            "scenes": [{"caption": "Scene", "chart": chart_payload}],
        }

        def fake_compose(_scenes, _wav, _duration, out_path, **_kwargs):
            Path(out_path).write_bytes(b"video")
            return Path(out_path)

        def fake_thumbnail(_title, out_path, **_kwargs):
            Path(out_path).write_bytes(b"thumbnail")
            return Path(out_path)

        with (
            patch.object(config, "OUTPUT", self.output_dir),
            patch.object(ai_text, "generate", return_value=script),
            patch.object(
                run_daily.voicevox,
                "synthesize",
                return_value=voicevox.TtsResult(
                    wav_path=self.root / "fake.wav",
                    duration=10.0,
                    segments=[],
                ),
            ),
            patch("doci.chart_bg.ensure", return_value=chart_payload),
            patch.object(
                run_daily.compose, "compose", side_effect=fake_compose
            ) as compose_mock,
            patch("doci.thumbnail.render", side_effect=fake_thumbnail) as thumbnail_mock,
            patch("doci.thumbnail.to_16x9", side_effect=fake_thumbnail),
            patch("doci.publish.publish", return_value=[]),
        ):
            run_daily.run(spec, "2026-07-16", "a", do_upload=True, video_scenes=0)

        self.assertIs(compose_mock.call_args.kwargs["style"], spec.style)
        self.assertIs(thumbnail_mock.call_args.kwargs["style"], spec.style.thumbnail)
        self.assertIs(compose_mock.call_args.args[0][0].chart_spec, chart_payload)


if __name__ == "__main__":
    unittest.main()
