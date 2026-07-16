from __future__ import annotations

import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from doci import channel, config


class ChannelSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.channels_dir = self.root / "channels"

    def _write_channel(
        self,
        channel_id: str = "sample",
        *,
        toml: str | None = None,
        voice_keys: tuple[str, ...] = ("narrator",),
    ) -> Path:
        root = self.channels_dir / channel_id
        (root / "prompts").mkdir(parents=True)
        (root / "prompts" / "persona.md").write_text("persona", encoding="utf-8")
        (root / "prompts" / "corner.md").write_text("corner", encoding="utf-8")
        voices_data = {
            key: {"voicevox_speaker": index + 1}
            for index, key in enumerate(voice_keys)
        }
        (root / "voices.json").write_text(json.dumps(voices_data), encoding="utf-8")
        content = toml or f'''\
[channel]
id = "{channel_id}"
name = "Sample"

[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "narrator"
'''
        (root / "channel.toml").write_text(content, encoding="utf-8")
        return root

    def test_loads_minimal_config_with_defaults(self) -> None:
        root = self._write_channel()
        with patch.object(config, "OUTPUT", self.root / "output"):
            spec = channel.load("sample", channels_dir=self.channels_dir)
            self.assertEqual(spec.output_dir, self.root / "output" / "sample")
            self.assertEqual(
                spec.history_file,
                self.root / "output" / "sample" / "history.jsonl",
            )

        self.assertEqual(spec.id, "sample")
        self.assertEqual(spec.rotation, ["main"])
        self.assertEqual(spec.corners["main"].persona_path, root / "prompts" / "persona.md")
        self.assertEqual(spec.voices_path, root / "voices.json")
        self.assertEqual(spec.pipeline_get("max_images", 60), 60)

    def test_reference_ideology_matches_current_corners(self) -> None:
        spec = channel.load("ideology")

        self.assertEqual(spec.rotation, ["capitalism", "communism"])
        self.assertEqual(set(spec.corners), {"capitalism", "communism"})
        self.assertEqual(spec.corners["communism"].label, "共産主義ネタ")
        self.assertEqual(spec.corners["communism"].voice_key, "chinese_ai")
        self.assertEqual(
            spec.corners["communism"].persona_path.name,
            "persona_chinese.md",
        )
        self.assertEqual(spec.corners["capitalism"].label, "資本主義ネタ")
        self.assertEqual(spec.corners["capitalism"].voice_key, "american_ai")
        self.assertEqual(spec.voice_for("communism").speaker, 109)

    def test_reports_missing_required_key(self) -> None:
        self._write_channel(toml='''\
[channel]
id = "sample"
name = "Sample"
[corners.main]
label = "Main"
corner = "prompts/corner.md"
voice = "narrator"
''')
        with self.assertRaisesRegex(channel.ChannelConfigError, "corners.main.persona"):
            channel.load("sample", channels_dir=self.channels_dir)

    def test_reports_missing_referenced_file(self) -> None:
        self._write_channel(toml='''\
[channel]
id = "sample"
name = "Sample"
[corners.main]
label = "Main"
persona = "prompts/missing.md"
corner = "prompts/corner.md"
voice = "narrator"
''')
        with self.assertRaisesRegex(channel.ChannelConfigError, "persona"):
            channel.load("sample", channels_dir=self.channels_dir)

    def test_reports_missing_voice_key(self) -> None:
        self._write_channel(toml='''\
[channel]
id = "sample"
name = "Sample"
[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "missing"
''')
        with self.assertRaisesRegex(channel.ChannelConfigError, "missing voices key"):
            channel.load("sample", channels_dir=self.channels_dir)

    def test_reports_rotation_mismatch(self) -> None:
        self._write_channel(toml='''\
[channel]
id = "sample"
name = "Sample"
rotation = ["missing"]
[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "narrator"
''')
        with self.assertRaisesRegex(channel.ChannelConfigError, "missing corners"):
            channel.load("sample", channels_dir=self.channels_dir)

    def test_unknown_key_warns_but_loads(self) -> None:
        self._write_channel(toml='''\
future_option = true
[channel]
id = "sample"
name = "Sample"
[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "narrator"
''')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            spec = channel.load("sample", channels_dir=self.channels_dir)
        self.assertEqual(spec.id, "sample")
        self.assertTrue(any("future_option" in str(item.message) for item in caught))

    def test_loads_typed_style_settings(self) -> None:
        root = self._write_channel(toml='''\
[channel]
id = "sample"
name = "Sample"
[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "narrator"
[style.subtitle]
font = "prompts/persona.md"
fill = "#ffeeaa"
stroke = "#112233"
box_color = "#334455"
box_alpha = 0.3
position_ratio = 0.7
[style.thumbnail]
font_family = "serif"
title_color = "#abcdef"
[style.chart]
palette = ["#111111", "#222222"]
font = "prompts/persona.md"
[style.video]
pad_color = "0x123456"
filter = "eq=saturation=0.8"
[style.bgm]
dir = "music"
volume = 0.12
rotation = "daily"
[style.credits]
template = "{voicevox_credit} / {asset_credit}"
''')

        spec = channel.load("sample", channels_dir=self.channels_dir)

        self.assertEqual(spec.style.subtitle.font, root / "prompts" / "persona.md")
        self.assertEqual(spec.style.subtitle.fill, "#ffeeaa")
        self.assertEqual(spec.style.subtitle.box_alpha, 0.3)
        self.assertEqual(spec.style.subtitle.position_ratio, 0.7)
        self.assertEqual(spec.style.thumbnail.font_family, "serif")
        self.assertEqual(spec.style.thumbnail.title_color, "#abcdef")
        self.assertEqual(spec.style.chart.palette, ("#111111", "#222222"))
        self.assertEqual(spec.style.video.pad_color, "0x123456")
        self.assertEqual(spec.style.video.filter, "eq=saturation=0.8")
        self.assertEqual(spec.style.bgm.dir, root / "music")
        self.assertEqual(spec.style.bgm.volume, 0.12)
        self.assertEqual(spec.style.bgm.rotation, "daily")
        self.assertIn("{voicevox_credit}", spec.style.credits.template)

    def test_loads_channel_publish_accounts_with_repo_relative_paths(self) -> None:
        self._write_channel(toml='''\
[channel]
id = "sample"
name = "Sample"
[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "narrator"
[publish]
platforms = ["youtube", "tiktok", "instagram"]
[publish.youtube]
privacy = "private"
client_secret = "secrets/sample/client_secret.json"
token = "secrets/sample/youtube_token.json"
[publish.tiktok]
token = "secrets/sample/tiktok_token.json"
privacy = "SELF_ONLY"
[publish.instagram]
user_id = "123456"
access_token_env = "IG_TOKEN_SAMPLE"
''')

        spec = channel.load("sample", channels_dir=self.channels_dir)

        self.assertEqual(spec.publish.platforms, ("youtube", "tiktok", "instagram"))
        self.assertEqual(spec.publish.youtube.privacy, "private")
        self.assertEqual(
            spec.publish.youtube.client_secret,
            (config.ROOT / "secrets/sample/client_secret.json").resolve(),
        )
        self.assertEqual(
            spec.publish.youtube.token,
            (config.ROOT / "secrets/sample/youtube_token.json").resolve(),
        )
        self.assertEqual(
            spec.publish.tiktok.token,
            (config.ROOT / "secrets/sample/tiktok_token.json").resolve(),
        )
        self.assertEqual(spec.publish.instagram.user_id, "123456")
        self.assertEqual(spec.publish.instagram.access_token_env, "IG_TOKEN_SAMPLE")

    def test_publish_defaults_preserve_global_settings(self) -> None:
        self._write_channel()

        spec = channel.load("sample", channels_dir=self.channels_dir)

        self.assertEqual(spec.publish.platforms, ("youtube", "tiktok", "instagram"))
        self.assertEqual(spec.publish.youtube.privacy, config.YOUTUBE_PRIVACY)
        self.assertEqual(
            spec.publish.youtube.token,
            (config.ROOT / config.YOUTUBE_TOKEN_FILE).resolve(),
        )

    def test_rejects_secret_value_in_instagram_env_name(self) -> None:
        self._write_channel(toml='''\
[channel]
id = "sample"
name = "Sample"
[corners.main]
label = "Main"
persona = "prompts/persona.md"
corner = "prompts/corner.md"
voice = "narrator"
[publish.instagram]
access_token_env = "EAAB token value"
''')

        with self.assertRaisesRegex(channel.ChannelConfigError, "environment variable name"):
            channel.load("sample", channels_dir=self.channels_dir)

    def test_discover_and_default_channel(self) -> None:
        self.assertEqual(channel.discover(channels_dir=self.channels_dir), [])
        with self.assertRaisesRegex(channel.ChannelConfigError, "no channels"):
            channel.default_channel(channels_dir=self.channels_dir)
        with patch.dict(os.environ, {"DOCI_CHANNEL": "configured"}):
            self.assertEqual(
                channel.default_channel(channels_dir=self.channels_dir), "configured"
            )

        self._write_channel("one")
        self.assertEqual(channel.discover(channels_dir=self.channels_dir), ["one"])
        self.assertEqual(channel.default_channel(channels_dir=self.channels_dir), "one")

        self._write_channel("two")
        with self.assertRaisesRegex(channel.ChannelConfigError, "multiple channels"):
            channel.default_channel(channels_dir=self.channels_dir)
        with patch.dict(os.environ, {"DOCI_CHANNEL": "two"}):
            self.assertEqual(channel.default_channel(channels_dir=self.channels_dir), "two")


if __name__ == "__main__":
    unittest.main()
