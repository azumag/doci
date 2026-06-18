"""Smithsonian Open Access API 検証プローブ（issue #9・使い捨て）。

各検索語について:
  q = '<term> AND online_media_type:"Images"' で画像に絞り、
  各 row の online_media.media[].usage.access == "CC0" を確認、
  CC0画像の取得URLと題名を表示して「一致精度」と「CC0在庫」を目視評価する。

使い方:
  SI_API_KEY=<api.data.gov key> python tools/probe_smithsonian.py "Soviet Union" "propaganda poster" ...
  キー未設定なら DEMO_KEY（低レート）にフォールバック。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.si.edu/openaccess/api/v1.0/search"
KEY = os.environ.get("SI_API_KEY") or "DEMO_KEY"
ROWS = int(os.environ.get("SI_ROWS", "20"))

DEFAULT_TERMS = [
    "Soviet Union",
    "propaganda poster",
    "subway",
    "bread line",
    "factory worker",
    "artificial intelligence",  # 抽象概念=苦手予想
]


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "doci-probe/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def search(term: str) -> dict:
    q = f'{term} AND online_media_type:"Images"'
    qs = urllib.parse.urlencode({"api_key": KEY, "q": q, "rows": ROWS})
    return _get(f"{API}?{qs}")


def cc0_images(row: dict) -> list[dict]:
    """row から CC0 の画像メディアを抽出。[{title, access, url, thumb}]"""
    out = []
    dnr = (row.get("content") or {}).get("descriptiveNonRepeating") or {}
    title = (dnr.get("title") or {}).get("content") or row.get("title") or "(no title)"
    media = (dnr.get("online_media") or {}).get("media") or []
    for m in media:
        if m.get("type") != "Images":
            continue
        access = ((m.get("usage") or {}).get("access")) or "?"
        out.append(
            {
                "title": title,
                "access": access,
                "url": m.get("content") or "",
                "thumb": m.get("thumbnail") or "",
                "guid": m.get("guid") or "",
            }
        )
    return out


def main() -> None:
    terms = sys.argv[1:] or DEFAULT_TERMS
    print(f"# Smithsonian probe  key={'DEMO_KEY' if KEY == 'DEMO_KEY' else 'data.gov(set)'}  rows={ROWS}\n")
    for term in terms:
        try:
            d = search(term)
        except urllib.error.HTTPError as e:
            print(f"## {term!r}: HTTP {e.code} {e.read().decode()[:200]}\n")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"## {term!r}: ERROR {e}\n")
            continue
        resp = d.get("response") or {}
        rowcount = resp.get("rowCount", 0)
        rows = resp.get("rows") or []
        all_imgs = [img for r in rows for img in cc0_images(r)]
        cc0 = [i for i in all_imgs if i["access"] == "CC0"]
        print(f"## {term!r}: rowCount={rowcount}  page rows={len(rows)}  "
              f"画像メディア={len(all_imgs)}  うちCC0={len(cc0)}")
        # access 内訳
        breakdown: dict[str, int] = {}
        for i in all_imgs:
            breakdown[i["access"]] = breakdown.get(i["access"], 0) + 1
        if breakdown:
            print("   access内訳:", ", ".join(f"{k}={v}" for k, v in sorted(breakdown.items())))
        for i in cc0[:3]:
            print(f"   - CC0 | {i['title'][:60]}")
            print(f"          url={i['url'][:110]}")
        print()


if __name__ == "__main__":
    main()
