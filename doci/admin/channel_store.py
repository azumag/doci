"""channel.toml の読込・ステージング検証・保存。

`tomllib` は読み取り専用でダンプ機能が無く、実際の `channel.toml` は issue番号を
引用する詳細な理由コメントを含むため、dictへ変換して丸ごとダンプし直す往復は
取らない（コメントを全部消してしまう）。生テキストをそのまま編集対象にし、
検証は既存の `doci.channel.load()` をそのまま再利用する。
"""
from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import warnings
from pathlib import Path

from .. import channel, config
from . import safeio


class ChannelNotFoundError(KeyError):
    pass


@dataclasses.dataclass(frozen=True)
class ChannelValidation:
    ok: bool
    error: str
    warnings: list[str]
    summary: dict | None


@dataclasses.dataclass(frozen=True)
class SaveResult:
    ok: bool
    error: str
    warnings: list[str]
    code: int
    needs_confirmation: bool = False
    summary: dict | None = None
    fingerprint: str = ""


def discover() -> list[str]:
    return channel.discover()


def toml_path(channel_id: str) -> Path:
    return config.ROOT / "channels" / channel_id / "channel.toml"


def read_toml(channel_id: str) -> str:
    _require_known(channel_id)
    path = toml_path(channel_id)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_known(channel_id: str) -> None:
    if channel_id not in channel.discover():
        raise ChannelNotFoundError(channel_id)


def _summarize(spec: channel.ChannelSpec) -> dict:
    """ChannelSpecをJSON化可能な「解決後プレビュー」に変換する。"""

    def _path(p: Path | None) -> str | None:
        return str(p) if p is not None else None

    return {
        "id": spec.id,
        "name": spec.name,
        "root": _path(spec.root),
        "rotation": list(spec.rotation),
        "corners": {
            key: {
                "label": c.label,
                "persona_path": _path(c.persona_path),
                "corner_path": _path(c.corner_path),
                "voice_key": c.voice_key,
            }
            for key, c in spec.corners.items()
        },
        "voices_path": _path(spec.voices_path),
        "style": {
            "theme": spec.style.theme,
            "bgm": {
                "dir": _path(spec.style.bgm.dir),
                "volume": spec.style.bgm.volume,
                "rotation": spec.style.bgm.rotation,
            },
        },
        "publish": {
            "platforms": list(spec.publish.platforms),
            "youtube": {
                "privacy": spec.publish.youtube.privacy,
                "client_secret": _path(spec.publish.youtube.client_secret),
                "token": _path(spec.publish.youtube.token),
                "analytics_token": _path(spec.publish.youtube.analytics_token),
                "review_enabled": spec.publish.youtube.review.enabled,
            },
            "tiktok": {
                "token": _path(spec.publish.tiktok.token),
                "privacy": spec.publish.tiktok.privacy,
            },
            "instagram": {
                "user_id": spec.publish.instagram.user_id,
                "access_token_env": spec.publish.instagram.access_token_env,
            },
        },
        "pipeline": dict(spec.pipeline),
    }


def validate_candidate(channel_id: str, toml_text: str) -> ChannelValidation:
    _require_known(channel_id)
    real_dir = config.ROOT / "channels" / channel_id
    with tempfile.TemporaryDirectory() as td:
        staged_channels = Path(td) / "channels"
        staged_dir = staged_channels / channel_id
        staged_dir.mkdir(parents=True)
        for entry in real_dir.iterdir():
            if entry.name == "channel.toml":
                continue
            (staged_dir / entry.name).symlink_to(entry)
        (staged_dir / "channel.toml").write_text(toml_text, encoding="utf-8")
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                spec = channel.load(channel_id, channels_dir=staged_channels)
        except channel.ChannelConfigError as exc:
            return ChannelValidation(ok=False, error=str(exc), warnings=[], summary=None)
    return ChannelValidation(
        ok=True,
        error="",
        warnings=[str(w.message) for w in caught],
        summary=_summarize(spec),
    )


def validate_real(channel_id: str) -> ChannelValidation:
    """既に保存済みの実ファイルを、ステージング無しで直接 `channel.load()` する。

    `validate_candidate()` は常に一時ディレクトリへ待避してから読み込むため、
    戻り値の `summary["root"]` はこの関数呼び出しが終わった時点で既に削除された
    一時パスを指してしまう（実際に確認した: `save()` が「実ディレクトリに対して
    再度読み込み」のつもりで `validate_candidate()` を呼び直していたが、実際には
    毎回新しい一時ディレクトリへステージングし直すだけだった）。読み込み対象が
    「これから保存する未検証の候補」ではなく「既にディスク上にある実ファイル」の
    場合はステージングそのものが不要なので、この関数を使う。
    """
    _require_known(channel_id)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            spec = channel.load(channel_id)
    except channel.ChannelConfigError as exc:
        return ChannelValidation(ok=False, error=str(exc), warnings=[], summary=None)
    return ChannelValidation(
        ok=True,
        error="",
        warnings=[str(w.message) for w in caught],
        summary=_summarize(spec),
    )


def save(
    channel_id: str,
    toml_text: str,
    *,
    confirm_warnings: bool = False,
    base_fingerprint: str | None = None,
) -> SaveResult:
    _require_known(channel_id)
    with safeio.surface_lock(f"channel:{channel_id}"):
        current_text = read_toml(channel_id)
        current_fp = content_fingerprint(current_text)
        if base_fingerprint is not None and base_fingerprint != current_fp:
            return SaveResult(
                ok=False,
                error="保存の直前に channel.toml が別の変更で更新されていました。最新の内容を読み込み直してください。",
                warnings=[],
                code=409,
            )

        validation = validate_candidate(channel_id, toml_text)
        if not validation.ok:
            return SaveResult(ok=False, error=validation.error, warnings=[], code=400)
        if validation.warnings and not confirm_warnings:
            return SaveResult(
                ok=False,
                error="",
                warnings=validation.warnings,
                code=409,
                needs_confirmation=True,
            )

        path = toml_path(channel_id)
        safeio.backup(path, surface="channel", name=channel_id)
        safeio.atomic_write_text(path, toml_text)

        # 実ディレクトリに対して直接読み込み、実パスでのサマリを返す
        # (validate_candidate()は常に一時ディレクトリへステージングするため使わない)。
        real_validation = validate_real(channel_id)
        return SaveResult(
            ok=True,
            error="",
            warnings=real_validation.warnings,
            code=200,
            summary=real_validation.summary,
            fingerprint=content_fingerprint(toml_text),
        )
