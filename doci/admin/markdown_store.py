"""Markdown プロンプトファイル（persona/corner/output_rules系）の読込・ソフト検証・保存。

`doci/corners.py:build_prompt` は corner テンプレートを `.format()` ではなく単純な
`str.replace("{date}", ...)`/`.replace("{past_topics}", ...)` で埋め込むため、
任意の `{...}` を含んでいても実行時エラーにはならない。トークン欠落は「エラー」では
なく「その情報がサイレントに生成から抜け落ちる」ソフト警告として扱う。

slot は `shared:output_rules` / `<cid>:output_rules` / `<cid>:output_rules_addendum` /
`<cid>:persona:<corner>` / `<cid>:corner:<corner>` の不透明な識別子。クライアントから
生パスは一切受け取らず、既知のチャンネル/コーナー構造からサーバ側だけで解決する。
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

from .. import channel, config
from . import safeio


class SlotNotFoundError(KeyError):
    pass


@dataclasses.dataclass(frozen=True)
class PromptFile:
    slot: str
    path: str
    exists: bool
    required_tokens: tuple[str, ...]
    used_by: tuple[str, ...]
    creatable: bool


@dataclasses.dataclass(frozen=True)
class SaveResult:
    ok: bool
    error: str
    warnings: list[str]
    code: int
    needs_confirmation: bool = False
    fingerprint: str = ""


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slot_map(channel_id: str | None = None) -> dict[str, tuple[Path, tuple[str, ...], bool]]:
    slots: dict[str, tuple[Path, tuple[str, ...], bool]] = {
        "shared:output_rules": (config.PROMPTS / "output_rules.md", (), False)
    }
    cids = [channel_id] if channel_id else channel.discover()
    for cid in cids:
        try:
            spec = channel.load(cid)
        except channel.ChannelConfigError:
            # 一覧表示(channel_id未指定)では1チャンネルのchannel.tomlが壊れていても
            # 他チャンネルのプロンプト一覧まで道連れにしない(実際にこのUIはまさに
            # そのchannel.tomlを直すためのツールなので、直せなくなる事態を避ける)。
            # 特定チャンネルを明示指定した場合は、その壊れている事実を伝えるため
            # そのまま例外を送出する。
            if channel_id is not None:
                raise
            continue
        slots[f"{cid}:output_rules"] = (spec.root / "prompts" / "output_rules.md", (), True)
        slots[f"{cid}:output_rules_addendum"] = (
            spec.root / "prompts" / "output_rules_addendum.md",
            (),
            True,
        )
        for key, corner in spec.corners.items():
            slots[f"{cid}:persona:{key}"] = (corner.persona_path, (), False)
            slots[f"{cid}:corner:{key}"] = (
                corner.corner_path,
                ("{date}", "{past_topics}"),
                False,
            )
    return slots


def list_prompts(channel_id: str | None = None) -> list[PromptFile]:
    slots = _slot_map(channel_id)
    by_path: dict[Path, list[str]] = {}
    for slot_id, (path, _, _) in slots.items():
        key = path.resolve() if path.exists() else path
        by_path.setdefault(key, []).append(slot_id)
    out = []
    for slot_id in sorted(slots):
        path, tokens, creatable = slots[slot_id]
        key = path.resolve() if path.exists() else path
        used_by = tuple(sorted(s for s in by_path.get(key, []) if s != slot_id))
        out.append(
            PromptFile(
                slot=slot_id,
                path=str(path),
                exists=path.is_file(),
                required_tokens=tokens,
                used_by=used_by,
                creatable=creatable,
            )
        )
    return out


def _resolve_slot(slot: str) -> tuple[Path, tuple[str, ...], bool]:
    if slot == "shared:output_rules":
        return config.PROMPTS / "output_rules.md", (), False
    parts = slot.split(":")
    if len(parts) < 2:
        raise SlotNotFoundError(slot)
    cid = parts[0]
    if cid not in channel.discover():
        raise SlotNotFoundError(slot)
    spec = channel.load(cid)
    if len(parts) == 2 and parts[1] == "output_rules":
        return spec.root / "prompts" / "output_rules.md", (), True
    if len(parts) == 2 and parts[1] == "output_rules_addendum":
        return spec.root / "prompts" / "output_rules_addendum.md", (), True
    if len(parts) == 3 and parts[1] == "persona":
        corner = spec.corners.get(parts[2])
        if corner is None:
            raise SlotNotFoundError(slot)
        return corner.persona_path, (), False
    if len(parts) == 3 and parts[1] == "corner":
        corner = spec.corners.get(parts[2])
        if corner is None:
            raise SlotNotFoundError(slot)
        return corner.corner_path, ("{date}", "{past_topics}"), False
    raise SlotNotFoundError(slot)


def read_prompt(slot: str) -> dict:
    path, tokens, creatable = _resolve_slot(slot)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {
        "slot": slot,
        "path": str(path),
        "text": text,
        "exists": path.is_file(),
        "required_tokens": list(tokens),
        "creatable": creatable,
        "fingerprint": content_fingerprint(text),
    }


def validate(slot: str, text: str) -> list[str]:
    _, tokens, _ = _resolve_slot(slot)
    warnings: list[str] = []
    for token in tokens:
        if token not in text:
            warnings.append(f"{token} が含まれていません。生成時にこの情報が本文へ差し込まれなくなります。")
    if not text.strip():
        warnings.append("本文が空です。")
    return warnings


def save(
    slot: str,
    text: str,
    *,
    confirm_warnings: bool = False,
    base_fingerprint: str | None = None,
) -> SaveResult:
    path, _tokens, _creatable = _resolve_slot(slot)
    with safeio.surface_lock(f"prompt:{slot}"):
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        current_fp = content_fingerprint(current)
        if base_fingerprint is not None and base_fingerprint != current_fp:
            return SaveResult(
                ok=False,
                error="保存の直前に別の変更で更新されていました。最新の内容を読み込み直してください。",
                warnings=[],
                code=409,
            )
        warns = validate(slot, text)
        if warns and not confirm_warnings:
            return SaveResult(ok=False, error="", warnings=warns, code=409, needs_confirmation=True)
        if path.is_file():
            safeio.backup(path, surface="prompt", name=slot)
        safeio.atomic_write_text(path, text)
        return SaveResult(
            ok=True, error="", warnings=warns, code=200, fingerprint=content_fingerprint(text)
        )
