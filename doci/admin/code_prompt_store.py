"""Python内蔵プロンプト定数の安全な編集: AST位置特定によるバイト単位スプライス。

対象は `doci/admin/code_prompt_registry.py` に手書き登録された11個の定数のみ
（汎用探索はしない）。`ast` の `col_offset`/`end_col_offset` は UTF-8 **バイト**
オフセットであり文字オフセットではない — 日本語主体のソースでこれを取り違えると
文字化けするため、切り出した範囲を `ast.literal_eval()` で元の値と突き合わせてから
初めて安全とみなす（`locate()`）。書き込み前には (1) レンダリング往復チェック、
(2) 未知/位置指定プレースホルダの検出、(3) 実際のkwargでの `.format()` ドライラン、
(4) スプライス後ソース全体の構文チェック、を全てブロッキングで通す。
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import string as _string
import subprocess
import sys
from pathlib import Path

from .. import config
from . import code_prompt_registry, safeio

# guarded_byのテスト群は実際のtests/ディレクトリと実dociパッケージに対して
# `python -m unittest` するため、config.ROOT(テストが差し替えうる、対象定数の
# ファイル解決に使う値)ではなく、このパッケージが物理的に置かれている場所を
# 使う。config.ROOTを使うと、code_prompt_store自体のテストがconfig.ROOTを
# 一時ディレクトリへpatchした状況で万一 _run_guarded_tests が実行された場合に、
# 存在しないtests/ディレクトリへcdしてしまう（env_store.pyの_PACKAGE_ROOTと
# 同じ理由・同じ修正）。
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent


class PromptSourceError(ValueError):
    """定数の位置特定・レンダリングが自己検証に失敗した場合。"""


class PromptNotFoundError(KeyError):
    pass


@dataclasses.dataclass(frozen=True)
class Located:
    start: int  # UTF-8 バイトオフセット
    end: int
    value: str


@dataclasses.dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    format_preview: str


@dataclasses.dataclass(frozen=True)
class SaveResult:
    ok: bool
    error: str
    errors: list[str]
    warnings: list[str]
    code: int
    needs_confirmation: bool = False
    fingerprint: str = ""
    test_result: dict | None = None


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_entry(const_id: str) -> code_prompt_registry.PromptConstant:
    entry = code_prompt_registry.BY_ID.get(const_id)
    if entry is None:
        raise PromptNotFoundError(const_id)
    return entry


def _source_path(entry: code_prompt_registry.PromptConstant) -> Path:
    return config.ROOT / entry.relpath


def _byte_offsets(source: str) -> list[int]:
    """i番目 = i行目(0-indexed)の開始バイトオフセット。

    `str.splitlines()` は `\\n` 以外にも U+2028/U+2029/U+000C/U+0085 等を行区切り
    として分割してしまい、ast/tokenizerの行番号とずれる（実際に確認した）。
    このファイルは常に `path.read_text(encoding="utf-8")`（universal newlines）で
    読むため、実際に残る改行は `\\n` だけで良い。
    """
    offsets = [0]
    for line in source.split("\n"):
        offsets.append(offsets[-1] + len(line.encode("utf-8")) + 1)  # +1 は "\n" の1バイト
    return offsets


def locate(source: str, name: str) -> Located:
    tree = ast.parse(source)
    found = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != name:
            continue
        found = node
        break
    if found is None:
        raise PromptSourceError(f"モジュール直下に定数が見つかりません: {name}")
    value_node = found.value
    if not (isinstance(value_node, ast.Constant) and isinstance(value_node.value, str)):
        raise PromptSourceError(f"{name} は単一の文字列リテラルではありません")

    offsets = _byte_offsets(source)
    start = offsets[value_node.lineno - 1] + value_node.col_offset
    end = offsets[value_node.end_lineno - 1] + value_node.end_col_offset
    data = source.encode("utf-8")
    segment = data[start:end].decode("utf-8")
    try:
        literal_value = ast.literal_eval(segment)
    except (SyntaxError, ValueError) as exc:
        raise PromptSourceError(f"{name} の位置特定の自己検証に失敗しました: {exc}") from exc
    if literal_value != value_node.value:
        raise PromptSourceError(f"{name} の位置特定の自己検証に失敗しました（範囲が値と一致しません）")
    return Located(start=start, end=end, value=value_node.value)


def render_literal(text: str) -> str:
    """`text` を表す `\"\"\"...\"\"\"` 形式のPythonソース断片を生成する。

    既存の11定数は全て `\"\"\"\\` + 改行で始まる規約（開始直後の改行を潰す）なので
    それに合わせる。`repr()` は使わない — 数千文字の日本語プロンプトを1行に潰すと
    差分レビューが不可能になるため。生成後に `ast.literal_eval()` して元テキストと
    一致するか確認してから初めて返す。
    """
    body = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if body.endswith('"'):
        body = body[:-1] + '\\"'
    literal = '"""\\\n' + body + '"""'
    try:
        round_tripped = ast.literal_eval(literal)
    except (SyntaxError, ValueError) as exc:
        raise PromptSourceError(f"レンダリングした文字列リテラルが不正です: {exc}") from exc
    if not isinstance(round_tripped, str) or round_tripped != text:
        raise PromptSourceError("レンダリングした文字列リテラルの往復確認に失敗しました")
    return literal


def _fields_in(text: str) -> tuple[set[str], list[str]]:
    """`.format()` が参照するフィールド名集合と、位置指定/自動採番プレースホルダを返す。"""
    fields: set[str] = set()
    positional: list[str] = []
    try:
        parsed = list(_string.Formatter().parse(text))
    except ValueError as exc:
        raise PromptSourceError(f"波括弧の対応が取れていません: {exc}") from exc
    for _literal, field_name, _format_spec, _conversion in parsed:
        if field_name is None:
            continue
        base = field_name.split(".")[0].split("[")[0]
        if base == "" or base.isdigit():
            positional.append(field_name or "(空)")
        else:
            fields.add(base)
    return fields, positional


def read(const_id: str) -> dict:
    entry = _require_entry(const_id)
    source = _source_path(entry).read_text(encoding="utf-8")
    located = locate(source, entry.name)
    return {
        "id": entry.id,
        "relpath": entry.relpath,
        "name": entry.name,
        "text": located.value,
        "fields": sorted(entry.fields),
        "call_site": entry.call_site,
        "guarded_by": list(entry.guarded_by),
        "description": entry.description,
        "fingerprint": _fp(located.value),
    }


def validate(const_id: str, text: str) -> ValidationResult:
    entry = _require_entry(const_id)
    errors: list[str] = []
    warnings: list[str] = []

    try:
        render_literal(text)
    except PromptSourceError as exc:
        errors.append(str(exc))

    try:
        fields, positional = _fields_in(text)
    except PromptSourceError as exc:
        errors.append(str(exc))
        fields, positional = set(), []

    if positional:
        errors.append(
            "位置指定/自動採番のプレースホルダ({}や{0}等)は使えません: "
            + ", ".join(sorted(set(positional)))
        )
    unknown = fields - entry.fields
    if unknown:
        errors.append(f"未知のプレースホルダです: {', '.join(sorted(unknown))}")
    dropped = entry.fields - fields
    if dropped:
        warnings.append(
            "登録済みのプレースホルダが本文から削除されています: "
            + ", ".join(sorted(dropped))
            + "（生成時にこの入力が本文へ差し込まれなくなります）"
        )
    if entry.guarded_by:
        warnings.append(
            "この定数の内容をアサートする既存テストがあります: " + ", ".join(entry.guarded_by)
        )

    preview = ""
    if not errors:
        dummy = {name: f"DUMMY:{name}" for name in entry.fields}
        try:
            preview = text.format(**dummy)
        except Exception as exc:  # noqa: BLE001 - .format() 実行時エラーをそのまま報告する
            errors.append(f".format() のドライランに失敗しました: {type(exc).__name__}: {exc}")

    return ValidationResult(
        ok=not errors, errors=errors, warnings=warnings, format_preview=preview[:4000]
    )


def _run_guarded_tests(guarded_by: tuple[str, ...], *, timeout: float = 120.0) -> dict:
    modules = []
    for rel in guarded_by:
        parts = Path(rel).with_suffix("").parts
        modules.append(".".join(parts))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *modules],
            cwd=str(_PACKAGE_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "modules": modules, "output": "テスト実行がタイムアウトしました"}
    output = (proc.stdout + proc.stderr)[-4000:]
    return {"ok": proc.returncode == 0, "modules": modules, "output": output}


def save(
    const_id: str,
    text: str,
    *,
    confirm_warnings: bool = False,
    base_fingerprint: str | None = None,
    run_guarded_tests: bool = True,
) -> SaveResult:
    entry = _require_entry(const_id)
    path = _source_path(entry)

    with safeio.surface_lock("code_prompts"):
        source = path.read_text(encoding="utf-8")
        located = locate(source, entry.name)
        current_fp = _fp(located.value)
        if base_fingerprint is not None and base_fingerprint != current_fp:
            return SaveResult(
                ok=False,
                error="保存の直前に別の変更で更新されていました。最新の内容を読み込み直してください。",
                errors=[],
                warnings=[],
                code=409,
            )

        result = validate(const_id, text)
        if not result.ok:
            return SaveResult(
                ok=False, error="", errors=result.errors, warnings=result.warnings, code=400
            )
        if result.warnings and not confirm_warnings:
            return SaveResult(
                ok=False,
                error="",
                errors=[],
                warnings=result.warnings,
                code=409,
                needs_confirmation=True,
            )

        literal = render_literal(text)
        # located.start/.end はUTF-8バイトオフセット（astの col_offset と同じ単位）。
        # source(str)を文字インデックスとして直接スライスすると、対象より前に
        # 日本語などマルチバイト文字がある場合に位置がずれるため、必ずエンコード
        # したbytes上でスライスしてからデコードし直す。
        data = source.encode("utf-8")
        new_data = data[: located.start] + literal.encode("utf-8") + data[located.end :]
        new_source = new_data.decode("utf-8")
        try:
            compile(new_source, str(path), "exec")
        except SyntaxError as exc:
            return SaveResult(
                ok=False, error=f"構文チェックに失敗しました: {exc}", errors=[], warnings=[], code=400
            )

        relocated = locate(new_source, entry.name)
        if relocated.value != text:
            return SaveResult(
                ok=False,
                error="内部エラー: スプライス結果の自己検証に失敗しました。書き込みは行っていません。",
                errors=[],
                warnings=[],
                code=500,
            )

        safeio.backup(path, surface="code_prompt", name=entry.id)
        safeio.atomic_write_text(path, new_source)

        written = path.read_text(encoding="utf-8")
        written_located = locate(written, entry.name)
        if written_located.value != text:
            return SaveResult(
                ok=False,
                error="内部エラー: 書き込み後の内容が一致しません。バックアップから復元してください。",
                errors=[],
                warnings=[],
                code=500,
            )

        test_result = None
        if run_guarded_tests and entry.guarded_by:
            test_result = _run_guarded_tests(entry.guarded_by)
            if not test_result.get("ok", False):
                # guarded_by は「この定数の内容をアサートする既存テスト」であり、
                # 失敗はまさにこの書き込みが本番プロンプトを壊したことを意味する。
                # 以前はここで ok=True/code=200 を返しており、UIが先に「保存しました」
                # と表示しテスト失敗はその下に付記されるだけだった(自己レビューで
                # 未検出だったが、リポジトリ側Claude Actionのレビューで指摘され実際に
                # 再現した: guarded testが失敗しても書き込みは取り消されなかった)。
                # 直前に取ったバックアップを待たず、既に手元にある元のソース(source)
                # へ直接書き戻して自動的に取り消す(fail-closed。他の自己検証と同じ方針)。
                safeio.atomic_write_text(path, source)
                return SaveResult(
                    ok=False,
                    error="この定数の内容をアサートする既存テストが失敗したため、変更を取り消しました。",
                    errors=[],
                    warnings=result.warnings,
                    code=400,
                    test_result=test_result,
                )

        return SaveResult(
            ok=True,
            error="",
            errors=[],
            warnings=result.warnings,
            code=200,
            fingerprint=_fp(text),
            test_result=test_result,
        )
