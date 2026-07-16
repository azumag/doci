"""YouTube Data API v3 アップロード（v1 は unlisted/private）。

初回のみ OAuth 同意が必要:
    python -m doci.youtube --auth
    python -m doci.youtube --auth --channel <id>
これで refresh token を YOUTUBE_TOKEN_FILE に保存。以降は無人で更新される。

依存: google-api-python-client, google-auth-oauthlib, google-auth-httplib2
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
ACCOUNT_SCOPES = [*SCOPES, "https://www.googleapis.com/auth/youtube.readonly"]


def _token_has_scopes(token_file: Path, required_scopes: list[str]) -> bool:
    """保存済みtoken JSONに実際に記録されたscopeを比較する。"""
    try:
        raw = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    stored = raw.get("scopes") or []
    if isinstance(stored, str):
        stored = stored.split()
    return set(required_scopes).issubset(set(stored))


def _load_credentials(
    interactive: bool,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
    scopes: list[str] | None = None,
):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = Path(token_file or config.YOUTUBE_TOKEN_FILE)
    client_secret_file = Path(
        client_secret_file or config.YOUTUBE_CLIENT_SECRET_FILE
    )
    required_scopes = scopes or SCOPES
    creds = None
    token_scopes_ok = token_file.exists() and _token_has_scopes(
        token_file, required_scopes
    )
    if token_scopes_ok:
        creds = Credentials.from_authorized_user_file(str(token_file), required_scopes)
    elif token_file.exists() and not interactive:
        raise RuntimeError(
            "YouTube token のscopeが不足しています。"
            "`python -m doci.youtube --auth [--channel <id>]` で再認証してください。"
        )
    if creds and not creds.has_scopes(required_scopes):
        if not interactive:
            raise RuntimeError(
                "YouTube token のscopeが不足しています。"
                "`python -m doci.youtube --auth [--channel <id>]` で再認証してください。"
            )
        creds = None
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
                    "YouTube のrefresh_tokenが失効しています。"
                    "`python -m doci.youtube --auth [--channel <id>]` で再認証してください。"
                )
    if not interactive:
        raise RuntimeError(
            "有効な YouTube 認証がありません。先に "
            "`python -m doci.youtube --auth [--channel <id>]` を実行してください。"
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secret_file.exists():
        raise RuntimeError(
            f"OAuthクライアント秘密ファイルがありません: {client_secret_file}"
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret_file), required_scopes
    )
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def account_info(
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> list[dict[str, str]]:
    """tokenが紐づくYouTubeチャンネルのIDと表示名を返す。"""
    from googleapiclient.discovery import build

    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ACCOUNT_SCOPES,
    )
    youtube = build("youtube", "v3", credentials=creds)
    data = youtube.channels().list(part="id,snippet", mine=True).execute()
    return [
        {
            "id": item.get("id", ""),
            "title": item.get("snippet", {}).get("title", ""),
        }
        for item in data.get("items", [])
    ]


def upload(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str | None = None,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    privacy = privacy or config.YOUTUBE_PRIVACY
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
    )
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


def set_thumbnail(
    video_id: str,
    thumbnail_path: Path,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> None:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
    )
    youtube = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(str(thumbnail_path), mimetype="image/png")
    youtube.thumbnails().set(videoId=video_id, media_body=media).execute()


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube アップロード")
    ap.add_argument("--auth", action="store_true", help="初回OAuth同意してtokenを保存")
    ap.add_argument(
        "--whoami",
        action="store_true",
        help="tokenが紐づくYouTubeチャンネルIDと表示名を確認",
    )
    ap.add_argument("--channel", help="channel.toml の YouTube 資格情報を使用")
    ap.add_argument("--video")
    ap.add_argument("--title", default="doci test")
    ap.add_argument("--description", default="")
    args = ap.parse_args()
    privacy = config.YOUTUBE_PRIVACY
    token_file = Path(config.YOUTUBE_TOKEN_FILE)
    client_secret_file = Path(config.YOUTUBE_CLIENT_SECRET_FILE)
    if args.channel:
        from . import channel

        spec = channel.load(args.channel)
        privacy = spec.publish.youtube.privacy
        token_file = spec.publish.youtube.token
        client_secret_file = spec.publish.youtube.client_secret
    if args.auth:
        _load_credentials(
            interactive=True,
            token_file=token_file,
            client_secret_file=client_secret_file,
            scopes=ACCOUNT_SCOPES,
        )
        print(f"認証完了: {token_file}")
        return
    if args.whoami:
        accounts = account_info(
            token_file=token_file,
            client_secret_file=client_secret_file,
        )
        if not accounts:
            raise RuntimeError("tokenに紐づくYouTubeチャンネルが見つかりません")
        for account in accounts:
            print(f"channel_id={account['id']} title={account['title']}")
        return
    if args.video:
        upload(
            Path(args.video),
            args.title,
            args.description,
            [],
            privacy,
            token_file=token_file,
            client_secret_file=client_secret_file,
        )


if __name__ == "__main__":
    main()
