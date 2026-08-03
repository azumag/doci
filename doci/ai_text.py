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

from . import channel, config, corners, history, llm
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
                        _log(
                            f"OpenCode Go ({model_id}) 生成中 "
                            f"({elapsed:.0f}s / 本文{text_chars}字)"
                        )
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
    _log(
        f"OpenCode Go ({model_id}) 完了 "
        f"({time.monotonic() - started:.1f}s / 本文{len(text)}字)"
    )
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
    if backend == "codex":
        # 本文生成はWeb取得を必須にしない（plan/chart_bg段と同じ扱い）。
        # timeout未指定＝無制限は他バックエンド(claude_cli等)と揃える。
        # run_codexの既定timeout=600に暗黙で丸め込まない。
        if timeout is None:
            return llm.run_codex(
                prompt, config.CODEX_MODEL, timeout=None, min_web_fetches=0
            )
        return llm.run_codex(
            prompt, config.CODEX_MODEL, timeout=timeout, min_web_fetches=0
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
    source_candidates = [t.strip() for t in recent_topics if t.strip()]
    candidates = [t[:text_limit] for t in source_candidates[:limit]]
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
        source_candidates[idx - 1]
        if isinstance(idx, int)
        and not isinstance(idx, bool)
        and 1 <= idx <= len(candidates)
        else source_candidates[0]
    )
    confidence = data.get("confidence")
    score = confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 1.0
    return matched, max(0.0, min(1.0, float(score)))


_TITLE_PATTERN_DUPLICATE_PROMPT = """\
あなたはYouTube動画タイトルの重複審査担当です。「新しいタイトル案」が「直近のタイトル一覧」の
いずれかと、次の観点で同じ型の使い回しに見えないかを判定してください。

- proper_noun: 固有名詞の重複（同じ企業名・人名・製品名などを核にしている）
- problem_word: 問題語・キーワードの重複（同じ課題語「改善」「クリック率」等を核にしている）
- rhetorical_template: 修辞テンプレートの重複（疑問形、「〜するな」型の警告・逆説、
  「真実」「設計図」「罠」等の紋切り型比喩、「が殺す/壊す」型の煽り構文など、言い回しの骨格が同じ）

上記3観点のうち2つ以上が同じ直近タイトルと重なる場合だけ重複と判定してください。
1つだけ重なる（例えばどちらも疑問形なだけ）場合は重複ではありません。
題材・具体的な結論が明確に異なる場合も重複ではありません。

新しいタイトル案: {candidate}

直近のタイトル一覧:
{numbered}

出力は有効なJSONオブジェクトのみ（説明・コードフェンス禁止）:
{{"duplicate": true または false, "matched_index": 一致した番号（1始まり。無ければnull）,
  "overlapping_axes": ["proper_noun","problem_word","rhetorical_template"]のうち該当するものの配列,
  "confidence": 0から1の確信度, "reason": "判定理由（1文）"}}
"""


