"""YouTube Data API v3 アップロード（v1 は unlisted/private）。

初回のみ OAuth 同意が必要:
    python -m doci.youtube --auth
これで refresh token を YOUTUBE_TOKEN_FILE に保存。以降は無人で更新される。

依存: google-api-python-client, google-auth-oauthlib, google-auth-httplib2
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _load_credentials(interactive: bool):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = Path(config.YOUTUBE_TOKEN_FILE)
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError:
            # refresh_token 自体が失効/取り消し済み。interactive なら再認証へフォールスルーする。
            if not interactive:
                raise RuntimeError(
                    "YouTube のrefresh_tokenが失効しています。`python -m doci.youtube --auth` で再認証してください。"
                )
    if not interactive:
        raise RuntimeError(
            "有効な YouTube 認証がありません。先に `python -m doci.youtube --auth` を実行してください。"
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = config.YOUTUBE_CLIENT_SECRET_FILE
    if not Path(secret).exists():
        raise RuntimeError(f"OAuthクライアント秘密ファイルがありません: {secret}")
    flow = InstalledAppFlow.from_client_secrets_file(secret, SCOPES)
    creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def upload(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str | None = None,
) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    privacy = privacy or config.YOUTUBE_PRIVACY
    creds = _load_credentials(interactive=False)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags[:30],
            "categoryId": "24",  # Entertainment
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"upload {int(status.progress() * 100)}%")
    video_id = resp["id"]
    print(f"uploaded: https://youtu.be/{video_id} (privacy={privacy})")
    return video_id


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube アップロード")
    ap.add_argument("--auth", action="store_true", help="初回OAuth同意してtokenを保存")
    ap.add_argument("--video")
    ap.add_argument("--title", default="doci test")
    ap.add_argument("--description", default="")
    args = ap.parse_args()
    if args.auth:
        _load_credentials(interactive=True)
        print(f"認証完了: {config.YOUTUBE_TOKEN_FILE}")
        return
    if args.video:
        upload(Path(args.video), args.title, args.description, [])


if __name__ == "__main__":
    main()
