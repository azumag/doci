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

from . import config, corners, llm

REQUIRED_KEYS = ("title", "description", "tags", "narration", "scenes")

# 互換用エイリアス（JSON抽出/CLI実行は共通モジュール llm に集約）
_extract_json = llm.extract_json


def _run_claude_cli(prompt: str, model: str) -> str:
    return llm.run_claude(prompt, model, timeout=240)


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


def _run_opencode(prompt: str, model: str, agent: str) -> str:
    cmd = ["opencode", "run"]
    if model:
        cmd += ["-m", model]
    elif agent:
        cmd += ["--agent", agent]
    else:
        raise RuntimeError(
            "OPENCODE_MODEL か OPENCODE_AGENT のどちらかを設定してください (TEXT_BACKEND=opencode)"
        )
    # opencode はエージェント動作でカレントにファイルを書くことがあるため、
    # 使い捨ての作業ディレクトリに隔離する（生成物の repo 汚染を防ぐ）。
    scratch = config.OUTPUT / ".opencode_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    cmd += ["--dir", str(scratch), prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
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
        return _run_opencode(prompt, config.OPENCODE_MODEL, config.OPENCODE_AGENT)
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


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


def generate(corner: corners.Corner, day: str, past_topics: list[str]) -> dict:
    # 1) 前段リサーチ（issue #6）: 題材選定＋Web裏取り。失敗してもリサーチ無しで続行。
    research = None
    if config.SCRIPT_RESEARCH:
        from . import research as research_mod

        _log("前段リサーチ (claude+Web)…")
        try:
            research = research_mod.web_research(corner, past_topics)
            if research:
                _log(f"題材: {research.get('topic', '')} / 裏取り事実 {len(research.get('facts', []))}件")
        except Exception as e:  # noqa: BLE001
            _log(f"リサーチ失敗→リサーチ無しで続行: {e}")
            research = None

    # 2) 下書き（minimax-m3 等）。リサーチがあれば具体を織り込ませる。
    #    minimax は稀に不完全JSON（narration/scenes 欠落・分割）を返すため再生成で吸収。
    prompt = corners.build_prompt(corner, day, past_topics, research=research)
    script = None
    last_err: Exception | None = None
    for attempt in range(1, config.SCRIPT_DRAFT_RETRIES + 1):
        try:
            script = _validate(_extract_json(_dispatch(prompt)))
            break
        except ValueError as e:  # JSON抽出/必須キー不足（JSONDecodeError含む）
            last_err = e
            _log(f"下書きJSON不良(試行{attempt}/{config.SCRIPT_DRAFT_RETRIES})→再生成: {e}")
    if script is None:
        raise RuntimeError(f"下書きが規定回数で揃いませんでした: {last_err}")

    # 3) 後段ファクトチェック（issue #6）: 別モデル(opus)＋Web検証で narration を自動修正。
    if config.SCRIPT_FACTCHECK:
        from . import factcheck

        _log("後段ファクトチェック (opus+Web)…")
        try:
            fc = factcheck.verify_and_correct(script["narration"], research)
            if fc and fc.get("narration", "").strip():
                issues = fc.get("issues") or []
                if fc.get("changed") and issues:
                    _log(f"ファクトチェック: {len(issues)}件修正")
                script["narration"] = fc["narration"].strip()
                script["_factcheck"] = issues
        except Exception as e:  # noqa: BLE001
            _log(f"ファクトチェック失敗→修正なしで続行: {e}")

    script["_corner"] = corner.key
    script["_speaker"] = corner.speaker
    script["_date"] = day
    if research:
        script["_research"] = research
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
