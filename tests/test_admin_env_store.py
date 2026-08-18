""".env 読込・パッチ・検証・保存のテスト。

実リポジトリの `.env`/`.env.example` は読み取り専用の参照にのみ使い、書き込み系
テストは必ず一時ディレクトリへ `config.ROOT` を patch してから行う。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config
from doci.admin import env_store


EXAMPLE_TEXT = (config.ROOT / ".env.example").read_text(encoding="utf-8")


class ParseValueTest(unittest.TestCase):
    def test_matches_config_load_dotenv_semantics(self) -> None:
        cases = [
            ("codex", "codex"),
            ("  codex  ", "codex"),
            ('"codex"', "codex"),
            ("'codex'", "codex"),
            ("0          # 1=実投稿せずログのみ", "0          # 1=実投稿せずログのみ"),
        ]
        for raw, expected in cases:
            self.assertEqual(env_store.parse_value(raw), expected, raw)


class EncodeValueTest(unittest.TestCase):
    def test_round_trips_ordinary_values(self) -> None:
        for value in ("codex", "gpt-5.6-luna", "0.18", "https://example.com/v1"):
            encoded = env_store.encode_value(value)
            self.assertEqual(env_store.parse_value(encoded), value)

    def test_rejects_newline(self) -> None:
        with self.assertRaises(env_store.EnvValueError):
            env_store.encode_value("a\nb")

    def test_rejects_surrounding_whitespace(self) -> None:
        with self.assertRaises(env_store.EnvValueError):
            env_store.encode_value(" codex")

    def test_rejects_surrounding_quotes(self) -> None:
        with self.assertRaises(env_store.EnvValueError):
            env_store.encode_value('"codex"')
        with self.assertRaises(env_store.EnvValueError):
            env_store.encode_value("codex'")

    def test_empty_value_is_accepted(self) -> None:
        # `value[:1] in "\"'"` は空文字列に対して常にTrueを返してしまうため
        # (空文字列はどんな文字列の部分文字列でもある)、値を空にクリアする操作が
        # 常にエラーになっていた。実際に確認して修正済み。
        self.assertEqual(env_store.encode_value(""), "")


class ApplyPatchTest(unittest.TestCase):
    def test_noop_patch_is_byte_identical(self) -> None:
        new_text, warnings = env_store.apply_patch(EXAMPLE_TEXT, {})
        self.assertEqual(new_text, EXAMPLE_TEXT)
        self.assertEqual(warnings, [])

    def test_single_key_change_touches_only_that_line(self) -> None:
        new_text, warnings = env_store.apply_patch(EXAMPLE_TEXT, {"TEXT_BACKEND": "opencode"})
        self.assertEqual(warnings, [])
        old_lines = EXAMPLE_TEXT.splitlines()
        new_lines = new_text.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))
        diffs = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        self.assertEqual(diffs, [old_lines.index("TEXT_BACKEND=codex")])
        self.assertIn("TEXT_BACKEND=opencode", new_text)

    def test_comment_lines_are_preserved(self) -> None:
        new_text, _ = env_store.apply_patch(EXAMPLE_TEXT, {"TEXT_BACKEND": "opencode"})
        old_comment_count = sum(1 for l in EXAMPLE_TEXT.splitlines() if l.strip().startswith("#"))
        new_comment_count = sum(1 for l in new_text.splitlines() if l.strip().startswith("#"))
        self.assertEqual(old_comment_count, new_comment_count)

    def test_new_key_is_appended(self) -> None:
        new_text, warnings = env_store.apply_patch(EXAMPLE_TEXT, {"BRAND_NEW_KEY": "hello"})
        self.assertIn("BRAND_NEW_KEY=hello", new_text)
        self.assertEqual(warnings, [])
        # 2回目に別の新規キーを足しても、フッターマーカーは重複しない
        new_text2, _ = env_store.apply_patch(new_text, {"ANOTHER_NEW_KEY": "x"})
        self.assertEqual(new_text2.count("doci admin UI が追加したキー"), 1)

    def test_delete_comments_out_active_line(self) -> None:
        new_text, warnings = env_store.apply_patch(EXAMPLE_TEXT, {"TEXT_BACKEND": None})
        self.assertIn("#TEXT_BACKEND=codex", new_text)
        self.assertNotIn("\nTEXT_BACKEND=codex\n", new_text)
        self.assertEqual(warnings, [])

    def test_enable_commented_key_with_new_value(self) -> None:
        new_text, warnings = env_store.apply_patch(
            EXAMPLE_TEXT, {"CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB": "1"}
        )
        self.assertIn("CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB=1", new_text)
        self.assertEqual(warnings, [])

    def test_enable_without_value_uses_commented_value(self) -> None:
        text = "# FOO=bar\n"
        new_text, warnings = env_store.apply_patch(text, {}, enable=["FOO"])
        self.assertIn("FOO=bar", new_text)
        self.assertEqual(warnings, [])

    def test_enable_missing_key_warns(self) -> None:
        new_text, warnings = env_store.apply_patch("X=1\n", {}, enable=["NOPE"])
        self.assertTrue(any("NOPE" in w for w in warnings))

    def test_delete_missing_key_warns_and_is_noop(self) -> None:
        new_text, warnings = env_store.apply_patch("X=1\n", {"NOPE": None})
        self.assertEqual(new_text, "X=1\n")
        self.assertTrue(any("NOPE" in w for w in warnings))

    def test_bad_value_raises_and_does_not_partially_apply(self) -> None:
        with self.assertRaises(env_store.EnvValueError):
            env_store.apply_patch(EXAMPLE_TEXT, {"TEXT_BACKEND": "bad\nvalue"})

    def test_inline_hash_warns_because_parser_does_not_strip_it(self) -> None:
        _, warnings = env_store.apply_patch(EXAMPLE_TEXT, {"TEXT_BACKEND": "codex # note"})
        self.assertTrue(any("TEXT_BACKEND" in w and "#" in w for w in warnings))

    def test_replacing_line_with_existing_inline_comment_warns_about_loss(self) -> None:
        # config._load_dotenv は行内コメントを剥がさないため、既存の行内コメント付き
        # キーの値だけを書き換えると、そのコメントは黙って失われる
        # (`PUBLISH_DRY_RUN=0          # 1=実投稿せずログのみ` のような行が実在する)。
        # 気付けるよう警告を出すべきで、以前は無警告で消えていた。
        text = "FOO=0          # some comment\n"
        new_text, warnings = env_store.apply_patch(text, {"FOO": "1"})
        self.assertEqual(new_text, "FOO=1\n")
        self.assertTrue(any("FOO" in w and "コメント" in w for w in warnings))

    def test_no_warning_when_original_line_has_no_comment(self) -> None:
        new_text, warnings = env_store.apply_patch("FOO=0\n", {"FOO": "1"})
        self.assertEqual(warnings, [])


class ReadEntriesTest(unittest.TestCase):
    def test_first_active_occurrence_wins_on_duplicates(self) -> None:
        text = "FOO=first\nFOO=second\n"
        entries = {e.key: e for e in env_store.read_entries(text)}
        self.assertEqual(entries["FOO"].value, "first")

    def test_secret_values_are_never_exposed(self) -> None:
        text = "ANTHROPIC_API_KEY=sk-realsecret\n"
        entries = {e.key: e for e in env_store.read_entries(text)}
        self.assertIsNone(entries["ANTHROPIC_API_KEY"].value)
        self.assertTrue(entries["ANTHROPIC_API_KEY"].is_secret)
        self.assertTrue(entries["ANTHROPIC_API_KEY"].is_set)
        self.assertNotEqual(entries["ANTHROPIC_API_KEY"].fingerprint, "")

    def test_real_env_example_parses_without_error(self) -> None:
        entries = env_store.read_entries(EXAMPLE_TEXT)
        self.assertGreater(len(entries), 50)
        keys = {e.key for e in entries}
        self.assertIn("TEXT_BACKEND", keys)


class ValidateCandidateIsolationTest(unittest.TestCase):
    """検証サブプロセスが「継承環境」でなく「候補内容」を検証していることの確認。"""

    def test_inherited_process_env_does_not_leak_into_validation(self) -> None:
        real_text = env_store.read_env_text()
        if not real_text:
            self.skipTest("real .env not present in this environment")
        with mock.patch.dict("os.environ", {"TEXT_BACKEND": "bogus"}):
            result = env_store.validate_candidate(real_text)
        self.assertTrue(result.ok, result.error)

    def test_bad_combination_is_rejected_with_exact_message(self) -> None:
        # .env.example それ自体は自己完結した有効な値集合ではない(CODEX_PROVIDER=chatgpt +
        # RESEARCH_BACKEND=codexの組み合わせがCODEX_CHATGPT_ALLOW_UNTRUSTED_WEB無しで
        # 既に無効)。ここでは制御された最小の有効ベースに不正値を1つだけ加える。
        minimal_valid = "TEXT_BACKEND=codex\nCODEX_PROVIDER=minimax\n"
        result_ok = env_store.validate_candidate(minimal_valid)
        self.assertTrue(result_ok.ok, result_ok.error)

        # 単純追記だと setdefault 方式で最初の行(codex)が勝ってしまうため、置換する。
        bad = "TEXT_BACKEND=bogus\nCODEX_PROVIDER=minimax\n"
        result = env_store.validate_candidate(bad)
        self.assertFalse(result.ok)
        self.assertIn("TEXT_BACKEND", result.error)

    def test_secret_values_never_leak_into_validation_response(self) -> None:
        bad = EXAMPLE_TEXT + "\nANTHROPIC_API_KEY=SENTINEL_ABC123\nTEXT_BACKEND=bogus\n"
        result = env_store.validate_candidate(bad)
        self.assertNotIn("SENTINEL_ABC123", result.error)
        self.assertNotIn("SENTINEL_ABC123", result.detail)


class SaveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # 検証サブプロセスは実dociパッケージの物理配置(_PACKAGE_ROOT)からimportするため
        # 実チャンネル(ideology/youtube-growth)を検証対象にする。ここではsave()が
        # `config.ROOT/.env` を正しく読み書きすることだけを見るので、
        # `channels/`ディレクトリ自体はこのtempには不要。
        (self.root / "output").mkdir(parents=True)
        (self.root / ".env").write_text(
            "TEXT_BACKEND=codex\nRESEARCH_BACKEND=codex\nFACTCHECK_BACKEND=codex\n"
            "CHART_BG_BACKEND=codex\nPLAN_BACKEND=codex\nCODEX_PROVIDER=minimax\n",
            encoding="utf-8",
        )
        self.root_patcher = mock.patch.object(config, "ROOT", self.root)
        self.output_patcher = mock.patch.object(config, "OUTPUT", self.root / "output")
        self.root_patcher.start()
        self.output_patcher.start()
        self.addCleanup(self.root_patcher.stop)
        self.addCleanup(self.output_patcher.stop)

    def test_save_writes_validated_change(self) -> None:
        result = env_store.save({"TEXT_BACKEND": "opencode_go"})
        self.assertTrue(result.ok, result.error)
        self.assertIn("TEXT_BACKEND=opencode_go", (self.root / ".env").read_text(encoding="utf-8"))

    def test_save_creates_backup_before_write(self) -> None:
        from doci.admin import safeio

        env_store.save({"TEXT_BACKEND": "opencode_go"})
        backups = safeio.list_backups("env", "env")
        self.assertEqual(len(backups), 1)
        self.assertIn("TEXT_BACKEND=codex", backups[0].path.read_text(encoding="utf-8"))

    def test_save_rejects_invalid_backend_and_does_not_write(self) -> None:
        before = (self.root / ".env").read_text(encoding="utf-8")
        result = env_store.save({"TEXT_BACKEND": "bogus_backend"})
        self.assertFalse(result.ok)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), before)

    def test_stale_fingerprint_returns_409_and_does_not_write(self) -> None:
        before = (self.root / ".env").read_text(encoding="utf-8")
        result = env_store.save({"TEXT_BACKEND": "opencode_go"}, base_fingerprint="deadbeef")
        self.assertEqual(result.code, 409)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), before)

    def test_matching_fingerprint_succeeds(self) -> None:
        fp = env_store.content_fingerprint(env_store.read_env_text())
        result = env_store.save({"TEXT_BACKEND": "opencode_go"}, base_fingerprint=fp)
        self.assertTrue(result.ok, result.error)

    def test_invalid_key_name_rejected(self) -> None:
        result = env_store.save({"not a valid key": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 400)


if __name__ == "__main__":
    unittest.main()
