"""Pexels API 検証プローブ（issue #9・使い捨て）。

縦(portrait)在庫・一致精度・直接DL可否を、実題材で目視評価する。
Pexels: 商用OK・帰属不要。無料キーは https://www.pexels.com/api/ で即発行。

使い方:
  PEXELS_API_KEY=<key> python tools/probe_pexels.py "Soviet Union" "subway" ...
  キー未設定ならエラーで中断。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.pexels.com/v1/search"
KEY = os.environ.get("PEXELS_API_KEY", "")
PER = int(os.environ.get("PEXELS_PER", "15"))

DEFAULT_TERMS = [
    "Soviet Union",
    "propaganda poster",
    "subway",
    "bread line",
    "factory worker",
    "artificial intelligence",  # 抽象概念（Smithsonianでは失格だったもの）
    "robot",
    "data center",
]


def search(term: str, orientation: str = "portrait") -> dict:
    qs = urllib.parse.urlencode(
        {"query": term, "orientation": orientation, "per_page": PER}
    )
    req = urllib.request.Request(
        f"{API}?{qs}",
        headers={"Authorization": KEY, "User-Agent": "doci-probe/0.1"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    if not KEY:
        sys.exit("PEXELS_API_KEY 未設定。`export PEXELS_API_KEY=...` してから実行。")
    terms = sys.argv[1:] or DEFAULT_TERMS
    print(f"# Pexels probe  per_page={PER}  orientation=portrait\n")
    for term in terms:
        try:
            d = search(term)
        except urllib.error.HTTPError as e:
            print(f"## {term!r}: HTTP {e.code} {e.read().decode()[:200]}\n")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"## {term!r}: ERROR {e}\n")
            continue
        total = d.get("total_results", 0)
        photos = d.get("photos") or []
        # 9:16 に近い縦長(高さ/幅 >= 1.55)が何枚あるか
        tall = [p for p in photos if p.get("height", 0) >= p.get("width", 1) * 1.55]
        print(f"## {term!r}: total_results={total}  page={len(photos)}  9:16近接(縦長)={len(tall)}")
        for p in photos[:4]:
            w, h = p.get("width"), p.get("height")
            ratio = f"{h/ w:.2f}" if w else "?"
            alt = (p.get("alt") or "").strip()[:54]
            print(f"   - {w}x{h} (h/w={ratio}) | {alt}")
            print(f"     portrait={(p.get('src') or {}).get('portrait','')[:80]}")
        print()


if __name__ == "__main__":
    main()
