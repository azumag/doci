"""claude CLI の薄いラッパと JSON 抽出（台本リサーチ/ファクトチェック共通）。

`claude -p ... --output-format json` をヘッドレスで叩く。Web検索が要る段は
`allowed_tools=["WebSearch","WebFetch"]` を渡す（print モードで実検索が走ることを実測確認済）。
"""
from __future__ import annotations

import json
import subprocess


def run_claude(
    prompt: str,
    model: str,
    allowed_tools: list[str] | None = None,
    timeout: int = 240,
) -> str:
    """claude CLI を print モードで実行し、本文(result)文字列を返す。"""
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {proc.stderr[:500]}")
    out = proc.stdout.strip()
    # --output-format json は {"type":"result","result":"...",...} を返す
    try:
        env = json.loads(out)
        if isinstance(env, dict) and "result" in env:
            return env["result"]
    except json.JSONDecodeError:
        pass
    return out


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