def check_title_pattern_duplicate(
    candidate_title: str,
    recent_titles: list[str],
    *,
    limit: int = 24,
    text_limit: int = 200,
) -> dict | None:
    """タイトルの修辞パターン重複(固有名詞・問題語・型)をLLMに判定させる。

    issue #37: 題材レベルのcooldown一致だけでは、題材(具体的な事実・数値)が
    違ってもタイトルの型（固有名詞+問題語+疑問形/煽り構文）が繰り返される
    使い回しを検出できない。一致ありなら判定結果の辞書を返す。通信・応答
    不良時は誤検出より見逃しを優先しNoneを返す（生成を止めない）。
    """
    candidate_title = candidate_title.strip()
    candidates = [t.strip()[:text_limit] for t in recent_titles if t.strip()][-limit:]
    if not candidate_title or not candidates:
        return None
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(candidates))
    prompt = _TITLE_PATTERN_DUPLICATE_PROMPT.format(
        candidate=candidate_title[:text_limit], numbered=numbered
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
    score = (
        confidence
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        else 1.0
    )
    axes = data.get("overlapping_axes")
    overlapping_axes = (
        [a for a in axes if isinstance(a, str)] if isinstance(axes, list) else []
    )
    reason = data.get("reason")
    return {
        "matched_title": matched,
        "confidence": max(0.0, min(1.0, float(score))),
        "overlapping_axes": overlapping_axes,
        "reason": str(reason)[:300] if isinstance(reason, str) else "",
    }


_NARRATION_OPENING_PATTERN_DUPLICATE_PROMPT = """\
あなたはナレーション台本の書き出し重複審査担当です。「新しい書き出し案」が「直近の書き出し一覧」の
いずれかと、次の観点で同じ型の使い回しに見えないかを判定してください。

- opening_syntax: 文の構文構造の重複（同じ疑問文型・同じ命令文型など、文の骨格が同じ）
- subject_frame: 主語・視点の枠組みの重複（同じ主語（人類/私たち等）や同じ対象への呼びかけ方）
- rhetorical_move: 修辞的な仕掛けの重複（反語、逆説、対比、結論先出しなど、同じ仕掛けを使っている）

上記3観点のうち2つ以上が同じ直近の書き出しと重なる場合だけ重複と判定してください。
1つだけ重なる場合は重複ではありません。話題・具体的な内容が明確に異なる場合も重複ではありません。

新しい書き出し案: {candidate}

直近の書き出し一覧:
{numbered}

出力は有効なJSONオブジェクトのみ（説明・コードフェンス禁止）:
{{"duplicate": true または false, "matched_index": 一致した番号（1始まり。無ければnull）,
  "overlapping_axes": ["opening_syntax","subject_frame","rhetorical_move"]のうち該当するものの配列,
  "confidence": 0から1の確信度, "reason": "判定理由（1文）"}}
"""


def check_narration_opening_pattern_duplicate(
    candidate_opening: str,
    recent_openings: list[str],
    *,
    limit: int = 12,
    text_limit: int = 80,
) -> dict | None:
    """narration書き出しの修辞パターン重複(構文・主語枠・修辞技法)をLLMに判定させる(issue #70)。

    Layer 2の正規表現ファミリー(_OPENING_FAMILIES)が拾えない未知の重複パターンを
    検出・記録するためだけのもので、生成をブロックしない
    （check_title_pattern_duplicateと同じ検出・記録のみの運用）。通信・応答不良時は
    誤検出より見逃しを優先しNoneを返す（生成を止めない）。
    """
    candidate_opening = candidate_opening.strip()
    candidates = [o.strip()[:text_limit] for o in recent_openings if o.strip()][-limit:]
    if not candidate_opening or not candidates:
        return None
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(candidates))
    prompt = _NARRATION_OPENING_PATTERN_DUPLICATE_PROMPT.format(
        candidate=candidate_opening[:text_limit], numbered=numbered
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
    score = (
        confidence
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        else 1.0
    )
    axes = data.get("overlapping_axes")
    overlapping_axes = (
        [a for a in axes if isinstance(a, str)] if isinstance(axes, list) else []
    )
    reason = data.get("reason")
    return {
        "matched_opening": matched,
        "confidence": max(0.0, min(1.0, float(score))),
        "overlapping_axes": overlapping_axes,
        "reason": str(reason)[:300] if isinstance(reason, str) else "",
    }


_AMBIGUOUS_DATE_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("year", re.compile(r"20\d{2}年(?:\s*\d{1,2}月)?")),
    ("month_revision", re.compile(r"\d{1,2}月\s*改[訂定]")),
    ("revision", re.compile(r"改[訂定]版?")),
    ("latest", re.compile(r"最新版?")),
)
_DATE_ROLE_CURRENT_AS_OF = "current_as_of"
_DATE_ROLES = {"historical_event", _DATE_ROLE_CURRENT_AS_OF, "deadline"}
# 年抽出は検出パターンの"year"(20\d{2}年)と同じ厳格さで揃える。裸の4桁数値
# ("登録者2000人"等)を年と誤認しないよう、"年"を必須にする(issue #57レビュー指摘)。
_TITLE_YEAR_MONTH_RE = re.compile(r"(20\d{2})年(?:\s*(\d{1,2})月)?")


