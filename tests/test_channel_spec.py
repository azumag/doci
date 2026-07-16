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
