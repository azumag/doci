"""Python内蔵プロンプト定数(11個)の位置特定・レンダリング・検証・保存のテスト。

`ast` の col_offset/end_col_offset は UTF-8バイトオフセットであり文字オフセット
ではないため、対象の前に日本語などマルチバイト文字がある場合に取り違えると
文字化けする。このテストの中心はその回帰確認と、書き込み前チェックの網羅。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config
from doci.admin import code_prompt_registry as reg, code_prompt_store as store, safeio


class RealRegistryRoundTripTest(unittest.TestCase):
    """実ソース(doci/*.py)を対象にした読み取り専用の往復確認。書き込みはしない。"""

    def test_locate_self_verifies_for_all_11_constants(self) -> None:
        for entry in reg.REGISTRY:
            with self.subTest(entry.id):
                payload = store.read(entry.id)
                self.assertTrue(payload["text"])

    def test_noop_render_is_byte_identical_for_all_11_constants(self) -> None:
        for entry in reg.REGISTRY:
            with self.subTest(entry.id):
                r = store.read(entry.id)
                literal = store.render_literal(r["text"])
                source = store._source_path(entry).read_text(encoding="utf-8")
                located = store.locate(source, entry.name)
                data = source.encode("utf-8")
                new_data = data[: located.start] + literal.encode("utf-8") + data[located.end :]
                self.assertEqual(new_data.decode("utf-8"), source, entry.id)

    def test_all_11_constants_validate_cleanly(self) -> None:
        for entry in reg.REGISTRY:
            with self.subTest(entry.id):
                r = store.read(entry.id)
                v = store.validate(entry.id, r["text"])
                self.assertTrue(v.ok, v.errors)

    def test_registry_fields_are_superset_of_fields_actually_used(self) -> None:
        # レジストリのfieldsが現在の本文で実際に参照されているフィールドの
        # 超集合であること(登録漏れが無いこと)を確認する。
        for entry in reg.REGISTRY:
            with self.subTest(entry.id):
                r = store.read(entry.id)
                fields, positional = store._fields_in(r["text"])
                self.assertEqual(positional, [])
                self.assertTrue(fields <= entry.fields, f"{entry.id}: {fields - entry.fields}")


class ByteOffsetLineSeparatorTest(unittest.TestCase):
    """`str.splitlines()` はU+2028/U+2029/U+000C/U+0085等もast/tokenizerと異なる
    基準で行区切りとみなすため、対象定数より前にこれらの文字があると
    `_byte_offsets` の行番号がastの`lineno`とずれる。修正前は self-check
    (`ast.literal_eval(segment) == value`) が失敗し `PromptSourceError` になっていた
    (データ破損はしない=fail-closedだが、正当な保存が理由不明のエラーになっていた)。
    """

    def test_line_separator_before_target_does_not_desync_offsets(self) -> None:
        source = (
            'PRECEDING = "line separator paragraphformfeednel"\n'
            'TARGET_CONST = """\\\n'
            "hello {name}\n"
            '"""\n'
        )
        located = store.locate(source, "TARGET_CONST")
        self.assertEqual(located.value, "hello {name}\n")

    def test_old_splitlines_based_offsets_would_have_failed_self_check(self) -> None:
        # 修正が実際に必要だったことを裏付ける: str.splitlines()方式に戻すと
        # 同じ入力で自己検証が失敗する。
        source = 'PRECEDING = "line separator"\nTARGET_CONST = """\\\nhello\n"""\n'

        def _broken_byte_offsets(src: str) -> list[int]:
            offsets = [0]
            for line in src.splitlines(keepends=True):
                offsets.append(offsets[-1] + len(line.encode("utf-8")))
            return offsets

        with mock.patch(
            "doci.admin.code_prompt_store._byte_offsets", side_effect=_broken_byte_offsets
        ):
            with self.assertRaises(store.PromptSourceError):
                store.locate(source, "TARGET_CONST")


class RenderLiteralTest(unittest.TestCase):
    def test_round_trips_plain_text(self) -> None:
        text = "こんにちは {name} です。\n"
        literal = store.render_literal(text)
        self.assertTrue(literal.startswith('"""\\\n'))

    def test_round_trips_text_with_backslash(self) -> None:
        text = "パスの例: C:\\\\Users\\\\test\n正規表現: \\\\d+\n"
        literal = store.render_literal(text)
        import ast

        self.assertEqual(ast.literal_eval(literal), text)

    def test_round_trips_text_containing_triple_quotes(self) -> None:
        text = 'コード例: """hello"""\n'
        literal = store.render_literal(text)
        import ast

        self.assertEqual(ast.literal_eval(literal), text)

    def test_round_trips_text_ending_with_quote(self) -> None:
        text = '最後が引用符"\n'
        literal = store.render_literal(text)
        import ast

        self.assertEqual(ast.literal_eval(literal), text)


class ValidateAdversarialTest(unittest.TestCase):
    def test_broken_brace_doubling_is_rejected(self) -> None:
        entry = reg.BY_ID["plan:_PROMPT"]
        text = store.read(entry.id)["text"]
        broken = text.replace("{{", "{", 1)
        v = store.validate(entry.id, broken)
        self.assertFalse(v.ok)

    def test_unknown_placeholder_is_rejected(self) -> None:
        entry = reg.BY_ID["ai_text:_ENGAGEMENT_COMMENT_PROMPT"]
        text = store.read(entry.id)["text"]
        v = store.validate(entry.id, text + "\n{totally_unknown_field}\n")
        self.assertFalse(v.ok)
        self.assertTrue(any("totally_unknown_field" in e for e in v.errors))

    def test_positional_placeholder_is_rejected(self) -> None:
        entry = reg.BY_ID["ai_text:_ENGAGEMENT_COMMENT_PROMPT"]
        text = store.read(entry.id)["text"]
        v = store.validate(entry.id, text + "\n{}\n")
        self.assertFalse(v.ok)

    def test_dropped_field_is_a_warning_not_an_error(self) -> None:
        entry = reg.BY_ID["ai_text:_ENGAGEMENT_COMMENT_PROMPT"]
        text = store.read(entry.id)["text"]
        dropped = text.replace("{narration_excerpt}", "")
        v = store.validate(entry.id, dropped)
        self.assertTrue(v.ok)
        self.assertTrue(any("narration_excerpt" in w for w in v.warnings))

    def test_guarded_by_produces_warning(self) -> None:
        entry = reg.BY_ID["factcheck:_PROMPT"]
        text = store.read(entry.id)["text"]
        v = store.validate(entry.id, text)
        self.assertTrue(any("test_viewer_segment_claims" in w for w in v.warnings))

    def test_unknown_const_id_raises(self) -> None:
        with self.assertRaises(store.PromptNotFoundError):
            store.validate("nope:_NOPE", "x")


class SaveIsolatedTest(unittest.TestCase):
    """実ファイルを一切触らず、合成ソースへの書き込みだけを検証する。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "doci").mkdir()
        (self.root / "output").mkdir()
        self.entry = reg.BY_ID["ai_text:_ENGAGEMENT_COMMENT_PROMPT"]
        # 対象定数の前に大量のマルチバイト文字を置く(バイトオフセット回帰テスト)。
        self.src = (
            "# -*- coding: utf-8 -*-\n"
            '"""これはマルチバイト文字だらけのドキュメント文字列です。日本語日本語日本語。"""\n'
            "from __future__ import annotations\n\n"
            'DUMMY_JAPANESE_CONST = "あいうえおかきくけこさしすせそたちつてと" * 5\n\n'
            f'{self.entry.name} = """\\\n'
            "こんにちは {corner_label} {title} {description} {narration_excerpt}\n"
            '"""\n\n'
            "def _after(): pass\n"
        )
        self.target_path = self.root / self.entry.relpath
        self.target_path.write_text(self.src, encoding="utf-8")
        self.root_patcher = mock.patch.object(config, "ROOT", self.root)
        self.output_patcher = mock.patch.object(config, "OUTPUT", self.root / "output")
        self.root_patcher.start()
        self.output_patcher.start()
        self.addCleanup(self.root_patcher.stop)
        self.addCleanup(self.output_patcher.stop)

    def test_save_splices_correctly_around_multibyte_prefix(self) -> None:
        r = store.read(self.entry.id)
        new_text = r["text"].replace("こんにちは", "こんばんは")
        result = store.save(self.entry.id, new_text, confirm_warnings=True, run_guarded_tests=False)
        self.assertTrue(result.ok, result.errors or result.error)
        written = self.target_path.read_text(encoding="utf-8")
        self.assertIn("こんばんは", written)
        self.assertIn("_after", written)  # 後続コードが壊れていない
        self.assertIn("DUMMY_JAPANESE_CONST", written)  # 前方の日本語も壊れていない
        # 書き込み後のファイルが再度正しく位置特定できる(自己検証込み)
        relocated = store.locate(written, self.entry.name)
        self.assertEqual(relocated.value, new_text)

    def test_save_creates_backup(self) -> None:
        r = store.read(self.entry.id)
        store.save(
            self.entry.id,
            r["text"].replace("こんにちは", "こんばんは"),
            confirm_warnings=True,
            run_guarded_tests=False,
        )
        backups = safeio.list_backups("code_prompt", self.entry.id)
        self.assertEqual(len(backups), 1)
        self.assertTrue(str(backups[0].path).startswith(str(self.root)))  # 実リポジトリへ漏れない

    def test_stale_fingerprint_returns_409_and_does_not_write(self) -> None:
        r = store.read(self.entry.id)
        result = store.save(
            self.entry.id,
            r["text"].replace("こんにちは", "こんばんは"),
            confirm_warnings=True,
            base_fingerprint="deadbeef",
        )
        self.assertEqual(result.code, 409)
        self.assertEqual(self.target_path.read_text(encoding="utf-8"), self.src)

    def test_invalid_placeholder_rejected_before_write(self) -> None:
        r = store.read(self.entry.id)
        before = self.target_path.read_text(encoding="utf-8")
        result = store.save(
            self.entry.id, r["text"] + "{oops}", confirm_warnings=True, run_guarded_tests=False
        )
        self.assertEqual(result.code, 400)
        self.assertEqual(self.target_path.read_text(encoding="utf-8"), before)

    def test_forced_bad_render_never_reaches_atomic_write(self) -> None:
        r = store.read(self.entry.id)
        with mock.patch(
            "doci.admin.code_prompt_store.render_literal",
            side_effect=store.PromptSourceError("forced failure"),
        ):
            with mock.patch("doci.admin.safeio.atomic_write_text") as mocked_write:
                result = store.save(
                    self.entry.id,
                    r["text"].replace("こんにちは", "こんばんは"),
                    confirm_warnings=True,
                    run_guarded_tests=False,
                )
        self.assertFalse(result.ok)
        mocked_write.assert_not_called()

    def test_guarded_tests_run_and_reported(self) -> None:
        entry = reg.BY_ID["tactic_backfill:_EXTRACT_PROMPT"]
        src = (
            f'{entry.name} = """\\\n'
            "narration: {narration}\n"
            '"""\n'
        )
        path = self.root / entry.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
        with mock.patch(
            "doci.admin.code_prompt_store._run_guarded_tests",
            return_value={"ok": True, "modules": list(entry.guarded_by), "output": "stub"},
        ) as mocked:
            result = store.save(entry.id, "narration: {narration}\nadded\n", confirm_warnings=True)
        self.assertTrue(result.ok, result.errors or result.error)
        mocked.assert_called_once()
        self.assertEqual(result.test_result["ok"], True)


if __name__ == "__main__":
    unittest.main()
