"""Markdownプロンプト(persona/corner/output_rules系)の読込・ソフト検証・保存のテスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config
from doci.admin import markdown_store
from tests.admin_test_helpers import write_channel, write_minimal_repo


class MarkdownStoreTest(unittest.TestCase):
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
        self.root_patcher = mock.patch.object(config, "ROOT", self.root)
        self.prompts_patcher = mock.patch.object(config, "PROMPTS", self.root / "doci" / "prompts")
        self.output_patcher = mock.patch.object(config, "OUTPUT", self.root / "output")
        for p in (self.root_patcher, self.prompts_patcher, self.output_patcher):
            p.start()
            self.addCleanup(p.stop)

    def test_shared_output_rules_slot_resolves_to_patched_prompts_dir(self) -> None:
        prompts = {p.slot: p for p in markdown_store.list_prompts()}
        self.assertIn("shared:output_rules", prompts)
        self.assertTrue(prompts["shared:output_rules"].exists)
        self.assertTrue(prompts["shared:output_rules"].path.startswith(str(self.root)))

    def test_channel_local_overrides_are_creatable_and_absent_by_default(self) -> None:
        prompts = {p.slot: p for p in markdown_store.list_prompts()}
        self.assertFalse(prompts["testch:output_rules"].exists)
        self.assertTrue(prompts["testch:output_rules"].creatable)
        self.assertFalse(prompts["testch:output_rules_addendum"].exists)

    def test_corner_slots_have_required_tokens(self) -> None:
        prompts = {p.slot: p for p in markdown_store.list_prompts()}
        self.assertEqual(prompts["testch:corner:a"].required_tokens, ("{date}", "{past_topics}"))
        self.assertEqual(prompts["testch:persona:a"].required_tokens, ())

    def test_shared_persona_across_corners_is_deduped_with_used_by(self) -> None:
        # 2つのコーナーが別々のpersonaファイルを持つデフォルトfixtureでは used_by は空。
        prompts = {p.slot: p for p in markdown_store.list_prompts()}
        self.assertEqual(prompts["testch:persona:a"].used_by, ())

        # persona_aとpersona_bを同じファイルに向け直すと、used_byで検出される。
        channel_dir = self.root / "channels" / "testch"
        (channel_dir / "prompts" / "persona_b.md").write_text(
            (channel_dir / "prompts" / "persona_a.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        toml_path = channel_dir / "channel.toml"
        text = toml_path.read_text(encoding="utf-8")
        toml_path.write_text(text.replace('persona = "prompts/persona_b.md"', 'persona = "prompts/persona_a.md"'), encoding="utf-8")

        prompts2 = {p.slot: p for p in markdown_store.list_prompts()}
        self.assertIn("testch:persona:b", prompts2["testch:persona:a"].used_by)
        self.assertIn("testch:persona:a", prompts2["testch:persona:b"].used_by)

    def test_read_prompt_returns_text_and_fingerprint(self) -> None:
        payload = markdown_store.read_prompt("testch:corner:a")
        self.assertIn("{date}", payload["text"])
        self.assertEqual(
            payload["fingerprint"], markdown_store.content_fingerprint(payload["text"])
        )

    def test_unknown_slot_raises(self) -> None:
        with self.assertRaises(markdown_store.SlotNotFoundError):
            markdown_store.read_prompt("testch:corner:does-not-exist")
        with self.assertRaises(markdown_store.SlotNotFoundError):
            markdown_store.read_prompt("nope:corner:a")
        with self.assertRaises(markdown_store.SlotNotFoundError):
            markdown_store.read_prompt("testch:corner:../../secrets")

    def test_validate_missing_token_warns(self) -> None:
        text = markdown_store.read_prompt("testch:corner:a")["text"]
        stripped = text.replace("{date}", "")
        warnings = markdown_store.validate("testch:corner:a", stripped)
        self.assertTrue(any("{date}" in w for w in warnings))

    def test_validate_empty_text_warns(self) -> None:
        warnings = markdown_store.validate("testch:persona:a", "   \n  ")
        self.assertTrue(any("空です" in w for w in warnings))

    def test_save_blocks_on_warnings_without_confirm(self) -> None:
        text = markdown_store.read_prompt("testch:corner:a")["text"]
        stripped = text.replace("{date}", "")
        result = markdown_store.save("testch:corner:a", stripped, confirm_warnings=False)
        self.assertFalse(result.ok)
        self.assertTrue(result.needs_confirmation)
        self.assertEqual(
            markdown_store.read_prompt("testch:corner:a")["text"], text
        )  # 実ファイルは無変更

    def test_save_succeeds_with_confirm(self) -> None:
        text = markdown_store.read_prompt("testch:corner:a")["text"]
        stripped = text.replace("{date}", "")
        result = markdown_store.save("testch:corner:a", stripped, confirm_warnings=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(markdown_store.read_prompt("testch:corner:a")["text"], stripped)

    def test_save_creates_channel_local_override_file(self) -> None:
        result = markdown_store.save(
            "testch:output_rules", "チャンネル固有ルール\n", confirm_warnings=True
        )
        self.assertTrue(result.ok, result.error)
        path = self.root / "channels" / "testch" / "prompts" / "output_rules.md"
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "チャンネル固有ルール\n")

    def test_save_creates_backup_only_when_file_previously_existed(self) -> None:
        from doci.admin import safeio

        # 新規作成(バックアップ対象が存在しない)
        markdown_store.save("testch:output_rules", "v1\n", confirm_warnings=True)
        self.assertEqual(safeio.list_backups("prompt", "testch:output_rules"), [])
        # 2回目は既存ファイルの上書きなのでバックアップが作られる
        markdown_store.save("testch:output_rules", "v2\n", confirm_warnings=True)
        self.assertEqual(len(safeio.list_backups("prompt", "testch:output_rules")), 1)

    def test_stale_fingerprint_returns_409(self) -> None:
        result = markdown_store.save(
            "testch:persona:a", "new text\n", confirm_warnings=True, base_fingerprint="deadbeef"
        )
        self.assertEqual(result.code, 409)


if __name__ == "__main__":
    unittest.main()
