"""Issue #165: YouTube終了画面1枠の手動運用記録。"""
from __future__ import annotations

import json
import math
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
                click_rate=3.5,
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

    def test_plan_rejects_checksum_mismatch_on_start(self) -> None:
        """計画後のmanifest改変（リンク先変更・制約フラグ偽）をstartで拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["link_video_id"] = "AnotherId9999"
        data["plan_sha256"] = "f" * 64
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "checksum mismatch"):
            end_screen.start_experiment(
                self.spec,
                "esc-0000000000000001",
                studio_setup_confirmed=True,
            )

    def test_plan_rejects_weakened_constraints_even_with_recomputed_checksum(self) -> None:
        """制約フラグを偽へ変更しチェックサムを再計算しても、startで拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["single_slot_only"] = False
        data["end_screen_setup"]["subscription_button_prohibited"] = False
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "single video slot"):
            end_screen.start_experiment(
                self.spec,
                "esc-0000000000000001",
                studio_setup_confirmed=True,
            )

    def test_manifest_rejects_self_link_and_id_mismatch(self) -> None:
        """自己リンク・ID/ディレクトリ不一致・非object JSONを拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["link_video_id"] = self.video_id
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "must differ"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # ID/ディレクトリ不一致
        data2 = json.loads(self._manifest_file("esc-0000000000000001").read_text(encoding="utf-8"))
        data2["experiment_id"] = "esc-9999999999999999"
        data2["plan_sha256"] = end_screen._plan_checksum(data2)
        self._manifest_file("esc-0000000000000001").write_text(
            json.dumps(data2, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "mismatch"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 非object JSON
        self._manifest_file("esc-0000000000000001").write_text("[1,2,3]\n", encoding="utf-8")
        with self.assertRaisesRegex(end_screen.EndScreenError, "invalid manifest"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_corrupt_active_manifest_blocks_second_plan(self) -> None:
        """壊れた既存manifest（不正JSON・不正setup・欠落）があると、
        同一動画の2件目planを拒否する（fail-closed・active一意性）。"""
        self._plan("esc-0000000000000001")
        path = self._manifest_file("esc-0000000000000001")
        path.write_text("{broken json\n", encoding="utf-8")
        with self.assertRaises(end_screen.EndScreenError):
            self._plan("esc-0000000000000002")
        self.assertFalse(self._manifest_file("esc-0000000000000002").exists())

        # 不正setup
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["end_screen_setup"]["element"] = "playlist"
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(end_screen.EndScreenError):
            self._plan("esc-0000000000000002")

        # manifest欠落
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        path.unlink()
        with self.assertRaisesRegex(end_screen.EndScreenError, "manifest missing"):
            self._plan("esc-0000000000000002")

    def test_symlink_manifest_directory_blocks_plan(self) -> None:
        """実験ディレクトリがsymlinkなら拒否する。"""
        self._plan("esc-0000000000000001")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        target = self.spec.output_dir / "end_screen_tests" / "esc-0000000000000002"
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(end_screen.EndScreenError, "symlink"):
            self._plan("esc-0000000000000002")

    def test_root_symlink_blocks_plan(self) -> None:
        """記録先ルート自体がsymlinkなら外部書込み前に拒否する。"""
        self._plan("esc-0000000000000001")
        root = self.spec.output_dir / "end_screen_tests"
        outside = Path(self.tmp.name) / "outside-root"
        outside.mkdir()
        root.rename(outside)
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(end_screen.EndScreenError, "root must not be a symlink"):
            self._plan("esc-0000000000000003")

    def test_complete_requires_click_rate_for_clicked(self) -> None:
        """clicked outcomeはクリック率必須。省略を拒否する。"""
        self._plan()
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "click_rate is required"):
            end_screen.complete_experiment(
                self.spec,
                "esc-0000000000000001",
                outcome="clicked",
                setup_unchanged_confirmed=True,
            )

    def test_complete_rejects_outcome_rate_contradiction_and_nonfinite(self) -> None:
        """clickedで0%・not_clickedで正値・NaN/Infinityを拒否する。"""
        for index, (outcome, click_rate) in enumerate(
            (
            ("clicked", 0.0),
            ("not_clicked", 3.0),
            ("clicked", math.nan),
            ("clicked", math.inf),
            ("clicked", "3.5"),
            )
        ):
            experiment_id = f"esc-{index + 2:016d}"
            video_id = f"AbCdEf{index:05d}"
            with self.subTest(outcome=outcome, click_rate=click_rate):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                end_screen.start_experiment(
                    self.spec,
                    experiment_id,
                    studio_setup_confirmed=True,
                )
                with self.assertRaises(end_screen.EndScreenError):
                    end_screen.complete_experiment(
                        self.spec,
                        experiment_id,
                        outcome=outcome,
                        click_rate=click_rate,
                        setup_unchanged_confirmed=True,
                    )

    def test_manifest_result_validates_status_outcome_and_flags(self) -> None:
        """completed/invalidated manifestのstatus-outcome整合・確認フラグ・
        日時・率を検証する。"""
        self._plan()
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        end_screen.complete_experiment(
            self.spec,
            "esc-0000000000000001",
            outcome="clicked",
            click_rate=3.5,
            setup_unchanged_confirmed=True,
        )
        path = self._manifest_file("esc-0000000000000001")

        # status/outcome不一致
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "invalidated"
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "require"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 確認フラグ欠落
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "completed"
        data["result"]["setup_unchanged_confirmed"] = False
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "setup_unchanged_confirmed"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 日時欠落
        data = json.loads(path.read_text(encoding="utf-8"))
        data["result"]["setup_unchanged_confirmed"] = True
        data["result"]["recorded_at"] = ""
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "recorded_at"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_stopped_changed_setup_round_trip_and_followup_plan(self) -> None:
        """stopped_changed_setup は invalidated + 確認フラグFalse + 率Noneで
        有効な終端状態。showで再読込でき、別動画のplanも継続できる。"""
        self._plan()
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        manifest = end_screen.complete_experiment(
            self.spec,
            "esc-0000000000000001",
            outcome="stopped_changed_setup",
            notes="構成を変更した",
        )
        self.assertEqual(manifest["status"], "invalidated")
        self.assertIsNone(manifest["result"]["click_rate"])

        shown = end_screen.show_experiment(self.spec, "esc-0000000000000001")
        self.assertEqual(shown["status"], "invalidated")
        self.assertIsNone(shown["result"]["click_rate"])

        # 別動画のplanが継続できる
        self._write_history(video_id="NewVidId0001")
        plan = end_screen.plan_experiment(
            self.spec,
            video_id="NewVidId0001",
            link_video_id=self.link_video_id,
            content_direct_confirmed=True,
            experiment_id="esc-0000000000000003",
        )
        self.assertEqual(plan["status"], "planned")

    def test_manifest_result_rejects_rate_contradiction_and_missing_timestamps(self) -> None:
        """手動生成した clicked+0% / not_clicked+正値 / completed_at欠落を拒否する。"""
        cases = (
            (
                "esc-0000000000000010",
                lambda d: d["result"].update(click_rate=0.0),
                "positive click_rate",
            ),
            (
                "esc-0000000000000011",
                lambda d: d["result"].update(outcome="not_clicked", click_rate=3.0),
                "zero click_rate",
            ),
            (
                "esc-0000000000000012",
                lambda d: d.update(completed_at=""),
                "ISO-8601",
            ),
        )
        for index, (experiment_id, mutate, message) in enumerate(cases):
            video_id = f"AbCdEf{index + 10:05d}"
            with self.subTest(message=message):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                end_screen.start_experiment(
                    self.spec,
                    experiment_id,
                    studio_setup_confirmed=True,
                )
                end_screen.complete_experiment(
                    self.spec,
                    experiment_id,
                    outcome="clicked",
                    click_rate=3.5,
                    setup_unchanged_confirmed=True,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                mutate(data)
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(end_screen.EndScreenError, message):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_manifest_rejects_invalid_video_id(self) -> None:
        """対象video_idがYouTube ID形式でないmanifestを、チェックサム再計算後も
        拒否する。"""
        for index, bad in enumerate(("", "not valid", "短い")):
            experiment_id = f"esc-{index + 20:016d}"
            video_id = f"AbCdEf{index + 20:05d}"
            with self.subTest(bad=bad):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["video_id"] = bad
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(end_screen.EndScreenError, "invalid video_id"):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_show_rejects_root_and_manifest_symlinks(self) -> None:
        """showでもroot symlink・manifest file symlinkを拒否する。"""
        self._plan()
        root = self.spec.output_dir / "end_screen_tests"
        outside = Path(self.tmp.name) / "outside-root"
        outside.mkdir()
        root.rename(outside)
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(end_screen.EndScreenError, "root must not be a symlink"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")
        root.unlink()
        outside.rename(root)

        # manifest file symlink
        manifest_path = root / "esc-0000000000000001" / "manifest.json"
        real = manifest_path.read_bytes()
        manifest_path.unlink()
        fake = Path(self.tmp.name) / "fake-manifest.json"
        fake.write_bytes(real)
        manifest_path.symlink_to(fake)
        with self.assertRaisesRegex(end_screen.EndScreenError, "manifest must not be a symlink"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_manifest_rejects_non_string_video_ids(self) -> None:
        """整数等の非文字列IDを、チェックサム再計算後も拒否する。"""
        for index, key in enumerate(("video_id", "link_video_id")):
            experiment_id = f"esc-{index + 30:016d}"
            video_id = f"AbCdEf{index + 30:05d}"
            with self.subTest(key=key):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                if key == "video_id":
                    data["video_id"] = 123456
                else:
                    data["end_screen_setup"]["link_video_id"] = 123456
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(end_screen.EndScreenError, "must be a string"):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_manifest_status_schema_rejects_invalid_transitions(self) -> None:
        """planned/runningへのterminal field混入・runningのstarted_at欠落・
        数値日時を拒否する。"""
        self._plan()
        path = self._manifest_file("esc-0000000000000001")

        # plannedへのterminal field混入
        data = json.loads(path.read_text(encoding="utf-8"))
        data["completed_at"] = self.now.isoformat()
        data["result"] = {"outcome": "clicked", "click_rate": 3.5}
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "planned manifest must not"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # runningのstarted_at欠落
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "running"
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "started_at"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

        # 数値日時
        shutil.rmtree(
            self.spec.output_dir / "end_screen_tests" / "esc-0000000000000001"
        )
        self._plan("esc-0000000000000001")
        end_screen.start_experiment(
            self.spec,
            "esc-0000000000000001",
            studio_setup_confirmed=True,
        )
        end_screen.complete_experiment(
            self.spec,
            "esc-0000000000000001",
            outcome="clicked",
            click_rate=3.5,
            setup_unchanged_confirmed=True,
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        data["started_at"] = 123
        data["plan_sha256"] = end_screen._plan_checksum(data)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(end_screen.EndScreenError, "ISO-8601"):
            end_screen.show_experiment(self.spec, "esc-0000000000000001")

    def test_manifest_status_schema_rejects_explicit_null_fields(self) -> None:
        """planned/runningへの明示的なnullフィールド混入を拒否する。"""
        for index, field in enumerate(("started_at", "completed_at", "result")):
            experiment_id = f"esc-{index + 40:016d}"
            video_id = f"AbCdEf{index + 40:05d}"
            with self.subTest(field=field):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data[field] = None
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    end_screen.EndScreenError,
                    f"planned manifest must not have {field}",
                ):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )

    def test_manifest_timestamp_rejects_impossible_datetimes(self) -> None:
        """正規表現に一致しても実在しない日時（2月30日・月13・時刻25時・
        不正offset）を拒否する。"""
        for index, bad in enumerate(
            (
            "2026-02-30T00:00:00+00:00",
            "2026-13-01T00:00:00+00:00",
            "2026-01-01T25:00:00+00:00",
            "2026-01-01T00:00:00+99:99",
            )
        ):
            experiment_id = f"esc-{index + 50:016d}"
            video_id = f"AbCdEf{index + 50:05d}"
            with self.subTest(bad=bad):
                self._write_history(video_id=video_id)
                end_screen.plan_experiment(
                    self.spec,
                    video_id=video_id,
                    link_video_id=self.link_video_id,
                    content_direct_confirmed=True,
                    experiment_id=experiment_id,
                )
                end_screen.start_experiment(
                    self.spec,
                    experiment_id,
                    studio_setup_confirmed=True,
                )
                end_screen.complete_experiment(
                    self.spec,
                    experiment_id,
                    outcome="clicked",
                    click_rate=3.5,
                    setup_unchanged_confirmed=True,
                )
                path = self._manifest_file(experiment_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["started_at"] = bad
                data["plan_sha256"] = end_screen._plan_checksum(data)
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(end_screen.EndScreenError):
                    end_screen.show_experiment(self.spec, experiment_id)
                shutil.rmtree(
                    self.spec.output_dir / "end_screen_tests" / experiment_id
                )


if __name__ == "__main__":
    unittest.main()
