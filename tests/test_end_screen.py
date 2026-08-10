"""Issue #165: YouTube終了画面1枠の手動運用記録。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from doci import end_screen


class EndScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.spec = SimpleNamespace(
            id="youtube-growth",
            output_dir=root / "output" / "youtube-growth",
            history_file=root / "output" / "youtube-growth" / "history.jsonl",
        )
        self.spec.history_file.parent.mkdir(parents=True)
        self.video_id = "AbCdEf12345"
        self.link_video_id = "ZzYyXx98765"
        self.now = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
        self._write_history()

    def _write_history(self, **overrides) -> None:
        row = {
            "ts": "2026-08-10T00:00:00+00:00",
            "channel": "youtube-growth",
            "corner": "video",
            "title": "現在のタイトル",
            "video_id": self.video_id,
            "status": "published",
            "tier": "longform",
            "youtube_privacy": "unlisted",
            "workdir": "/tmp/workdir",
        }
        row.update(overrides)
        self.spec.history_file.write_text(
            json.dumps(row, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _plan(self, experiment_id: str = "esc-0000000000000001") -> dict:
        return end_screen.plan_experiment(
            self.spec,
            video_id=self.video_id,
            link_video_id=self.link_video_id,
            content_direct_confirmed=True,
            now=self.now,
            experiment_id=experiment_id,
        )

    def _manifest_file(self, experiment_id: str) -> Path:
        return (
            self.spec.output_dir
            / "end_screen_tests"
            / experiment_id
            / "manifest.json"
        )

    def test_plan_fixes_single_video_element_without_youtube_write(self) -> None:
        manifest = self._plan()

        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(manifest["decision_metric"], "youtube_studio.end_screen_click_rate")
        setup = manifest["end_screen_setup"]
        self.assertEqual(setup["element"], "video")
        self.assertEqual(setup["link_video_id"], self.link_video_id)
        self.assertTrue(setup["single_slot_only"])
        self.assertTrue(setup["subscription_button_prohibited"])
        self.assertTrue(setup["playlist_element_prohibited"])
        root = self.spec.output_dir / "end_screen_tests" / manifest["experiment_id"]
        self.assertTrue((root / "manifest.json").is_file())
        plan = (root / "plan.md").read_text(encoding="utf-8")
        self.assertIn("登録ボタン・再生リスト", plan)
        self.assertIn(end_screen.OFFICIAL_HELP_URL, plan)

    def test_plan_requires_content_direct_confirmation(self) -> None:
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "directly continues",
        ):
            end_screen.plan_experiment(
                self.spec,
                video_id=self.video_id,
                link_video_id=self.link_video_id,
            )

    def test_plan_rejects_self_link(self) -> None:
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "must differ",
        ):
            end_screen.plan_experiment(
                self.spec,
                video_id=self.video_id,
                link_video_id=self.video_id,
                content_direct_confirmed=True,
            )

    def test_plan_rejects_short_private_or_unpublished_videos(self) -> None:
        for field, value, message in (
            ("tier", "short", "not available for Shorts"),
            ("youtube_privacy", "private", "public or unlisted"),
            ("status", "publishing", "not recorded as published"),
        ):
            with self.subTest(field=field, value=value):
                self._write_history(**{field: value})
                with self.assertRaisesRegex(end_screen.EndScreenError, message):
                    end_screen.plan_experiment(
                        self.spec,
                        video_id=self.video_id,
                        link_video_id=self.link_video_id,
                        content_direct_confirmed=True,
                    )
                self._write_history()

    def test_plan_rejects_duplicate_active_test_for_video(self) -> None:
        self._plan("esc-0000000000000001")
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "active end screen test already exists",
        ):
            self._plan("esc-0000000000000002")

    def test_start_requires_studio_setup_confirmation(self) -> None:
        self._plan()
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "confirm that the single end screen",
        ):
            end_screen.start_experiment(self.spec, "esc-0000000000000001")

    def test_start_moves_planned_to_running(self) -> None:
        self._plan()
        manifest = end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
            now=self.now,
        )
        self.assertEqual(manifest["status"], "running")
        self.assertIn("started_at", manifest)

    def test_complete_requires_running_and_confirmation(self) -> None:
        self._plan()
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "only a running",
        ):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                outcome="clicked",
                setup_unchanged_confirmed=True,
            )
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "was not manually changed",
        ):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                outcome="clicked",
            )

    def test_complete_records_click_rate_and_memo(self) -> None:
        self._plan()
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        manifest = end_screen.complete_experiment(
            self.spec,
            "esc-0000000000000001",
            outcome="clicked",
            click_rate=3.5,
            notes="次の一本の冒頭が視聴された",
            setup_unchanged_confirmed=True,
            now=self.now,
        )
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["result"]["outcome"], "clicked")
        self.assertEqual(manifest["result"]["click_rate"], 3.5)
        memo = (
            self.spec.output_dir
            / "end_screen_tests"
            / "esc-0000000000000001"
            / "next_idea_memo.md"
        ).read_text(encoding="utf-8")
        self.assertIn("終了画面1枠", memo)
        self.assertIn("3.5", memo)

    def test_complete_rejects_out_of_range_click_rate(self) -> None:
        self._plan()
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "between 0 and 100",
        ):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                outcome="clicked",
                click_rate=101.0,
                setup_unchanged_confirmed=True,
            )

    def test_complete_insufficient_views_forbids_click_rate(self) -> None:
        self._plan()
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        with self.assertRaisesRegex(
            end_screen.EndScreenError,
            "must not be recorded",
        ):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                outcome="insufficient_views",
                click_rate=3.0,
                setup_unchanged_confirmed=True,
            )

    def test_show_returns_saved_manifest(self) -> None:
        self._plan()
        manifest = end_screen.show_experiment(self.spec, "esc-0000000000000001")
        self.assertEqual(manifest["experiment_id"], "esc-0000000000000001")
        self.assertEqual(manifest["video_id"], self.video_id)


if __name__ == "__main__":
    unittest.main()
