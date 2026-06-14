"""尺（秒）→配信ルーティング（issue #3 の基本実装）。

ナレーション実測レートは約5.5〜6.3字/秒。投稿先の上限・好適域:
- YouTube Shorts: 最大180秒・縦。好適域おおむね30〜60秒。
- Instagram Reels: 最大180秒・縦。好適域15〜30秒。
- TikTok: 最大600秒。好適域21〜34秒。

v1 で実投稿するのは YouTube のみ。tier と platforms は将来のマルチ投稿用メタも兼ねる。
"""
from __future__ import annotations

from dataclasses import dataclass

# YouTube Shorts / IG Reels の尺上限（縦動画。これを超えると Short 扱いにならない）
SHORTS_MAX_SEC = 180


@dataclass(frozen=True)
class Route:
    tier: str  # "short" | "long_short" | "longform"
    is_youtube_short: bool  # YouTube で Short として出すか
    platforms: list[str]  # 推奨投稿先（将来のマルチ投稿用メタ）
    hashtag: str  # タイトル/概要に付すタグ（Short のとき "#Shorts"、長尺は ""）


def classify(duration_sec: float) -> Route:
    """ナレーション（≒動画）の尺から配信ルートを決める。"""
    if duration_sec <= 60:
        # どのショート枠にも好適。最も拡散しやすい帯。
        return Route("short", True, ["youtube_short", "tiktok", "reels"], "#Shorts")
    if duration_sec <= SHORTS_MAX_SEC:
        # まだ Short/Reels の上限内。やや長めなので TikTok 寄り。
        return Route("long_short", True, ["youtube_short", "reels", "tiktok"], "#Shorts")
    # 180秒超は Short にならない → 通常の YouTube 動画として扱う。
    return Route("longform", False, ["youtube"], "")
