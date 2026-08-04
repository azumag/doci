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
も無効化するが、これは外部送信を防ぐだけでサンドボックス内のファイル書き込み自体は
防がない点に注意（他バックエンド由来の汚染されたコンテンツがプロンプト経由でcodex段に
渡された場合、network_access=falseでも隔離ホーム内のauth.json書き換えは起こり得る）。
chatgptプロバイダで web fetch必須(min_web_fetches>=1)の呼び出しは、
CODEX_CHATGPT_ALLOW_UNTRUSTED_WEBを明示しない限り既定で拒否する。
auth.json書き戻し前の検証(account_id一致)は別アカウントへのなりすましは防ぐが、
同一account_idを保ったままのトークン破壊までは防げないため、書き戻し前の実auth.json
を config.CODEX_CHATGPT_AUTH_BACKUP へ退避し、万一の際に手動復旧できるようにしている。
詳細は _sync_refreshed_chatgpt_auth のdocstringを参照。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
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
# シェル経由(MiniMax/minimax想定)の検出専用で、chatgptプロバイダの組み込みweb_search
# ツール由来のitem(下記_parse_codex_events参照)は別途カウントする。
_WEB_FETCH_RE = re.compile(r"curl|wget|https?://", re.IGNORECASE)


def _write_secret_bytes(path: Path, data: bytes) -> None:
    """0600で新規作成/上書きしてから書き込む。write→os.chmodの順序だと、umaskが
    緩い環境(既定0o022ならファイルは0o644で作られる)で作成直後からchmodまでの間、
    他ローカルユーザーから読める窓ができてしまう。os.openでモードを最初から
    指定すれば、umaskはモードを緩める方向には働かないため確実に0600以下になる。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)


def _mkdir_private(path: Path) -> None:
    """0700で作成する（既存ならそのまま）。同じ理由でumaskに緩められない
    よう明示的に指定する。中身のファイルへの到達をディレクトリ実行権限の
    レベルでも塞ぐ。"""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)


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
    _mkdir_private(home)
    cfg_path = home / "config.toml"
    _write_secret_bytes(
        cfg_path,
        _CODEX_CONFIG_TOML.format(
            model=model,
            base_url=config.CODEX_MINIMAX_BASE_URL,
        ).encode("utf-8"),
    )
    return home


def _log(msg: str) -> None:
    print(f"[doci/llm] {msg}", flush=True)


# 直近の _ensure_chatgpt_codex_home 呼び出しでコピーした時点の実auth.jsonの
# バイト列。_sync_refreshed_chatgpt_auth が「コピー後に実ホーム側が変化していないか
# (＝対話的codex利用等の並行更新がなかったか)」を確認するために使う。
_chatgpt_auth_snapshot: bytes | None = None


def _ensure_chatgpt_codex_home() -> Path:
    """実 ~/.codex の auth.json だけをコピーした隔離 CODEX_HOME を返す。
    コピー元(config.CODEX_REAL_HOME)には一切書き込まない。前回実行の残置ファイル
    (codex execがサンドボックス内で書き込んだ config.toml 等)を次回実行が無検査で
    信用しないよう、隔離ホームは毎回完全に作り直す。

    config.CODEX_CHATGPT_AUTH_BACKUP が存在しない場合に限り、その時点の実
    auth.jsonをそこへ退避する。一度作成したバックアップは、以後(プロセスを
    跨いでも)自動では二度と上書きしない。「_sync_refreshed_chatgpt_authの検証
    （account_id一致等）は、サンドボックス内の攻撃者が同一account_idを保った
    まま形式的に有効な偽トークンへ差し替える攻撃までは防げない」という前提の
    もとでは、もし一度でも汚染された内容が実ホームへ伝播してしまうと、
    「プロセス内1回だけ」のような時限式の保護では次のプロセス起動時にその
    汚染済みauth.jsonを新しい正常なバックアップとして採用してしまい、唯一の
    手動復旧手段を失う。ファイルの存在有無だけで判定することで、プロセス境界
    を越えても最初に確認できた既知良好な状態を永続的に保持する。
    （バックアップを更新したい場合はユーザーが手動でファイルを削除する）
    実auth.jsonが _auth_tokens で検証できない（既に壊れている等）場合は
    バックアップを作成しない。

    毎回、コピーした時点のバイト列を _chatgpt_auth_snapshot に記録する
    （_sync_refreshed_chatgpt_auth が並行更新を検知するために使う）。"""
    global _chatgpt_auth_snapshot
    real_auth = config.CODEX_REAL_HOME / "auth.json"
    if not real_auth.exists():
        raise RuntimeError(
            f"{real_auth} が見つかりません。`codex login` でChatGPT認証を済ませてください"
            "（CODEX_PROVIDER=chatgpt には実 ~/.codex の認証が必須です）"
        )
    current_bytes = real_auth.read_bytes()
    _chatgpt_auth_snapshot = current_bytes
    backup = config.CODEX_CHATGPT_AUTH_BACKUP
    if not backup.exists():
        if _auth_tokens(current_bytes) is not None:
            _mkdir_private(backup.parent)
            _write_secret_bytes(backup, current_bytes)
        else:
            _log("chatgptバックアップ: 実auth.jsonが不正な形式のため作成をスキップ")
    home = config.CODEX_CHATGPT_HOME
    if home.exists():
        shutil.rmtree(home)
    _mkdir_private(home)
    dest = home / "auth.json"
    _write_secret_bytes(dest, current_bytes)
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
    auth.jsonを書き換えている可能性がある。「JSONとして読めるか」だけでは不十分
    なので、(1) tokens.access_token/refresh_token/account_id が全て揃っている
    こと、(2) account_id が書き換え前の実auth.jsonと完全一致すること、の両方を
    確認してから書き戻す。auth.json以外のファイルは実ホームへ一切書き込まない。

    【この検証の限界】 サンドボックス内でコマンドを実行できる攻撃者は隔離ホームの
    auth.jsonを読めるため、正しいaccount_idを保ったままトークン値だけをデタラメ
    な値に差し替えることができ、その場合は両チェックを通過してしまう
    （＝別アカウントへのなりすましは防げるが、同一account_idを保った上での
    トークン破壊＝可用性攻撃までは防げない）。この経路への備えとして、
    _ensure_chatgpt_codex_home が実行直前の実auth.jsonを
    config.CODEX_CHATGPT_AUTH_BACKUP へ退避しており、万一破壊されても手動で
    復元できる。書き戻しをスキップした場合は理由をログに残す（無音で正規の
    リフレッシュ結果を捨てると、ログイン切れの原因が追えなくなるため）。

    さらに、実ホームがコピー時点(_chatgpt_auth_snapshot)から変化していない
    ことも確認してから書き戻す。TEXT_BACKEND=codexはtimeout=None(無制限)になり
    得るため、doci実行中にユーザーが対話的にcodexを使い実ホーム側でトークンが
    ローテーションされる可能性がある。その変化を無視して書き戻すと、対話
    セッション側の新しいrefresh_tokenをdoci側の古い系列のトークンで上書きし、
    このPRが防ごうとしている「ログイン破壊事故」を別経路で起こしてしまう。"""
    copied = home / "auth.json"
    try:
        new_bytes = copied.read_bytes()
    except OSError as exc:
        _log(f"chatgpt認証同期: 隔離ホームのauth.jsonを読めずスキップ: {exc}")
        return
    new_tokens = _auth_tokens(new_bytes)
    if new_tokens is None:
        _log("chatgpt認証同期: 隔離ホームのauth.jsonが不正な形式のためスキップ")
        return
    real_auth = config.CODEX_REAL_HOME / "auth.json"
    try:
        real_bytes = real_auth.read_bytes()
    except OSError as exc:
        _log(f"chatgpt認証同期: 実ホームのauth.jsonを読めずスキップ: {exc}")
        return
    if real_bytes == new_bytes:
        return
    if (
        _chatgpt_auth_snapshot is not None
        and real_bytes != _chatgpt_auth_snapshot
    ):
        _log(
            "chatgpt認証同期: 実ホームのauth.jsonがコピー時点から変化しているため"
            "スキップ（対話的codex利用等の並行更新の疑い）"
        )
        return
    real_tokens = _auth_tokens(real_bytes)
    if real_tokens is None:
        _log("chatgpt認証同期: 実ホームのauth.jsonが不正な形式のためスキップ")
        return
    if new_tokens["account_id"] != real_tokens["account_id"]:
        _log(
            "chatgpt認証同期: account_idが一致しないためスキップ"
            "（別アカウントへのなりすましの疑い）"
        )
        return
    tmp = real_auth.with_name(real_auth.name + ".doci-tmp")
    _write_secret_bytes(tmp, new_bytes)
    tmp.replace(real_auth)
    _log("chatgpt認証同期: リフレッシュされたauth.jsonを実ホームへ反映しました")


