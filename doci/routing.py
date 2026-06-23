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
    landscape: bool  # 出力の向き。通常動画(longform)は横16:9、ショートは縦9:16


def classify(duration_sec: float) -> Route:
    """ナレーション（≒動画）の尺から配信ルートを決める。"""
    if duration_sec <= 60:
        # どのショート枠にも好適。最も拡散しやすい帯。
        return Route("short", True, ["youtube_short", "tiktok", "reels"], "#Shorts", False)
    if duration_sec <= SHORTS_MAX_SEC:
        # まだ Short/Reels の上限内。やや長めなので TikTok 寄り。
        return Route("long_short", True, ["youtube_short", "reels", "tiktok"], "#Shorts", False)
    # 180秒超は Short にならない → 通常の YouTube 動画（横16:9）として扱う。
    return Route("longform", False, ["youtube"], "", True)


def output_spec(route: Route, base_w: int, base_h: int) -> tuple[int, int, str]:
    """route と基準寸法(縦想定)から (width, height, orientation) を返す。

    longform は横（長辺×短辺を入替）、それ以外は縦のまま。base は config の
    VIDEO_WIDTH/HEIGHT を想定（縦 1080x1920）。
    """
    long_e, short_e = max(base_w, base_h), min(base_w, base_h)
    if route.landscape:
        return long_e, short_e, "landscape"   # 1920x1080
    return short_e, long_e, "portrait"          # 1080x1920
