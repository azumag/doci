""".env の読込・コメント保持パッチ・保存前サブプロセス検証・保存。

`config._load_dotenv`（`doci/config.py`）のパース挙動 — `setdefault`（重複キーは
最初の出現が勝つ）・行内コメントを剥がさない・値の前後空白と一重/二重引用符だけを
`strip()` する — を厳密に再現する。dictへ変換して丸ごとダンプし直す方式は
`.env`/`.env.example` の大量のコメント（各設定の理由説明）を破壊するため取らない。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .. import config
from . import env_schema, safeio, security

# 検証サブプロセスに渡す PYTHONPATH/cwd は、doci パッケージが実際に置かれている
# 物理的な場所を指す必要がある。`config.ROOT` を使うと、テストがデータ置き場を
# 一時ディレクトリへ差し替えるために `config.ROOT` を patch した途端、サブプロセスが
# `doci` パッケージ自体を見失って import に失敗する（実際に単体テストで再現した:
# rc=1で検証プロセスが何も出力を返さなくなった）。本番では config.ROOT は常にこの
# パッケージの物理配置と一致するため、動作は変わらない。
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent


class EnvValueError(ValueError):
    """候補の値が .env のパーサへ書いても元通りに読み戻せない場合。"""


@dataclass(frozen=True)
class EnvEntry:
    key: str
    value: str | None  # secretはNone（値は絶対に外へ出さない）
    line_no: int | None
    enabled: bool
    is_secret: bool
    is_set: bool
    fingerprint: str
    kind: str
    choices: tuple[str, ...]
    doc: str
    known: bool


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    error: str
    warnings: list[str]
    channels: list[str]
    detail: str = ""


@dataclass(frozen=True)
class SaveResult:
    ok: bool
    error: str
    warnings: list[str]
    code: int
    needs_confirmation: bool = False
    detail: str = ""
    fingerprint: str = ""


def env_path() -> Path:
    return config.ROOT / ".env"


def read_env_text() -> str:
    path = env_path()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- config._load_dotenv と同一のパース規則 ---


def _effective_assignment(raw: str) -> tuple[str, str] | None:
    """`config._load_dotenv` が「有効な代入行」とみなす行だけ (key, raw_value) を返す。"""
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, val = line.partition("=")
    return key.strip(), val


def parse_value(raw_value: str) -> str:
    return raw_value.strip().strip('"').strip("'")


_COMMENTED_ASSIGN_RE = re.compile(r"^#+\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _commented_assignment(raw: str) -> tuple[str, str] | None:
    m = _COMMENTED_ASSIGN_RE.match(raw.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def encode_value(value: str) -> str:
    """このまま `.env` に書いた場合に `parse_value()` で元通り読み戻せることを保証する。"""
    if "\n" in value or "\r" in value:
        raise EnvValueError("値に改行は含められません（.envは1設定1行のパーサです）")
    if value != value.strip():
        raise EnvValueError("先頭/末尾の空白は .env のパーサで失われます")
    # `value[:1] in "\"'"` は空文字列に対して常にTrueを返してしまう(空文字列は
    # どんな文字列の部分文字列でもあるため)。空値へのクリアが常にエラーになって
    # いたのを実際に確認した。startswith/endswithで判定し直す。
    if value.startswith(('"', "'")) or value.endswith(('"', "'")):
        raise EnvValueError("先頭/末尾の引用符は .env のパーサで剥がされます")
    if parse_value(value) != value:
        raise EnvValueError("この値は .env に書いても元通りには読み戻せません")
    return value


def _line_ending(raw: str) -> str:
    if raw.endswith("\r\n"):
        return "\r\n"
    if raw.endswith("\n"):
        return "\n"
    return ""


# --- 読込 ---


def read_entries(text: str | None = None) -> list[EnvEntry]:
    if text is None:
        text = read_env_text()
    schema = env_schema.build_schema()
    entries: dict[str, EnvEntry] = {}
    for idx, raw in enumerate(text.splitlines()):
        eff = _effective_assignment(raw)
        if eff is not None:
            key, raw_value = eff
            if key in entries and entries[key].enabled:
                continue  # 最初の有効な出現が勝つ（config._load_dotenvのsetdefaultと同じ）
            entries[key] = _build_entry(key, parse_value(raw_value), idx, True, schema)
            continue
        commented = _commented_assignment(raw)
        if commented is not None:
            key, raw_value = commented
            if key in entries:
                continue  # 有効な行が別にあればそちらを優先表示
            entries[key] = _build_entry(key, parse_value(raw_value), idx, False, schema)

    for key, meta in schema.items():
        if key not in entries:
            entries[key] = EnvEntry(
                key=key,
                value=None,
                line_no=None,
                enabled=False,
                is_secret=meta.secret,
                is_set=False,
                fingerprint="",
                kind=meta.kind,
                choices=meta.choices,
                doc=meta.doc,
                known=meta.known,
            )
    return sorted(entries.values(), key=lambda e: e.key)


def _build_entry(
    key: str, value: str, line_no: int, enabled: bool, schema: dict[str, env_schema.EnvKeySchema]
) -> EnvEntry:
    meta = schema.get(key)
    is_secret = security.is_secret(key)
    return EnvEntry(
        key=key,
        value=None if is_secret else value,
        line_no=line_no,
        enabled=enabled,
        is_secret=is_secret,
        is_set=enabled,
        fingerprint=security.fingerprint(value) if value else "",
        kind=meta.kind if meta else "str",
        choices=meta.choices if meta else (),
        doc=meta.doc if meta else "",
        known=meta.known if meta else False,
    )


# --- パッチ ---


def apply_patch(
    text: str, changes: dict[str, str | None], enable: Iterable[str] = ()
) -> tuple[str, list[str]]:
    """コメント・行順序を保ったまま指定キーだけを書き換える。

    changes: key -> 新しい値（Noneはコメントアウトして無効化＝復元可能な削除）。
    enable: 値変更はせず、既にコメントアウトされているキーを有効化する対象。
    戻り値: (新テキスト, warnings)。エラーは EnvValueError を送出する。
    """
    remaining = dict(changes)
    enable_set = set(enable)
    warnings: list[str] = []
    seen_active: set[str] = set()
    out: list[str] = []

    for raw in text.splitlines(keepends=True):
        ending = _line_ending(raw)
        eff = _effective_assignment(raw)
        if eff is not None:
            key, original_raw_value = eff
            if key in seen_active:
                warnings.append(f"{key}: .env に複数の有効な代入行があります（最初の行だけが実際に使われます）")
                out.append(raw)
                continue
            seen_active.add(key)
            if key in remaining:
                new_value = remaining.pop(key)
                if new_value is None:
                    body = raw[: len(raw) - len(ending)] if ending else raw
                    out.append(f"#{body}{ending}")
                else:
                    if "#" in original_raw_value and "#" not in new_value:
                        # 行内コメントは.envのパーサが値の一部として読んでしまうため
                        # (config._load_dotenvはコメントを剥がさない)、値を書き換える
                        # と元の行内コメントは失われる。実際に確認した挙動なので、
                        # 気付けるよう警告する。
                        warnings.append(
                            f"{key}: 元の行にあった行内コメント（.envは値の一部として"
                            "扱うため、値を書き換えると失われます）"
                        )
                    out.append(f"{key}={encode_value(new_value)}{ending}")
                continue
            out.append(raw)
            continue

        commented = _commented_assignment(raw)
        if commented is not None:
            key, _ = commented
            if key in seen_active:
                out.append(raw)
                continue
            if key in remaining:
                new_value = remaining.pop(key)
                if new_value is not None:
                    out.append(f"{key}={encode_value(new_value)}{ending}")
                    seen_active.add(key)
                else:
                    out.append(raw)  # 既に無効な行を再度Noneにしても変化なし
                enable_set.discard(key)
                continue
            if key in enable_set:
                _, raw_value = commented
                out.append(f"{key}={encode_value(parse_value(raw_value))}{ending}")
                seen_active.add(key)
                enable_set.discard(key)
                continue
            out.append(raw)
            continue

        out.append(raw)

    for key in sorted(enable_set):
        warnings.append(f"{key}: 有効化対象のコメントアウト行が見つかりませんでした")

    new_keys = {k: v for k, v in remaining.items() if v is not None}
    skipped_delete = [k for k, v in remaining.items() if v is None]
    for key in sorted(skipped_delete):
        warnings.append(f"{key}: 削除対象のキーが .env に見つかりませんでした（何もしていません）")

    result = "".join(out)
    if new_keys:
        if result and not result.endswith("\n"):
            result += "\n"
        footer_marker = "# --- doci admin UI が追加したキー ---"
        if footer_marker not in result:
            result += f"\n{footer_marker}\n"
        for key in sorted(new_keys):
            result += f"{key}={encode_value(new_keys[key])}\n"

    for key, value in changes.items():
        if value is not None and "#" in value:
            warnings.append(
                f"{key}: 値に '#' を含んでいます。.env のパーサは行内コメントを剥がさないため、"
                "この文字列全体が値として使われます。"
            )

    return result, warnings


# --- 検証（サブプロセス） ---


def _secret_values_in_text(text: str) -> list[str]:
    values: list[str] = []
    for raw in text.splitlines():
        eff = _effective_assignment(raw)
        if eff is None:
            continue
        key, raw_value = eff
        if security.is_secret(key):
            value = parse_value(raw_value)
            if value:
                values.append(value)
    return values


def validate_candidate(text: str, *, timeout: float = 30.0) -> ValidationResult:
    secret_values = _secret_values_in_text(text)
    with tempfile.TemporaryDirectory() as td:
        candidate_path = Path(td) / "candidate.env"
        candidate_path.write_text(text, encoding="utf-8")
        candidate_path.chmod(0o600)
        # tools/cron_generate.sh が実際にexportする環境と同じ、PATH/HOMEのみの最小環境。
        # os.environを継承すると setdefault の実行順序上、継承側の値が優先され
        # 候補ではなく実環境を検証してしまう。
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": str(_PACKAGE_ROOT),
            "DOCI_DOTENV": str(candidate_path),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "doci.admin.env_validate_child"],
                cwd=str(_PACKAGE_ROOT),
                env=child_env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(ok=False, error="検証がタイムアウトしました", warnings=[], channels=[])

    stdout = security.scrub(proc.stdout.strip(), secret_values)
    stderr = security.scrub(proc.stderr.strip(), secret_values)
    if not stdout:
        return ValidationResult(
            ok=False,
            error=f"検証プロセスが出力を返しませんでした (rc={proc.returncode})",
            warnings=[],
            channels=[],
            detail=stderr[-4000:],
        )
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return ValidationResult(
            ok=False,
            error="検証プロセスの出力を解釈できませんでした",
            warnings=[],
            channels=[],
            detail=(stdout + "\n" + stderr)[-4000:],
        )
    if payload.get("ok"):
        return ValidationResult(
            ok=True,
            error="",
            warnings=list(payload.get("warnings", [])),
            channels=list(payload.get("channels", [])),
        )
    return ValidationResult(
        ok=False,
        error=str(payload.get("error", "検証に失敗しました")),
        warnings=[],
        channels=[],
        detail=stderr[-4000:],
    )


# --- 保存 ---


def save(
    changes: dict[str, str | None],
    *,
    enable: Iterable[str] = (),
    confirm_warnings: bool = False,
    base_fingerprint: str | None = None,
) -> SaveResult:
    with safeio.surface_lock("env"):
        current_text = read_env_text()
        current_fp = content_fingerprint(current_text)
        if base_fingerprint is not None and base_fingerprint != current_fp:
            return SaveResult(
                ok=False,
                error="保存の直前に .env が別の変更で更新されていました。最新の内容を読み込み直してください。",
                warnings=[],
                code=409,
            )
        for key in changes:
            if not security.is_valid_env_key(key):
                return SaveResult(ok=False, error=f"不正なキー名です: {key}", warnings=[], code=400)
        try:
            candidate, patch_warnings = apply_patch(current_text, changes, enable)
        except EnvValueError as exc:
            return SaveResult(ok=False, error=str(exc), warnings=[], code=400)

        if patch_warnings and not confirm_warnings:
            return SaveResult(
                ok=False, error="", warnings=patch_warnings, code=409, needs_confirmation=True
            )

        validation = validate_candidate(candidate)
        if not validation.ok:
            return SaveResult(
                ok=False, error=validation.error, warnings=[], code=400, detail=validation.detail
            )

        safeio.backup(env_path(), surface="env", name="env")
        safeio.atomic_write_text(env_path(), candidate, mode=0o600)
        new_fp = content_fingerprint(candidate)
        return SaveResult(
            ok=True,
            error="",
            warnings=validation.warnings + patch_warnings,
            code=200,
            fingerprint=new_fp,
        )
