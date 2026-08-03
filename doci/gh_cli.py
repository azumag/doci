"""gh CLIの薄い共有ラッパー。既存認証のみ使用し、出力からsecret形状を除去する。"""
from __future__ import annotations

import re
import subprocess

_SECRET_RE = re.compile(
    r"(?:github_pat_|gh[opsu]_|sk-ant-|ya29\.)[A-Za-z0-9_.-]{12,}"
    r"|Bearer\s+\S+",
    re.IGNORECASE,
)


def redact(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", value)


def run_gh(
    args: list[str],
    *,
    stdin: str | None = None,
    timeout: int = 60,
) -> str:
    """ghの既存認証だけを使う。トークンを引数・ログ・ファイルへ渡さない。"""
    proc = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = redact((proc.stderr or proc.stdout).strip())[:600]
        raise RuntimeError(f"GitHub操作に失敗しました (rc={proc.returncode}): {detail}")
    return proc.stdout.strip()