def _title_year_months(title: str) -> list[tuple[str, str | None]]:
    return [
        (m.group(1), m.group(2).zfill(2) if m.group(2) else None)
        for m in _TITLE_YEAR_MONTH_RE.finditer(title)
    ]


def check_ambiguous_date_title(title: str, facts: list[dict] | None) -> dict | None:
    """タイトル内の過去年月・「改訂」「最新」表現を、企画データの日付根拠と突き合わせる(issue #57)。

    LLMを使わない正規表現ベースの判定（対象が機械的に検出できるパターンのため）。
    生成をブロックせず検出・記録のみに使う（check_title_pattern_duplicateと同じ運用）。

    戻り値: 該当表現が無ければ None。あれば {"matched_patterns", "matched_texts",
    "supported", "missing", "reason"} を持つ dict。supported=False は公開前の
    確認対象（日付の役割・確認日・出典のいずれかが企画データに揃っていない、
    またはタイトルの年が変更日ではなく確認日にしか対応しない=取り違えの疑い）。
    """
    matched: list[tuple[str, str]] = []
    for name, pattern in _AMBIGUOUS_DATE_TITLE_PATTERNS:
        m = pattern.search(title)
        if m:
            matched.append((name, m.group(0)))
    if not matched:
        return None

    def _is_dated_fact(f: object) -> bool:
        if not isinstance(f, dict):
            return False
        date_role = str(f.get("date_role") or "").strip()
        if date_role not in _DATE_ROLES:
            return False
        if not str(f.get("verified_at") or "").strip():
            return False
        if not str(f.get("source_url") or "").strip():
            return False
        # current_as_ofは「変更日不明でも現在有効と確認した」ケースのため
        # effective_dateを必須にしない(research.pyのプロンプトも不明時は
        # 空文字のままにする指示のため、常に付くとは限らない)。
        if date_role != _DATE_ROLE_CURRENT_AS_OF and not str(
            f.get("effective_date") or ""
        ).strip():
            return False
        return True

    dated_facts = [f for f in (facts or []) if _is_dated_fact(f)]

    matched_pattern_names = {name for name, _ in matched}
    # "最新"は年月の裏付けとは別に「現在も有効」という主張そのものなので、
    # 年月が一致していてもcurrent_as_ofが無ければ根拠不十分とする
    # (レビュー指摘: 「2025年最新」のように年月とfreshness語が併記される
    # 主要ケースで、年一致だけでは"最新"の裏付けにならない)。
    # "改訂"は変更が起きた時点を述べるだけで「現在有効」の主張ではなく、
    # その曖昧さ(制度の改訂日か動画自体の改訂日か)は年月一致で解消できる
    # ため、current_as_of の追加要求はしない。
    requires_freshness_confirmation = "latest" in matched_pattern_names

    missing: list[str] = []
    if not dated_facts:
        missing = ["effective_date", "date_role", "verified_at", "source_url"]
        supported = False
        reason = "企画データに日付根拠(effective_date/date_role/verified_at/source_url)がありません"
    else:
        has_current_as_of = any(
            f["date_role"] == _DATE_ROLE_CURRENT_AS_OF for f in dated_facts
        )
        year_months = _title_year_months(title)
        if year_months:
            def _fact_matches(year: str, month: str | None, fact: dict) -> bool:
                # current_as_ofはeffective_dateキー自体が無いことがあるため
                # (issue #57レビュー指摘: KeyErrorでデイリー実行全体が停止し得た)
                # .get()で安全に読む。
                effective = str(fact.get("effective_date") or "")
                if effective[:4] != year:
                    return False
                if month is None:
                    return True
                fact_month = effective[5:7] if len(effective) >= 7 else None
                # factが年粒度までしか記録していない場合、月の矛盾は確認できない
                # ため年一致のみで根拠として認める(記録されている粒度が限界)。
                return fact_month is None or fact_month == month

            matched_ok = any(
                _fact_matches(year, month, f)
                for year, month in year_months
                for f in dated_facts
            )
            matched_verified_only = any(
                str(f["verified_at"])[:4] == year
                for year, _ in year_months
                for f in dated_facts
            )
            if matched_ok and requires_freshness_confirmation and not has_current_as_of:
                supported = False
                reason = "「現在も有効」を裏付けるcurrent_as_ofの日付根拠がありません"
                missing = ["date_role"]
            elif matched_ok:
                supported, reason = True, ""
            elif matched_verified_only:
                supported = False
                reason = (
                    "タイトルの年が変更日(effective_date)ではなく"
                    "確認日(verified_at)にしか対応していません"
                )
                missing = ["effective_date"]
            else:
                supported = False
                reason = "タイトルの年月と一致するeffective_dateがありません"
                missing = ["effective_date"]
        elif requires_freshness_confirmation:
            # 年を伴わない「最新」表現。年月で裏付けようがないため
            # current_as_ofの有無だけで判定する。
            supported = has_current_as_of
            reason = (
                ""
                if supported
                else "「現在も有効」を裏付けるcurrent_as_ofの日付根拠がありません"
            )
            if not supported:
                missing = ["date_role"]
        else:
            # 年を伴わない「改訂」表現(例: "7月改訂"に対応する年が本文に無い)は
            # 変更時点を述べるだけで「現在有効」の主張ではないためcurrent_as_of
            # を要求しない。年が無く月だけでは照合しようがないため、出典・
            # 確認日を備えた日付根拠(dated_facts)が存在すること自体で足りるとする。
            supported, reason = True, ""

    return {
        "matched_patterns": [name for name, _ in matched],
        "matched_texts": [text for _, text in matched],
        "supported": supported,
        "missing": missing,
        "reason": reason,
    }


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
    # 冒頭チェックは括弧除去「後」の、実際にTTSで読み上げられる最終テキストに対して
    # 行う。「突然」ですが等、鉤括弧除去によって隣接語が禁止フレーズへ結合する場合、
    # それは配信物として本物の冒頭違反になるため検出すべき（除去前の原文を見ると
    # 素通りしてしまう）。
    script["narration"] = _strip_bracket_quotes(script.get("narration", ""))
    _check_cold_open(script["narration"])
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