def _parse_codex_events(stdout: str) -> tuple[str, int]:
    """codex exec --json の JSONL 出力から (最終 agent_message の text, web fetch数) を取り出す。
    不正な行はスキップする。web fetch数は「検索したフリ」検出に使い、次の2経路を合算する:
    - command_execution(completed)のうち command に curl/wget/URL が含まれる件数
      （シェル経由でのWeb取得。MiniMax等、組み込みWeb検索ツールを持たない構成向け）
    - web_search(completed)のうちqueryが空でない件数（chatgptプロバイダ配下のモデルが
      持つ組み込みWeb検索ツールの呼び出し。issue #82: 実ChatGPT認証経由のモデルはシェルの
      curl/wgetでなくこのitem typeでWeb検索するため、command_executionだけを見ると常に
      0件になる）。

    注意: item.type=="web_search"というリテラルは、ローカルのcodex-cli 0.144.0バイナリの
    埋め込み文字列（command_executionと同じsnake_case命名列に隣接して出現）と、
    `codex app-server generate-json-schema`が出力するapp-server v2プロトコル
    （camelCaseの"webSearch"、フィールドはid/query/action）を突き合わせた静的検証で
    確認したものであり、CODEX_PROVIDER=chatgptの実ライブ出力(*.jsonl)で直接確認した
    わけではない（実ChatGPT認証での実行はAPI利用を伴うため、無許可では行っていない）。
    もしこのリテラルが誤っていた場合でも本分岐は単に発火せず、修正前と同じfetch_count=0の
    挙動に留まるだけで新たな害はない。
    """
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
        elif item_type == "web_search":
            if item.get("query"):
                fetch_count += 1
    return last_message, fetch_count


