"""横断の安全な書き込み機構: アトミック書き込み・バックアップ・ロック・稼働中判定。

`doci/output_cleanup.py` の `_write_json_atomic`／`fcntl.flock` パターンをテキスト
書き込み・任意サーフェス向けに一般化したもの。新規発明はしない。
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .. import config

DEFAULT_KEEP = 20

# バックアップ/ロックの対象として許される surface 名の厳格な allowlist。
# `_backup_dir()` がこれを検証しないと、クライアントが `surface` に絶対パス
# (例: "/Users/azumag/.ssh") を渡した場合に `Path("base") / "/abs/path"` が
# pathlib の仕様で "/abs/path" 側だけが使われてしまい、`output/.admin_backups/`
# の外側 — 任意のディレクトリ — を列挙できてしまう(実際に `~/.ssh` の一覧化を
# 確認した)。「クライアントは生パスを渡さない」という設計上の不変条件をここでも守る。
VALID_SURFACES = frozenset({"env", "channel", "prompt", "code_prompt"})


def _require_valid_surface(surface: str) -> str:
    if surface not in VALID_SURFACES:
        raise ValueError(f"不正なsurfaceです: {surface}")
    return surface


# バックアップ・ロックの置き場所。output/ は .gitignore 対象なので、.env の秘密情報を
# 含むバックアップがgitへ紛れ込むことがない。config.OUTPUT を毎回動的に参照する
# （モジュール読み込み時に定数化すると、テストでの `patch.object(config, "OUTPUT", ...)`
# が効かなくなる — tests/test_channel_spec.py 等の既存パターンと同じ理由）。
def _backup_root() -> Path:
    return config.OUTPUT / ".admin_backups"


def _lock_dir() -> Path:
    return config.OUTPUT / ".admin_locks"


_CRON_LOCK_RE = re.compile(r"^\.cron_generate_.*\.lock$")


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """同一ディレクトリへ一時ファイルを書き、fsync後にatomic renameする。

    途中で例外が起きても対象ファイルは書き換わらない（tmpが残っても finally で消す）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = None
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    # tmpは常に0600で新規作成する(umask依存で一時的に世出読取可能になる窓を作らない)。
    # 最終的なmodeがそれより緩い場合だけ、rename直前に緩める。
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class BackupEntry:
    timestamp: str
    path: Path
    size: int


_UNSAFE_NAME_CHARS_RE = re.compile(r"[^A-Za-z0-9_:-]")


def _backup_dir(surface: str, name: str) -> Path:
    _require_valid_surface(surface)
    # `.replace("/", "__")` だけでは ".." 単体を素通りさせてしまい、
    # `_backup_root()/surface/".."` が `_backup_root()` 自身へ解決されて
    # 他surfaceのディレクトリ構造が疑似バックアップとして見えてしまう
    # (surfaceのallowlistと同じ「クライアントは生パスを渡さない」不変条件が
    # name側では貫徹されていなかった。リポジトリ側Claude Actionのレビューで
    # 指摘・実際に再現した)。英数字・アンダースコア・コロン・ハイフン以外は
    # 全て "_" に置き換えるallowlist方式にし、"." を残さないことで
    # ".."を含むあらゆる相対パス表現を無害化する。
    safe_name = _UNSAFE_NAME_CHARS_RE.sub("_", name)
    return _backup_root() / surface / safe_name


def backup(path: Path, *, surface: str, name: str, keep: int = DEFAULT_KEEP) -> Path | None:
    """書き込み直前の現在の内容を退避する。対象がまだ存在しなければ何もしない。"""
    # surfaceの検証は存在チェックより先に行う。後だと「対象が無い」で早期returnする
    # 経路が検証をすり抜けてしまい、不正なsurfaceを渡しても静かに無視されてしまう。
    _require_valid_surface(surface)
    if not path.is_file():
        return None
    dest_dir = _backup_dir(surface, name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # マイクロ秒だけでは連続保存で衝突しうる（実際に単体テストで再現した:
    # 5回連続backup()した結果1ファイルにしかならなかった）。短いランダム値を
    # 足して一意性を保証する。ソート順は timestamp が先頭なので実用上崩れない。
    dest = dest_dir / f"{stamp}-{uuid.uuid4().hex[:8]}{path.suffix or '.bak'}"
    data = path.read_bytes()
    tmp = dest.with_name(f".{dest.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    # バックアップは元ファイルのmodeを引き継がず常に0600にする。.envのように元が
    # 0644でも(実リポジトリで実際に0644だった)、秘密情報を含みうる退避先である
    # output/.admin_backups/ 配下は常に非公開にする。
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    _prune_backups(dest_dir, keep)
    return dest


def _prune_backups(dest_dir: Path, keep: int) -> None:
    entries = sorted(dest_dir.iterdir(), key=lambda p: p.name)
    excess = len(entries) - keep
    if excess <= 0:
        # entries[: negative] は「末尾N件を除く全部」を意味してしまい、keep未満の
        # 個数しか無い場合に既存バックアップを誤って消してしまう（実際に単体テストで
        # 再現した: keep=3で5回保存した結果、直近1件しか残らなかった）。
        return
    for stale in entries[:excess]:
        try:
            stale.unlink()
        except FileNotFoundError:
            pass


def list_backups(surface: str, name: str) -> list[BackupEntry]:
    dest_dir = _backup_dir(surface, name)
    if not dest_dir.is_dir():
        return []
    out = []
    for entry in sorted(dest_dir.iterdir(), key=lambda p: p.name, reverse=True):
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        out.append(BackupEntry(timestamp=entry.stem, path=entry, size=size))
    return out


@contextmanager
def surface_lock(name: str) -> Iterator[None]:
    """対象サーフェス単位の排他ロック。2タブ同時保存でのread-modify-write競合を防ぐ。"""
    lock_dir = _lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.:-]", "_", name)
    lock_path = lock_dir / f"{safe_name}.lock"
    with lock_path.open("a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class RunningRun:
    run_name: str
    pid: int
    lock_path: Path


def pipeline_running() -> list[RunningRun]:
    """tools/cron_generate.sh が書くロックファイルから、稼働中のcron runを列挙する。"""
    out: list[RunningRun] = []
    if not config.OUTPUT.is_dir():
        return out
    for lock_path in sorted(config.OUTPUT.glob(".cron_generate_*.lock")):
        if not _CRON_LOCK_RE.match(lock_path.name):
            continue
        try:
            raw = lock_path.read_text(encoding="utf-8").strip()
            pid = int(raw)
        except (OSError, ValueError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            # 別ユーザーのPIDが再利用された等。存在はするとみなす。
            pass
        run_name = lock_path.name[len(".cron_generate_") : -len(".lock")]
        out.append(RunningRun(run_name=run_name, pid=pid, lock_path=lock_path))
    return out
