"""Minimax 映像生成（画像=image-01 / 動画=Hailuo）。公式 REST を直叩き。

画像: POST /image_generation                       (同期, base64)
動画: POST /video_generation -> task_id            (非同期)
      GET  /query/video_generation?task_id=...     -> status, file_id
      GET  /files/retrieve?file_id=...             -> file.download_url
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import time
import urllib.request
from pathlib import Path

from . import config


class MinimaxError(RuntimeError):
    pass


def _headers() -> dict:
    if not config.MINIMAX_API_KEY:
        raise MinimaxError("MINIMAX_API_KEY が未設定です")
    return {
        "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }


def _post(path: str, payload: dict, timeout: int = 120) -> dict:
    url = f"{config.MINIMAX_MEDIA_BASE_URL}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=_headers(), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str, timeout: int = 60) -> dict:
    url = f"{config.MINIMAX_MEDIA_BASE_URL}{path}"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _check_base_resp(data: dict) -> None:
    br = data.get("base_resp") or {}
    code = br.get("status_code", 0)
    if code not in (0, None):
        raise MinimaxError(f"Minimax error {code}: {br.get('status_msg')}")


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def generate_image(prompt: str, out_path: Path, aspect_ratio: str = "9:16") -> Path:
    payload = {
        "model": config.MINIMAX_IMAGE_MODEL,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "response_format": "base64",
        "n": 1,
    }
    data = _post("/image_generation", payload)
    _check_base_resp(data)
    d = data.get("data") or {}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if d.get("image_base64"):
        out_path.write_bytes(base64.b64decode(d["image_base64"][0]))
        return out_path
    # URL 形式へのフォールバック
    urls = d.get("image_urls") or d.get("urls") or []
    if urls:
        with urllib.request.urlopen(urls[0], timeout=120) as r:
            out_path.write_bytes(r.read())
        return out_path
    raise MinimaxError(f"画像データが応答にありません: {json.dumps(data)[:400]}")


def generate_video(
    prompt: str,
    out_path: Path,
    first_frame_image: Path | None = None,
    duration: int = 6,
    resolution: str = "1080P",
    poll_interval: int = 10,
    poll_timeout: int = 600,
) -> Path:
    payload: dict = {
        "model": config.MINIMAX_VIDEO_MODEL,
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
    }
    if first_frame_image:
        payload["first_frame_image"] = _data_url(Path(first_frame_image))

    created = _post("/video_generation", payload)
    _check_base_resp(created)
    task_id = created.get("task_id")
    if not task_id:
        raise MinimaxError(f"task_id が返りません: {json.dumps(created)[:400]}")

    deadline = time.monotonic() + poll_timeout
    file_id = None
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        st = _get(f"/query/video_generation?task_id={task_id}")
        status = st.get("status")
        if status == "Success":
            file_id = st.get("file_id")
            break
        if status == "Fail":
            raise MinimaxError(f"動画生成に失敗: {json.dumps(st)[:400]}")
        # Preparing / Queueing / Processing は継続
    if not file_id:
        raise MinimaxError(f"動画生成がタイムアウト (task_id={task_id})")

    retrieved = _get(f"/files/retrieve?file_id={file_id}")
    download_url = (retrieved.get("file") or {}).get("download_url")
    if not download_url:
        raise MinimaxError(f"download_url が取れません: {json.dumps(retrieved)[:400]}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(download_url, timeout=300) as r:
        out_path.write_bytes(r.read())
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Minimax 画像/動画生成テスト")
    ap.add_argument("--image", help="画像生成プロンプト")
    ap.add_argument("--video", help="動画生成プロンプト")
    ap.add_argument("--first-frame", help="動画の初期フレーム画像パス")
    ap.add_argument("--out", default=str(config.OUTPUT / "minimax_test"))
    args = ap.parse_args()
    if args.image:
        p = generate_image(args.image, Path(args.out + ".jpg"))
        print(f"image -> {p} ({p.stat().st_size} bytes)")
    if args.video:
        p = generate_video(
            args.video,
            Path(args.out + ".mp4"),
            first_frame_image=Path(args.first_frame) if args.first_frame else None,
        )
        print(f"video -> {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
