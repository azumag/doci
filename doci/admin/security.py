"""ローカル限定・認証なしUIの最低限の安全対策。

「認証なし」は操作者にパスワードを求めないという意味であり、loopbackポートへ
到達不能という意味ではない。ここでの対策はユーザー認証の代替ではなく、
ブラウザで開いている他タブ/他サイトからの無断書き込み(CSRF)・パストラバーサル
を防ぐための最低限のガード。
"""
from __future__ import annotations

import hashlib
import re
import secrets as _secrets
from urllib.parse import urlsplit

# --- secret判定 ---

_KNOWN_SECRET_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENCODE_GO_API_KEY",
        "PEXELS_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "MINIMAX_API_KEY",
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
        "INSTAGRAM_ACCESS_TOKEN",
    }
)

# 値ではなくファイルパスを指すキー。サフィックスだけ見ると秘密情報に見えるが、
# 実体は「どのファイルを読むか」の設定なので秘密値として扱わない
# （ファイル自体は .gitignore で守られ、admin UI からは触れない）。
_PATH_KEYS = frozenset(
    {
        "YOUTUBE_CLIENT_SECRET_FILE",
        "YOUTUBE_TOKEN_FILE",
        "YOUTUBE_ANALYTICS_TOKEN_FILE",
        "TIKTOK_TOKEN_FILE",
        "OPENCODE_AUTH_FILE",
    }
)

_SECRET_SUFFIXES = (
    "_API_KEY",
    "_SECRET",
    "_TOKEN",
    "_ACCESS_TOKEN",
    "_CLIENT_KEY",
    "_PASSWORD",
)


def is_secret(key: str) -> bool:
    """fail-closed: 将来追加される `FOO_API_KEY` 等も自動でマスク対象になる。"""
    if key in _PATH_KEYS:
        return False
    if key in _KNOWN_SECRET_KEYS:
        return True
    return any(key.endswith(suffix) for suffix in _SECRET_SUFFIXES)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def scrub(text: str, secret_values: list[str]) -> str:
    """レスポンスへ返す前に、既知の秘密値そのものをマスクする最終防御。"""
    out = text
    for value in secret_values:
        if value:
            out = out.replace(value, "***")
    return out


# --- CSRF / Host / Origin ---


def make_token() -> str:
    return _secrets.token_urlsafe(32)


def check_host(host_header: str | None, expected_host: str, expected_port: int) -> bool:
    if not host_header:
        return False
    hostname = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    if hostname not in {"127.0.0.1", "localhost"}:
        return False
    if hostname != expected_host and expected_host not in {"127.0.0.1", "localhost"}:
        return False
    port_part = host_header.rsplit(":", 1)[1] if ":" in host_header else ""
    if port_part and port_part != str(expected_port):
        return False
    return True


def check_origin(origin_header: str | None, expected_port: int) -> bool:
    """書き込み系メソッドでのみ呼ぶ。Originヘッダが無いリクエスト(非ブラウザCLI等)は許可する。"""
    if not origin_header:
        return True
    parts = urlsplit(origin_header)
    if parts.hostname not in {"127.0.0.1", "localhost"}:
        return False
    # `parts.port` はOriginにポートが明示されていない場合Noneになる(その場合は
    # スキームの既定ポート80/443が暗黙のポート)。`if parts.port and ...`だと
    # Noneの場合に条件全体が偽になりポート検証そのものがスキップされてしまい、
    # `Origin: http://127.0.0.1`(ポート省略=80番のつもり)がadminサーバの実際の
    # 待受ポート(既定8787)と一致しなくても通ってしまう実バグがあった
    # (リポジトリ側Claude Actionのレビューで指摘・実際に再現して確認した)。
    # 省略時は既定ポートとして扱い、必ず比較する。
    origin_port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    if origin_port != expected_port:
        return False
    return True


def check_token(header_value: str | None, expected_token: str) -> bool:
    if not header_value:
        return False
    return _secrets.compare_digest(header_value, expected_token)


# --- 静的ファイル配信 ---

STATIC_WHITELIST = frozenset({"index.html", "app.js", "app.css"})


def resolve_static_name(name: str) -> str | None:
    if name in STATIC_WHITELIST:
        return name
    return None


_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_valid_env_key(key: str) -> bool:
    return bool(_KEY_RE.match(key))
