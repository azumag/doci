from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from doci import output_cleanup


class OutputCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "output" / "sample"
        self.output_dir.mkdir(parents=True)

    def _workdir(self, name: str = "2026-08-02_video_120000") -> Path:
        workdir = self.output_dir / name
        workdir.mkdir()
        (workdir / "script.json").write_text(
            json.dumps(
                {
                    "title": "test",
                    "description": "description",
                    "tags": [],
                    "narration": "narration",
                    "scenes": [{"visual_prompt": "image"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return workdir

    def test_cleanup_deletes_only_media_and_keeps_recovery_inputs(self) -> None:
        workdir = self._workdir()
        (workdir / "video.mp4").write_bytes(b"video")
        (workdir / "narration.wav").write_bytes(b"voice")
        (workdir / "scene_00.png").write_bytes(b"image")
        nested = workdir / "assets"
        nested.mkdir()
        (nested / "source.webp").write_bytes(b"source")
        (nested / "chart.json").write_text('{"type":"bar"}\n', encoding="utf-8")
        (workdir / "notes.txt").write_text("keep\n", encoding="utf-8")

        result = output_cleanup.cleanup_workdir(
            self.output_dir,
            workdir,
            apply=True,
            recovery={"channel": "sample", "script": "script.json"},
        )

        self.assertEqual(result.status, "cleaned")
        self.assertEqual(result.files, 4)
        self.assertFalse((workdir / "video.mp4").exists())
        self.assertFalse((workdir / "narration.wav").exists())
        self.assertFalse((workdir / "scene_00.png").exists())
        self.assertFalse((nested / "source.webp").exists())
        self.assertTrue((workdir / "script.json").exists())
        self.assertTrue((nested / "chart.json").exists())
        self.assertTrue((workdir / "notes.txt").exists())
        manifest = json.loads(
            (workdir / output_cleanup.RECOVERY_MANIFEST).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["recovery"]["script"], "script.json")
        self.assertEqual(manifest["cleanup"]["status"], "cleaned")
        self.assertEqual(manifest["cleanup"]["deleted_files"], 4)

    def test_preview_is_read_only(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")

        result = output_cleanup.cleanup_workdir(
            self.output_dir,
            workdir,
            apply=False,
        )

        self.assertEqual(result.status, "preview")
        self.assertEqual(result.files, 1)
        self.assertTrue(video.exists())
        self.assertFalse((workdir / output_cleanup.RECOVERY_MANIFEST).exists())

    def test_preview_ignores_media_removed_after_listing(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")

        def list_then_remove(_workdir):
            video.unlink()
            return [video]

        with patch.object(
            output_cleanup,
            "_media_files",
            side_effect=list_then_remove,
        ):
            result = output_cleanup.cleanup_workdir(
                self.output_dir,
                workdir,
                apply=False,
            )

        self.assertEqual(result.status, "preview")
        self.assertEqual(result.files, 0)
        self.assertEqual(result.bytes, 0)
        self.assertFalse((workdir / output_cleanup.RECOVERY_MANIFEST).exists())

    def test_cleanup_rejects_a_directory_outside_channel_output(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        video = outside / "video.mp4"
        video.write_bytes(b"video")

        with self.assertRaises(ValueError):
            output_cleanup.cleanup_workdir(
                self.output_dir,
                outside,
                apply=True,
            )

        self.assertTrue(video.exists())

    def test_cleanup_requires_a_valid_script_before_deleting_media(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")
        (workdir / "script.json").unlink()

        with self.assertRaises(ValueError):
            output_cleanup.cleanup_workdir(
                self.output_dir,
                workdir,
                apply=True,
            )

        self.assertTrue(video.exists())
        self.assertFalse((workdir / output_cleanup.RECOVERY_MANIFEST).exists())

    def test_cleanup_retains_media_if_script_disappears_after_validation(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")
        original_validate = output_cleanup._validated_script

        def validate_then_remove(target):
            script = original_validate(target)
            (target / "script.json").unlink()
            return script

        with (
            patch.object(
                output_cleanup,
                "_validated_script",
                side_effect=validate_then_remove,
            ),
            self.assertRaises(ValueError),
        ):
            output_cleanup.cleanup_workdir(
                self.output_dir,
                workdir,
                apply=True,
            )

        self.assertTrue(video.exists())
        self.assertFalse((workdir / output_cleanup.RECOVERY_MANIFEST).exists())

    def test_cleanup_rejects_non_regenerable_script_shapes(self) -> None:
        invalid_values = (
            {"narration": ""},
            {"scenes": []},
            {"scenes": ["not-an-object"]},
            {"title": ""},
        )
        for index, overrides in enumerate(invalid_values):
            with self.subTest(overrides=overrides):
                workdir = self._workdir(f"2026-08-02_video_12{index:02d}00")
                video = workdir / "video.mp4"
                video.write_bytes(b"video")
                script_path = workdir / "script.json"
                script = json.loads(script_path.read_text(encoding="utf-8"))
                script.update(overrides)
                script_path.write_text(json.dumps(script) + "\n", encoding="utf-8")

                with self.assertRaises(ValueError):
                    output_cleanup.cleanup_workdir(
                        self.output_dir,
                        workdir,
                        apply=True,
                    )

                self.assertTrue(video.exists())

    def test_manifest_failure_happens_before_any_media_is_deleted(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")

        with (
            patch.object(
                output_cleanup,
                "_write_json_atomic",
                side_effect=OSError("disk full"),
            ),
            self.assertRaises(OSError),
        ):
            output_cleanup.cleanup_workdir(
                self.output_dir,
                workdir,
                apply=True,
            )

        self.assertTrue(video.exists())

    def test_final_manifest_failure_does_not_claim_media_was_retained(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")
        original_write = output_cleanup._write_json_atomic
        calls = 0

        def fail_second_write(path, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original_write(path, payload)

        with patch.object(
            output_cleanup,
            "_write_json_atomic",
            side_effect=fail_second_write,
        ):
            result = output_cleanup.cleanup_workdir(
                self.output_dir,
                workdir,
                apply=True,
            )

        self.assertEqual(result.status, "partial")
        self.assertFalse(video.exists())
        self.assertTrue(result.errors)
        planned = json.loads(
            (workdir / output_cleanup.RECOVERY_MANIFEST).read_text(encoding="utf-8")
        )
        self.assertEqual(planned["cleanup"]["status"], "planned")

    def test_repeated_cleanup_preserves_exact_saved_recovery_settings(self) -> None:
        workdir = self._workdir()
        (workdir / "video.mp4").write_bytes(b"video")
        output_cleanup.cleanup_workdir(
            self.output_dir,
            workdir,
            apply=True,
            recovery={
                "render": {"width": 1080, "height": 1920, "video_scenes": 3},
                "voice": {"speaker": 1, "speed": 0.9},
            },
        )

        second = output_cleanup.cleanup_workdir(
            self.output_dir,
            workdir,
            apply=True,
            recovery={
                "render": {"duration_sec": 10, "width": 720},
                "voice": {"speaker": 99, "pitch": 0.1},
            },
        )

        self.assertEqual(second.status, "already_clean")
        manifest = json.loads(
            (workdir / output_cleanup.RECOVERY_MANIFEST).read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["recovery"]["render"],
            {
                "width": 1080,
                "height": 1920,
                "video_scenes": 3,
                "duration_sec": 10,
            },
        )
        self.assertEqual(
            manifest["recovery"]["voice"],
            {"speaker": 1, "speed": 0.9, "pitch": 0.1},
        )

    def test_concurrent_cleanup_is_serialized_per_workdir(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    output_cleanup.cleanup_workdir,
                    self.output_dir,
                    workdir,
                    apply=True,
                    recovery={"first": "kept"},
                ),
                executor.submit(
                    output_cleanup.cleanup_workdir,
                    self.output_dir,
                    workdir,
                    apply=True,
                    recovery={"second": "kept"},
                ),
            ]
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(
            sorted(result.status for result in results),
            ["already_clean", "cleaned"],
        )
        self.assertEqual(sum(result.files for result in results), 1)
        self.assertFalse(video.exists())
        manifest = json.loads(
            (workdir / output_cleanup.RECOVERY_MANIFEST).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["recovery"]["first"], "kept")
        self.assertEqual(manifest["recovery"]["second"], "kept")
        self.assertEqual(manifest["cleanup"]["status"], "already_clean")

    def test_publish_completion_requires_ok_without_retry_status(self) -> None:
        self.assertTrue(
            output_cleanup.publish_results_complete(
                [
                    {"platform": "youtube", "status": "ok"},
                    {"platform": "tiktok", "status": "skipped"},
                ]
            )
        )
        for blocker in ("error", "unknown", "dry_run", "unexpected"):
            with self.subTest(blocker=blocker):
                self.assertFalse(
                    output_cleanup.publish_results_complete(
                        [
                            {"platform": "youtube", "status": "ok"},
                            {"platform": "tiktok", "status": blocker},
                        ]
                    )
                )
        self.assertFalse(
            output_cleanup.publish_results_complete(
                [{"platform": "youtube", "status": "skipped"}]
            )
        )

    def test_maintenance_cleans_only_history_confirmed_uploads(self) -> None:
        uploaded = self._workdir("2026-08-02_video_120000")
        failed = self._workdir("2026-08-02_video_130000")
        legacy = self._workdir("2026-08-02_video_140000")
        for workdir in (uploaded, failed, legacy):
            (workdir / "video.mp4").write_bytes(b"video")
            (workdir / "scene_00.jpg").write_bytes(b"image")
        history_file = self.output_dir / "history.jsonl"
        rows = [
            {
                "workdir": str(uploaded),
                "status": "published",
                "video_id": "uploaded123",
                "publish": [
                    {"platform": "youtube", "status": "ok"},
                    {"platform": "tiktok", "status": "skipped"},
                ],
            },
            {
                "workdir": str(failed),
                "status": "published",
                "video_id": "partial123",
                "publish": [
                    {"platform": "youtube", "status": "ok"},
                    {"platform": "tiktok", "status": "error"},
                ],
            },
            {
                "workdir": str(legacy),
                "video_id": "legacy123",
            },
        ]
        history_file.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        spec = SimpleNamespace(
            id="sample",
            output_dir=self.output_dir,
            history_file=history_file,
        )

        preview = output_cleanup.cleanup_uploaded_outputs(spec, apply=False)
        self.assertEqual(preview["workdirs"], 2)
        self.assertTrue((uploaded / "video.mp4").exists())

        applied = output_cleanup.cleanup_uploaded_outputs(spec, apply=True)

        self.assertEqual(applied["workdirs"], 2)
        self.assertFalse((uploaded / "video.mp4").exists())
        self.assertFalse((legacy / "video.mp4").exists())
        self.assertTrue((failed / "video.mp4").exists())
        self.assertTrue((uploaded / "script.json").exists())
        self.assertTrue((legacy / "script.json").exists())

    def test_maintenance_cleans_unknown_after_explicit_published_recovery(self) -> None:
        workdir = self._workdir()
        video = workdir / "video.mp4"
        video.write_bytes(b"video")
        history_file = self.output_dir / "history.jsonl"
        rows = [
            {
                "workdir": str(workdir),
                "status": "publishing",
                "reservation_id": "local-1",
                "topic_ledger_reservation_id": "global-1",
                "publish": [
                    {"platform": "youtube", "status": "unknown"},
                ],
            },
            {
                "status": "published",
                "reservation_id": "local-1",
                "topic_ledger_reservation_id": "global-1",
                "video_id": "confirmed123",
                "recovery_reason": "YouTube Studioで投稿済みを確認",
                "publish_results": [
                    {"platform": "youtube", "status": "unknown"},
                ],
            },
        ]
        history_file.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        spec = SimpleNamespace(
            id="sample",
            output_dir=self.output_dir,
            history_file=history_file,
        )

        preview = output_cleanup.cleanup_uploaded_outputs(spec, apply=False)
        self.assertEqual(preview["workdirs"], 1)
        applied = output_cleanup.cleanup_uploaded_outputs(spec, apply=True)

        self.assertEqual(applied["workdirs"], 1)
        self.assertFalse(video.exists())
        manifest = json.loads(
            (workdir / output_cleanup.RECOVERY_MANIFEST).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["recovery"]["video_id"], "confirmed123")


if __name__ == "__main__":
    unittest.main()
