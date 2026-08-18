"""doci.admin.safeio のテスト: アトミック書き込み・バックアップ・ロック・稼働中判定。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config
from doci.admin import safeio


class AtomicWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_write_creates_file(self) -> None:
        path = self.root / "sub" / "x.txt"
        safeio.atomic_write_text(path, "hello")
        self.assertEqual(path.read_text(encoding="utf-8"), "hello")

    def test_no_leftover_tmp_on_success(self) -> None:
        path = self.root / "x.txt"
        safeio.atomic_write_text(path, "hello")
        leftovers = list(self.root.glob(".*.tmp.*"))
        self.assertEqual(leftovers, [])

    def test_preserves_existing_mode(self) -> None:
        path = self.root / "x.txt"
        path.write_text("old")
        os.chmod(path, 0o600)
        safeio.atomic_write_text(path, "new")
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")

    def test_explicit_mode_overrides(self) -> None:
        path = self.root / "x.txt"
        safeio.atomic_write_text(path, "new", mode=0o600)
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")

    def test_no_leftover_tmp_on_write_failure(self) -> None:
        path = self.root / "x.txt"
        path.write_text("original")
        with mock.patch("doci.admin.safeio.os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                safeio.atomic_write_text(path, "new content")
        # 書き込み失敗後も元の内容は残り、tmpの残骸も無い
        self.assertEqual(path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.root.glob(".*.tmp.*")), [])


class BackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.patcher = mock.patch.object(config, "OUTPUT", self.output)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_backup_missing_file_is_noop(self) -> None:
        result = safeio.backup(self.root / "nope.txt", surface="env", name="env")
        self.assertIsNone(result)

    def test_backup_copies_content(self) -> None:
        target = self.root / ".env"
        target.write_text("TEXT_BACKEND=codex\n", encoding="utf-8")
        dest = safeio.backup(target, surface="env", name="env")
        self.assertIsNotNone(dest)
        self.assertEqual(dest.read_text(encoding="utf-8"), "TEXT_BACKEND=codex\n")
        self.assertTrue(str(dest).startswith(str(self.output)))

    def test_backup_rotation_keeps_n(self) -> None:
        target = self.root / ".env"
        for i in range(5):
            target.write_text(f"N={i}\n", encoding="utf-8")
            safeio.backup(target, surface="env", name="env", keep=3)
        entries = safeio.list_backups("env", "env")
        self.assertEqual(len(entries), 3)
        # 最新が先頭
        self.assertEqual(entries[0].path.read_text(encoding="utf-8"), "N=4\n")

    def test_list_backups_empty_when_none(self) -> None:
        self.assertEqual(safeio.list_backups("env", "nope"), [])

    def test_backup_is_always_0600_regardless_of_source_mode(self) -> None:
        target = self.root / ".env"
        target.write_text("X=1\n", encoding="utf-8")
        os.chmod(target, 0o644)  # 実リポジトリの.envが実際に0644だったことを再現
        dest = safeio.backup(target, surface="env", name="env")
        self.assertEqual(oct(dest.stat().st_mode)[-3:], "600")

    def test_unknown_surface_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            safeio.backup(self.root / ".env", surface="not-a-real-surface", name="x")
        with self.assertRaises(ValueError):
            safeio.list_backups("not-a-real-surface", "x")

    def test_absolute_path_disguised_as_surface_is_rejected(self) -> None:
        # target="<絶対パス>:<name>" のような入力でも、pathlibの
        # `Path("base") / "/abs/path"` 仕様により output/.admin_backups/ の
        # 外側(任意のディレクトリ)を列挙できてしまわないことを確認する。
        with self.assertRaises(ValueError):
            safeio.list_backups(str(Path.home()), ".ssh")
        with self.assertRaises(ValueError):
            safeio.list_backups("/etc", "passwd")


class SurfaceLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "output"
        self.patcher = mock.patch.object(config, "OUTPUT", self.output)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_lock_is_reentrant_safe_sequential(self) -> None:
        calls = []
        with safeio.surface_lock("env"):
            calls.append(1)
        with safeio.surface_lock("env"):
            calls.append(2)
        self.assertEqual(calls, [1, 2])

    def test_lock_sanitizes_name_for_filesystem(self) -> None:
        with safeio.surface_lock("channel:some/weird:name"):
            pass
        # 例外を投げず、ロックファイルが作られていること
        locks = list((self.output / ".admin_locks").glob("*.lock"))
        self.assertEqual(len(locks), 1)


class PipelineRunningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "output"
        self.output.mkdir()
        self.patcher = mock.patch.object(config, "OUTPUT", self.output)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_no_lock_files(self) -> None:
        self.assertEqual(safeio.pipeline_running(), [])

    def test_dead_pid_is_not_running(self) -> None:
        (self.output / ".cron_generate_default.lock").write_text("99999", encoding="utf-8")
        with mock.patch("doci.admin.safeio.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(safeio.pipeline_running(), [])

    def test_alive_pid_is_running(self) -> None:
        (self.output / ".cron_generate_default.lock").write_text(str(os.getpid()), encoding="utf-8")
        running = safeio.pipeline_running()
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].run_name, "default")
        self.assertEqual(running[0].pid, os.getpid())

    def test_malformed_lock_file_ignored(self) -> None:
        (self.output / ".cron_generate_default.lock").write_text("not-a-pid", encoding="utf-8")
        self.assertEqual(safeio.pipeline_running(), [])

    def test_unrelated_file_ignored(self) -> None:
        (self.output / "not_a_lock.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(safeio.pipeline_running(), [])

    def test_missing_output_dir(self) -> None:
        other = Path(self.tmp.name) / "does-not-exist"
        with mock.patch.object(config, "OUTPUT", other):
            self.assertEqual(safeio.pipeline_running(), [])


if __name__ == "__main__":
    unittest.main()
