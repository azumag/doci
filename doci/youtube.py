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
import re
import time
from urllib.parse import parse_qs, urlparse

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


def search_public_videos(
    query: str,
    *,
    max_results: int = 6,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> list[dict[str, str]]:
    """YouTube Data APIで公開動画の調査候補を取得する（アップロード操作なし）。"""
    from googleapiclient.discovery import build

    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ACCOUNT_SCOPES,
    )
    service = build("youtube", "v3", credentials=creds)
    data = (
        service.search()
        .list(
            part="snippet",
            q=query,
            type="video",
            order="relevance",
            relevanceLanguage="ja",
            safeSearch="moderate",
            maxResults=max(1, min(max_results, 25)),
        )
        .execute()
    )
    results: list[dict[str, str]] = []
    video_ids: list[str] = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId", "")
        snippet = item.get("snippet", {})
        if not video_id or not snippet.get("title"):
            continue
        video_ids.append(video_id)
        results.append(
            {
                "video_id": video_id,
                "title": snippet["title"],
                "channel": snippet.get("channelTitle", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_at": snippet.get("publishedAt", ""),
                "description": snippet.get("description", ""),
            }
        )
    if video_ids:
        details = (
            service.videos()
            .list(
                part="snippet,contentDetails,statistics",
                id=",".join(video_ids),
            )
            .execute()
        )
        by_id = {item.get("id", ""): item for item in details.get("items", [])}
        for result in results:
            detail = by_id.get(result["video_id"], {})
            snippet = detail.get("snippet", {})
            statistics = detail.get("statistics", {})
            content = detail.get("contentDetails", {})
            result["description"] = snippet.get(
                "description", result.get("description", "")
            )
            result["duration"] = str(content.get("duration", ""))
            result["view_count"] = str(statistics.get("viewCount", ""))
            result["like_count"] = str(statistics.get("likeCount", ""))
    return results


def _video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    if host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [""])[0]
        match = re.match(r"^/(?:shorts|embed)/([^/?]+)", parsed.path)
        if match:
            return match.group(1)
    return ""


def fetch_public_transcript(
    video_url: str,
    *,
    max_chars: int = 6000,
    cache_dir: Path | None = None,
) -> str:
    """公開動画ページに埋め込まれた字幕トラックから日本語字幕を取得する。"""
    from youtube_transcript_api import YouTubeTranscriptApi

    video_id = _video_id(video_url)
    if not video_id:
        raise ValueError(f"YouTube動画URLではありません: {video_url}")
    cache_root = Path(cache_dir or (config.OUTPUT / ".cache/youtube-transcripts"))
    cache_path = cache_root / f"{video_id}.txt"
    try:
        cached = cache_path.read_text(encoding="utf-8")
    except OSError:
        cached = ""
    if cached:
        return cached[:max_chars]
    rows = YouTubeTranscriptApi().fetch(video_id, languages=["ja"])
    fragments = [
        str(row.text).replace("\n", " ").strip()
        for row in rows
        if str(row.text).strip()
    ]
    transcript = re.sub(r"\s+", " ", " ".join(fragments)).strip()
    if transcript:
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(transcript, encoding="utf-8")
    return transcript[:max_chars]


def add_public_transcripts(
    videos: list[dict[str, str]], *, limit: int = 3
) -> list[dict[str, str]]:
    """検索候補の先頭から公開字幕を付加する。字幕なし・取得失敗は候補として残す。"""
    enriched: list[dict[str, str]] = []
    for index, video in enumerate(videos):
        item = dict(video)
        if index < limit:
            transcript = ""
            for attempt in range(2):
                try:
                    transcript = fetch_public_transcript(
                        item.get("url", ""), max_chars=2500
                    )
                    break
                except Exception:  # noqa: BLE001 - 字幕なし/地域制限でも候補検索は継続
                    if attempt == 0:
                        time.sleep(1.0)
            if transcript:
                item["transcript_excerpt"] = transcript
        enriched.append(item)
    return enriched


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