# issue #70: narrationの書き出しが動画をまたいで同じ修辞の型に偏る事故（実測でideology
# チャンネルの反語疑問「〜はなぜ…でしょうか」がほぼ100%）を検出するための定型パターン。
# cold-openの固定文字列と違い主語のバリエーションを吸収する必要があるため正規表現にする。
_OPENING_FAMILIES: tuple[tuple[str, re.Pattern], ...] = (
    # 文末アンカー必須: 「なぜ〜のかを」「なぜ〜のか特定できていません」等、
    # のか/でしょうかが文中の活用として現れる通常の説明文を誤検出しないため
    # （反語疑問の型は必ず文末がでしょうか/のかで終わる）。
    ("rhetorical_why", re.compile(r"なぜ.+(でしょうか|のか)$")),
    ("next_video_directive", re.compile(r"次の(ショート)?動画では")),
    ("conclusion_first", re.compile(r"^結論(から言うと|を伝えます)")),
)


def _opening_sentence(narration: str, max_chars: int = 200) -> str:
    """narration冒頭の一文（最初の句読点まで）を切り出す。history.recent_narration_openings
    と同じ抽出規則（句点/疑問符/感嘆符で区切り、max_chars文字まで）に揃える。

    max_charsは_OPENING_FAMILIESの文末アンカー（$）が一文全体を見られるよう、
    実測の書き出し文（最長55字程度）に十分な余裕を持たせている（issue #70
    レビュー指摘: 60字だと反語疑問の「でしょうか」が切り詰めで欠けて検出漏れになる）。"""
    head = re.split(r"[。？！]", narration or "", maxsplit=1)[0]
    return head[:max_chars].strip()


