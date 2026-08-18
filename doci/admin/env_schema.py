"""`.env` の各キーの型・選択肢・説明文を、値を二重管理せず収集する。

型(`kind`)は `doci/config.py` を AST 走査して `get_bool`/`get_int`/`get_float`/`get`
呼び出しの第1引数(キー名文字列)から機械的に判定する。選択肢(`choices`)は
`config.py` が実際に検証で使っている `_SUPPORTED_*` frozenset をそのまま
`getattr` で参照する（値のコピーは作らない）。説明文(`doc`)は `.env.example` の
該当キー直前の連続コメント行から抽出する。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .. import config
from . import security

_KIND_BY_CALL = {"get_bool": "bool", "get_int": "int", "get_float": "float", "get": "str"}

# 実際に config.validate_pipeline_backends() が使う frozenset を参照するだけで、
# 選択肢の値そのものは一切ここに書き写さない。
_CHOICES_ATTR_BY_KEY = {
    "TEXT_BACKEND": "_SUPPORTED_TEXT_BACKENDS",
    "RESEARCH_BACKEND": "_SUPPORTED_PIPELINE_BACKENDS",
    "FACTCHECK_BACKEND": "_SUPPORTED_PIPELINE_BACKENDS",
    "CHART_BG_BACKEND": "_SUPPORTED_PIPELINE_BACKENDS",
    "CODEX_PROVIDER": "_SUPPORTED_CODEX_PROVIDERS",
    "CODEX_REASONING_EFFORT": "_SUPPORTED_CODEX_REASONING_EFFORTS",
}


@dataclass(frozen=True)
class EnvKeySchema:
    key: str
    kind: str  # "bool" | "int" | "float" | "str"
    choices: tuple[str, ...]
    doc: str
    known: bool  # config.py が実際に読んでいるキーか
    secret: bool


def _harvest_kinds(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    kinds: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
            continue
        kind = _KIND_BY_CALL.get(value.func.id)
        if not kind or not value.args:
            continue
        first = value.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            # 同じキーに複数回 kind が付く場合、より具体的な型(bool/int/float)を優先する。
            existing = kinds.get(first.value)
            if existing in (None, "str"):
                kinds[first.value] = kind
    return kinds


_DOC_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def _harvest_docs(env_example_text: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    pending: list[str] = []
    for raw in env_example_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            pending.append(stripped.lstrip("#").strip())
            continue
        if not stripped:
            pending = []
            continue
        m = _DOC_LINE_RE.match(stripped)
        if m and pending:
            docs[m.group(1)] = "\n".join(p for p in pending if p)
        pending = []
    return docs


def build_schema() -> dict[str, EnvKeySchema]:
    config_source = Path(config.__file__).read_text(encoding="utf-8")
    kinds = _harvest_kinds(config_source)
    example_path = config.ROOT / ".env.example"
    docs = (
        _harvest_docs(example_path.read_text(encoding="utf-8"))
        if example_path.is_file()
        else {}
    )
    keys = set(kinds) | set(docs)
    schema: dict[str, EnvKeySchema] = {}
    for key in sorted(keys):
        choices: tuple[str, ...] = ()
        attr = _CHOICES_ATTR_BY_KEY.get(key)
        if attr is not None and hasattr(config, attr):
            choices = tuple(sorted(getattr(config, attr)))
        schema[key] = EnvKeySchema(
            key=key,
            kind=kinds.get(key, "str"),
            choices=choices,
            doc=docs.get(key, ""),
            known=key in kinds,
            secret=security.is_secret(key),
        )
    return schema
