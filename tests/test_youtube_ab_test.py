"""Issue #151: YouTube Studio A/Bテストの手動運用記録。"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from doci import youtube_ab_test


class YouTubeABTestTest(unittest.TestCase):
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
        self._write_history()
        self.now = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)

    def _write_history(self, **overrides) -> None:
        row = {
            "ts": "2026-08-08T00:00:00+00:00",
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

    def _plan_title(self, experiment_id: str = "yab-0000000000000001") -> dict:
        return youtube_ab_test.plan_experiment(
            self.spec,
            video_id=self.video_id,
            mode="title",
            titles=["案A", "案B", "案C"],
            studio_eligible_confirmed=True,
            now=self.now,
            experiment_id=experiment_id,
        )

    def _image(self, name: str, color: str, size=(1280, 720)) -> Path:
        path = Path(self.tmp.name) / name
        Image.new("RGB", size, color).save(path)
        return path

    def _manifest_file(self, experiment_id: str) -> Path:
        return (
            self.spec.output_dir
            / "youtube_ab_tests"
            / experiment_id
            / "manifest.json"
        )

    def _rewrite_plan(self, experiment_id: str, mutate) -> dict:
        path = self._manifest_file(experiment_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        data["plan_sha256"] = youtube_ab_test._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return data

    def test_plan_freezes_two_or_three_title_variants_without_youtube_write(self) -> None:
        manifest = self._plan_title()

        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(manifest["decision_metric"], "youtube_studio.watch_time_share")
        self.assertTrue(manifest["manual_changes_prohibited_while_running"])
        self.assertEqual(
            [item["title"] for item in manifest["variants"]],
            ["案A", "案B", "案C"],
        )
        root = self.spec.output_dir / "youtube_ab_tests" / manifest["experiment_id"]
        self.assertTrue((root / "manifest.json").is_file())
        plan = (root / "plan.md").read_text(encoding="utf-8")
        self.assertIn("テスト中はタイトル・サムネイルを手動変更しません", plan)
        self.assertIn(youtube_ab_test.OFFICIAL_HELP_URL, plan)

    def test_plan_requires_explicit_studio_eligibility_confirmation(self) -> None:
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "confirm desktop Studio access",
        ):
            youtube_ab_test.plan_experiment(
                self.spec,
                video_id=self.video_id,
                mode="title",
                titles=["案A", "案B"],
            )

    def test_plan_rejects_short_private_or_unpublished_videos(self) -> None:
        for field, value, message in (
            ("tier", "short", "not available for Shorts"),
            ("youtube_privacy", "private", "public or unlisted"),
            ("status", "publishing", "not recorded as published"),
        ):
            with self.subTest(field=field):
                self._write_history(**{field: value})
                with self.assertRaisesRegex(
                    youtube_ab_test.YouTubeABTestError,
                    message,
                ):
                    youtube_ab_test.plan_experiment(
                        self.spec,
                        video_id=self.video_id,
                        mode="title",
                        titles=["案A", "案B"],
                        studio_eligible_confirmed=True,
                    )

    def test_plan_rejects_invalid_variant_count_and_mode_payload(self) -> None:
        for titles in (["案A"], ["A", "B", "C", "D"]):
            with self.subTest(count=len(titles)):
                with self.assertRaisesRegex(
                    youtube_ab_test.YouTubeABTestError,
                    "require 2 or 3 variants",
                ):
                    youtube_ab_test.plan_experiment(
                        self.spec,
                        video_id=self.video_id,
                        mode="title",
                        titles=titles,
                        studio_eligible_confirmed=True,
                    )
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "must not include thumbnail",
        ):
            youtube_ab_test.plan_experiment(
                self.spec,
                video_id=self.video_id,
                mode="title",
                titles=["A", "B"],
                thumbnail_paths=[self._image("a.png", "red")],
                studio_eligible_confirmed=True,
            )

    def test_both_mode_copies_and_hashes_thumbnail_variants(self) -> None:
        first = self._image("first.png", "red")
        second = self._image("second.jpg", "blue")
        manifest = youtube_ab_test.plan_experiment(
            self.spec,
            video_id=self.video_id,
            mode="both",
            titles=["案A", "案B"],
            thumbnail_paths=[first, second],
            studio_eligible_confirmed=True,
            now=self.now,
            experiment_id="yab-0000000000000002",
        )

        directory = self.spec.output_dir / "youtube_ab_tests" / manifest["experiment_id"]
        self.assertEqual(len(manifest["variants"]), 2)
        for variant in manifest["variants"]:
            thumbnail = variant["thumbnail"]
            copied = directory / thumbnail["file"]
            self.assertTrue(copied.is_file())
            self.assertEqual(thumbnail["width"], 1280)
            self.assertEqual(thumbnail["height"], 720)
            self.assertEqual(
                thumbnail["sha256"],
                hashlib.sha256(copied.read_bytes()).hexdigest(),
            )

    def test_thumbnail_quality_warnings_are_recorded_without_guessing_result(self) -> None:
        first = self._image("small.png", "red", size=(640, 360))
        second = self._image("square.png", "blue", size=(800, 800))
        manifest = youtube_ab_test.plan_experiment(
            self.spec,
            video_id=self.video_id,
            mode="thumbnail",
            thumbnail_paths=[first, second],
            studio_eligible_confirmed=True,
            experiment_id="yab-0000000000000003",
        )

        self.assertGreaterEqual(len(manifest["warnings"]), 3)
        self.assertNotIn("result", manifest)

    def test_duplicate_title_or_thumbnail_variants_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "title variants must be distinct",
        ):
            youtube_ab_test.plan_experiment(
                self.spec,
                video_id=self.video_id,
                mode="title",
                titles=["同じ案", " 同じ案 "],
                studio_eligible_confirmed=True,
            )
        first = self._image("same-a.png", "red")
        second = Path(self.tmp.name) / "same-b.png"
        second.write_bytes(first.read_bytes())
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "distinct file contents",
        ):
            youtube_ab_test.plan_experiment(
                self.spec,
                video_id=self.video_id,
                mode="thumbnail",
                thumbnail_paths=[first, second],
                studio_eligible_confirmed=True,
            )

    def test_second_active_experiment_for_same_video_is_blocked(self) -> None:
        first = self._plan_title()
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            first["experiment_id"],
        ):
            youtube_ab_test.plan_experiment(
                self.spec,
                video_id=self.video_id,
                mode="title",
                titles=["新案A", "新案B"],
                studio_eligible_confirmed=True,
                experiment_id="yab-0000000000000004",
            )

    def test_start_requires_confirmation_and_only_accepts_planned_state(self) -> None:
        manifest = self._plan_title()
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "confirm that the frozen variants",
        ):
            youtube_ab_test.start_experiment(self.spec, manifest["experiment_id"])

        running = youtube_ab_test.start_experiment(
            self.spec,
            manifest["experiment_id"],
            studio_started_confirmed=True,
            now=self.now,
        )
        self.assertEqual(running["status"], "running")
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "only a planned experiment",
        ):
            youtube_ab_test.start_experiment(
                self.spec,
                manifest["experiment_id"],
                studio_started_confirmed=True,
            )

    def test_winner_completion_records_result_and_next_idea_memo(self) -> None:
        manifest = self._plan_title()
        youtube_ab_test.start_experiment(
            self.spec,
            manifest["experiment_id"],
            studio_started_confirmed=True,
            now=self.now,
        )
        completed = youtube_ab_test.complete_experiment(
            self.spec,
            manifest["experiment_id"],
            outcome="winner",
            winner_variant="B",
            notes="具体的な表現が総再生時間につながった。",
            no_manual_change_confirmed=True,
            now=self.now,
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["winner_variant"], "B")
        self.assertTrue(completed["result"]["no_manual_change_confirmed"])
        memo = (
            self.spec.output_dir
            / "youtube_ab_tests"
            / manifest["experiment_id"]
            / "next_idea_memo.md"
        ).read_text(encoding="utf-8")
        self.assertIn("winner_variant: `B`", memo)
        self.assertIn("別動画へそのまま一般化せず", memo)

    def test_non_winner_outcomes_cannot_claim_a_winner(self) -> None:
        manifest = self._plan_title()
        youtube_ab_test.start_experiment(
            self.spec,
            manifest["experiment_id"],
            studio_started_confirmed=True,
        )
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "must not record a winner",
        ):
            youtube_ab_test.complete_experiment(
                self.spec,
                manifest["experiment_id"],
                outcome="inconclusive",
                winner_variant="A",
                no_manual_change_confirmed=True,
            )

    def test_normal_completion_requires_no_manual_change_confirmation(self) -> None:
        manifest = self._plan_title()
        youtube_ab_test.start_experiment(
            self.spec,
            manifest["experiment_id"],
            studio_started_confirmed=True,
        )

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "confirm that no title or thumbnail",
        ):
            youtube_ab_test.complete_experiment(
                self.spec,
                manifest["experiment_id"],
                outcome="performed_same",
            )

    def test_manual_change_invalidates_running_test(self) -> None:
        manifest = self._plan_title()
        youtube_ab_test.start_experiment(
            self.spec,
            manifest["experiment_id"],
            studio_started_confirmed=True,
        )
        invalidated = youtube_ab_test.complete_experiment(
            self.spec,
            manifest["experiment_id"],
            outcome="stopped_manual_change",
            notes="テスト中にタイトルを変更した。",
        )

        self.assertEqual(invalidated["status"], "invalidated")
        self.assertIsNone(invalidated["result"]["winner_variant"])
        self.assertFalse(invalidated["result"]["no_manual_change_confirmed"])

    def test_manual_change_outcome_rejects_contradictory_confirmation_or_winner(
        self,
    ) -> None:
        manifest = self._plan_title()
        youtube_ab_test.start_experiment(
            self.spec,
            manifest["experiment_id"],
            studio_started_confirmed=True,
        )

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "conflicts with no-manual-change confirmation",
        ):
            youtube_ab_test.complete_experiment(
                self.spec,
                manifest["experiment_id"],
                outcome="stopped_manual_change",
                no_manual_change_confirmed=True,
            )
        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "must not record a winner",
        ):
            youtube_ab_test.complete_experiment(
                self.spec,
                manifest["experiment_id"],
                outcome="stopped_manual_change",
                winner_variant="A",
            )

    def test_plan_checksum_detects_manifest_edit(self) -> None:
        manifest = self._plan_title()
        path = self._manifest_file(manifest["experiment_id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["variants"][0]["title"] = "改ざんされた案"
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "plan checksum mismatch",
        ):
            youtube_ab_test.start_experiment(
                self.spec,
                manifest["experiment_id"],
                studio_started_confirmed=True,
            )

    def test_start_rejects_duplicate_title_even_with_recomputed_checksum(self) -> None:
        manifest = self._plan_title()
        self._rewrite_plan(
            manifest["experiment_id"],
            lambda data: data["variants"][1].__setitem__(
                "title", data["variants"][0]["title"]
            ),
        )

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "duplicate A/B test manifest titles",
        ):
            youtube_ab_test.start_experiment(
                self.spec,
                manifest["experiment_id"],
                studio_started_confirmed=True,
            )

    def test_start_rejects_missing_title_even_with_recomputed_checksum(self) -> None:
        manifest = self._plan_title()
        self._rewrite_plan(
            manifest["experiment_id"],
            lambda data: data["variants"][0].pop("title"),
        )

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "invalid A/B test variant title",
        ):
            youtube_ab_test.start_experiment(
                self.spec,
                manifest["experiment_id"],
                studio_started_confirmed=True,
            )

    def test_start_rejects_mode_payload_mismatch(self) -> None:
        manifest = self._plan_title()
        self._rewrite_plan(
            manifest["experiment_id"],
            lambda data: data.__setitem__("mode", "thumbnail"),
        )

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "thumbnail mode contains a title",
        ):
            youtube_ab_test.start_experiment(
                self.spec,
                manifest["experiment_id"],
                studio_started_confirmed=True,
            )

    def test_start_rejects_missing_frozen_thumbnail(self) -> None:
        manifest = youtube_ab_test.plan_experiment(
            self.spec,
            video_id=self.video_id,
            mode="thumbnail",
            thumbnail_paths=[
                self._image("first.png", "red"),
                self._image("second.png", "blue"),
            ],
            studio_eligible_confirmed=True,
            experiment_id="yab-0000000000000005",
        )
        directory = self._manifest_file(manifest["experiment_id"]).parent
        (directory / manifest["variants"][0]["thumbnail"]["file"]).unlink()

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "frozen thumbnail is missing",
        ):
            youtube_ab_test.start_experiment(
                self.spec,
                manifest["experiment_id"],
                studio_started_confirmed=True,
            )

    def test_start_rejects_changed_frozen_thumbnail(self) -> None:
        manifest = youtube_ab_test.plan_experiment(
            self.spec,
            video_id=self.video_id,
            mode="thumbnail",
            thumbnail_paths=[
                self._image("first.png", "red"),
                self._image("second.png", "blue"),
            ],
            studio_eligible_confirmed=True,
            experiment_id="yab-0000000000000006",
        )
        directory = self._manifest_file(manifest["experiment_id"]).parent
        copied = directory / manifest["variants"][0]["thumbnail"]["file"]
        Image.new("RGB", (1280, 720), "green").save(copied)

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "frozen thumbnail changed after planning",
        ):
            youtube_ab_test.start_experiment(
                self.spec,
                manifest["experiment_id"],
                studio_started_confirmed=True,
            )

    def test_plan_hashes_copied_bytes_when_source_changes_during_copy(self) -> None:
        first = self._image("first.png", "red")
        second = self._image("second.png", "blue")
        original_digest = hashlib.sha256(first.read_bytes()).hexdigest()
        real_copyfile = shutil.copyfile

        def change_then_copy(source, destination):
            if Path(source).resolve() == first.resolve():
                Image.new("RGB", (1280, 720), "green").save(first)
            return real_copyfile(source, destination)

        with mock.patch.object(
            youtube_ab_test.shutil,
            "copyfile",
            side_effect=change_then_copy,
        ):
            manifest = youtube_ab_test.plan_experiment(
                self.spec,
                video_id=self.video_id,
                mode="thumbnail",
                thumbnail_paths=[first, second],
                studio_eligible_confirmed=True,
                experiment_id="yab-0000000000000007",
            )

        directory = self._manifest_file(manifest["experiment_id"]).parent
        copied = directory / manifest["variants"][0]["thumbnail"]["file"]
        recorded_digest = manifest["variants"][0]["thumbnail"]["sha256"]
        self.assertNotEqual(recorded_digest, original_digest)
        self.assertEqual(
            recorded_digest,
            hashlib.sha256(copied.read_bytes()).hexdigest(),
        )

    def test_corrupt_existing_manifest_fails_closed(self) -> None:
        corrupt = (
            self.spec.output_dir
            / "youtube_ab_tests"
            / "yab-ffffffffffffffff"
        )
        corrupt.mkdir(parents=True)
        (corrupt / "manifest.json").write_text("{", encoding="utf-8")

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "invalid A/B test manifest",
        ):
            self._plan_title()

    def test_manifest_channel_mismatch_fails_closed(self) -> None:
        manifest = self._plan_title()
        path = (
            self.spec.output_dir
            / "youtube_ab_tests"
            / manifest["experiment_id"]
            / "manifest.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        data["channel"] = "other-channel"
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "channel mismatch",
        ):
            youtube_ab_test.show_experiment(self.spec, manifest["experiment_id"])

    def test_symlink_experiment_directory_is_rejected(self) -> None:
        external = Path(self.tmp.name) / "external"
        external.mkdir()
        directory = (
            self.spec.output_dir
            / "youtube_ab_tests"
            / "yab-eeeeeeeeeeeeeeee"
        )
        directory.parent.mkdir(parents=True)
        directory.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(
            youtube_ab_test.YouTubeABTestError,
            "must not be a symlink",
        ):
            youtube_ab_test.show_experiment(self.spec, directory.name)


if __name__ == "__main__":
    unittest.main()
