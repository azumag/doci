"""映像の権利回避ヘルパ（issue #9 派生）。

ストック素材/AI生成に、ブランド・商標・ロゴ・識別可能な人物・判読可能な文字が
極力入らないようにするための共有定義。

効き目の正直な整理:
- AI生成: AVOID_SUFFIX をプロンプトに足せば、生成物は実在ロゴ/人物をほぼ確実に避けられる。
- Pexels等のストック: 検索語からブランド語を除けても、generic な語(例 "smartphone")でも
  在庫の多くがロゴ付き実機なので、ロゴ混入は完全には防げない。批判文脈で特定ブランドを
  映す事故を避けたいビートは、抽象/メタファ寄りの visual_prompt にする（台本側で対応）か、
  AI生成にフォールバックするのが確実。
"""
from __future__ import annotations

import re

# AI生成プロンプトの末尾に足す否定制約。
AVOID_SUFFIX = (
    "no brand logos, no trademarks, no recognizable real people or faces, "
    "no legible text or watermark"
)

# 検索語から落とす代表的ブランド/製品名（小文字・部分一致）。網羅ではなく事故防止の最低限。
BRAND_DENYLIST = (
    "iphone", "ipad", "macbook", "apple", "android", "samsung", "galaxy", "pixel",
    "google", "microsoft", "windows", "amazon", "sony", "nintendo", "playstation",
    "xbox", "tesla", "nike", "adidas", "puma", "coca-cola", "coca cola", "pepsi",
    "mcdonald", "mcdonalds", "starbucks", "disney", "netflix", "gucci", "prada",
    "rolex", "louis vuitton", "chanel", "ferrari", "toyota", "bmw", "mercedes",
)


def add_avoid(prompt: str) -> str:
    """AI生成プロンプトに否定制約を付与。"""
    p = prompt.rstrip().rstrip(".")
    return f"{p}. {AVOID_SUFFIX}"


def strip_brands(query: str) -> str:
    """検索語からブランド/製品名を除去（残りで検索する）。空になれば元を返す。"""
    out = query
    for b in BRAND_DENYLIST:
        out = re.sub(rf"(?<![A-Za-z]){re.escape(b)}(?![A-Za-z])", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.-")
    return out or query
