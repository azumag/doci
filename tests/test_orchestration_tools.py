from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from doci import history


ROOT = Path(__file__).resolve().parent.parent


def _load_migration_module():
    path = ROOT / "tools/migrate_channels.py"
    spec = importlib.util.spec_from_file_location("migrate_channels", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_dry_run_then_apply_preserves_history_and_credentials(self) -> None:
        history_path = self.root / "output/history.jsonl"
        history_path.parent.mkdir(parents=True)
        history_path.write_text(
            json.dumps(
                {
                    "corner": "communism",
                    "title": "Last",
                    "description": "Last angle",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        token = self.root / "youtube_token.json"
        token.write_text("secret-token-json", encoding="utf-8")
        migration = _load_migration_module()

        dry_results = migration.migrate(self.root, apply=False)

        self.assertTrue(history_path.exists())
        self.assertTrue(any(item["status"] == "would_move" for item in dry_results))

        results = migration.migrate(self.root, apply=True)

        migrated_history = self.root / "output/ideology/history.jsonl"
        migrated_token = self.root / "secrets/ideology/youtube_token.json"
        self.assertFalse(history_path.exists())
        self.assertEqual(
            json.loads(migrated_history.read_text(encoding="utf-8"))["corner"],
            "communism",
        )
        self.assertEqual(migrated_token.read_text(encoding="utf-8"), "secret-token-json")
        self.assertTrue(any(item["status"] == "moved" for item in results))
        migrated_spec = SimpleNamespace(history_file=migrated_history)
        self.assertEqual(history.last_corner(migrated_spec), "communism")
        self.assertEqual(history.recent_topics(migrated_spec), ["Last（Last angle）"])

    def test_existing_destination_is_never_overwritten(self) -> None:
        source = self.root / "youtube_token.json"
        destination = self.root / "secrets/ideology/youtube_token.json"
        destination.parent.mkdir(parents=True)
        source.write_text("old", encoding="utf-8")
        destination.write_text("keep", encoding="utf-8")
        migration = _load_migration_module()

        results = migration.migrate(self.root, apply=True)

        self.assertEqual(destination.read_text(encoding="utf-8"), "keep")
        self.assertEqual(source.read_text(encoding="utf-8"), "old")
        self.assertTrue(
            any(item["status"] == "destination_exists" for item in results)
        )


class LaunchdTemplateTest(unittest.TestCase):
    def _render(self, *args: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            env = {
                **os.environ,
                "HOME": td,
                "DOCI_LAUNCHD_DRY_RUN": "1",
            }
            result = subprocess.run(
                ["/bin/zsh", str(ROOT / "tools/install_launchd.sh"), *args],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_default_job_runs_all_channels(self) -> None:
        output = self._render("1234")

        self.assertIn("com.azumag.doci.generate.plist", output)
        self.assertIn("<string>--all-channels</string>", output)
        self.assertIn("<integer>1234</integer>", output)

    def test_channel_job_uses_scoped_label_and_argument(self) -> None:
        output = self._render("3600", "ideology")

        self.assertIn("com.azumag.doci.generate.ideology.plist", output)
        self.assertIn("<string>--channel</string>", output)
        self.assertIn("<string>ideology</string>", output)


class CronGenerateTest(unittest.TestCase):
    def test_review_reconciliation_runs_before_voicevox_start(self) -> None:
        script = (ROOT / "tools/cron_generate.sh").read_text(encoding="utf-8")

        reconcile_at = script.index("--reconcile-youtube-reviews")
        voicevox_at = script.index("/usr/local/bin/orb start")
        generation_at = script.index('-m doci.run_daily "$@"')
        long_generation_lock_at = script.index('LOCK="$PROJ/output/.cron_generate_')

        self.assertLess(reconcile_at, long_generation_lock_at)
        self.assertLess(reconcile_at, voicevox_at)
        self.assertLess(reconcile_at, generation_at)
        self.assertIn("DOCI_REVIEW_RECONCILED=1", script)
        self.assertIn('if [ "$review_rc" != "0" ]', script)
        self.assertIn("else\n  export DOCI_REVIEW_RECONCILED=1", script)


if __name__ == "__main__":
    unittest.main()
