"""台本生成（OpenCode Go / qwen3.7-plus）。

Minimax は文章生成に使わない（方針）。
バックエンド:
  - claude_cli (旧・明示時のみ): 認証済みの `claude` CLI を print モードで呼ぶ
  - anthropic        (クラウド): Anthropic API (ANTHROPIC_API_KEY) を直叩き
  - opencode         (代替):     `opencode run --agent ...`
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import socket
import subprocess
import threading
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
_DEFAULT_WRITE_TIMEOUT = object()
_RESPONSE_END = object()


def _shutdown_response(response) -> None:  # type: ignore[no-untyped-def]
    """読み取り待ちのソケットを、可能ならshutdownしてワーカーを解放する。"""
    for candidate in (
        getattr(response, "fp", None),
        getattr(getattr(response, "fp", None), "raw", None),
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
    ):
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except (OSError, TypeError):
                pass


class _ResponseReadWorker:
    """settimeout非対応の応答を、接続ごとに1本の読み取りワーカーで消費する。"""

    def __init__(
        self,
        reader: Callable[[], object],
        response,  # type: ignore[no-untyped-def]
        *,
        empty_is_end: bool = False,
    ) -> None:
        self._reader = reader
        self._response = response
        self._empty_is_end = empty_is_end
        self._events: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        self._cancelled = threading.Event()
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="doci-response-reader"
        )
        self._worker.start()

    def _publish(self, event: tuple[bool, object]) -> bool:
        while not self._cancelled.is_set():
            try:
                self._events.put(event, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        while not self._cancelled.is_set():
            try:
                value = self._reader()
            except StopIteration:
                self._publish((False, _RESPONSE_END))
                return
            except BaseException as exc:  # noqa: BLE001 - propagate reader errors
                self._publish((False, exc))
                return
            if not self._publish((True, value)):
                return
            if self._empty_is_end and not value:
                return

    def next(self, timeout_seconds: float) -> object:
        try:
            succeeded, value = self._events.get(timeout=max(0.001, timeout_seconds))
        except queue.Empty as exc:
            self.cancel()
            raise socket.timeout("stream read timeout") from exc
        if succeeded:
            return value
        if value is _RESPONSE_END:
            raise StopIteration
        raise value  # type: ignore[misc]

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        _shutdown_response(self._response)
        close = getattr(self._response, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass
        self._worker.join(timeout=0.2)


def _read_response_until_deadline(response, deadline: float) -> bytes:  # type: ignore[no-untyped-def]
    """settimeout非対応のHTTP応答を、接続単位のworkerで期限まで読む。"""
    chunks: list[bytes] = []
    response_timeout_supported = False
    fallback_reader: _ResponseReadWorker | None = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Anthropic API が時間上限に達しました")
            for candidate in (
                getattr(response, "fp", None),
                getattr(getattr(response, "fp", None), "raw", None),
                getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
            ):
                setter = getattr(candidate, "settimeout", None)
                if callable(setter):
                    try:
                        setter(remaining)
                    except OSError:
                        pass
                    else:
                        response_timeout_supported = True
            try:
                if fallback_reader is not None:
                    chunk = fallback_reader.next(remaining)
                elif response_timeout_supported:
                    chunk = response.read(4096)
                else:
                    fallback_reader = _ResponseReadWorker(
                        lambda: response.read(4096), response, empty_is_end=True
                    )
                    chunk = fallback_reader.next(remaining)
            except socket.timeout as exc:
                raise TimeoutError("Anthropic API が時間上限に達しました") from exc
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)  # type: ignore[arg-type]
    finally:
        if fallback_reader is not None:
            fallback_reader.cancel()


def _monotonic() -> float:
    """差し替え可能な時計。全体予算のテストが標準timeを汚染しないようにする。"""
    return time.monotonic()

# 互換用エイリアス（JSON抽出/CLI実行は共通モジュール llm に集約）
_extract_json = llm.extract_json


def _write_timeout(
    override: int | float | None | object = _DEFAULT_WRITE_TIMEOUT,
) -> int | float | None:
    """CLI/APIの全体待機上限。0は明示的な長文待機モード。"""
    if override is _DEFAULT_WRITE_TIMEOUT:
        return _whole_write_timeout()
    if override is None:
        return None
    return override if override > 0 else None


def _whole_write_timeout() -> int | None:
    """ストリーム全体の上限。0は長文を最後まで待つ。"""
    return config.WRITE_LLM_TIMEOUT if config.WRITE_LLM_TIMEOUT > 0 else None


def _run_claude_cli(
    prompt: str,
    model: str,
    timeout: int | float | None | object = _DEFAULT_WRITE_TIMEOUT,
) -> str:
    return llm.run_claude(
        prompt, config.legacy_claude_model(model), timeout=_write_timeout(timeout)
    )


def _run_anthropic(
    prompt: str,
    model: str,
    timeout: int | float | None | object = _DEFAULT_WRITE_TIMEOUT,
) -> str:
    key = config.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です (TEXT_BACKEND=anthropic)")
    body = json.dumps(
        {
            "model": config.legacy_claude_model(model),
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
    request_timeout = _write_timeout(timeout)
    deadline = (
        time.monotonic() + request_timeout if request_timeout is not None else None
    )
    open_timeout = request_timeout
    if deadline is not None:
        open_timeout = max(0.001, deadline - time.monotonic())
    with urllib.request.urlopen(req, timeout=open_timeout) as resp:
        payload = resp.read() if deadline is None else _read_response_until_deadline(resp, deadline)
        data = json.loads(payload.decode("utf-8"))
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


def _opencode_go_model(model: str) -> str:
    """OpenCode Go APIへ渡せるモデル名だけを許可する。

    bare model はゲートウェイの既定プロバイダとして後方互換に受け入れるが、
    provider-qualified な別プロバイダ名をそのまま送ると、Go APIではなく別の
    認証経路を暗黙に要求するため、既定のQwenへ安全に戻す。
    """
    if not model:
        raise RuntimeError(
            "TEXT_BACKEND=opencode_go ではモデルを指定してください "
            f"（例: {config.OPENCODE_GO_DEFAULT_MODEL}）"
        )
    provider, separator, _ = model.partition("/")
    if separator and provider != "opencode-go":
        raise RuntimeError(
            "TEXT_BACKEND=opencode_go では OPENCODE_MODEL を "
            "opencode-go/<model> 形式で指定してください"
        )
    model_id = model.split("/", 1)[1] if separator else model
    if model_id.startswith(("claude-", "anthropic/")):
        raise RuntimeError(
            "TEXT_BACKEND=opencode_go ではClaudeモデルを使えません。 "
            f"{config.OPENCODE_GO_DEFAULT_MODEL} を指定してください"
        )
    return model


_DEFAULT_OPENCODE_GO_TIMEOUT = object()


def _run_opencode_go(
    prompt: str,
    model: str,
    timeout: int | None | object = _DEFAULT_OPENCODE_GO_TIMEOUT,
) -> str:
    """OpenCode CLIを介さず、OpenCode GoのAnthropic互換APIへ直接接続する。"""
    model = _opencode_go_model(model)
    _, sep, model_id = model.partition("/")
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
    if timeout is _DEFAULT_OPENCODE_GO_TIMEOUT:
        deadline_timeout = _whole_write_timeout()
    else:
        deadline_timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else None
    # 全体上限とidle上限を同時に0にすると、Claudeフォールバックなしの既定経路が
    # 無音SSEで復帰不能になるため、明示的な全体無制限時にもidleだけは残す。
    idle_timeout = (
        config.WRITE_LLM_IDLE_TIMEOUT
        if config.WRITE_LLM_IDLE_TIMEOUT > 0
        else (300 if deadline_timeout is None else None)
    )
    request_limits = [value for value in (deadline_timeout, idle_timeout) if value is not None]
    request_timeout = min(request_limits) if request_limits else None
    deadline_expired = threading.Event()
    received_terminal = False

    def set_stream_timeout(response, timeout_seconds: float | None) -> bool:  # type: ignore[no-untyped-def]
        """各行の読み取り前にソケットへ残り時間を設定し、全体上限をreadlineにも適用する。"""
        if timeout_seconds is None:
            return True
        candidates = (
            getattr(getattr(response, "fp", None), "raw", None),
            getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        )
        for candidate in candidates:
            setter = getattr(candidate, "settimeout", None)
            if callable(setter):
                try:
                    setter(timeout_seconds)
                except OSError:
                    pass
                else:
                    return True
        return False

    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            fallback_reader: _ResponseReadWorker | None = None
            try:
                stream = iter(resp)
                stream_timeout_warning_logged = False
                response_read = getattr(resp, "read1", None) or getattr(resp, "read", None)
                byte_mode = callable(response_read)
                line_buffer = bytearray()
                last_socket_timeout: float | None = None
                while True:
                    remaining = (
                        deadline_timeout - (time.monotonic() - started)
                        if deadline_timeout is not None
                        else None
                    )
                    if remaining is not None and remaining <= 0:
                        deadline_expired.set()
                        raise RuntimeError("OpenCode Go API が時間上限に達しました")
                    if remaining is not None:
                        remaining = max(0.001, remaining)
                    read_timeout = remaining
                    if idle_timeout is not None:
                        read_timeout = (
                            min(read_timeout, idle_timeout)
                            if read_timeout is not None
                            else idle_timeout
                        )
                    update_socket_timeout = (
                        read_timeout is not None
                        and (
                            last_socket_timeout is None
                            or abs(read_timeout - last_socket_timeout) >= 0.1
                        )
                    )
                    if read_timeout is None:
                        stream_timeout_applied = True
                    elif update_socket_timeout:
                        stream_timeout_applied = set_stream_timeout(resp, read_timeout)
                    else:
                        stream_timeout_applied = last_socket_timeout is not None
                    if update_socket_timeout and stream_timeout_applied:
                        last_socket_timeout = read_timeout
                    if not stream_timeout_applied:
                        if not stream_timeout_warning_logged:
                            _log("警告: OpenCode Goストリームのソケット期限を設定できません")
                            stream_timeout_warning_logged = True
                    try:
                        if b"\n" in line_buffer:
                            raw_line, _, remainder = line_buffer.partition(b"\n")
                            line_buffer = bytearray(remainder)
                            raw = raw_line + b"\n"
                        elif byte_mode:
                            if fallback_reader is not None:
                                chunk = fallback_reader.next(read_timeout)
                            elif stream_timeout_applied:
                                chunk = response_read(4096)
                            else:
                                fallback_reader = _ResponseReadWorker(
                                    lambda: response_read(4096), resp
                                )
                                chunk = fallback_reader.next(read_timeout)
                            if not chunk:
                                if line_buffer:
                                    raw = bytes(line_buffer)
                                    line_buffer.clear()
                                else:
                                    raise StopIteration
                            else:
                                line_buffer.extend(chunk)
                                continue
                        elif fallback_reader is not None:
                            raw = fallback_reader.next(read_timeout)
                        elif stream_timeout_applied:
                            raw = next(stream)
                        else:
                            fallback_reader = _ResponseReadWorker(
                                lambda: next(stream), resp
                            )
                            raw = fallback_reader.next(read_timeout)
                    except socket.timeout as exc:
                        if (
                            deadline_timeout is not None
                            and time.monotonic() - started >= deadline_timeout
                        ):
                            deadline_expired.set()
                            raise RuntimeError("OpenCode Go API が時間上限に達しました") from exc
                        raise RuntimeError("OpenCode Go API の無通信タイムアウト") from exc
                    except StopIteration:
                        if line_buffer:
                            raw = bytes(line_buffer)
                            line_buffer.clear()
                        else:
                            if (
                                deadline_expired.is_set()
                                or (
                                    deadline_timeout is not None
                                    and time.monotonic() - started >= deadline_timeout
                                )
                            ):
                                deadline_expired.set()
                                raise RuntimeError(
                                    "OpenCode Go API が時間上限に達しました"
                                )
                            # [DONE]/stop_reasonを省略するゲートウェイでも、ストリームの
                            # 自然終了は完全な本文の終端として受け入れる。
                            received_terminal = True
                            break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].lstrip()
                    if not payload:
                        continue
                    if payload == "[DONE]":
                        received_terminal = True
                        break
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
                        if stop_reason:
                            received_terminal = True
                            break
                    if (
                        not received_terminal
                        and (
                            deadline_expired.is_set()
                            or (
                                deadline_timeout is not None
                                and time.monotonic() - started >= deadline_timeout
                            )
                        )
                    ):
                        raise RuntimeError("OpenCode Go API が時間上限に達しました")
                    elapsed = time.monotonic() - started
                    if elapsed >= next_progress:
                        _log(f"Qwen直接API生成中 ({elapsed:.0f}s / 本文{text_chars}字)")
                        next_progress += 60.0
            finally:
                if fallback_reader is not None:
                    fallback_reader.cancel()
            if deadline_expired.is_set() and not received_terminal:
                raise RuntimeError("OpenCode Go API が時間上限に達しました")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OpenCode Go API failed (HTTP {exc.code}): {detail}") from exc
    except (TimeoutError, OSError, ValueError) as exc:
        if deadline_expired.is_set() and not received_terminal:
            raise RuntimeError("OpenCode Go API が時間上限に達しました") from exc
        raise
    text = "".join(text_parts)
    if stop_reason == "max_tokens":
        raise RuntimeError(
            f"OpenCode Go API が max_tokens={config.OPENCODE_GO_MAX_TOKENS} に達しました"
        )
    if not text.strip():
        raise RuntimeError(f"OpenCode Go API が空の本文を返しました (stop_reason={stop_reason or 'unknown'})")
    _log(f"Qwen直接API完了 ({time.monotonic() - started:.1f}s / 本文{len(text)}字)")
    return text


def _run_opencode(
    prompt: str,
    model: str,
    agent: str,
    timeout: int | float | None | object = _DEFAULT_WRITE_TIMEOUT,
) -> str:
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
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_write_timeout(timeout))
    if proc.returncode != 0:
        raise RuntimeError(f"opencode failed (rc={proc.returncode}): {proc.stderr[:500]}")
    return proc.stdout


def _opencode_cli_model(model: str) -> str:
    """OpenCode CLIのagent-only設定を補助段でも維持する。"""
    candidate = config.OPENCODE_MODEL or ("" if config.OPENCODE_AGENT else model)
    return _validate_opencode_cli_model(candidate)


def _validate_opencode_cli_model(model: str) -> str:
    """OpenCode CLIへClaudeプロバイダを暗黙に渡さない。"""
    if model.startswith(("claude-", "anthropic/", "opencode-go/claude-")):
        raise RuntimeError(
            "TEXT_BACKEND=opencode ではClaudeモデルを使えません。"
            " OPENCODE_MODELまたは段別モデルをOpenCode対応値へ変更してください"
        )
    return model


def _opencode_cli_aux_model(model: str, *, explicit: bool) -> str:
    """補助段は段別モデルを優先し、未指定時だけ既存CLI設定を使う。"""
    if explicit:
        return _validate_opencode_cli_model(model)
    return _opencode_cli_model(model)


def _dispatch(prompt: str, timeout: int | float | None = None) -> str:
    backend = config.TEXT_BACKEND
    model = config.TEXT_MODEL
    if backend == "claude_cli":
        if timeout is None:
            return _run_claude_cli(prompt, model)
        return _run_claude_cli(prompt, model, timeout=timeout)
    if backend == "anthropic":
        if timeout is None:
            return _run_anthropic(prompt, model)
        return _run_anthropic(prompt, model, timeout=timeout)
    if backend == "opencode_go":
        if timeout is None:
            return _run_opencode_go(prompt, config.OPENCODE_MODEL or model)
        return _run_opencode_go(prompt, config.OPENCODE_MODEL or model, timeout=timeout)
    if backend == "opencode":
        # agent-only の既存設定では TEXT_MODEL の既定値を混ぜず、
        # _run_opencode 側に空モデルを渡して --agent を有効にする。
        opencode_model = _opencode_cli_model(model)
        if timeout is None:
            return _run_opencode(prompt, opencode_model, config.OPENCODE_AGENT)
        return _run_opencode(
            prompt, opencode_model, config.OPENCODE_AGENT, timeout=timeout
        )
    raise ValueError(f"unknown TEXT_BACKEND: {backend}")


_SEMANTIC_DUPLICATE_PROMPT = """\
あなたはショート動画企画の重複審査担当です。「新しい題材候補」が「直近の題材一覧」のいずれかと、
表現・比喩・切り口を変えただけで結論や主張構造が実質同じ使い回しでないかを判定してください。
（例: 「見えざる手」と「見えない手」、「成長という名の列車」と「成長という名の神様」は同じ主張の言い換えで重複）
テーマ領域が同じでも、具体的な結論・視点・題材が明確に異なるものは重複ではありません。

