"""台本生成（opus 4.8）。

Minimax は文章生成に使わない（方針）。
バックエンド:
  - claude_cli (既定/ローカル): 認証済みの `claude` CLI を print モードで呼ぶ
  - anthropic        (クラウド): Anthropic API (ANTHROPIC_API_KEY) を直叩き
  - opencode         (代替):     `opencode run --agent ...`
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date as _date

from . import config, corners

REQUIRED_KEYS = ("title", "description", "tags", "narration", "scenes")


def _extract_json(text: str) -> dict:
    """モデル出力から最初の JSON オブジェクトを取り出す。"""
    text = text.strip()
    # コードフェンス除去
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


def _run_claude_cli(prompt: str, model: str) -> str:
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
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


def _run_anthropic(prompt: str, model: str) -> str:
    import urllib.request

    key = config.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です (TEXT_BACKEND=anthropic)")
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []))


def _run_opencode(prompt: str, agent: str) -> str:
    if not agent:
        raise RuntimeError("OPENCODE_AGENT が未設定です (TEXT_BACKEND=opencode)")
    proc = subprocess.run(
        ["opencode", "run", "--agent", agent, prompt],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"opencode failed (rc={proc.returncode}): {proc.stderr[:500]}")
    return proc.stdout


def _dispatch(prompt: str) -> str:
    backend = config.TEXT_BACKEND
    model = config.TEXT_MODEL
    if backend == "claude_cli":
        return _run_claude_cli(prompt, model)
    if backend == "anthropic":
        return _run_anthropic(prompt, model)
    if backend == "opencode":
        return _run_opencode(prompt, config.OPENCODE_AGENT)
    raise ValueError(f"unknown TEXT_BACKEND: {backend}")


def _validate(script: dict) -> dict:
    for k in REQUIRED_KEYS:
        if k not in script:
            raise ValueError(f"生成JSONに必須キー '{k}' がありません: {list(script)}")
    if not isinstance(script["scenes"], list) or not script["scenes"]:
        raise ValueError("scenes が空です")
    for s in script["scenes"]:
        s.setdefault("caption", "")
        s.setdefault("visual_prompt", "")
        s.setdefault("motion", "")
    if isinstance(script["tags"], str):
        script["tags"] = [t.strip() for t in script["tags"].split(",") if t.strip()]
    return script


def generate(corner: corners.Corner, day: str, past_topics: list[str]) -> dict:
    prompt = corners.build_prompt(corner, day, past_topics)
    raw = _dispatch(prompt)
    script = _validate(_extract_json(raw))
    script["_corner"] = corner.key
    script["_speaker"] = corner.speaker
    script["_date"] = day
    return script


def main() -> None:
    ap = argparse.ArgumentParser(description="台本生成 (opus 4.8)")
    ap.add_argument("--corner", choices=list(corners.CORNERS), default="communism")
    ap.add_argument("--date", default=_date.today().isoformat())
    args = ap.parse_args()
    corner = corners.CORNERS[args.corner]
    script = generate(corner, args.date, past_topics=[])
    print(json.dumps(script, ensure_ascii=False, indent=2))
    print(
        f"\n--- corner={corner.key} voice={corner.voice_key} speaker={corner.speaker} "
        f"narration_chars={len(script['narration'])} scenes={len(script['scenes'])} ---",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
