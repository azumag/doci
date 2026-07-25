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
import re
import subprocess
from collections.abc import Callable
import sys
import time
import urllib.error
import urllib.request
from datetime import date as _date
from pathlib import Path

from . import channel, config, corners, llm
from .channel import ChannelSpec, CornerSpec

REQUIRED_KEYS = ("title", "description", "tags", "narration", "scenes")

# 互換用エイリアス（JSON抽出/CLI実行は共通モジュール llm に集約）
_extract_json = llm.extract_json


def _write_timeout() -> int | None:
    """台本執筆の待機上限。0以下は長文生成を途中で切らない無制限モード。"""
    return config.WRITE_LLM_TIMEOUT if config.WRITE_LLM_TIMEOUT > 0 else None


def _run_claude_cli(prompt: str, model: str) -> str:
    return llm.run_claude(prompt, model, timeout=_write_timeout())


def _run_anthropic(prompt: str, model: str) -> str:
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
    with urllib.request.urlopen(req, timeout=_write_timeout()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []))


def _opencode_go_key() -> str:
    """環境変数を優先し、無ければ OpenCode の既存認証ストアから Go の鍵を読む。"""
    if config.OPENCODE_GO_API_KEY:
        return config.OPENCODE_GO_API_KEY
    auth_file = Path(config.OPENCODE_AUTH_FILE).expanduser()
    try:
        auth = json.loads(auth_file.read_text(encoding="utf-8"))
        key = auth.get("opencode-go", {}).get("key", "")
    except (OSError, json.JSONDecodeError, AttributeError):
        key = ""
    if not key:
        raise RuntimeError(
            "OPENCODE_GO_API_KEY が未設定で、OpenCode認証ストアにも "
            f"opencode-go の鍵がありません: {auth_file}"
        )
    return key


def _run_opencode_go(prompt: str, model: str) -> str:
    """OpenCode CLIを介さず、OpenCode GoのAnthropic互換APIへ直接接続する。"""
    if not model:
        raise RuntimeError("OPENCODE_MODEL が未設定です (TEXT_BACKEND=opencode_go)")
    provider, sep, model_id = model.partition("/")
    if sep and provider != "opencode-go":
        raise RuntimeError(
            "TEXT_BACKEND=opencode_go では OPENCODE_MODEL を "
            "opencode-go/<model> 形式で指定してください"
        )
    if not sep:
        model_id = model
    body = json.dumps(
        {
            "model": model_id,
            "max_tokens": config.OPENCODE_GO_MAX_TOKENS,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{config.OPENCODE_GO_BASE_URL}/messages",
        data=body,
        headers={
            "x-api-key": _opencode_go_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            # Python標準UAはGoゲートウェイで403になるため、アプリ固有UAを明示する。
            "user-agent": "doci/1.0",
        },
    )
    started = time.monotonic()
    next_progress = 60.0
    text_parts: list[str] = []
    text_chars = 0
    stop_reason = ""
    try:
        with urllib.request.urlopen(req, timeout=_write_timeout()) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].lstrip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "error":
                    raise RuntimeError(f"OpenCode Go API error: {event.get('error', event)}")
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    part = delta.get("text", "")
                    text_parts.append(part)
                    text_chars += len(part)
                if event_type == "message_delta":
                    stop_reason = delta.get("stop_reason") or stop_reason
                elapsed = time.monotonic() - started
                if elapsed >= next_progress:
                    _log(f"Qwen直接API生成中 ({elapsed:.0f}s / 本文{text_chars}字)")
                    next_progress += 60.0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OpenCode Go API failed (HTTP {exc.code}): {detail}") from exc

    text = "".join(text_parts)
    if stop_reason == "max_tokens":
        raise RuntimeError(
            f"OpenCode Go API が max_tokens={config.OPENCODE_GO_MAX_TOKENS} に達しました"
        )
    if not text.strip():
        raise RuntimeError(f"OpenCode Go API が空の本文を返しました (stop_reason={stop_reason or 'unknown'})")
    _log(f"Qwen直接API完了 ({time.monotonic() - started:.1f}s / 本文{len(text)}字)")
    return text


