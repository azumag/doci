"""Codex exec と旧Claude CLIの薄いラッパ、JSON抽出（リサーチ/ファクトチェック共通）。

Codex exec は明示設定時の経路。`claude -p ... --output-format json` は旧設定を明示した場合だけ叩く。Web検索が要る段は
`allowed_tools=["WebSearch","WebFetch"]` を渡す（print モードで実検索が走ることを実測確認済）。
`run_codex` は本番バックエンド（codex exec + MiniMax-M3 等）で、隔離 CODEX_HOME 配下の
sandbox からシェル(curl等)でWeb検索/取得させる。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import config


def run_claude(
    prompt: str,
    model: str,
    allowed_tools: list[str] | None = None,
    timeout: int | None = 240,
) -> str:
    """claude CLI を print モードで実行し、本文(result)文字列を返す。"""
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (rc={proc.returncode}): "
            f"stderr={proc.stderr[:500]} stdout={proc.stdout[:500]}"
        )
    out = proc.stdout.strip()
    # --output-format json は {"type":"result","result":"...",...} を返す
    try:
        env = json.loads(out)
        if isinstance(env, dict) and "result" in env:
            return env["result"]
    except json.JSONDecodeError:
        pass
    return out


_CODEX_CONFIG_TOML = """\
model = "{model}"
model_provider = "minimax"
approval_policy = "never"
sandbox_mode = "read-only"

[model_providers.minimax]
name = "MiniMax"
base_url = "{base_url}"
env_http_headers = {{ Authorization = "DOCI_MINIMAX_AUTHORIZATION" }}
wire_api = "responses"
"""

# command_execution の command にこれらが含まれれば「実際にWeb取得を試みた」とみなす。
_WEB_FETCH_RE = re.compile(r"curl|wget|https?://", re.IGNORECASE)


def _ensure_codex_home(model: str) -> Path:
    """隔離 CODEX_HOME を用意し、MiniMax 用 config.toml を毎回上書き生成する。
    ユーザーの ~/.codex には一切触れない（ChatGPTログイン破壊事故を避けるため）。"""
    if not config.MINIMAX_API_KEY:
        raise RuntimeError("MINIMAX_API_KEY が未設定です（codex バックエンドには必須）")
    home = config.CODEX_HOME
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.toml"
    cfg_path.write_text(
        _CODEX_CONFIG_TOML.format(
            model=model,
            base_url=config.CODEX_MINIMAX_BASE_URL,
        ),
        encoding="utf-8",
    )
    os.chmod(cfg_path, 0o600)
    return home


def _parse_codex_events(stdout: str) -> tuple[str, int]:
    """codex exec --json の JSONL 出力から (最終 agent_message の text, web fetch数) を取り出す。
    不正な行はスキップする。web fetch数は command_execution(completed)のうち command に
    curl/wget/URL が含まれる件数で、「検索したフリ」検出に使う。"""
    last_message = ""
    fetch_count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "item.completed":
            continue
        item = ev.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "agent_message":
            last_message = item.get("text", "") or ""
        elif item_type == "command_execution":
            command = item.get("command") or ""
            if _WEB_FETCH_RE.search(command):
                fetch_count += 1
    return last_message, fetch_count


def run_codex(prompt: str, model: str, timeout: int | None = 600, min_web_fetches: int = 1) -> str:
    """codex exec (--json, MiniMax等) をヘッドレスで実行し、最終 agent_message の text を返す。

    隔離 CODEX_HOME(config.CODEX_HOME)を毎回用意して実行する（ユーザーの ~/.codex は不使用）。
    web fetch(curl/wget/URL実行)が min_web_fetches 未満なら「検索したフリ」とみなし ValueError
    にする（呼び出し側の既存リトライ/劣化継続に乗せる）。
    """
    home = _ensure_codex_home(model)
    scratch = config.OUTPUT / "codex-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.CODEX_BIN,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-m",
        model,
        "-",
    ]
    env = {
        **os.environ,
        "CODEX_HOME": str(home),
        "DOCI_MINIMAX_AUTHORIZATION": f"Bearer {config.MINIMAX_API_KEY}",
    }
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(scratch),
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"codex exec failed (rc={proc.returncode}): {proc.stderr[:500]}")
    message, fetch_count = _parse_codex_events(proc.stdout)
    if not message.strip():
        raise RuntimeError(f"codex exec が空の応答を返しました（stdout先頭500字）: {proc.stdout[:500]}")
    if fetch_count < min_web_fetches:
        raise ValueError(
            f"Web取得が確認できません（{fetch_count}件 < {min_web_fetches}件）。"
            "検索した体裁だけで内部知識のみで回答した疑いがあります。"
        )
    return message


def extract_json(text: str) -> dict:
    """モデル出力から最初の JSON オブジェクトを取り出す（コードフェンス耐性あり）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    if start < 0:
        raise ValueError(f"JSON object not found in output:\n{text[:500]}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("Unbalanced JSON braces in model output")