def _opening_family(opening: str) -> str | None:
    """書き出し文がどの定型パターンに属するかを返す（該当なしはNone）。"""
    for name, pattern in _OPENING_FAMILIES:
        if pattern.search(opening):
            return name
    return None


def _check_opening_pattern(
    narration: str,
    recent_openings: list[str],
    *,
    window: int = 6,
    max_family_share: int = 2,
) -> None:
    """直近の書き出しと同じ修辞パターンへ偏っていないかを検証する（issue #70）。

    直前と全く同じ型、または直近window件中でmax_family_share件を超える場合は
    ValueErrorを送出し、呼び出し側(generate())でプロンプトに回避指示を足して
    再生成させる。未分類の書き出し・履歴なしは常に許可する（誤検出より見逃しを
    優先し、cold-openと違って統計的な偏りの検出なので致命的エラーにはしない）。
    """
    family = _opening_family(_opening_sentence(narration))
    if family is None or not recent_openings:
        return
    recent_families = [_opening_family(o) for o in recent_openings[-window:]]
    if recent_families and recent_families[-1] == family:
        raise ValueError(
            f"書き出しの型「{family}」が直前の動画と同じです: {narration[:60]!r}"
        )
    share = recent_families.count(family)
    if share >= max_family_share:
        raise ValueError(
            f"書き出しの型「{family}」が直近{window}件中{share}件と偏っています: "
            f"{narration[:60]!r}"
        )


# 図表は本来 scenes 側の要素として置く設計だが、執筆モデルが稀に図表マーカー
# {"chart_id":N,"caption":"…"} を narration 本文へインラインしてしまう。放置すると
# (1) TTS が JSON をそのまま読み上げ (2) scene 側に chart_id が無く図表が出ない、の二重事故になる。
# issue #59: caption キー無しの {"chart_id": N} 単体形式も実測で混入し、本文に生JSONが
# 残ったまま TTS・字幕に露出する事故（「文字化け」報告）を起こしたため caption はオプショナルにする。
# group(1)=chart_id、group(2)=caption（無ければ None）。
_INLINE_CHART = re.compile(
    r'\s*\{\s*"chart_id"\s*:\s*(\d+)\s*(?:,\s*"caption"\s*:\s*"([^"]*)"\s*)?\}'
)


