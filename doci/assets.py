"""素材調達プロバイダ抽象。`ASSET_BACKEND` で切替（issue #9）。

AI生成(コスト・課金上限・学習データ素性)の前段に、実フリー素材を当てる。
取得できなければ呼び出し側が `imagegen` のAI生成へフォールバックする二段構え。

- pexels (既定): Pexels Photo API。商用OK・帰属不要・無料キー。縦(portrait)在庫が厚く、
  画像URLの `?fit=crop&w=W&h=H` でサーバ側が直接9:16にクロップして返すため、
  合成側でのクロップが不要。検索語は台本の `visual_prompt`(英語)をそのまま使える。

`fetch_image()` は素材を out_path に保存して返す。該当が無ければ None。
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, imagery

_UA = "doci/0.1 (+https://github.com/azumag/doci)"
PEXELS_SEARCH = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_SEARCH = "https://api.pexels.com/videos/search"


class AssetError(RuntimeError):
    pass


# 同一プロセス(=1本の生成)内で検索結果をキャッシュ。長尺で同じシーンの query を
# 変種ごとに何度も叩いてレート制限(403)に当たるのを防ぐ。キーは (種別, query)。
_search_cache: dict[tuple[str, str], list[dict]] = {}


def _resolve(
    width: int | None, height: int | None, orientation: str | None
) -> tuple[int, int, str]:
    """未指定なら config 既定（縦）で補完して (w, h, orientation) を返す。"""
    w = width or config.VIDEO_WIDTH
    h = height or config.VIDEO_HEIGHT
    o = orientation or config.PEXELS_ORIENTATION
    return w, h, o


# ---------------- Pexels ----------------
def _pexels_search(query: str, key: str, per_page: int, orientation: str) -> list[dict]:
    ck = ("photo", query)
    if ck in _search_cache:
        return _search_cache[ck]
    qs = urllib.parse.urlencode(
        {"query": query[:400], "orientation": orientation, "per_page": per_page}
    )
    req = urllib.request.Request(
        f"{PEXELS_SEARCH}?{qs}",
        headers={"Authorization": key, "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AssetError(f"Pexels HTTP {e.code}: {e.read().decode()[:200]}")
    photos = d.get("photos") or []
    _search_cache[ck] = photos
    return photos


def _pexels_fetch(
    query: str, out_path: Path, width: int, height: int, orientation: str, variant: int
) -> Path | None:
    key = config.PEXELS_API_KEY
    if not key:
        raise AssetError("PEXELS_API_KEY が未設定です (ASSET_BACKEND=pexels)")
    # 権利回避: 検索語からブランド/製品名を除く（generic語でも実機ロゴ混入は完全には防げない）。
    query = imagery.strip_brands(query)
    photos = _pexels_search(query, key, config.ASSET_PER_PAGE, orientation)
    if not photos:
        return None
    # 同一シーンの2枚目以降(variant>0)は別候補を選んで使い回しの単調を避ける。
    photo = photos[variant % len(photos)]
    base = (photo.get("src") or {}).get("original") or ""
    if not base:
        return None
    # 画像URLパラメータでサーバ側に直接 width×height へクロップさせる（合成側のクロップ不要）。
    url = f"{base}?auto=compress&cs=tinysrgb&fit=crop&w={width}&h={height}"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out_path.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        raise AssetError(f"Pexels画像DL HTTP {e.code}: {url}")
    return out_path


def fetch_image(
    query: str,
    out_path: Path,
    *,
    width: int | None = None,
    height: int | None = None,
    orientation: str | None = None,
    variant: int = 0,
) -> Path | None:
    """素材を1枚取得して out_path に保存。寸法/向き未指定は config 既定(縦)。該当無しは None。"""
    backend = config.ASSET_BACKEND
    if backend in ("", "none"):
        return None
    if backend == "pexels":
        w, h, o = _resolve(width, height, orientation)
        return _pexels_fetch(query, out_path, w, h, o, variant)
    raise AssetError(f"unknown ASSET_BACKEND: {backend}")


# ---------------- Pexels Videos（動画素材） ----------------
def _pexels_video_search(query: str, key: str, per_page: int, orientation: str) -> list[dict]:
    ck = ("video", query)
    if ck in _search_cache:
        return _search_cache[ck]
    qs = urllib.parse.urlencode(
        {"query": query[:400], "orientation": orientation, "per_page": per_page}
    )
    req = urllib.request.Request(
        f"{PEXELS_VIDEO_SEARCH}?{qs}",
        headers={"Authorization": key, "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AssetError(f"Pexels動画検索 HTTP {e.code}: {e.read().decode()[:200]}")
    videos = d.get("videos") or []
    _search_cache[ck] = videos
    return videos


def _best_file(video: dict, max_long_edge: int, landscape: bool) -> dict | None:
    """向き(landscape=横)に合う動画ファイルから、長辺≦max_long_edge の最大を選ぶ。
    無ければ最小（DL抑制）。"""
    files = [f for f in (video.get("video_files") or []) if f.get("link")]

    def match(f: dict) -> bool:
        w, h = f.get("width") or 0, f.get("height") or 1
        return (w >= h) if landscape else (h >= w)

    def long_edge(f: dict) -> int:
        return max(f.get("width") or 0, f.get("height") or 0)

    cand = [f for f in files if match(f)] or files
    if not cand:
        return None
    le = [f for f in cand if long_edge(f) <= max_long_edge]
    if le:
        return max(le, key=long_edge)
    return min(cand, key=long_edge)


def _pexels_video_fetch(
    query: str, out_path: Path, width: int, height: int, orientation: str, variant: int
) -> Path | None:
    key = config.PEXELS_API_KEY
    if not key:
        raise AssetError("PEXELS_API_KEY が未設定です (ASSET_BACKEND=pexels)")
    query = imagery.strip_brands(query)
    videos = _pexels_video_search(query, key, config.ASSET_PER_PAGE, orientation)
    if not videos:
        return None
    video = videos[variant % len(videos)]
    f = _best_file(video, max(width, height), orientation == "landscape")
    if not f:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(f["link"], headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out_path.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        raise AssetError(f"Pexels動画DL HTTP {e.code}")
    return out_path


def fetch_video(
    query: str,
    out_path: Path,
    *,
    width: int | None = None,
    height: int | None = None,
    orientation: str | None = None,
    variant: int = 0,
) -> Path | None:
    """動画素材を取得して out_path(mp4) に保存。該当無しは None。compose側で width×height クロップ＆尺ループ。"""
    backend = config.ASSET_BACKEND
    if backend in ("", "none"):
        return None
    if backend == "pexels":
        w, h, o = _resolve(width, height, orientation)
        return _pexels_video_fetch(query, out_path, w, h, o, variant)
    raise AssetError(f"unknown ASSET_BACKEND: {backend}")


def main() -> None:
    ap = argparse.ArgumentParser(description="素材取得テスト")
    ap.add_argument("--query", required=True)
    ap.add_argument("--orientation", default="portrait", choices=["portrait", "landscape"])
    ap.add_argument("--video", action="store_true", help="動画素材を取得")
    ap.add_argument("--variant", type=int, default=0)
    ap.add_argument("--out", default=str(config.OUTPUT / "asset_test.jpg"))
    args = ap.parse_args()
    # 向きに応じて width/height（長辺1920・短辺1080）を決める。
    long_e, short_e = max(config.VIDEO_WIDTH, config.VIDEO_HEIGHT), min(config.VIDEO_WIDTH, config.VIDEO_HEIGHT)
    w, h = (long_e, short_e) if args.orientation == "landscape" else (short_e, long_e)
    fn = fetch_video if args.video else fetch_image
    p = fn(args.query, Path(args.out), width=w, height=h, orientation=args.orientation, variant=args.variant)
    if p is None:
        print(f"[{config.ASSET_BACKEND}] 該当素材なし: {args.query!r}")
    else:
        print(f"asset[{config.ASSET_BACKEND}] -> {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
