"""配信投稿のディスパッチ (issue #3)。

`route.platforms`（短尺=youtube_short/tiktok/reels、長尺=youtube）を正規プラットフォーム
(youtube/tiktok/instagram) に写像・重複排除し、チャンネルの `PublishSpec`、各 PUBLISH_*
安全弁、資格情報の全てが有効な投稿先だけに投稿する。`PUBLISH_DRY_RUN`（または dry_run
引数）で実投稿せずログのみ。各段は独立に try され、1つが失敗しても他は続行する。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import config
from .channel import ChannelSpec, PublishSpec

# routing.Route.platforms の語 → 正規プラットフォーム
_CANON = {
    "youtube": "youtube",
    "youtube_short": "youtube",
    "tiktok": "tiktok",
    "reels": "instagram",
    "instagram": "instagram",
}


@dataclass
class PublishResult:
    platform: str
    status: str  # "ok" | "skipped" | "error" | "unknown" | "dry_run"
    url: str | None = None
    id: str | None = None
    detail: str = ""


def _canonical_platforms(route) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in route.platforms:
        c = _CANON.get(p, p)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _enabled(platform: str, spec: PublishSpec) -> tuple[bool, str]:
    """(有効か, 無効理由)。資格情報の有無もここで判定。"""
    if platform not in spec.platforms:
        return False, "channel publish.platforms の対象外"
    if platform == "youtube":
        if not config.PUBLISH_YOUTUBE:
            return False, "PUBLISH_YOUTUBE=0"
        if not spec.youtube.token.is_file():
            return False, f"YouTube token がありません: {spec.youtube.token}"
        return True, ""
    if platform == "tiktok":
        if not config.PUBLISH_TIKTOK:
            return False, "PUBLISH_TIKTOK=0"
        if not (config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET):
            return False, "TikTok資格情報(TIKTOK_CLIENT_KEY/SECRET)未設定"
        if not spec.tiktok.token.is_file():
            return False, f"TikTok token がありません: {spec.tiktok.token}"
        return True, ""
    if platform == "instagram":
        if not config.PUBLISH_INSTAGRAM:
            return False, "PUBLISH_INSTAGRAM=0(後回し・公開ホスト未定)"
        if not spec.instagram.user_id:
            return False, "Instagram user_id 未設定"
        if not spec.instagram.access_token_env:
            return False, "Instagram access_token_env 未設定"
        if not os.environ.get(spec.instagram.access_token_env):
            return False, f"Instagram token env 未設定: {spec.instagram.access_token_env}"
        return True, ""
    return False, f"unknown platform: {platform}"


def _credential_detail(platform: str, spec: PublishSpec) -> str:
    if platform == "youtube":
        return f"token={spec.youtube.token}"
    if platform == "tiktok":
        return f"token={spec.tiktok.token}"
    if platform == "instagram":
        return f"access_token_env={spec.instagram.access_token_env}"
    return ""


def _do_upload(
    platform: str, video: Path, title: str, description: str, tags: list[str], route,
    publish_spec: PublishSpec,
    thumbnail: Path | None = None,
    youtube_privacy: str | None = None,
) -> PublishResult:
    if platform == "youtube":
        from . import youtube

        desc = description + (f"\n\n{route.hashtag}" if route.hashtag else "")
        ytags = tags + (["Shorts"] if route.is_youtube_short and "Shorts" not in tags else [])
        settings = publish_spec.youtube
        vid = youtube.upload(
            video,
            title,
            desc,
            ytags,
            youtube_privacy or settings.privacy,
            token_file=settings.token,
            client_secret_file=settings.client_secret,
        )
        if thumbnail is not None:
            # サムネイル設定はおまけ機能。失敗しても動画投稿自体の成功は損なわない。
            try:
                youtube.set_thumbnail(
                    vid,
                    thumbnail,
                    token_file=settings.token,
                    client_secret_file=settings.client_secret,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[doci] サムネイル設定失敗（動画投稿は成功のまま継続）: {e}")
        return PublishResult("youtube", "ok", url=f"https://youtu.be/{vid}", id=vid)
    if platform == "tiktok":
        from . import tiktok

        settings = publish_spec.tiktok
        r = tiktok.upload(
            video,
            title,
            description,
            tags,
            token_file=settings.token,
            privacy=settings.privacy,
        )
        return PublishResult("tiktok", "ok", id=r.get("publish_id"), detail=r.get("status", ""))
    if platform == "instagram":
        from . import instagram

        settings = publish_spec.instagram
        r = instagram.upload(
            video,
            title,
            description,
            tags,
            user_id=settings.user_id,
            access_token=os.environ[settings.access_token_env],
        )
        return PublishResult("instagram", "ok", id=r.get("id"), url=r.get("permalink"))
    return PublishResult(platform, "error", detail="未対応プラットフォーム")


def publish(
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    route,
    spec: ChannelSpec | None = None,
    dry_run: bool | None = None,
    thumbnail: Path | None = None,
    youtube_privacy: str | None = None,
) -> list[PublishResult]:
    """route に従い有効プラットフォームへ投稿。各結果を返す（例外は投げない）。"""
    dry = config.PUBLISH_DRY_RUN if dry_run is None else dry_run
    publish_spec = spec.publish if spec is not None else PublishSpec()
    results: list[PublishResult] = []
    for platform in _canonical_platforms(route):
        ok, why = _enabled(platform, publish_spec)
        if not ok:
            results.append(PublishResult(platform, "skipped", detail=why))
            continue
        if dry:
            detail = _credential_detail(platform, publish_spec)
            results.append(
                PublishResult(
                    platform,
                    "dry_run",
                    detail=f"PUBLISH_DRY_RUN: 実投稿せず ({detail})",
                )
            )
            continue
        try:
            results.append(
                _do_upload(
                    platform,
                    Path(video_path),
                    title,
                    description,
                    tags,
                    route,
                    publish_spec,
                    thumbnail,
                    youtube_privacy,
                )
            )
        except Exception as e:  # noqa: BLE001 送信後の結果不明を安全側へ倒す
            preflight_error = False
            if platform == "youtube":
                from . import youtube

                preflight_error = isinstance(e, youtube.UploadPreflightError)
            elif platform == "tiktok":
                from . import tiktok

                preflight_error = isinstance(e, tiktok.TikTokUploadPreflightError)
            elif platform == "instagram":
                from . import instagram

                preflight_error = isinstance(e, instagram.InstagramUploadPreflightError)
            if preflight_error:
                results.append(
                    PublishResult(
                        platform,
                        "error",
                        detail=f"投稿前検証失敗: {str(e)[:180]}",
                    )
                )
                continue
            results.append(
                PublishResult(
                    platform,
                    "unknown",
                    detail=f"投稿結果不明: {str(e)[:180]}",
                )
            )
    return results