def _run_opencode(prompt: str, model: str, agent: str) -> str:
    if not model and not agent:
        raise RuntimeError(
            "OPENCODE_MODEL か OPENCODE_AGENT のどちらかを設定してください (TEXT_BACKEND=opencode)"
        )
    # 呼び出し元がカスタムエージェントを明示していればそれを優先。未指定（既定）の場合は
    # 下で opencode.json に定義する最小エージェント doci-write を使う。
    # -m（モデル指定）と --agent（エージェント指定）は併用可能な CLI 仕様のため、
    # model の有無に関わらず必ず --agent を付ける。
    effective_agent = agent or "doci-write"
    cmd = ["opencode", "run"]
    if model:
        cmd += ["-m", model]
    cmd += ["--agent", effective_agent]
    # opencode はエージェント動作でカレントにファイルを書くことがあるため、
    # 使い捨ての作業ディレクトリに隔離する（生成物の repo 汚染を防ぐ）。
    scratch = config.OUTPUT / ".opencode_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    # ヘッドレス(無人)実行では build エージェントの "ask" 権限(doom_loop /
    # external_directory 等)が応答待ちでブロックし、タイムアウトまでハングすることがある。
    # この使い捨て scratch にスコープ限定の設定を置き、権限を all-allow にして
    # 権限プロンプトで止まらないようにする（ユーザーのグローバル opencode 設定は変更しない）。
    #
    # 加えて doci-write という最小エージェントを定義する。既定の build エージェントは
    # コーディングアシスタント用のフルシステムプロンプト（ツール定義・スキル一覧・
    # グローバル CLAUDE.md 等、約8-10KB）を毎回上乗せするが、本用途は「JSON1個を
    # テキスト生成させるだけ」でありファイル編集・bash実行等のツール能力は不要。
    # prompt は空文字だと既定プロンプトへフォールバックする恐れがあるため半角スペース1つにする。
    (scratch / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "permission": {"*": "allow"},
                "agent": {
                    "doci-write": {
                        "mode": "primary",
                        "prompt": " ",
                        "tools": {"*": False},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cmd += ["--print-logs", "--log-level", "ERROR", "--dir", str(scratch), prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_write_timeout())
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
    if backend == "opencode_go":
        return _run_opencode_go(prompt, config.OPENCODE_MODEL)
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
        s.setdefault("act", "")
    if isinstance(script["tags"], str):
        script["tags"] = [t.strip() for t in script["tags"].split(",") if t.strip()]
    _check_cold_open(script.get("narration", ""))
    return script


_COLD_OPEN_PATTERNS = (
    "今日は", "面白い話", "お話です", "突然ですが",
    "こんにちは", "こんばんは",
    "知っていますか",
)


def _check_cold_open(narration: str) -> None:
    """冒頭が『いきなり本編』ルール(output_rules.md)に違反していないかを検証する。
    実測で narration 冒頭に禁止フレーズが混入する事故が高頻度(24件中13件=54%)で
    起きていたため、プロンプト指示だけに頼らずコード側でも検証しリトライへ回す。"""
    head = (narration or "")[:60]
    hit = next((p for p in _COLD_OPEN_PATTERNS if p in head), None)
    if hit:
        raise ValueError(f"冒頭が「いきなり本編」ルール違反（禁止フレーズ「{hit}」を検出）: {head!r}")


# 図表は本来 scenes 側の要素として置く設計だが、執筆モデルが稀に図表マーカー
# {"chart_id":N,"caption":"…"} を narration 本文へインラインしてしまう。放置すると
# (1) TTS が JSON をそのまま読み上げ (2) scene 側に chart_id が無く図表が出ない、の二重事故になる。
_INLINE_CHART = re.compile(
    r'\s*\{\s*"chart_id"\s*:\s*(\d+)\s*,\s*"caption"\s*:\s*"([^"]*)"\s*\}'
)


def _strip_chart_markers(text: str) -> str:
    """本文中に紛れた図表マーカー JSON を除去し、余分な改行を整える。"""
    cleaned = _INLINE_CHART.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _recover_inline_charts(script: dict) -> int:
    """narration にインラインされた図表マーカーを scenes の chart_id へ移し、本文から除去する。
    戻り値は scene へ移せた図表数。本文順＝シーン順とみなし、マーカーの文字位置から
    相当する scene を推定して chart_id を載せる（既に scene 側にある番号は重複させない）。"""
    narration = script.get("narration") or ""
    scenes = script.get("scenes") or []
    matches = list(_INLINE_CHART.finditer(narration))
    if not matches:
        return 0
    have = {int(s["chart_id"]) for s in scenes if s.get("chart_id") is not None}
    moved = 0
    if scenes:
        span = max(1, len(narration))
        for m in matches:
            cid = int(m.group(1))
            if cid in have:
                continue  # 既に scene 側へ正しく置かれている
            idx = min(len(scenes) - 1, int(m.start() / span * len(scenes)))
            # 推定位置から後方優先で、図表未割り当ての scene を探して載せる。
            order = list(range(idx, len(scenes))) + list(range(idx - 1, -1, -1))
            slot = next((j for j in order if scenes[j].get("chart_id") is None), None)
            if slot is None:
                continue
            scenes[slot]["chart_id"] = cid
            if m.group(2) and not scenes[slot].get("caption"):
                scenes[slot]["caption"] = m.group(2)
            have.add(cid)
            moved += 1
    script["narration"] = _strip_chart_markers(narration)
    return moved


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


def generate(
    spec: ChannelSpec,
    corner: CornerSpec,
    day: str,
    past_topics: list[str],
    topic_guard: Callable[[str], None] | None = None,
) -> dict:
    # 1) 前段リサーチ（issue #6）: 題材選定＋Web裏取り。失敗してもリサーチ無しで続行。
    research = None
    if spec.pipeline_get("research", config.SCRIPT_RESEARCH):
        from . import research as research_mod

        _log(f"前段リサーチ ({config.RESEARCH_BACKEND}+Web)…")
        try:
            research = research_mod.web_research(corner, past_topics, spec)
            if research:
                _log(f"題材: {research.get('topic', '')} / 裏取り事実 {len(research.get('facts', []))}件")
        except Exception as e:  # noqa: BLE001
            _log(f"リサーチ失敗→リサーチ無しで続行: {e}")
            research = None
    if research and topic_guard:
        # リサーチで題材が確定した時点で予約する。構成・執筆・音声・映像より前なので、
        # 重複候補に制作コストを使わず、並行runも同じ題材を選べない。
        topic_guard(str(research.get("topic") or ""))

    # 1.5) 構成プラン（issue #2）: minimax で起承転結＋図表を設計。失敗してもプラン無しで続行。
    plan = None
    if spec.pipeline_get("plan", config.SCRIPT_PLAN):
        from . import plan as plan_mod

        _log(f"構成プラン (minimax) …")
        try:
            plan = plan_mod.make_plan(corner, research)
            if plan:
                _log(f"構成: 起承転結{len(plan.get('beats', []))}ビート / 図表 {len(plan.get('charts', []))}個")
        except Exception as e:  # noqa: BLE001
            _log(f"プラン失敗→プラン無しで続行: {e}")
            plan = None

    # 2) 執筆（qwen3.7-plus 等）。リサーチの具体＋プランの構成/図表に沿わせる。
    #    稀に不完全JSONを返すため再生成で吸収。
    prompt = corners.build_prompt(
        spec, corner, day, past_topics, research=research, plan=plan
    )
    script = None
    last_err: Exception | None = None
    for attempt in range(1, config.SCRIPT_DRAFT_RETRIES + 1):
        try:
            script = _validate(_extract_json(_dispatch(prompt)))
            break
        except (ValueError, subprocess.TimeoutExpired, RuntimeError, OSError) as e:
            # JSON不良/必須キー不足（ValueError）だけでなく、執筆バックエンドのタイムアウト
            # (TimeoutExpired)・異常終了(RuntimeError)・ネットワーク失敗(OSError)も一過性とみなし
            # 再試行する（qwen 等が稀に固まり、1回の失敗で通し全体が即死するのを防ぐ）。
            last_err = e
            # 例外メッセージにプロンプト全文が載ることがあるため要約のみログする。
            _log(
                f"執筆失敗(試行{attempt}/{config.SCRIPT_DRAFT_RETRIES})→再生成: "
                f"{type(e).__name__}: {str(e)[:160]}"
            )
    # フォールバック: 主バックエンド(例 opencode/qwen)が規定回数で揃わなかった場合、
    # 稼働実績のある claude_cli で執筆をやり直す。qwen のハング/不調で通し全体が
    # 「執筆失敗」で終わるのを防ぐ安全網（qwen はあくまで主のまま）。
    if script is None and config.TEXT_BACKEND != "claude_cli":
        _log(f"執筆({config.TEXT_BACKEND})が揃わず→claude_cli にフォールバック")
        for attempt in range(1, config.SCRIPT_DRAFT_RETRIES + 1):
            try:
                script = _validate(_extract_json(_run_claude_cli(prompt, config.FALLBACK_TEXT_MODEL)))
                break
            except (ValueError, subprocess.TimeoutExpired, RuntimeError, OSError) as e:
                last_err = e
                _log(
                    f"claudeフォールバック失敗(試行{attempt}/{config.SCRIPT_DRAFT_RETRIES})→再試行: "
                    f"{type(e).__name__}: {str(e)[:160]}"
                )

    if script is None:
        raise RuntimeError(f"執筆が規定回数で揃いませんでした: {last_err}")
    if not research and topic_guard:
        # リサーチがフォールバックしたrunでも、動画生成・投稿へ進む前に
        # タイトルと概要の先頭行を題材としてcooldownを適用する。
        topic_guard(
            f"{script.get('title', '')} "
            f"{str(script.get('description') or '').splitlines()[0] if script.get('description') else ''}"
        )

    # 2.4) 救済: 執筆モデルが narration に図表マーカーをインラインした場合、本文から除去して
    #      相当する scene へ chart_id を移す（音声へのJSON混入と図表欠落を同時に防ぐ）。
    recovered = _recover_inline_charts(script)
    if recovered:
        _log(f"本文混入の図表マーカーを {recovered} 件回収（→scene へ移動）")

    # 2.5) chart_id を実図表仕様に解決（データはプラン＝minimax由来を正とし取り違えを防ぐ）。
    if plan and plan.get("charts"):
        by_id = {c["id"]: c for c in plan["charts"]}
        used = 0
        for s in script["scenes"]:
            cid = s.get("chart_id")
            if cid is not None and int(cid) in by_id:
                s["chart"] = by_id[int(cid)]
                s.pop("chart_id", None)
                used += 1
        if used:
            _log(f"図表を {used} シーンに配置")

    # 3) 後段ファクトチェック（issue #6）: 別モデル(opus)＋Web検証で narration を自動修正。
    if spec.pipeline_get("factcheck", config.SCRIPT_FACTCHECK):
        from . import factcheck

        _log(f"後段ファクトチェック ({config.FACTCHECK_BACKEND}+Web)…")
        try:
            fc = factcheck.verify_and_correct(script["narration"], research)
            if fc and fc.get("narration", "").strip():
                issues = fc.get("issues") or []
                if fc.get("changed") and issues:
                    _log(f"ファクトチェック: {len(issues)}件修正")
                script["narration"] = _strip_chart_markers(fc["narration"])
                script["_factcheck"] = issues
        except Exception as e:  # noqa: BLE001
            _log(f"ファクトチェック失敗→修正なしで続行: {e}")

    script["_corner"] = corner.key
    script["_speaker"] = spec.voice_for(corner).speaker
    script["_channel"] = spec.id
    script["_date"] = day
    if research:
        script["_research"] = research
    return script


def main() -> None:
    ap = argparse.ArgumentParser(description="台本生成 (opus 4.8)")
    ap.add_argument("--channel", help="チャンネルID（未指定時は既定チャンネル）")
    ap.add_argument("--corner", help="コーナーID")
    ap.add_argument("--date", default=_date.today().isoformat())
    args = ap.parse_args()
    spec = channel.load(args.channel or channel.default_channel())
    corner_key = args.corner or spec.rotation[0]
    if corner_key not in spec.corners:
        ap.error(
            f"unknown corner for channel {spec.id}: {corner_key}; "
            f"choose from {', '.join(spec.corners)}"
        )
    corner = spec.corners[corner_key]
    voice = spec.voice_for(corner)
    script = generate(spec, corner, args.date, past_topics=[])
    print(json.dumps(script, ensure_ascii=False, indent=2))
    print(
        f"\n--- channel={spec.id} corner={corner.key} voice={corner.voice_key} "
        f"speaker={voice.speaker} "
        f"narration_chars={len(script['narration'])} scenes={len(script['scenes'])} ---",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