_CHATGPT_HOME_LOCK_TIMEOUT_SECONDS = 3600.0
_CHATGPT_HOME_LOCK_RETRY_SECONDS = 1.0


@contextmanager
def _chatgpt_home_lock():
    """config.CODEX_CHATGPT_HOME(固定パス)への同時アクセスを直列化する。
    TEXT_BACKEND=codexはtimeout=None(無制限)になり得るため、複数チャンネルの
    cronジョブが並行してCODEX_PROVIDER=chatgptを使うと、後発プロセスの
    _ensure_chatgpt_codex_home が先行プロセスの実行中ホーム(auth.json含む)を
    rmtreeで破壊したり、両者の _sync_refreshed_chatgpt_auth が混線したりしうる。
    ロック保持中は隔離ホームの用意〜認証同期までを1プロセスに限定する。
    タイムアウトは通常の生成時間を十分に超える値にし、ロック保持者が異常終了
    した場合等の最終的なフェイルセーフとしてのみ働かせる。"""
    lock_path = config.CODEX_CHATGPT_HOME_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _CHATGPT_HOME_LOCK_TIMEOUT_SECONDS
    with lock_path.open("a+", encoding="utf-8") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "CODEX_PROVIDER=chatgpt の隔離ホームlockを"
                        f"{_CHATGPT_HOME_LOCK_TIMEOUT_SECONDS:g}秒以内に取得できません"
                        "（他のdociプロセスが使用中の可能性があります）"
                    )
                time.sleep(_CHATGPT_HOME_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_codex(prompt: str, model: str, timeout: int | None = 600, min_web_fetches: int = 1) -> str:
    """codex exec (--json) をヘッドレスで実行し、最終 agent_message の text を返す。

    隔離 CODEX_HOME を毎回用意して実行する（ユーザーの実 ~/.codex には一切書き込まない。
    chatgptプロバイダの詳細は _ensure_chatgpt_codex_home を参照）。approval_policy等は
    無人実行が承認待ちで詰まらないよう毎回 `-c` で明示上書きする（対話用 config.toml の
    値に依存しない）。min_web_fetches=0（web取得を要求しない呼び出し）ではサンドボックスの
    ネットワークアクセスも無効化し、外部送信の経路自体を塞ぐ。
    web fetch(curl/wget/URL実行、または組み込みweb_searchツール呼び出し。詳細は
    _parse_codex_events参照)が min_web_fetches 未満なら「検索したフリ」とみなし
    ValueError にする（呼び出し側の既存リトライ/劣化継続に乗せる）。

    CODEX_PROVIDER=chatgptでは、min_web_fetches>=1（外部Webページの内容をプロンプトへ
    取り込む呼び出し）を既定で拒否する。ネットワーク有効サンドボックス内に実ChatGPT
    認証(auth.json)が置かれるため、プロンプトインジェクション経由でトークンを外部送信
    される経路になり得る。CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB=1で明示許可した場合のみ通す。

    chatgptプロバイダは固定パスの隔離ホームを毎回作り直すため、複数チャンネルの
    並行実行と衝突しないよう _chatgpt_home_lock で1プロセスに直列化する
    （詳細はそのdocstringを参照）。
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
    if config.CODEX_PROVIDER == "chatgpt":
        with _chatgpt_home_lock():
            return _run_codex_once(prompt, model, timeout, min_web_fetches)
    return _run_codex_once(prompt, model, timeout, min_web_fetches)


def _run_codex_once(
    prompt: str, model: str, timeout: int | None, min_web_fetches: int
) -> str:
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
