"""YouTube Data API v3 アップロード・公開設定更新。

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
ANALYTICS_SCOPES = [
    *ACCOUNT_SCOPES,
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
# videos.update は `youtube` と `youtube.force-ssl` の両方を許可する。
# 公式scope説明で前者はアカウント全体管理、後者は動画・評価・コメント・字幕に
# 対象資源が限定されるため、公開設定更新には後者を選ぶ。
MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
MANAGE_SCOPES = [*ACCOUNT_SCOPES, MANAGE_SCOPE]
_YOUTUBE_API_TIMEOUT_SECONDS = 60
_WRITABLE_VIDEO_STATUS_KEYS = {
    "privacyStatus",
    "publishAt",
    "license",
    "embeddable",
    "publicStatsViewable",
    "selfDeclaredMadeForKids",
    "containsSyntheticMedia",
}


class UploadPreflightError(RuntimeError):
    """動画データ受理前に確定したローカル・認証・投稿前要求エラー。"""


def _build_service(credentials):
    """YouTube service構築を、依存未導入のfixtureテストから差し替え可能にする。"""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    http = AuthorizedHttp(
        credentials,
        http=httplib2.Http(timeout=_YOUTUBE_API_TIMEOUT_SECONDS),
    )
    return build("youtube", "v3", http=http)


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
    auth_flags: list[str] = []
    if "https://www.googleapis.com/auth/yt-analytics.readonly" in required_scopes:
        auth_flags.append("--analytics")
    if MANAGE_SCOPE in required_scopes:
        auth_flags.append("--manage")
    auth_hint = " ".join(
        [
            "python -m doci.youtube --auth [--channel <id>]",
            *auth_flags,
        ]
    )
    creds = None
    token_scopes_ok = token_file.exists() and _token_has_scopes(
        token_file, required_scopes
    )
    if token_scopes_ok:
        creds = Credentials.from_authorized_user_file(str(token_file), required_scopes)
    elif token_file.exists() and not interactive:
        raise RuntimeError(
            "YouTube token のscopeが不足しています。"
            f"`{auth_hint}` で再認証してください。"
        )
    if creds and not creds.has_scopes(required_scopes):
        if not interactive:
            raise RuntimeError(
                "YouTube token のscopeが不足しています。"
                f"`{auth_hint}` で再認証してください。"
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
                    f"`{auth_hint}` で再認証してください。"
                )
    if not interactive:
        raise RuntimeError(
            "有効な YouTube 認証がありません。先に "
            f"`{auth_hint}` を実行してください。"
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


def video_details(
    video_ids: list[str],
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> list[dict]:
    """所有動画を含む動画別の公開統計・状態を読み取る（更新操作なし）。"""
    from googleapiclient.discovery import build

    ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if not ids:
        return []
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ACCOUNT_SCOPES,
    )
    service = build("youtube", "v3", credentials=creds)
    results: list[dict] = []
    for offset in range(0, len(ids), 50):
        data = (
            service.videos()
            .list(
                part="snippet,contentDetails,statistics,status",
                id=",".join(ids[offset : offset + 50]),
            )
            .execute()
        )
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            status = item.get("status", {})
            results.append(
                {
                    "video_id": item.get("id", ""),
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "duration": item.get("contentDetails", {}).get("duration", ""),
                    "privacy_status": status.get("privacyStatus", ""),
                    "views": int(statistics.get("viewCount", 0) or 0),
                    "likes": int(statistics.get("likeCount", 0) or 0),
                    "comments": int(statistics.get("commentCount", 0) or 0),
                }
            )
    return results


def video_analytics(
    video_ids: list[str],
    *,
    start_date: str,
    end_date: str,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> list[dict]:
    """YouTube Analytics APIの動画別retention/watch指標を読み取る。"""
    from googleapiclient.discovery import build

    ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if not ids:
        return []
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ANALYTICS_SCOPES,
    )
    service = build("youtubeAnalytics", "v2", credentials=creds)
    metrics = (
        "views,estimatedMinutesWatched,averageViewDuration,"
        "averageViewPercentage,likes,comments"
    )
    results: list[dict] = []
    for offset in range(0, len(ids), 200):
        data = (
            service.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics=metrics,
                dimensions="video",
                filters=f"video=={','.join(ids[offset : offset + 200])}",
                sort="-views",
                maxResults=200,
            )
            .execute()
        )
        headers = [header.get("name", "") for header in data.get("columnHeaders", [])]
        for values in data.get("rows", []):
            row = dict(zip(headers, values))
            results.append(
                {
                    "video_id": str(row.get("video", "")),
                    "views": int(row.get("views", 0) or 0),
                    "estimated_minutes_watched": float(
                        row.get("estimatedMinutesWatched", 0) or 0
                    ),
                    "average_view_duration": float(
                        row.get("averageViewDuration", 0) or 0
                    ),
                    "average_view_percentage": float(
                        row.get("averageViewPercentage", 0) or 0
                    ),
                    "likes": int(row.get("likes", 0) or 0),
                    "comments": int(row.get("comments", 0) or 0),
                }
            )
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
    privacy = privacy or config.YOUTUBE_PRIVACY
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

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
        media = MediaFileUpload(
            str(video_path),
            chunksize=-1,
            resumable=True,
            mimetype="video/mp4",
        )
        req = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )
    except Exception as exc:
        raise UploadPreflightError(str(exc)[:240]) from exc
    # resumable uploadの最初のnext_chunk()は、セッション開始POSTだけでなく
    # メディア本体のPUTまで送る実装がある。URI未設定のセッション開始4xxだけを
    # 投稿前検証とし、URI設定後のメディア4xxは外部状態不明のまま返す。
    resp = None
    while resp is None:
        try:
            status, resp = req.next_chunk()
        except Exception as exc:  # noqa: BLE001 - 受理後の失敗はunknownのまま
            response = getattr(exc, "resp", None)
            http_status = getattr(exc, "status_code", None) or getattr(
                response, "status", None
            )
            resumable_uri = getattr(req, "resumable_uri", object())
            if (
                isinstance(http_status, int)
                and 400 <= http_status < 500
                and resumable_uri is None
            ):
                raise UploadPreflightError(
                    "YouTube投稿セッション開始が受理されませんでした "
                    f"(HTTP {http_status})"
                ) from exc
            raise
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


def _video_status(
    video_id: str,
    *,
    required_scopes: list[str] | None = None,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> tuple[object, dict]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        raise ValueError(f"invalid YouTube video id: {video_id!r}")
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=required_scopes or ACCOUNT_SCOPES,
    )
    service = _build_service(creds)
    current = service.videos().list(part="status", id=video_id).execute()
    items = current.get("items") or []
    if len(items) != 1:
        raise RuntimeError(f"YouTube動画が見つかりません: {video_id}")
    return service, dict(items[0].get("status") or {})


def privacy_status(
    video_id: str,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    """現在の公開設定を読み取る（変更なし）。"""
    _, status = _video_status(
        video_id,
        token_file=token_file,
        client_secret_file=client_secret_file,
    )
    return str(status.get("privacyStatus") or "")


def set_privacy(
    video_id: str,
    privacy: str,
    *,
    expected_privacy: str | None = None,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    """期待した現在状態のときだけ、他の書込み可能statusを保持して更新する。"""
    if privacy not in {"public", "unlisted", "private"}:
        raise ValueError(f"invalid YouTube privacy: {privacy!r}")
    if expected_privacy is not None and expected_privacy not in {
        "public",
        "unlisted",
        "private",
    }:
        raise ValueError(f"invalid expected YouTube privacy: {expected_privacy!r}")
    service, current_status = _video_status(
        video_id,
        required_scopes=MANAGE_SCOPES,
        token_file=token_file,
        client_secret_file=client_secret_file,
    )
    current_privacy = str(current_status.get("privacyStatus") or "")
    if current_privacy == privacy:
        return "unchanged"
    if expected_privacy is not None and current_privacy != expected_privacy:
        raise RuntimeError(
            f"YouTube公開設定が想定外です: expected={expected_privacy} "
            f"actual={current_privacy or 'missing'}"
        )
    status = {
        key: value
        for key, value in current_status.items()
        if key in _WRITABLE_VIDEO_STATUS_KEYS
    }
    status["privacyStatus"] = privacy
    service.videos().update(
        part="status",
        body={"id": video_id, "status": status},
    ).execute()
    return "updated"


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube アップロード")
    ap.add_argument("--auth", action="store_true", help="初回OAuth同意してtokenを保存")
    ap.add_argument(
        "--analytics",
        action="store_true",
        help="--auth時にYouTube Analytics読み取りscopeも要求",
    )
    ap.add_argument(
        "--manage",
        action="store_true",
        help="--auth時に動画公開設定の更新scopeも要求",
    )
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
        auth_scopes = list(
            ANALYTICS_SCOPES if args.analytics else ACCOUNT_SCOPES
        )
        if args.manage and MANAGE_SCOPE not in auth_scopes:
            auth_scopes.append(MANAGE_SCOPE)
        _load_credentials(
            interactive=True,
            token_file=token_file,
            client_secret_file=client_secret_file,
            scopes=auth_scopes,
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