def _strip_chart_markers(text: str) -> str:
    """本文中に紛れた図表マーカー JSON を除去し、余分な改行を整える。"""
    cleaned = _INLINE_CHART.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _strip_bracket_quotes(text: str) -> str:
    """narration 中の「」を除去する（issue #58: 多用するとTTSのテンポが悪くなるため）。
    括弧で囲まれた語自体は残し、記号のみ落とす。"""
    return (text or "").replace("「", "").replace("」", "")


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
    topic_metadata_guard: Callable[[dict], None] | None = None,
    performance_decision: dict | None = None,
    recent_openings: list[str] | None = None,
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
                require_structured_novelty=True,
            )
            if research:
                _log(f"題材: {research.get('topic', '')} / 裏取り事実 {len(research.get('facts', []))}件")
        except Exception as e:  # noqa: BLE001
            _log(f"リサーチ失敗→リサーチ無しで続行: {e}")
            research = None
    if research and topic_guard:
        # リサーチで題材が確定した時点で予約する。構成・執筆・音声・映像より前なので、
        # 重複候補に制作コストを使わず、並行runも同じ題材を選べない。
        if topic_metadata_guard:
            topic_metadata_guard(research)
        topic_guard(str(research.get("topic") or ""))

    # 1.5) 構成プラン（issue #2）: minimax で起承転結＋図表を設計。失敗してもプラン無しで続行。
    plan = None
    plan_topic_reserved = False
    if spec.pipeline_get("plan", config.SCRIPT_PLAN):
        from . import plan as plan_mod

        _log(f"構成プラン (minimax) …")
        if not research and topic_guard:
            # researchが無いコーナー(ideology等)は、plan.topic(起承転結の実質テーマ)を
            # 執筆前にcooldown照合する。タイトルは煽り文句で言い換えられやすく文字列/意味
            # 照合をすり抜けやすいため、内容そのものに近いこの段階で判定する（issue: 直近でも
            # 同じ内容の動画ばかりになる）。重複時は即スキップにせず、避けるべき題材を
            # avoidリストへ積んで既定PLAN_TOPIC_RETRIES回まで設計をやり直させる。
            # 候補ごとに毎回「実予約」を試す(プローブ用の別モードは持たない): 却下された
            # 候補はhistoryへskip行が残るだけで、公開済み/キュー済み判定にも次回以降の
            # プロンプトにも一切使われないため無害。試行を分けるとcooldown照合(LLMによる
            # 意味的重複チェックを含む)が候補ごとに二重に走ってしまうため、こちらが安い。
            cooldown_days = int(
                spec.pipeline_get("topic_cooldown_days", config.TOPIC_COOLDOWN_DAYS)
            )
            avoid_topics = (
                history.cooldown_window_topics(spec, cooldown_days=cooldown_days)
                if cooldown_days > 0
                else []
            )
            max_attempts = max(
                1,
                int(spec.pipeline_get("plan_topic_retries", config.PLAN_TOPIC_RETRIES)),
            )
            plan_topic_started = _monotonic()
            plan_topic_total_timeout = (
                config.PLAN_TOPIC_TOTAL_TIMEOUT
                if config.PLAN_TOPIC_TOTAL_TIMEOUT > 0
                else None
            )
            for attempt in range(1, max_attempts + 1):
                if (
                    attempt > 1
                    and plan_topic_total_timeout is not None
                    and _monotonic() - plan_topic_started >= plan_topic_total_timeout
                ):
                    # 他段(SCRIPT_DRAFT_TOTAL_TIMEOUT等)と同様、再設計ループ全体の予算切れは
                    # backend不調日に試行回数分の待ち時間が積み重なるのを防ぐための保険。
                    # プラン無しにフォールバックし、後段のタイトルベース照合に委ねる。
                    _log(
                        "構成プラン再設計が時間上限に達しました"
                        f"({attempt - 1}/{max_attempts}試行)→プラン無しで続行"
                    )
                    plan = None
                    break
                try:
                    plan = plan_mod.make_plan(corner, research, avoid_topics=avoid_topics)
                except Exception as e:  # noqa: BLE001
                    _log(f"プラン失敗→プラン無しで続行: {e}")
                    plan = None
                    break
                candidate_topic = str((plan or {}).get("topic") or "").strip()
                if not candidate_topic:
                    break
                if topic_metadata_guard:
                    # researchが無いのでresearch由来のstructured novelty(canonical_theme
                    # 等)は無く、実質{}にしかならないが、topic_guardの前に必ず
                    # topic_metadata_guardを呼ぶという他経路と同じ呼び出し順を維持する。
                    # 将来ideology等にも構造化メタデータを持たせる場合、ここに差し込むだけで
                    # 共通台帳へ伝わるようにしておく。
                    topic_metadata_guard({})
                try:
                    topic_guard(candidate_topic)
                except history.TopicCooldownSkip as exc:
                    if attempt == max_attempts:
                        raise
                    _log(
                        "構成プランの題材が重複"
                        f"(試行{attempt}/{max_attempts})→避けて再設計: {exc.match.topic}"
                    )
                    # 今回の却下分を先頭に積む: avoid_topics/_avoid_blockは新しい(=優先度
                    # が高い)順の前提で先頭から20件だけプロンプトへ載せるため、末尾に足すと
                    # 履歴の種が多いチャンネルで今回の却下自体が弾かれず伝わらなくなる。
                    avoid_topics = [exc.match.topic, candidate_topic] + avoid_topics
                    continue
                plan_topic_reserved = True
                break
        else:
            try:
                plan = plan_mod.make_plan(corner, research)
            except Exception as e:  # noqa: BLE001
                _log(f"プラン失敗→プラン無しで続行: {e}")
                plan = None
        if plan:
            _log(f"構成: 起承転結{len(plan.get('beats', []))}ビート / 図表 {len(plan.get('charts', []))}個")

    # issue #70: 書き出しの型が動画をまたいで同型化する事故（実測でideologyの反語疑問が
    # ほぼ100%）を防ぐ。opt-inチャンネルだけ、直近の書き出しをプロンプトへ注入(Layer1)し、
    # ドラフトretryループでも検証する(Layer2)。フラグ未設定のチャンネルはrecent_openingsが
    # 渡されていてもプロンプト・挙動を一切変えない。
    opening_guard_enabled = bool(recent_openings) and spec.pipeline_get(
        "narration_opening_guard", False
    )

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
        recent_openings=recent_openings if opening_guard_enabled else None,
    )
    script = None
    last_err: Exception | None = None
    draft_started = _monotonic()
    draft_total_timeout = (
        config.SCRIPT_DRAFT_TOTAL_TIMEOUT
        if config.SCRIPT_DRAFT_TOTAL_TIMEOUT > 0
        else None
    )
    # このパターンはモデルの最頻出力であり、cold-openの禁止語句と違って致命的raiseにすると
    # 再生成を使い切って動画が丸ごと落ちるリスクがあるため、リトライを使い切った場合は
    # 違反ありのまま採用しfallback_scriptで記録する（soft-enforce）。
    fallback_script: dict | None = None
    fallback_reason: str | None = None
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
                # fallback_scriptが既にあれば(=書き出しパターン違反だけの理由で
                # 直前の試行を却下していた場合)、時間切れでも致命的raiseにはせず
                # その違反ありドラフトを採用する。有効な原稿が既にあるのに時間予算
                # だけを理由に動画全体を落とすのは本末転倒なため(loop末尾で処理)。
                break
            per_attempt_timeout = _whole_write_timeout()
            attempt_timeout = (
                min(per_attempt_timeout, remaining_budget)
                if per_attempt_timeout is not None
                else remaining_budget
            )
        try:
            candidate = _validate(_extract_json(_dispatch(prompt, timeout=attempt_timeout)))
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
            continue
        if not opening_guard_enabled:
            script = candidate
            break
        try:
            _check_opening_pattern(candidate["narration"], recent_openings or [])
        except ValueError as e:
            fallback_script, fallback_reason = candidate, str(e)
            last_err = e
            if attempt == config.SCRIPT_DRAFT_RETRIES:
                break
            _log(
                f"書き出しパターン重複の疑い(試行{attempt}/{config.SCRIPT_DRAFT_RETRIES})"
                f"→避けて再生成: {e}"
            )
            prompt += (
                "\n## 直前の書き出しは使用禁止\n"
                f"直前の生成「{_opening_sentence(candidate['narration'])}」は"
                "直近の動画と同じ修辞の型のため、別の型で書き直してください。\n"
            )
            continue
        candidate["_opening_guard"] = {"accepted_with_violation": False}
        script = candidate
        break
    if script is None and fallback_script is not None:
        fallback_script["_opening_guard"] = {
            "accepted_with_violation": True,
            "reason": fallback_reason,
        }
        script = fallback_script
    if script is None:
        raise RuntimeError(f"執筆が規定回数で揃いませんでした: {last_err}")
    if not research and not plan_topic_reserved and topic_guard:
        # プラン段のtopicで既に予約済みならここは呼ばない（二重予約を避ける）。
        # プランが無効/失敗した場合の後方互換フォールバックとして、動画生成・投稿へ
        # 進む前にタイトルと概要の先頭行を題材としてcooldownを適用する。
        if topic_metadata_guard:
            script_research = script.get("_research")
            topic_metadata_guard(
                script_research if isinstance(script_research, dict) else {}
            )
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
                script["narration"] = _strip_bracket_quotes(
                    _strip_chart_markers(fc["narration"])
                )
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
