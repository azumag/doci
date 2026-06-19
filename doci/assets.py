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


class AssetError(RuntimeError):
    pass


def _dims(aspect_ratio: str) -> tuple[int, int]:
    """'9:16' 等 → 動画の縦(VIDEO_HEIGHT)基準で (w, h) を返す。"""
    try:
        aw, ah = (int(x) for x in aspect_ratio.split(":"))
        h = config.VIDEO_HEIGHT
        w = round(h * aw / ah)
        return w, h
    except Exception:  # noqa: BLE001
        return config.VIDEO_WIDTH, config.VIDEO_HEIGHT


# ---------------- Pexels ----------------
def _pexels_search(query: str, key: str, per_page: int, orientation: str) -> list[dict]:
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
    return d.get("photos") or []


def _pexels_fetch(query: str, out_path: Path, aspect_ratio: str, variant: int) -> Path | None:
    key = config.PEXELS_API_KEY
    if not key:
        raise AssetError("PEXELS_API_KEY が未設定です (ASSET_BACKEND=pexels)")
    w, h = _dims(aspect_ratio)
    # 権利回避: 検索語からブランド/製品名を除く（generic語でも実機ロゴ混入は完全には防げない）。
    query = imagery.strip_brands(query)
    photos = _pexels_search(query, key, config.ASSET_PER_PAGE, config.PEXELS_ORIENTATION)
    if not photos:
        return None
    # 同一シーンの2枚目以降(variant>0)は別候補を選んで使い回しの単調を避ける。
    photo = photos[variant % len(photos)]
    base = (photo.get("src") or {}).get("original") or ""
    if not base:
        return None
    # 画像URLパラメータでサーバ側に直接9:16クロップさせる（合成側のクロップ不要）。
    url = f"{base}?auto=compress&cs=tinysrgb&fit=crop&w={w}&h={h}"
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
    query: str, out_path: Path, aspect_ratio: str = "9:16", variant: int = 0
) -> Path | None:
    """素材を1枚取得して out_path に保存。該当無しは None、設定不備等は AssetError。"""
    backend = config.ASSET_BACKEND
    if backend in ("", "none"):
        return None
    if backend == "pexels":
        return _pexels_fetch(query, out_path, aspect_ratio, variant)
    raise AssetError(f"unknown ASSET_BACKEND: {backend}")


def main() -> None:
    ap = argparse.ArgumentParser(description="素材取得テスト")
    ap.add_argument("--query", required=True)
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--variant", type=int, default=0)
    ap.add_argument("--out", default=str(config.OUTPUT / "asset_test.jpg"))
    args = ap.parse_args()
    p = fetch_image(args.query, Path(args.out), args.aspect, args.variant)
    if p is None:
        print(f"[{config.ASSET_BACKEND}] 該当素材なし: {args.query!r}")
    else:
        print(f"asset[{config.ASSET_BACKEND}] -> {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
