"""channel.toml の読込・ステージング検証・保存のテスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import channel, config
from doci.admin import channel_store
from tests.admin_test_helpers import write_channel, write_minimal_repo


class ChannelStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_minimal_repo(self.root)
        write_channel(
            self.root,
            "testch",
            corners={
                "a": {"label": "コーナーA", "voice": "narrator"},
                "b": {"label": "コーナーB", "voice": "narrator"},
            },
        )
        self.patcher = mock.patch.object(config, "ROOT", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # config.OUTPUT は config.ROOT を patch しても連動しない(モジュール読込時に
        # 一度だけ計算される定数のため)。backup先が実リポジトリのoutput/へ漏れるのを
        # 防ぐため必ず個別にpatchする(実際に一度漏らして気づいた: tests/test_channel_spec.py
        # と同じ理由)。
        self.output_patcher = mock.patch.object(config, "OUTPUT", self.root / "output")
        self.output_patcher.start()
        self.addCleanup(self.output_patcher.stop)

    def test_discover_lists_channel(self) -> None:
        self.assertEqual(channel_store.discover(), ["testch"])

    def test_read_toml_returns_raw_text(self) -> None:
        text = channel_store.read_toml("testch")
        self.assertIn('id = "testch"', text)

    def test_unknown_channel_raises(self) -> None:
        with self.assertRaises(channel_store.ChannelNotFoundError):
            channel_store.read_toml("nope")

    def test_validate_good_toml_returns_summary(self) -> None:
        text = channel_store.read_toml("testch")
        v = channel_store.validate_candidate("testch", text)
        self.assertTrue(v.ok, v.error)
        self.assertEqual(v.warnings, [])
        self.assertEqual(v.summary["rotation"], ["a", "b"])
        self.assertEqual(sorted(v.summary["corners"].keys()), ["a", "b"])

    def test_validate_bad_voice_reference_fails_without_touching_real_file(self) -> None:
        text = channel_store.read_toml("testch")
        bad = text.replace('voice = "narrator"', 'voice = "does_not_exist"', 1)
        v = channel_store.validate_candidate("testch", bad)
        self.assertFalse(v.ok)
        self.assertIn("does_not_exist", v.error)
        # 実ファイルは一切変更されていない
        self.assertEqual(channel_store.read_toml("testch"), text)

    def test_validate_unknown_key_warns_but_does_not_error(self) -> None:
        text = channel_store.read_toml("testch")
        bad = text + "\nunknown_top_level_key = 1\n"
        v = channel_store.validate_candidate("testch", bad)
        self.assertTrue(v.ok, v.error)
        self.assertTrue(v.warnings)

    def test_save_writes_and_returns_summary(self) -> None:
        text = channel_store.read_toml("testch")
        changed = text.replace('name = "テストチャンネル"', 'name = "改名後"', 1)
        result = channel_store.save("testch", changed, confirm_warnings=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(channel_store.read_toml("testch"), changed)
        self.assertEqual(result.summary["name"], "改名後")

    def test_save_summary_root_points_at_real_directory_not_a_deleted_tempdir(self) -> None:
        # save()の最終ステップは以前 validate_candidate() を呼び直しており、それは
        # 常に新しい一時ディレクトリへステージングするため、戻り値のsummary["root"]は
        # 呼び出し完了時には既に削除された一時パスを指していた(実際に確認した)。
        text = channel_store.read_toml("testch")
        result = channel_store.save("testch", text, confirm_warnings=True)
        self.assertTrue(result.ok, result.error)
        root = Path(result.summary["root"])
        self.assertTrue(root.is_dir(), root)
        self.assertEqual(root, (self.root / "channels" / "testch").resolve())

    def test_read_summary_root_points_at_real_directory(self) -> None:
        v = channel_store.validate_real("testch")
        self.assertTrue(v.ok, v.error)
        root = Path(v.summary["root"])
        self.assertTrue(root.is_dir(), root)
        self.assertEqual(root, (self.root / "channels" / "testch").resolve())

    def test_save_rejects_invalid_and_does_not_write(self) -> None:
        text = channel_store.read_toml("testch")
        bad = text.replace('voice = "narrator"', 'voice = "does_not_exist"', 1)
        result = channel_store.save("testch", bad, confirm_warnings=True)
        self.assertFalse(result.ok)
        self.assertEqual(channel_store.read_toml("testch"), text)

    def test_save_with_warnings_blocked_without_confirm(self) -> None:
        text = channel_store.read_toml("testch")
        with_warning = text + "\nunknown_top_level_key = 1\n"
        result = channel_store.save("testch", with_warning, confirm_warnings=False)
        self.assertFalse(result.ok)
        self.assertTrue(result.needs_confirmation)
        self.assertEqual(result.code, 409)
        self.assertEqual(channel_store.read_toml("testch"), text)

        result2 = channel_store.save("testch", with_warning, confirm_warnings=True)
        self.assertTrue(result2.ok, result2.error)

    def test_save_creates_backup(self) -> None:
        from doci.admin import safeio

        text = channel_store.read_toml("testch")
        changed = text.replace('name = "テストチャンネル"', 'name = "改名後"', 1)
        channel_store.save("testch", changed, confirm_warnings=True)
        backups = safeio.list_backups("channel", "testch")
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].path.read_text(encoding="utf-8"), text)

    def test_stale_fingerprint_returns_409_and_does_not_write(self) -> None:
        text = channel_store.read_toml("testch")
        changed = text.replace('name = "テストチャンネル"', 'name = "改名後"', 1)
        result = channel_store.save(
            "testch", changed, confirm_warnings=True, base_fingerprint="deadbeef"
        )
        self.assertEqual(result.code, 409)
        self.assertEqual(channel_store.read_toml("testch"), text)

    def test_matching_fingerprint_succeeds(self) -> None:
        text = channel_store.read_toml("testch")
        fp = channel_store.content_fingerprint(text)
        changed = text.replace('name = "テストチャンネル"', 'name = "改名後"', 1)
        result = channel_store.save("testch", changed, confirm_warnings=True, base_fingerprint=fp)
        self.assertTrue(result.ok, result.error)

    def test_staged_validation_uses_symlinked_siblings(self) -> None:
        # プロンプトファイルは実チャンネルディレクトリのシンボリックリンク経由で
        # 検証されるため、editorがtoml以外を変更していなくても正しく解決できる。
        text = channel_store.read_toml("testch")
        v = channel_store.validate_candidate("testch", text)
        self.assertTrue(v.ok, v.error)
        persona_path = v.summary["corners"]["a"]["persona_path"]
        self.assertTrue(Path(persona_path).is_file())

    def test_real_repo_channels_still_validate(self) -> None:
        # このテストだけは意図的に実リポジトリを対象にする(読み取り専用)。
        self.patcher.stop()
        try:
            for cid in channel.discover():
                v = channel_store.validate_candidate(cid, channel_store.read_toml(cid))
                self.assertTrue(v.ok, f"{cid}: {v.error}")
        finally:
            self.patcher.start()


if __name__ == "__main__":
    unittest.main()
