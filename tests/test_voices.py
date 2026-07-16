from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doci import config


class VoiceConfigTest(unittest.TestCase):
    def _load_with(self, voices_json: dict, env: dict[str, str] | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg_dir = Path(tmp.name)
        (cfg_dir / "voices.json").write_text(
            json.dumps(voices_json, ensure_ascii=False), encoding="utf-8"
        )
        env_patch = patch.dict(os.environ, env or {}, clear=True)
        cfg_patch = patch.object(config, "CONFIG_DIR", cfg_dir)
        env_patch.start()
        cfg_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(cfg_patch.stop)
        from doci import voices

        return importlib.reload(voices)

    def tearDown(self) -> None:
        from doci import voices

        importlib.reload(voices)

    def test_reads_speaker_and_params_from_voices_json(self) -> None:
        voices = self._load_with(
            {
                "chinese_ai": {
                    "voicevox_speaker": 109,
                    "speed": 1.15,
                    "pitch": -0.02,
                    "intonation": 0.8,
                    "volume": 0.9,
                }
            }
        )

        v = voices.get("chinese_ai")
        self.assertEqual(v.speaker, 109)
        self.assertEqual(v.speed, 1.15)
        self.assertEqual(v.pitch, -0.02)
        self.assertEqual(v.intonation, 0.8)
        self.assertEqual(v.volume, 0.9)

    def test_explicit_env_speaker_overrides_voices_json(self) -> None:
        voices = self._load_with(
            {
                "chinese_ai": {
                    "voicevox_speaker": 109,
                    "speed": 1.15,
                    "pitch": -0.02,
                    "intonation": 0.8,
                    "volume": 1.0,
                }
            },
            {"VOICE_CHINESE_AI": "321"},
        )

        v = voices.get("chinese_ai")
        self.assertEqual(v.speaker, 321)
        self.assertEqual(v.speed, 1.15)
        self.assertEqual(v.pitch, -0.02)
        self.assertEqual(v.intonation, 0.8)

    def test_path_loader_supports_channel_specific_keys_without_env_override(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "voices.json"
        path.write_text(
            json.dumps(
                {
                    "narrator": {"voicevox_speaker": 42, "speed": 1.2},
                    "chinese_ai": {"voicevox_speaker": 109},
                }
            ),
            encoding="utf-8",
        )
        from doci import voices

        with patch.dict(os.environ, {"VOICE_CHINESE_AI": "321"}):
            loaded = voices.load(path)

        self.assertEqual(loaded["narrator"].speaker, 42)
        self.assertEqual(loaded["narrator"].speed, 1.2)
        self.assertEqual(loaded["chinese_ai"].speaker, 109)


if __name__ == "__main__":
    unittest.main()
