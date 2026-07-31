"""Instagram Graph API 投稿（Reels）(issue #3)。【後回し】

Graph API は動画を**公開URLから取得**する仕様（直接ファイル投稿不可）。よって投稿前に
動画をどこか公開ホストに上げて URL を得る段が必須。**そのホスト方針が未定のため本モジュールは
骨組みのみ**（`_host_video` が未実装で、有効化しても明示エラーになる）。

ホスト（例: Cloudflare R2 公開バケット）が決まったら `_host_video` を実装すれば有効化できる。

必要な資格情報:
- INSTAGRAM_USER_ID: IGビジネス/クリエイターアカウントの user id
- INSTAGRAM_ACCESS_TOKEN: 長期アクセストークン（Meta開発者アプリ＋FBページ連携）
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

_GRAPH = "https://graph.facebook.com/v21.0"


class InstagramError(RuntimeError):
    pass


class InstagramUploadPreflightError(InstagramError):
    """動画投稿を開始する前の資格情報・ローカル検証エラー。"""


def _host_video(video_path: Path) -> str:
    """動画を公開ホストに上げて取得URLを返す。【未実装】公開ホスト方針が未定。

    決まったらここを実装する（例: Cloudflare R2 公開バケットへ put して公開URLを返す）。
    INSTAGRAM_HOST_BASE 等の設定もそれに合わせて使う。
    """
    raise InstagramUploadPreflightError(
        "Instagram の公開ホストが未実装です（IGは後回し）。公開ホスト方針(R2等)を決めて "
        "_host_video を実装してください。"
    )


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def _post(path: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f"{_GRAPH}/{path}", data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def upload(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    *,
    user_id: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Reels として投稿。{id, permalink} を返す。"""
    uid = config.INSTAGRAM_USER_ID if user_id is None else user_id
    token = config.INSTAGRAM_ACCESS_TOKEN if access_token is None else access_token
    if not (uid and token):
        raise InstagramUploadPreflightError(
            "INSTAGRAM_USER_ID / INSTAGRAM_ACCESS_TOKEN 未設定"
        )
    video_path = Path(video_path)
    try:
        video_path.stat()
    except OSError as exc:
        raise InstagramUploadPreflightError(
            f"動画ファイルを読み込めません: {video_path}"
        ) from exc
    video_url = _host_video(video_path)  # ← 未実装で停止（後回し）

    caption = (f"{title}\n\n{description}").strip()[:2200]
    # 1) メディアコンテナ作成（公開URLから取得）
    try:
        cont = _post(
            f"{uid}/media",
            {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": token,
            },
        )
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            raise InstagramUploadPreflightError(
                f"コンテナ作成が受理されませんでした (HTTP {exc.code})"
            ) from exc
        raise
    creation_id = cont.get("id")
    if not creation_id:
        raise InstagramError(f"コンテナ作成失敗: {json.dumps(cont)[:300]}")
    # 2) 処理完了までポーリング
    for _ in range(30):
        st = _get(f"{_GRAPH}/{creation_id}?fields=status_code&access_token={token}")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise InstagramError(f"メディア処理エラー: {json.dumps(st)[:300]}")
        time.sleep(5)
    # 3) 公開
    pub = _post(f"{uid}/media_publish", {"creation_id": creation_id, "access_token": token})
    media_id = pub.get("id")
    if not media_id:
        raise InstagramError(f"公開失敗: {json.dumps(pub)[:300]}")
    perma = _get(f"{_GRAPH}/{media_id}?fields=permalink&access_token={token}").get("permalink")
    print(f"instagram: id={media_id} {perma or ''}")
    return {"id": media_id, "permalink": perma}
