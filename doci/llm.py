"""Codex exec と旧Claude CLIの薄いラッパ、JSON抽出（リサーチ/ファクトチェック共通）。

Codex exec は明示設定時の経路。`claude -p ... --output-format json` は旧設定を明示した場合だけ叩く。Web検索が要る段は
`allowed_tools=["WebSearch","WebFetch"]` を渡す（print モードで実検索が走ることを実測確認済）。
`run_codex` は config.CODEX_PROVIDER で接続先を切り替える:
- minimax(既定): 隔離 CODEX_HOME 配下に MiniMax プロバイダの config.toml を毎回生成し、
  ユーザーの ~/.codex には一切触れない。
- chatgpt: 実 ~/.codex から auth.json だけをコピーした別の隔離 CODEX_HOME を毎回作り
  直して使う（前回実行の残置ファイルを次回が無検査で信用しないため）。無人実行は
  リサーチ/ファクトチェック段で外部Webページの内容をプロンプトに取り込むため、実
  ~/.codex をそのまま使うとプロンプトインジェクション経由でプロジェクト一覧・MCP設定
  等の個人情報まで読まれ得る。認証情報1ファイルだけに絞って露出面を最小化する
  （auth.json自体が読める点は実ChatGPT認証を使う要件上避けられない）。実行後、隔離
  ホーム内でのトークンリフレッシュ結果は auth.json のみ実ホームへ書き戻す（一方向
  コピーのままだとローテーション型リフレッシュトークンが無効化され実ログインが
  壊れうるため）。コピー元の実 ~/.codex にはこの auth.json 以外一切書き込まない。
web fetch を要求しない呼び出し(min_web_fetches=0)ではサンドボックスのネットワークアクセス
も無効化し、外部送信の経路自体を塞ぐ。chatgptプロバイダで web fetch必須
(min_web_fetches>=1)の呼び出しは、CODEX_CHATGPT_ALLOW_UNTRUSTED_WEBを明示しない限り
既定で拒否する（ネットワーク有効サンドボックス内に実認証を置いたままプロンプト
インジェクションに晒すリスクを避けるため）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
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
    """CODEX_PROVIDER に応じた隔離 CODEX_HOME を用意する。
    minimax(既定): MiniMax 用 config.toml を毎回上書き生成する。
    chatgpt: 実 ~/.codex の auth.json だけをコピーする（他の個人設定は持ち込まない）。
    いずれも、ユーザーの実 ~/.codex には一切書き込まない。"""
    if config.CODEX_PROVIDER == "chatgpt":
        return _ensure_chatgpt_codex_home()
    if not config.MINIMAX_API_KEY:
        raise RuntimeError("MINIMAX_API_KEY が未設定です（codex/minimaxバックエンドには必須）")
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


def _ensure_chatgpt_codex_home() -> Path:
    """実 ~/.codex の auth.json だけをコピーした隔離 CODEX_HOME を返す。
    コピー元(config.CODEX_REAL_HOME)には一切書き込まない。前回実行の残置ファイル
    (codex execがサンドボックス内で書き込んだ config.toml 等)を次回実行が無検査で
    信用しないよう、隔離ホームは毎回完全に作り直す。"""
    real_auth = config.CODEX_REAL_HOME / "auth.json"
    if not real_auth.exists():
        raise RuntimeError(
            f"{real_auth} が見つかりません。`codex login` でChatGPT認証を済ませてください"
            "（CODEX_PROVIDER=chatgpt には実 ~/.codex の認証が必須です）"
        )
    home = config.CODEX_CHATGPT_HOME
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    dest = home / "auth.json"
    shutil.copy(real_auth, dest)
    os.chmod(dest, 0o600)
    return home


# codex CLI の auth.json 形式（実 ~/.codex/auth.json で確認済み）。
# {"auth_mode": ..., "tokens": {"id_token", "access_token", "refresh_token",
#  "account_id"}, "last_refresh": ...}
_AUTH_TOKEN_KEYS = ("access_token", "refresh_token", "account_id")


def _auth_tokens(data: bytes) -> dict | None:
    """auth.json をパースし tokens 辞書を返す。必須フィールドが全て非空文字列で
    揃っていなければ None（構文的に有効なJSONというだけでは信用しない）。"""
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    tokens = parsed.get("tokens")
    if not isinstance(tokens, dict):
        return None
    if not all(isinstance(tokens.get(k), str) and tokens.get(k) for k in _AUTH_TOKEN_KEYS):
        return None
    return tokens


def _sync_refreshed_chatgpt_auth(home: Path) -> None:
    """codex exec 実行後、隔離ホーム側でトークンリフレッシュが起きていたら実
    ~/.codex/auth.json へ書き戻す。ChatGPTのリフレッシュトークンはローテーション式
    になり得るため、書き戻さないと次回実行時に実ホーム側の（既に無効化された）
    古いauth.jsonで隔離ホームを上書きしてしまい、実ログイン自体が壊れうる
    （元のコードが避けようとしていた「ChatGPTログイン破壊事故」と同種の経路）。

    codex execのworkspace-writeサンドボックスはCODEX_HOME配下への書き込みを制限
    しないため、プロンプトインジェクションで実行されたコマンドが隔離ホームの
    auth.jsonを構文的に有効な別内容（トークン欠落・別アカウントのトークン等）に
    書き換えている可能性がある。「JSONとして読めるか」だけでは不十分なので、
    (1) tokens.access_token/refresh_token/account_id が全て揃っていること、
    (2) account_id が書き換え前の実auth.jsonと完全一致すること、の両方を
    確認してから書き戻す。auth.json以外のファイルは実ホームへ一切書き込まない。"""
    copied = home / "auth.json"
    try:
        new_bytes = copied.read_bytes()
    except OSError:
        return
    new_tokens = _auth_tokens(new_bytes)
    if new_tokens is None:
        return
    real_auth = config.CODEX_REAL_HOME / "auth.json"
    try:
        real_bytes = real_auth.read_bytes()
    except OSError:
        return
    if real_bytes == new_bytes:
        return
    real_tokens = _auth_tokens(real_bytes)
    if real_tokens is None or new_tokens["account_id"] != real_tokens["account_id"]:
        return
    tmp = real_auth.with_name(real_auth.name + ".doci-tmp")
    tmp.write_bytes(new_bytes)
    os.chmod(tmp, 0o600)
    tmp.replace(real_auth)


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
    """codex exec (--json) をヘッドレスで実行し、最終 agent_message の text を返す。

    隔離 CODEX_HOME を毎回用意して実行する（ユーザーの実 ~/.codex には一切書き込まない。
    chatgptプロバイダの詳細は _ensure_chatgpt_codex_home を参照）。approval_policy等は
    無人実行が承認待ちで詰まらないよう毎回 `-c` で明示上書きする（対話用 config.toml の
    値に依存しない）。min_web_fetches=0（web取得を要求しない呼び出し）ではサンドボックスの
    ネットワークアクセスも無効化し、外部送信の経路自体を塞ぐ。
    web fetch(curl/wget/URL実行)が min_web_fetches 未満なら「検索したフリ」とみなし ValueError
    にする（呼び出し側の既存リトライ/劣化継続に乗せる）。

    CODEX_PROVIDER=chatgptでは、min_web_fetches>=1（外部Webページの内容をプロンプトへ
    取り込む呼び出し）を既定で拒否する。ネットワーク有効サンドボックス内に実ChatGPT
    認証(auth.json)が置かれるため、プロンプトインジェクション経由でトークンを外部送信
    される経路になり得る。CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB=1で明示許可した場合のみ通す。
    """
    if (
        config.CODEX_PROVIDER == "chatgpt"
        and min_web_fetches > 0
        and not config.CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB
    ):
        raise RuntimeError(
            "CODEX_PROVIDER=chatgpt はWeb取得必須の呼び出し"
            f"(min_web_fetches={min_web_fetches})では既定で拒否されます。"
            "外部Webページの内容を取り込むプロンプトインジェクションで実ChatGPT認証が"
            "外部送信されるリスクがあるためです。リスクを理解した上で使う場合のみ"
            "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB=1 を明示してください。"
        )
    home = _ensure_codex_home(model)
    scratch = config.OUTPUT / "codex-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    network_access = "true" if min_web_fetches > 0 else "false"
    cmd = [
        config.CODEX_BIN,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "-c",
        "approval_policy=never",
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        f"sandbox_workspace_write.network_access={network_access}",
    ]
    if config.CODEX_REASONING_EFFORT:
        cmd += ["-c", f"model_reasoning_effort={config.CODEX_REASONING_EFFORT}"]
    cmd += ["-m", model, "-"]
    env = {**os.environ, "CODEX_HOME": str(home)}
    if config.CODEX_PROVIDER == "minimax":
        env["DOCI_MINIMAX_AUTHORIZATION"] = f"Bearer {config.MINIMAX_API_KEY}"
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(scratch),
            env=env,
        )
    finally:
        # タイムアウト等でも、隔離ホーム内で起きたトークンリフレッシュは
        # 可能な限り実ホームへ反映する（同期の詳細は _sync_refreshed_chatgpt_auth）。
        if config.CODEX_PROVIDER == "chatgpt":
            _sync_refreshed_chatgpt_auth(home)
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
