"""配信投稿のディスパッチ (issue #3)。

`route.platforms`（短尺=youtube_short/tiktok/reels、長尺=youtube）を正規プラットフォーム
(youtube/tiktok/instagram) に写像・重複排除し、各 PUBLISH_* と資格情報で有効なものだけに
投稿する。`PUBLISH_DRY_RUN`（または dry_run 引数）で実投稿せずログのみ。各段は独立に
try され、1つが失敗しても他は続行する。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config

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
    status: str  # "ok" | "skipped" | "error" | "dry_run"
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


def _enabled(platform: str) -> tuple[bool, str]:
    """(有効か, 無効理由)。資格情報の有無もここで判定。"""
    if platform == "youtube":
        if not config.PUBLISH_YOUTUBE:
            return False, "PUBLISH_YOUTUBE=0"
        return True, ""
    if platform == "tiktok":
        if not config.PUBLISH_TIKTOK:
            return False, "PUBLISH_TIKTOK=0"
        if not (config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET):
            return False, "TikTok資格情報(TIKTOK_CLIENT_KEY/SECRET)未設定"
        return True, ""
    if platform == "instagram":
        if not config.PUBLISH_INSTAGRAM:
            return False, "PUBLISH_INSTAGRAM=0(後回し・公開ホスト未定)"
        return True, ""
    return False, f"unknown platform: {platform}"


def _do_upload(
    platform: str, video: Path, title: str, description: str, tags: list[str], route,
    thumbnail: Path | None = None,
) -> PublishResult:
    if platform == "youtube":
        from . import youtube

        desc = description + (f"\n\n{route.hashtag}" if route.hashtag else "")
        ytags = tags + (["Shorts"] if route.is_youtube_short and "Shorts" not in tags else [])
        vid = youtube.upload(video, title, desc, ytags)
        if thumbnail is not None:
            # サムネイル設定はおまけ機能。失敗しても動画投稿自体の成功は損なわない。
            try:
                youtube.set_thumbnail(vid, thumbnail)
            except Exception as e:  # noqa: BLE001
                print(f"[doci] サムネイル設定失敗（動画投稿は成功のまま継続）: {e}")
        return PublishResult("youtube", "ok", url=f"https://youtu.be/{vid}", id=vid)
    if platform == "tiktok":
        from . import tiktok

        r = tiktok.upload(video, title, description, tags)
        return PublishResult("tiktok", "ok", id=r.get("publish_id"), detail=r.get("status", ""))
    if platform == "instagram":
        from . import instagram

        r = instagram.upload(video, title, description, tags)
        return PublishResult("instagram", "ok", id=r.get("id"), url=r.get("permalink"))
    return PublishResult(platform, "error", detail="未対応プラットフォーム")


def publish(
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    route,
    dry_run: bool | None = None,
    thumbnail: Path | None = None,
) -> list[PublishResult]:
    """route に従い有効プラットフォームへ投稿。各結果を返す（例外は投げない）。"""
    dry = config.PUBLISH_DRY_RUN if dry_run is None else dry_run
    results: list[PublishResult] = []
    for platform in _canonical_platforms(route):
        ok, why = _enabled(platform)
        if not ok:
            results.append(PublishResult(platform, "skipped", detail=why))
            continue
        if dry:
            results.append(PublishResult(platform, "dry_run", detail="PUBLISH_DRY_RUN: 実投稿せず"))
            continue
        try:
            results.append(_do_upload(platform, Path(video_path), title, description, tags, route, thumbnail))
        except Exception as e:  # noqa: BLE001 1つ失敗しても他は続行
            results.append(PublishResult(platform, "error", detail=str(e)[:200]))
    return results
