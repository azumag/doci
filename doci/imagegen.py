"""画像生成プロバイダ抽象。`IMAGE_BACKEND` で切替。

- gemini (既定): Google Gemini 2.5 Flash Image (nano banana)。AI Studio の無料キーで動く。
- openrouter:      OpenRouter の画像出力モデル（例: google/gemini-2.5-flash-image）。
- minimax:         Minimax image-01（※メディアトークン枠が要る。コーディングプランのキーは不可）。

いずれも縦9:16の単一画像を out_path(PNG) に保存して返す。
"""
from __future__ import annotations

import argparse
import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from . import config, imagery


class ImageGenError(RuntimeError):
    pass


# ---------------- Gemini (nano banana) ----------------
def _gemini_image(prompt: str, out_path: Path, aspect_ratio: str) -> Path:
    key = config.get("GEMINI_API_KEY")
    if not key:
        raise ImageGenError("GEMINI_API_KEY が未設定です (IMAGE_BACKEND=gemini)")
    model = config.GEMINI_IMAGE_MODEL
    ver = config.GEMINI_API_VERSION
    url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent"
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": aspect_ratio},
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise ImageGenError(f"Gemini HTTP {e.code}: {detail}")

    for cand in d.get("candidates", []):
        for part in (cand.get("content", {}) or {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                out_path = Path(out_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(base64.b64decode(inline["data"]))
                return out_path
    raise ImageGenError(f"画像が応答にありません: {json.dumps(d)[:400]}")


# ---------------- OpenRouter ----------------
def _openrouter_image(prompt: str, out_path: Path, aspect_ratio: str) -> Path:
    key = config.get("OPENROUTER_API_KEY")
    if not key:
        raise ImageGenError("OPENROUTER_API_KEY が未設定です (IMAGE_BACKEND=openrouter)")
    full = (
        f"{prompt}\n\nOutput a single image. Aspect ratio {aspect_ratio} "
        f"(vertical, full-frame). No text, no watermark, no border."
    )
    body = json.dumps(
        {
            "model": config.OPENROUTER_IMAGE_MODEL,
            "messages": [{"role": "user", "content": full}],
            "modalities": ["image", "text"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/azumag/doci",
            "X-Title": "doci",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ImageGenError(f"OpenRouter HTTP {e.code}: {e.read().decode()[:300]}")
    imgs = ((d.get("choices") or [{}])[0].get("message", {}) or {}).get("images") or []
    if not imgs:
        raise ImageGenError(f"画像が返りません: {json.dumps(d)[:300]}")
    u = (imgs[0].get("image_url") or {}).get("url", "")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if u.startswith("data:"):
        out_path.write_bytes(base64.b64decode(u.split(",", 1)[1]))
    else:
        with urllib.request.urlopen(u, timeout=120) as r:
            out_path.write_bytes(r.read())
    return out_path


def generate_image(prompt: str, out_path: Path, aspect_ratio: str = "9:16") -> Path:
    # 横(16:9)生成時は visual_prompt 内の縦向きの語を入替える。
    prompt = imagery.orient_prompt(prompt, aspect_ratio)
    # 権利回避: ロゴ/商標/実在人物/文字を避ける否定制約を付与（issue #9 派生）。
    prompt = imagery.add_avoid(prompt)
    backend = config.IMAGE_BACKEND
    if backend == "gemini":
        return _gemini_image(prompt, out_path, aspect_ratio)
    if backend == "openrouter":
        return _openrouter_image(prompt, out_path, aspect_ratio)
    if backend == "minimax":
        from . import minimax
        return minimax.generate_image(prompt, out_path, aspect_ratio)
    raise ImageGenError(f"unknown IMAGE_BACKEND: {backend}")


def main() -> None:
    ap = argparse.ArgumentParser(description="画像生成テスト")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--out", default=str(config.OUTPUT / "imagegen_test.png"))
    args = ap.parse_args()
    p = generate_image(args.prompt, Path(args.out), args.aspect)
    print(f"image[{config.IMAGE_BACKEND}] -> {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
