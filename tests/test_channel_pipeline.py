from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doci import ai_text, channel, config, corners, history, run_daily, voicevox


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

    def test_channel_output_rules_override_common_rules(self) -> None:
        spec = self._make_spec("alpha", output_rules="CHANNEL-RULES-ONLY")

        prompt = corners.build_prompt(spec, spec.corners["a"], "2026-07-16", [])

        self.assertIn("CHANNEL-RULES-ONLY", prompt)
        self.assertNotIn(
            (config.PROMPTS / "output_rules.md").read_text(encoding="utf-8"),
            prompt,
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
            self.assertEqual(history.recent_topics(beta), ["Beta title"])
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
            patch.object(ai_text, "_dispatch", return_value=raw),
        ):
            script = ai_text.generate(spec, spec.corners["a"], "2026-07-16", [])

        self.assertEqual(script["_channel"], "alpha")
        self.assertEqual(script["_corner"], "a")
        self.assertEqual(script["_speaker"], 41)

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
        ):
            result = run_daily.run(
                spec,
                "2026-07-16",
                "a",
                do_upload=False,
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
        self.assertTrue(workdir.name.startswith("2026-07-16_a_"))


if __name__ == "__main__":
    unittest.main()