新しい題材候補: {candidate}

直近の題材一覧:
{numbered}

出力は有効なJSONオブジェクトのみ（説明・コードフェンス禁止）:
{{"duplicate": true または false, "matched_index": 一致した番号（1始まり。無ければnull）, "confidence": 0から1の確信度, "reason": "判定理由（1文）"}}
"""


def check_semantic_duplicate(
    candidate_topic: str,
    recent_topics: list[str],
    *,
    limit: int = 24,
    text_limit: int = 120,
) -> tuple[str, float] | None:
    """語彙が一致しない言い換え重複をLLMに判定させる。

    一致ありなら (一致した過去題材, 確信度) を返す。通信・応答不良時は
    誤スキップより見逃しを優先し None を返す（生成を止めない）。
    """
    candidate_topic = candidate_topic.strip()
    candidates = [t.strip()[:text_limit] for t in recent_topics if t.strip()][-limit:]
    if not candidate_topic or not candidates:
        return None
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(candidates))
    prompt = _SEMANTIC_DUPLICATE_PROMPT.format(
        candidate=candidate_topic[:text_limit], numbered=numbered
    )
    try:
        data = _extract_json(_dispatch(prompt, timeout=60))
    except (
        ValueError,
        TimeoutError,
        subprocess.TimeoutExpired,
        RuntimeError,
        OSError,
    ):
        return None
    if not isinstance(data, dict) or not data.get("duplicate"):
        return None
    idx = data.get("matched_index")
    matched = (
        candidates[idx - 1]
        if isinstance(idx, int)
        and not isinstance(idx, bool)
        and 1 <= idx <= len(candidates)
        else candidates[0]
    )
    confidence = data.get("confidence")
    score = confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 1.0
    return matched, max(0.0, min(1.0, float(score)))


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
    performance_decision: dict | None = None,
) -> dict:
    performance_guidance = str(
        (performance_decision or {}).get("guidance") or ""
    )
    # 1) 前段リサーチ（issue #6）: 題材選定＋Web裏取り。失敗してもリサーチ無しで続行。
    research = None
    research_enabled = spec.pipeline_get("research", config.SCRIPT_RESEARCH)
    factcheck_enabled = spec.pipeline_get("factcheck", config.SCRIPT_FACTCHECK)
    if research_enabled:
        from . import research as research_mod

        _log(f"前段リサーチ ({config.RESEARCH_BACKEND}+Web)…")
        try:
            research = research_mod.web_research(
                corner,
                past_topics,
                spec,
                performance_guidance=performance_guidance,
            )
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
        spec,
        corner,
        day,
        past_topics,
        research=research,
        plan=plan,
        performance_guidance=performance_guidance,
    )
    script = None
    last_err: Exception | None = None
    draft_started = _monotonic()
    draft_total_timeout = (
        config.SCRIPT_DRAFT_TOTAL_TIMEOUT
        if config.SCRIPT_DRAFT_TOTAL_TIMEOUT > 0
        else None
    )
    for attempt in range(1, config.SCRIPT_DRAFT_RETRIES + 1):
        attempt_timeout = None
        if draft_total_timeout is not None:
            remaining_budget = draft_total_timeout - (_monotonic() - draft_started)
            if remaining_budget <= 0:
                detail = ""
                if last_err is not None:
                    detail = (
                        f" (直前の失敗: {type(last_err).__name__}: "
                        f"{str(last_err)[:160]})"
                    )
                last_err = TimeoutError(
                    f"執筆段全体の時間上限に達しました{detail}"
                )
                break
            per_attempt_timeout = _whole_write_timeout()
            attempt_timeout = (
                min(per_attempt_timeout, remaining_budget)
                if per_attempt_timeout is not None
                else remaining_budget
            )
        try:
            script = _validate(_extract_json(_dispatch(prompt, timeout=attempt_timeout)))
            break
        except (ValueError, TimeoutError, subprocess.TimeoutExpired, RuntimeError, OSError) as e:
            # JSON不良/必須キー不足（ValueError）だけでなく、執筆バックエンドのタイムアウト
            # (TimeoutExpired)・異常終了(RuntimeError)・ネットワーク失敗(OSError)も一過性とみなし
            # 再試行する（qwen 等が稀に固まり、1回の失敗で通し全体が即死するのを防ぐ）。
            last_err = e
            # 例外メッセージにプロンプト全文が載ることがあるため要約のみログする。
            _log(
                f"執筆失敗(試行{attempt}/{config.SCRIPT_DRAFT_RETRIES})→再生成: "
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

    # 2.9) OpenCode系ファクトチェックだけを有効にした場合は、下書き後に
    # 資料取得だけを行う。research=false の題材選定・cooldown意味は変えない。
    factcheck_research = research
    if (
        factcheck_enabled
        and config.SCRIPT_FACTCHECK_RESEARCH
        and config.FACTCHECK_BACKEND in {"opencode", "opencode_go"}
        and not research_enabled
    ):
        from . import research as research_mod

        _log(f"ファクトチェック用リサーチ ({config.FACTCHECK_BACKEND}+Web)…")
        try:
            factcheck_research = research_mod.web_research(
                corner,
                past_topics,
                spec,
                performance_guidance=performance_guidance,
                backend_override=config.FACTCHECK_BACKEND,
                model_override=config.FACTCHECK_MODEL,
                model_explicit_override=config._FACTCHECK_MODEL_EXPLICIT,
                focus_text=(
                    f"{script.get('title', '')}\n{script.get('narration', '')}"
                ),
                require_youtube_examples=False,
            )
        except Exception as e:  # noqa: BLE001
            _log(f"ファクトチェック用リサーチ失敗→原文維持: {e}")
            factcheck_research = None

    # 3) 後段ファクトチェック（issue #6）: 別モデル＋Web検証で narration を自動修正。
    if factcheck_enabled:
        from . import factcheck

        _log(f"後段ファクトチェック ({config.FACTCHECK_BACKEND}+Web)…")
        try:
            fc = factcheck.verify_and_correct(script["narration"], factcheck_research)
            if fc and fc.get("narration", "").strip():
                issues = fc.get("issues") or []
                if fc.get("changed") and issues:
                    _log(f"ファクトチェック: {len(issues)}件修正")
                script["narration"] = _strip_chart_markers(fc["narration"])
                script["_factcheck"] = issues
        except factcheck.FactcheckSourcesUnavailableError:
            _log("ファクトチェック資料がないため失敗として扱います")
            if config.SCRIPT_FACTCHECK_REQUIRE_SOURCES:
                raise
        except Exception as e:  # noqa: BLE001
            _log(f"ファクトチェック失敗→修正なしで続行: {e}")

    script["_corner"] = corner.key
    script["_speaker"] = spec.voice_for(corner).speaker
    script["_channel"] = spec.id
    script["_date"] = day
    if research:
        script["_research"] = research
    if performance_decision:
        script["_performance_feedback"] = performance_decision
    return script


def main() -> None:
    ap = argparse.ArgumentParser(description="台本生成 (OpenCode Go / qwen3.7-plus)")
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
