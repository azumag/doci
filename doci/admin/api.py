"""HTTP非依存の純粋なAPIディスパッチ層。

`dispatch(method, path, body) -> (status, payload)` はソケット・ヘッダを一切見ない
純粋関数。CSRF/Host/Originヘッダの検査は `server.py` 側の責務（HTTPの文脈が要る
ため）。これにより全エンドポイントをソケット無しで単体テストできる。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import channel
from . import (
    channel_store,
    code_prompt_registry,
    code_prompt_store,
    env_store,
    markdown_store,
    safeio,
)


def _asdict(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _asdict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_asdict(v) for v in obj]
    if isinstance(obj, Path):
        # RunningRun.lock_path 等、PathをそのままJSONへ渡すと
        # `TypeError: Object of type PosixPath is not JSON serializable` になる
        # (cron稼働中バナーの根拠となる/api/statusが丸ごと壊れるのを実際に確認した)。
        return str(obj)
    return obj


def _ok(payload: dict, code: int = 200) -> tuple[int, dict]:
    return code, payload


def _err(message: str, code: int = 400, **extra) -> tuple[int, dict]:
    return code, {"error": message, **extra}


def dispatch(method: str, path: str, body: dict | None) -> tuple[int, dict]:
    split = urlsplit(path)
    parts = [p for p in split.path.split("/") if p]
    query = {k: v[0] for k, v in parse_qs(split.query).items()}
    body = body or {}

    try:
        return _route(method.upper(), parts, query, body)
    except (
        env_store.EnvValueError,
        code_prompt_store.PromptSourceError,
        channel.ChannelConfigError,
    ) as exc:
        # channel.ChannelConfigError: _channels_list()/markdown_store._slot_map() は
        # 一覧系なので個別にtry/exceptして1チャンネルの破損を道連れにしないが、
        # _channel_voices() 等の単一チャンネル向けエンドポイントは channel.load() を
        # 直接呼んでおり、ここで拾わないと汎用500(server.py)まで素通りしてしまう。
        return _err(str(exc), 400)
    except (
        channel_store.ChannelNotFoundError,
        markdown_store.SlotNotFoundError,
        code_prompt_store.PromptNotFoundError,
    ) as exc:
        return _err(f"見つかりません: {exc}", 404)


def _route(method: str, parts: list[str], query: dict, body: dict) -> tuple[int, dict]:
    if not parts or parts[0] != "api":
        return _err("not found", 404)
    rest = parts[1:]

    if method == "GET" and rest == ["status"]:
        return _status()

    if rest == ["env"]:
        if method == "GET":
            return _env_list()
        return _err("method not allowed", 405)
    if rest == ["env", "validate"] and method == "POST":
        return _env_validate(body)
    if rest == ["env", "save"] and method == "POST":
        return _env_save(body)

    if rest == ["channels"] and method == "GET":
        return _channels_list()
    if len(rest) == 3 and rest[0] == "channels" and rest[2] == "toml" and method == "GET":
        return _channel_toml(rest[1])
    if len(rest) == 3 and rest[0] == "channels" and rest[2] == "voices" and method == "GET":
        return _channel_voices(rest[1])
    if len(rest) == 3 and rest[0] == "channels" and rest[2] == "validate" and method == "POST":
        return _channel_validate(rest[1], body)
    if len(rest) == 3 and rest[0] == "channels" and rest[2] == "save" and method == "POST":
        return _channel_save(rest[1], body)

    if rest == ["prompts"] and method == "GET":
        return _prompts_list(query.get("channel"))
    if len(rest) == 2 and rest[0] == "prompts" and method == "GET":
        return _prompt_read(rest[1])
    if len(rest) == 3 and rest[0] == "prompts" and rest[2] == "validate" and method == "POST":
        return _prompt_validate(rest[1], body)
    if len(rest) == 3 and rest[0] == "prompts" and rest[2] == "save" and method == "POST":
        return _prompt_save(rest[1], body)

    if rest == ["code-prompts"] and method == "GET":
        return _code_prompts_list()
    if len(rest) == 2 and rest[0] == "code-prompts" and method == "GET":
        return _code_prompt_read(rest[1])
    if len(rest) == 3 and rest[0] == "code-prompts" and rest[2] == "validate" and method == "POST":
        return _code_prompt_validate(rest[1], body)
    if len(rest) == 3 and rest[0] == "code-prompts" and rest[2] == "save" and method == "POST":
        return _code_prompt_save(rest[1], body)

    if rest == ["backups"] and method == "GET":
        return _backups_list(query.get("target", ""))
    if rest == ["backups", "restore"] and method == "POST":
        return _backups_restore(body)

    return _err("not found", 404)


# --- status ---


def _status() -> tuple[int, dict]:
    running = safeio.pipeline_running()
    return _ok(
        {
            "channels": channel.discover(),
            "pipeline_running": [_asdict(r) for r in running],
        }
    )


# --- env ---


def _env_list() -> tuple[int, dict]:
    entries = env_store.read_entries()
    return _ok(
        {
            "entries": [_asdict(e) for e in entries],
            "fingerprint": env_store.content_fingerprint(env_store.read_env_text()),
        }
    )


def _env_validate(body: dict) -> tuple[int, dict]:
    changes = body.get("changes") or {}
    enable = body.get("enable") or []
    try:
        candidate, patch_warnings = env_store.apply_patch(
            env_store.read_env_text(), changes, enable
        )
    except env_store.EnvValueError as exc:
        return _err(str(exc), 400)
    result = env_store.validate_candidate(candidate)
    return _ok(
        {
            "ok": result.ok and not patch_warnings,
            "error": result.error,
            "warnings": result.warnings + patch_warnings,
            "channels": result.channels,
        }
    )


def _env_save(body: dict) -> tuple[int, dict]:
    changes = body.get("changes") or {}
    enable = body.get("enable") or []
    confirm_warnings = bool(body.get("confirm_warnings"))
    base_fingerprint = body.get("base_fingerprint")
    result = env_store.save(
        changes,
        enable=enable,
        confirm_warnings=confirm_warnings,
        base_fingerprint=base_fingerprint,
    )
    return _ok(_asdict(result), result.code)


# --- channels ---


def _channels_list() -> tuple[int, dict]:
    ids = channel_store.discover()
    out = []
    for cid in ids:
        # 1チャンネルのchannel.tomlが壊れていても一覧全体を道連れにしない
        # （このUIはまさにそのchannel.tomlを直すためのツールなので、直せなくなる
        # 事態を避ける。実際にchannel.load()の未捕捉例外でエンドポイント全体が
        # 落ちることを確認した）。
        try:
            spec = channel.load(cid)
        except channel.ChannelConfigError as exc:
            out.append({"id": cid, "error": str(exc)})
            continue
        out.append(
            {
                "id": spec.id,
                "name": spec.name,
                "rotation": list(spec.rotation),
                "corners": list(spec.corners.keys()),
            }
        )
    return _ok({"channels": out})


def _channel_toml(channel_id: str) -> tuple[int, dict]:
    text = channel_store.read_toml(channel_id)
    # 読み込み対象は既にディスク上にある実ファイルなので、validate_candidate()の
    # 一時ディレクトリへのステージングは不要（validate_real()は実ディレクトリを
    # 直接読むため、summary内の解決済みパスも実パスになる）。
    validation = channel_store.validate_real(channel_id)
    return _ok(
        {
            "text": text,
            "fingerprint": channel_store.content_fingerprint(text),
            "validation": _asdict(validation),
        }
    )


def _channel_voices(channel_id: str) -> tuple[int, dict]:
    from .. import voices as voices_mod

    spec = channel.load(channel_id)
    loaded = voices_mod.load(spec.voices_path)
    return _ok({"voices": sorted(loaded.keys())})


def _channel_validate(channel_id: str, body: dict) -> tuple[int, dict]:
    text = body.get("text", "")
    validation = channel_store.validate_candidate(channel_id, text)
    return _ok(_asdict(validation))


def _channel_save(channel_id: str, body: dict) -> tuple[int, dict]:
    text = body.get("text", "")
    confirm_warnings = bool(body.get("confirm_warnings"))
    base_fingerprint = body.get("base_fingerprint")
    result = channel_store.save(
        channel_id,
        text,
        confirm_warnings=confirm_warnings,
        base_fingerprint=base_fingerprint,
    )
    return _ok(_asdict(result), result.code)


# --- markdown prompts ---


def _prompts_list(channel_id: str | None) -> tuple[int, dict]:
    prompts = markdown_store.list_prompts(channel_id)
    return _ok({"prompts": [_asdict(p) for p in prompts]})


def _prompt_read(slot: str) -> tuple[int, dict]:
    return _ok(markdown_store.read_prompt(slot))


def _prompt_validate(slot: str, body: dict) -> tuple[int, dict]:
    text = body.get("text", "")
    warnings = markdown_store.validate(slot, text)
    return _ok({"ok": True, "warnings": warnings})


def _prompt_save(slot: str, body: dict) -> tuple[int, dict]:
    text = body.get("text", "")
    confirm_warnings = bool(body.get("confirm_warnings"))
    base_fingerprint = body.get("base_fingerprint")
    result = markdown_store.save(
        slot, text, confirm_warnings=confirm_warnings, base_fingerprint=base_fingerprint
    )
    return _ok(_asdict(result), result.code)


# --- code prompts ---


def _code_prompts_list() -> tuple[int, dict]:
    entries = [
        {
            "id": e.id,
            "name": e.name,
            "relpath": e.relpath,
            "fields": sorted(e.fields),
            "call_site": e.call_site,
            "guarded_by": list(e.guarded_by),
            "description": e.description,
        }
        for e in code_prompt_registry.REGISTRY
    ]
    return _ok({"code_prompts": entries})


def _code_prompt_read(const_id: str) -> tuple[int, dict]:
    return _ok(code_prompt_store.read(const_id))


def _code_prompt_validate(const_id: str, body: dict) -> tuple[int, dict]:
    text = body.get("text", "")
    result = code_prompt_store.validate(const_id, text)
    return _ok(_asdict(result))


def _code_prompt_save(const_id: str, body: dict) -> tuple[int, dict]:
    text = body.get("text", "")
    confirm_warnings = bool(body.get("confirm_warnings"))
    base_fingerprint = body.get("base_fingerprint")
    run_guarded_tests = body.get("run_guarded_tests", True)
    result = code_prompt_store.save(
        const_id,
        text,
        confirm_warnings=confirm_warnings,
        base_fingerprint=base_fingerprint,
        run_guarded_tests=bool(run_guarded_tests),
    )
    return _ok(_asdict(result), result.code)


# --- backups ---


def _backups_list(target: str) -> tuple[int, dict]:
    if ":" not in target:
        return _err("target は '<surface>:<name>' 形式で指定してください", 400)
    surface, name = target.split(":", 1)
    if surface not in safeio.VALID_SURFACES:
        return _err(f"不正なsurfaceです: {surface}", 400)
    entries = safeio.list_backups(surface, name)
    return _ok(
        {
            "backups": [
                {"timestamp": e.timestamp, "size": e.size, "path": str(e.path)} for e in entries
            ]
        }
    )


def _backups_restore(body: dict) -> tuple[int, dict]:
    target = body.get("target", "")
    timestamp = body.get("timestamp", "")
    if ":" not in target:
        return _err("target は '<surface>:<name>' 形式で指定してください", 400)
    surface, name = target.split(":", 1)
    if surface not in safeio.VALID_SURFACES:
        return _err(f"復元できないsurfaceです: {surface}", 400)
    entries = {e.timestamp: e for e in safeio.list_backups(surface, name)}
    backup_entry = entries.get(timestamp)
    if backup_entry is None:
        return _err("指定のバックアップが見つかりません", 404)
    backup_text = backup_entry.path.read_text(encoding="utf-8")

    # channel/promptは各storeのsave()（検証→backup→atomic write→lockを内蔵）を
    # そのまま再利用する。復元操作そのものが明示的な確認なのでconfirm_warnings=True。
    if surface == "channel":
        result = channel_store.save(name, backup_text, confirm_warnings=True)
        return _ok(_asdict(result), result.code)

    if surface == "prompt":
        result = markdown_store.save(name, backup_text, confirm_warnings=True)
        return _ok(_asdict(result), result.code)

    if surface == "code_prompt":
        # code_promptのバックアップは対象定数だけでなく「その時点のファイル全体」
        # (splice前のソース)を保持している。保存前テキストとして渡すのはファイル
        # 全体ではなく、そこから抽出した対象定数の値でなければならない
        # （そのままsave()へ渡すとファイル全体を1つの文字列リテラルへ押し込もうと
        # して壊れる。実際に11定数すべてで400になることを確認した）。
        entry_meta = code_prompt_registry.BY_ID.get(name)
        if entry_meta is None:
            return _err("未登録の定数IDです", 404)
        try:
            located = code_prompt_store.locate(backup_text, entry_meta.name)
        except code_prompt_store.PromptSourceError as exc:
            return _err(f"バックアップの内容から定数を復元できませんでした: {exc}", 400)
        result = code_prompt_store.save(
            name, located.value, confirm_warnings=True, run_guarded_tests=False
        )
        return _ok(_asdict(result), result.code)

    if surface == "env":
        # .envは「特定キーの差分パッチ」ではなく全文置換が復元の意味そのものなので
        # env_store.save()（変更差分ベースのAPI）は使わず、他のsave()と同じ
        # 検証→backup→atomic writeの手順をここでも(lock込みで)直接行う。
        with safeio.surface_lock("env"):
            validation = env_store.validate_candidate(backup_text)
            if not validation.ok:
                return _err(f"バックアップ内容の再検証に失敗しました: {validation.error}", 400)
            safeio.backup(env_store.env_path(), surface="env", name="env")
            safeio.atomic_write_text(env_store.env_path(), backup_text, mode=0o600)
        return _ok({"ok": True})

    return _err("復元できないsurfaceです", 400)
