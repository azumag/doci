"""YouTube Data API v3 アップロード・公開設定更新。

初回のみ OAuth 同意が必要:
    python -m doci.youtube --auth
    python -m doci.youtube --auth --channel <id>
これで refresh token を YOUTUBE_TOKEN_FILE に保存。以降は無人で更新される。
Analytics専用read-only tokenは --auth --analytics-readonly で別ファイルへ保存する。

依存: google-api-python-client, google-auth-oauthlib, google-auth-httplib2
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/yt-analytics.readonly"
)
ANALYTICS_READONLY_SCOPES = [YOUTUBE_READONLY_SCOPE, ANALYTICS_READONLY_SCOPE]
ACCOUNT_SCOPES = [*SCOPES, YOUTUBE_READONLY_SCOPE]
ANALYTICS_SCOPES = [
    *ACCOUNT_SCOPES,
    ANALYTICS_READONLY_SCOPE,
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
# snippet更新はオブジェクト全体を送信する必要がある(部分送信だと省略した
# フィールドが消え得る)ため、書込可能キーだけを残して保持する(issue #57)。
_WRITABLE_VIDEO_SNIPPET_KEYS = {
    "title",
    "description",
    "tags",
    "categoryId",
    "defaultLanguage",
    "defaultAudioLanguage",
}
_MAX_VIDEO_TITLE_LENGTH = 100


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


def _token_scopes(token_file: Path) -> set[str] | None:
    """保存済みtoken JSONに記録されたscopeを返す。"""
    try:
        raw = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stored = raw.get("scopes") or []
    if isinstance(stored, str):
        stored = stored.split()
    if not isinstance(stored, list) or not all(
        isinstance(scope, str) for scope in stored
    ):
        return None
    return set(stored)


def _token_has_scopes(
    token_file: Path,
    required_scopes: list[str],
    *,
    exact: bool = False,
) -> bool:
    """保存済みtoken JSONに実際に記録されたscopeを比較する。"""
    stored = _token_scopes(token_file)
    if stored is None:
        return False
    required = set(required_scopes)
    return stored == required if exact else required.issubset(stored)


def _credentials_have_scopes(
    credentials,
    required_scopes: list[str],
    *,
    exact: bool,
) -> bool:
    """OAuth応答または保存tokenのscopeが要求境界内か確認する。"""
    granted = getattr(credentials, "granted_scopes", None)
    stored = granted if granted is not None else getattr(credentials, "scopes", None)
    if stored is None:
        return False
    actual = set(stored)
    required = set(required_scopes)
    return actual == required if exact else required.issubset(actual)


def _load_credentials(
    interactive: bool,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
    scopes: list[str] | None = None,
    exact_scopes: bool = False,
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
    if ANALYTICS_READONLY_SCOPE in required_scopes:
        auth_flags.append(
            "--analytics"
            if SCOPES[0] in required_scopes
            else "--analytics-readonly"
        )
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
        token_file,
        required_scopes,
        exact=exact_scopes,
    )
    if token_scopes_ok:
        # scopes引数を渡さずに読み込む: JSON内の保存scopesがそのまま
        # Credentials.scopes になり、refresh後のto_json()保存でも縮小されない。
        # 要求スコープを渡すと読み込み時にscopesが上書きされ、refresh保存で
        # 保存トークンのscopeが縮小されてしまう(issue #103)。
        creds = Credentials.from_authorized_user_file(str(token_file))
    elif token_file.exists() and not interactive:
        scope_problem = "要求と一致しません" if exact_scopes else "不足しています"
        raise RuntimeError(
            f"YouTube token のscopeが{scope_problem}。"
            f"`{auth_hint}` で再認証してください。"
        )
    if creds and not _credentials_have_scopes(
        creds, required_scopes, exact=exact_scopes
    ):
        if not interactive:
            scope_problem = "要求と一致しません" if exact_scopes else "不足しています"
            raise RuntimeError(
                f"YouTube token のscopeが{scope_problem}。"
                f"`{auth_hint}` で再認証してください。"
            )
        creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            if not _credentials_have_scopes(
                creds, required_scopes, exact=exact_scopes
            ):
                raise RuntimeError(
                    "更新後のYouTube tokenのscopeが要求と一致しません。"
                    f"`{auth_hint}` で再認証してください。"
                )
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
        include_granted_scopes="false" if exact_scopes else "true",
        prompt="consent",
    )
    if not _credentials_have_scopes(
        creds, required_scopes, exact=exact_scopes
    ):
        raise RuntimeError(
            "OAuthで取得したYouTube tokenのscopeが要求と一致しません。"
            "別アカウントまたはOAuth同意を確認してください。"
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


def owned_video_details_readonly(
    video_ids: list[str],
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> dict:
    """認証チャンネル所有動画の公開情報をread-onlyで取得する。

    YouTubeアプリから投稿した動画はdoci履歴に存在しないため、コメント返信Short
    実験ではData APIの所有チャンネルIDと各動画のchannelIdを照合する。更新scopeは
    要求しない。
    """
    from googleapiclient.discovery import build

    ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if not ids:
        return {"channel_id": None, "videos": []}
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ANALYTICS_READONLY_SCOPES,
        exact_scopes=True,
    )
    service = build("youtube", "v3", credentials=creds)
    channels = service.channels().list(part="id", mine=True).execute()
    channel_ids = [
        str(item.get("id") or "")
        for item in channels.get("items", [])
        if item.get("id")
    ]
    if len(channel_ids) != 1:
        raise RuntimeError(
            "YouTube readback could not identify exactly one authenticated channel"
        )
    channel_id = channel_ids[0]
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
            if str(snippet.get("channelId") or "") != channel_id:
                continue
            statistics = item.get("statistics", {})
            status = item.get("status", {})
            results.append(
                {
                    "video_id": str(item.get("id") or ""),
                    "channel_id": channel_id,
                    "title": str(snippet.get("title") or ""),
                    "published_at": str(snippet.get("publishedAt") or ""),
                    "duration": str(
                        item.get("contentDetails", {}).get("duration") or ""
                    ),
                    "privacy_status": str(status.get("privacyStatus") or ""),
                    "views": int(statistics.get("viewCount", 0) or 0),
                    "comments": int(statistics.get("commentCount", 0) or 0),
                }
            )
    by_id = {item["video_id"]: item for item in results}
    return {
        "channel_id": channel_id,
        "videos": [by_id[video_id] for video_id in ids if video_id in by_id],
    }


_COMMENT_REPLY_COUNT_COLUMNS = {
    "views",
    "comments",
    "subscribersGained",
    "subscribersLost",
}


def _validated_comment_reply_rows(data: object, *, dimension: str) -> list[dict]:
    """Analytics応答の列型、行幅、count型を検証してdictへ変換する。"""
    if not isinstance(data, dict):
        raise RuntimeError("YouTube Analytics returned an invalid report")
    headers = data.get("columnHeaders")
    if not isinstance(headers, list) or not headers:
        raise RuntimeError("YouTube Analytics report lacks column headers")
    allowed = {dimension, *_COMMENT_REPLY_COUNT_COLUMNS}
    names: list[str] = []
    for header in headers:
        if not isinstance(header, dict):
            raise RuntimeError("YouTube Analytics column header is invalid")
        name = header.get("name")
        if not isinstance(name, str) or name not in allowed or name in names:
            raise RuntimeError("YouTube Analytics column names are invalid")
        expected_column_type = "DIMENSION" if name == dimension else "METRIC"
        expected_data_type = "STRING" if name == dimension else "INTEGER"
        if (
            header.get("columnType") != expected_column_type
            or header.get("dataType") != expected_data_type
        ):
            raise RuntimeError(
                f"YouTube Analytics column type is invalid for {name}"
            )
        names.append(name)
    if dimension not in names:
        raise RuntimeError(
            f"YouTube Analytics report lacks {dimension} provenance"
        )
    raw_rows = data.get("rows", [])
    if raw_rows is None:
        raw_rows = []
    if not isinstance(raw_rows, list):
        raise RuntimeError("YouTube Analytics rows are invalid")
    rows: list[dict] = []
    for values in raw_rows:
        if not isinstance(values, list) or len(values) != len(names):
            raise RuntimeError("YouTube Analytics row width is invalid")
        row = dict(zip(names, values))
        if not isinstance(row[dimension], str) or not row[dimension]:
            raise RuntimeError(
                f"YouTube Analytics {dimension} value is invalid"
            )
        for name in _COMMENT_REPLY_COUNT_COLUMNS.intersection(row):
            value = row[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(
                    f"YouTube Analytics {name} must be a non-negative integer"
                )
        rows.append(row)
    return rows


def comment_reply_short_metrics(
    video_windows: list[dict],
    *,
    availability_start_date: str,
    availability_end_date: str,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> dict:
    """返信Shortと比較Shortの同じ公開後日数の指標をread-only取得する。

    `comments`は期間中のコメント操作数、登録者は動画watch pageへ帰属した
    `subscribersGained - subscribersLost`を保存する。最新日の欠落を0と誤認しない
    よう、同じメトリクス群の日次channel reportで利用可能最終日も確認する。
    """
    from googleapiclient.discovery import build

    if not video_windows:
        return {
            "source": "youtube_analytics_api_v2",
            "availability_start_date": availability_start_date,
            "availability_probe_end_date": availability_end_date,
            "data_through_date": None,
            "videos": [],
        }
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ANALYTICS_READONLY_SCOPES,
        exact_scopes=True,
    )
    service = build("youtubeAnalytics", "v2", credentials=creds)
    metrics = "views,comments,subscribersGained,subscribersLost"
    available_days: list[str] = []
    start_index = 1
    while True:
        availability = (
            service.reports()
            .query(
                ids="channel==MINE",
                startDate=availability_start_date,
                endDate=availability_end_date,
                metrics=metrics,
                dimensions="day",
                sort="day",
                maxResults=200,
                startIndex=start_index,
            )
            .execute()
        )
        page = _validated_comment_reply_rows(availability, dimension="day")
        for row in page:
            day = row["day"]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise RuntimeError("YouTube Analytics day value is invalid")
            try:
                date.fromisoformat(day)
            except ValueError as exc:
                raise RuntimeError(
                    "YouTube Analytics day value is invalid"
                ) from exc
            available_days.append(day)
        if len(page) < 200:
            break
        start_index += len(page)

    rows: list[dict] = []
    for window in video_windows:
        video_id = str(window.get("video_id") or "")
        start_date = str(window.get("start_date") or "")
        end_date = str(window.get("end_date") or "")
        data = (
            service.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics=metrics,
                dimensions="video",
                filters=f"video=={video_id}",
                sort="-views",
                maxResults=1,
            )
            .execute()
        )
        report_rows = _validated_comment_reply_rows(data, dimension="video")
        if len(report_rows) > 1:
            raise RuntimeError(
                "YouTube Analytics returned multiple rows for one video"
            )
        row = report_rows[0] if report_rows else {}
        if row and row["video"] != video_id:
            raise RuntimeError(
                "YouTube Analytics video provenance does not match the request"
            )

        def optional_count(name: str) -> int | None:
            value = row.get(name)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool)
                else None
            )

        gained = optional_count("subscribersGained")
        lost = optional_count("subscribersLost")
        rows.append(
            {
                "video_id": video_id,
                "start_date": start_date,
                "end_date": end_date,
                "views": optional_count("views"),
                "comments": optional_count("comments"),
                "subscribers_gained": gained,
                "subscribers_lost": lost,
                "net_subscribers": (
                    gained - lost if gained is not None and lost is not None else None
                ),
            }
        )
    return {
        "source": "youtube_analytics_api_v2",
        "metrics": [
            "views",
            "comments",
            "subscribersGained",
            "subscribersLost",
        ],
        "availability_start_date": availability_start_date,
        "availability_probe_end_date": availability_end_date,
        "data_through_date": max(available_days) if available_days else None,
        "videos": rows,
    }


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
    # engagedViews(issue #97): 2025年3月末のShorts仕様変更で`views`は
    # 再生開始のみでカウントされるようになった一方、それ以前の「数秒以上
    # 視聴」という定義はengagedViewsとして存続している。views/engagedViews
    # の乖離率は、離脱の多いスクロール型視聴とちゃんと見られた視聴を区別する
    # 指標になり得る。「Basic user activity statistics」の同じメトリクス群に
    # engagedViews/views/likes/comments/estimatedMinutesWatchedが並んでおり、
    # 既存メトリクスと組み合わせ可能。現時点ではsnapshotへの収集のみ行い、
    # performance.pyの比較ロジックはまだ消費しない
    # （Shorts corner限定の指標であり、他cornerと同列に扱うと長尺動画の
    # 乖離ゼロを誤ってシグナルとして拾いかねないため、消費する場合は
    # corner別の扱いを別途設計する）。
    # shares(issue #144)は共有率専用の video_share_metrics で取得する。
    # この90日クエリへ shares を混ぜると、shares の提供が他メトリクスより
    # 遅れた場合にレポート全体の利用可能最終日が古くなるため混ぜない
    # （Sol review指摘）。
    metrics = (
        "views,engagedViews,estimatedMinutesWatched,averageViewDuration,"
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
                    "engaged_views": int(row.get("engagedViews", 0) or 0),
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


def video_share_metrics(
    video_ids: list[str],
    *,
    start_date: str,
    end_date: str,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> list[dict]:
    """共有率用に views/shares だけを読み取る（issue #144）。

    30日集計の共有率はshorts専用のため、全corner・全メトリクスを再取得せず、
    対象動画に絞って `views,shares` のみを取得する。APIは要求した全メトリクスが
    揃う日までしか返さないため、無関係なメトリクスを含めると期間が短くなるのを
    避ける。欠落列は0でなくNoneで保持する（fail-closed）。
    """
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
    results: list[dict] = []
    for offset in range(0, len(ids), 200):
        data = (
            service.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics="views,shares",
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
                    "shares": (
                        int(row["shares"])
                        if row.get("shares") is not None
                        else None
                    ),
                }
            )
    return results


def shorts_bridge_metrics(
    source_video_id: str,
    target_video_id: str,
    *,
    start_date: str,
    end_date: str,
    availability_end_date: str,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> dict:
    """Shortsから関連動画へ遷移した視聴を同一期間で読む（issue #138）。

    分母は元Shortの ``views``、分子候補は遷移先動画の
    ``insightTrafficSourceType==RELATED_VIDEO`` 詳細のうち、参照元動画IDが
    元Shortと一致する ``views``。後者は公式APIが返す上位25件だけであり、行が
    無い場合は0と断定せず ``None`` を返す。これはクリック数ではなく遷移先で
    発生した視聴数なので、呼び出し側もCTRとは呼ばない。

    最初に ``day`` 次元で ``views`` の利用可能最終日を、観測終了日より後の
    完了日も含めて確認する。終了日以降の行が無ければ集計クエリを実行せず、
    呼び出し側が再試行または判定材料不足として終了できるようcountを ``None`` で
    返す。確認できた場合だけ、元の ``start_date`` / ``end_date`` で元Short合計と
    遷移先の参照元詳細を読む。
    """
    from googleapiclient.discovery import build

    source_id = str(source_video_id or "").strip()
    target_id = str(target_video_id or "").strip()
    if not source_id or not target_id:
        raise ValueError("source_video_id and target_video_id are required")
    if source_id == target_id:
        raise ValueError("source_video_id and target_video_id must differ")
    try:
        requested_end = date.fromisoformat(end_date)
        availability_end = date.fromisoformat(availability_end_date)
    except ValueError as exc:
        raise ValueError("end dates must be YYYY-MM-DD") from exc
    if availability_end < requested_end:
        raise ValueError("availability_end_date must not precede end_date")

    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ANALYTICS_READONLY_SCOPES,
        exact_scopes=True,
    )
    service = build("youtubeAnalytics", "v2", credentials=creds)

    availability_data = (
        service.reports()
        .query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=availability_end_date,
            metrics="views",
            dimensions="day",
            sort="day",
            maxResults=200,
        )
        .execute()
    )
    availability_headers = [
        header.get("name", "")
        for header in availability_data.get("columnHeaders", [])
    ]
    available_dates: list[str] = []
    for values in availability_data.get("rows", []):
        row = dict(zip(availability_headers, values))
        raw_day = str(row.get("day") or "")
        try:
            parsed = date.fromisoformat(raw_day)
        except ValueError:
            continue
        if start_date <= parsed.isoformat() <= availability_end_date:
            available_dates.append(parsed.isoformat())
    data_through_date = max(available_dates, default=None)
    base_result = {
        "source_video_id": source_id,
        "target_video_id": target_id,
        "start_date": start_date,
        "end_date": end_date,
        "availability_probe_end_date": availability_end_date,
        "views_data_through_date": data_through_date,
        "source_views": None,
        "attributed_target_views": None,
        "attribution_source_type": "RELATED_VIDEO",
        "attribution_detail_limit": 25,
    }
    if data_through_date is None or data_through_date < end_date:
        return base_result

    source_data = (
        service.reports()
        .query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="views",
            dimensions="video",
            filters=f"video=={source_id}",
            maxResults=1,
        )
        .execute()
    )
    source_headers = [
        header.get("name", "") for header in source_data.get("columnHeaders", [])
    ]
    source_views: int | None = None
    for values in source_data.get("rows", []):
        row = dict(zip(source_headers, values))
        if str(row.get("video") or "") != source_id:
            continue
        raw_views = row.get("views")
        if raw_views is None:
            continue
        source_views = int(raw_views)
        break

    target_data = (
        service.reports()
        .query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="views",
            dimensions="insightTrafficSourceDetail",
            filters=(
                f"video=={target_id};"
                "insightTrafficSourceType==RELATED_VIDEO"
            ),
            sort="-views",
            maxResults=25,
        )
        .execute()
    )
    target_headers = [
        header.get("name", "") for header in target_data.get("columnHeaders", [])
    ]
    attributed_views: int | None = None
    for values in target_data.get("rows", []):
        row = dict(zip(target_headers, values))
        if str(row.get("insightTrafficSourceDetail") or "") != source_id:
            continue
        raw_views = row.get("views")
        if raw_views is None:
            continue
        value = int(raw_views)
        attributed_views = (attributed_views or 0) + value

    return {
        **base_result,
        "source_views": source_views,
        "attributed_target_views": attributed_views,
    }


def video_retention_curves(
    video_ids: list[str],
    *,
    start_date: str,
    end_date: str,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """動画別の視聴者維持率カーブを読み取る（issue #149）。

    `audienceWatchRatio`（各時点の視聴維持率。0.9=90%）を
    `elapsedVideoTimeRatio`（経過時間比率 0〜1）ディメンションで取得する。
    Audience retention report の `video` filter は単一IDのみのため、動画ごとに
    1リクエストを発行する。動画固有と確認済みの理由（プライバシー閾値等）だけ
    `failed_by_video` へ記録して継続する。`invalidFilters` 等のリクエスト構造
    不備や 401/403/429/5xx・ネットワーク障害は全体障害として即時 raise する。
    Shorts等でAPIがデータを返さない場合は空のまま（欠落を0や「なし」と断定
    しない fail-closed）。

    戻り値は `(by_video, failed_by_video)` のタプル。
    """
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    # 動画固有と確認済みのHttpError reason（allow-list）。`invalidFilters` 等の
    # リクエスト構造不備は含めない。
    video_specific_reasons = frozenset(
        {
            "privacy",
            "private",
            "videonotfound",
            "invalidvideoid",
            "forbidden",
        }
    )

    def _http_reason(exc: HttpError) -> str:
        try:
            details = exc.error_details
        except Exception:
            details = None
        if isinstance(details, list):
            for entry in details:
                if isinstance(entry, dict) and entry.get("reason"):
                    return " ".join(str(entry["reason"]).split()).casefold()
        try:
            public_reason = exc.reason
        except Exception:
            public_reason = None
        if public_reason:
            return " ".join(str(public_reason).split()).casefold()
        return ""

    ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if not ids:
        return {}, {}
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ANALYTICS_SCOPES,
    )
    service = build("youtubeAnalytics", "v2", credentials=creds)
    by_video: dict[str, list[dict]] = {}
    failed_by_video: dict[str, str] = {}
    for video_id in ids:
        try:
            data = (
                service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="audienceWatchRatio",
                    dimensions="elapsedVideoTimeRatio",
                    filters=f"video=={video_id}",
                    maxResults=100,
                )
                .execute()
            )
        except HttpError as exc:
            status = exc.resp.status
            reason = _http_reason(exc)
            if status in (400, 404) and reason in video_specific_reasons:
                failed_by_video[video_id] = f"HTTP {status}: {reason}"
                continue
            raise
        headers = [header.get("name", "") for header in data.get("columnHeaders", [])]
        points: list[dict] = []
        for values in data.get("rows", []):
            row = dict(zip(headers, values))
            ratio = row.get("elapsedVideoTimeRatio")
            watch_ratio = row.get("audienceWatchRatio")
            if ratio is None or watch_ratio is None:
                continue
            try:
                ratio_value = float(ratio)
                watch_value = float(watch_ratio)
            except (TypeError, ValueError):
                continue
            if not (0.0 <= ratio_value <= 1.0):
                continue
            points.append(
                {"elapsed_ratio": ratio_value, "watch_ratio": watch_value}
            )
        if points:
            points.sort(key=lambda item: item["elapsed_ratio"])
            by_video[video_id] = points
    return by_video, failed_by_video


def video_traffic_sources(
    video_ids: list[str],
    *,
    start_date: str,
    end_date: str,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> dict[str, dict[str, int]]:
    """動画別トラフィックソース種別のviewsを読み取る（issue #164）。

    `insightTrafficSourceType` ディメンションで、`YT_SEARCH`（YouTube検索）等の
    種別ごとの views を返す。取得できない動画・種別は含めない（取得可能な
    readbackであり、欠落を0として推測しない）。Shorts等でAPIがデータを返さない
    場合は空のまま（fail-closed）。
    """
    from googleapiclient.discovery import build

    ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if not ids:
        return {}
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ANALYTICS_SCOPES,
    )
    service = build("youtubeAnalytics", "v2", credentials=creds)
    by_video: dict[str, dict[str, int]] = {}
    for offset in range(0, len(ids), 200):
        # startIndexページング: 200動画×複数sourceで200行を超えると
        # 下位行が暗黙に切り捨てられるため、APIが返せる全行を読む
        # （Sol review指摘5）。
        start_index = 1
        while True:
            data = (
                service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="views",
                    dimensions="video,insightTrafficSourceType",
                    filters=f"video=={','.join(ids[offset : offset + 200])}",
                    sort="-views",
                    maxResults=200,
                    startIndex=start_index,
                )
                .execute()
            )
            headers = [
                header.get("name", "") for header in data.get("columnHeaders", [])
            ]
            rows = data.get("rows", [])
            for values in rows:
                row = dict(zip(headers, values))
                video_id = str(row.get("video", ""))
                source_type = str(row.get("insightTrafficSourceType", "") or "")
                views = int(row.get("views", 0) or 0)
                if not video_id or not source_type or views <= 0:
                    continue
                by_video.setdefault(video_id, {})[source_type] = views
            if len(rows) < 200:
                break
            start_index += len(rows)
    return by_video


def video_search_terms(
    video_ids: list[str],
    *,
    start_date: str,
    end_date: str,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """動画別の具体的な検索語句とviewsを読み取る（issue #164）。

    `insightTrafficSourceDetail` ディメンション（公式仕様）で、YouTube検索
    （`insightTrafficSourceType==YT_SEARCH`）から流入した検索語句を動画単位で
    返す。`maxResults` は公式上限の25。APIがShorts等でデータを返さない場合は
    空リスト（欠落を0や「なし」と断定しない）。取得できる範囲だけ記録する。

    戻り値は `(by_video, failed_by_video)` のタプル。動画固有と確認済みの理由
    （`insightTrafficSourceType` 除外・プライバシー閾値・動画ID不明）だけを
    `failed_by_video` へ記録し、成功分は保持する。`invalidFilters` 等の
    リクエスト構造不備・認証・権限・クォータ・サーバ障害（401/403/429/5xx）や
    ネットワーク障害は全体障害として即時 raise する。
    """
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    # 動画固有の取得不能として許容するHttpError reason（allow-list）。
    # `invalidFilters` 等のリクエスト構造不備や、動画IDとは無関係の理由は含めない。
    video_specific_reasons = frozenset(
        {
            "insighttrafficsourcedetail",
            "insighttrafficsourcetype",
            "privacy",
            "private",
            "videonotfound",
            "invalidvideoid",
        }
    )

    def _http_reason(exc: HttpError) -> str:
        """Google APIエラーのsemantic reasonを抽出する。

        `resp.reason` は通常「Bad Request」等のHTTP reason phraseであり分類に
        使えないため、semantic reasonは `error_details[].reason` → 公開属性
        `exc.reason` の順で取り、どちらも無ければ空文字を返す。
        """
        try:
            details = exc.error_details
        except Exception:
            details = None
        if isinstance(details, list):
            for entry in details:
                if isinstance(entry, dict) and entry.get("reason"):
                    return " ".join(str(entry["reason"]).split()).casefold()
        public_reason = getattr(exc, "reason", None)
        if public_reason:
            return " ".join(str(public_reason).split()).casefold()
        return ""

    ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if not ids:
        return {}, {}
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ANALYTICS_SCOPES,
    )
    service = build("youtubeAnalytics", "v2", credentials=creds)
    by_video: dict[str, list[dict]] = {}
    failed_by_video: dict[str, str] = {}
    last_video_specific_error: HttpError | None = None
    all_failed = True
    for video_id in ids:
        try:
            data = (
                service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="views",
                    dimensions="insightTrafficSourceDetail",
                    filters=f"video=={video_id};insightTrafficSourceType==YT_SEARCH",
                    sort="-views",
                    maxResults=25,
                )
                .execute()
            )
            all_failed = False
        except HttpError as exc:
            status = exc.resp.status
            reason = _http_reason(exc)
            if status in (400, 404) and reason in video_specific_reasons:
                # 動画固有と確認済みの理由（プライバシー閾値・不正ID等）だけ
                # 他動画の結果へ波及させずスキップし、失敗情報だけ記録する。
                failed_by_video[video_id] = (
                    f"HTTP {status}: {reason or 'unknown'}"
                )
                last_video_specific_error = exc
                continue
            # invalidFilters・不正dimensions/date等のリクエスト不備や
            # 認証・クォータ・サーバ障害は全体障害として即時中断する。
            raise
        except Exception:
            # 非HttpError（ネットワーク等）は全体障害として即時中断する。
            raise
        headers = [header.get("name", "") for header in data.get("columnHeaders", [])]
        for values in data.get("rows", []):
            row = dict(zip(headers, values))
            term = str(row.get("insightTrafficSourceDetail", "") or "")
            views = int(row.get("views", 0) or 0)
            if not term or views <= 0:
                continue
            by_video.setdefault(video_id, []).append({"term": term, "views": views})
    if all_failed and last_video_specific_error is not None:
        # 全動画が動画固有エラーで失敗した場合、部分取得成功ではなく
        # 全体障害として扱い呼び出し元が status へ記録できるよう再送出する。
        raise last_video_specific_error
    return by_video, failed_by_video


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


def _video_snippet(
    video_id: str,
    *,
    required_scopes: list[str] | None = None,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> tuple[object, dict]:
    """_video_status のsnippet版。part="snippet" で現在値を取得する。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        raise ValueError(f"invalid YouTube video id: {video_id!r}")
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=required_scopes or ACCOUNT_SCOPES,
    )
    service = _build_service(creds)
    current = service.videos().list(part="snippet", id=video_id).execute()
    items = current.get("items") or []
    if len(items) != 1:
        raise RuntimeError(f"YouTube動画が見つかりません: {video_id}")
    return service, dict(items[0].get("snippet") or {})


def video_snippet(
    video_id: str,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> dict:
    """現在のtitle/description等を読み取る（変更なし）。"""
    _, snippet = _video_snippet(
        video_id,
        token_file=token_file,
        client_secret_file=client_secret_file,
    )
    return snippet


def update_title_description(
    video_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    expected_title: str | None = None,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    """期待した現在タイトルのときだけ、他の書込み可能snippetを保持してtitle/descriptionを更新する(issue #57)。

    YouTube Data APIのsnippet更新はオブジェクト全体送信が必要(部分送信では
    tags/categoryId等の省略フィールドが失われ得る)ため、現snippetを取得して
    書込可能キーだけ保持し、title/descriptionのみ上書きして送り返す。
    """
    if title is None and description is None:
        raise ValueError("title と description の少なくとも一方を指定してください")
    if title is not None:
        if not title.strip():
            raise ValueError("title を空文字にはできません")
        if len(title) > _MAX_VIDEO_TITLE_LENGTH:
            raise ValueError(
                f"title は{_MAX_VIDEO_TITLE_LENGTH}文字以内にしてください: {len(title)}文字"
            )
        if "<" in title or ">" in title:
            raise ValueError("title に '<' '>' は使用できません")
    service, current_snippet = _video_snippet(
        video_id,
        required_scopes=MANAGE_SCOPES,
        token_file=token_file,
        client_secret_file=client_secret_file,
    )
    current_title = str(current_snippet.get("title") or "")
    if expected_title is not None and current_title != expected_title:
        raise RuntimeError(
            f"YouTubeタイトルが想定外です: expected={expected_title!r} "
            f"actual={current_title!r}"
        )
    new_title = current_title if title is None else title
    new_description = (
        str(current_snippet.get("description") or "")
        if description is None
        else description
    )
    if new_title == current_title and new_description == str(
        current_snippet.get("description") or ""
    ):
        return "unchanged"
    snippet = {
        key: value
        for key, value in current_snippet.items()
        if key in _WRITABLE_VIDEO_SNIPPET_KEYS
    }
    snippet["title"] = new_title
    snippet["description"] = new_description
    service.videos().update(
        part="snippet",
        body={"id": video_id, "snippet": snippet},
    ).execute()
    return "updated"


def ensure_playlist(
    title: str,
    *,
    description: str = "",
    privacy: str = "unlisted",
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    """titleと同名の再生リストのIDを返す。無ければ作成する(issue #86)。"""
    if not title.strip():
        raise ValueError("title を空文字にはできません")
    if privacy not in {"public", "unlisted", "private"}:
        raise ValueError(f"invalid YouTube privacy: {privacy!r}")
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=MANAGE_SCOPES,
    )
    service = _build_service(creds)
    page_token = None
    while True:
        resp = (
            service.playlists()
            .list(part="snippet", mine=True, maxResults=50, pageToken=page_token)
            .execute()
        )
        for item in resp.get("items") or []:
            if (item.get("snippet") or {}).get("title") == title:
                return item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    created = (
        service.playlists()
        .insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": privacy},
            },
        )
        .execute()
    )
    return created["id"]


def playlist_video_ids(
    playlist_id: str,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> set[str]:
    """再生リストに現在含まれる動画IDの集合を返す（変更なし）。"""
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=ACCOUNT_SCOPES,
    )
    service = _build_service(creds)
    video_ids: set[str] = set()
    page_token = None
    while True:
        resp = (
            service.playlistItems()
            .list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("items") or []:
            video_id = (item.get("contentDetails") or {}).get("videoId")
            if video_id:
                video_ids.add(video_id)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return video_ids


def add_video_to_playlist(
    playlist_id: str,
    video_id: str,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    """再生リストに動画を追加する。既に含まれていれば何もしない(issue #86)。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        raise ValueError(f"invalid YouTube video id: {video_id!r}")
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=MANAGE_SCOPES,
    )
    service = _build_service(creds)
    existing = (
        service.playlistItems()
        .list(part="id", playlistId=playlist_id, videoId=video_id)
        .execute()
    )
    if existing.get("items"):
        return "already_present"
    service.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()
    return "added"


_MAX_CHANNEL_KEYWORDS_LENGTH = 500


def set_channel_keywords(
    keywords: list[str],
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    """チャンネルのbrandingSettings.channel.keywordsを設定する(issue #86)。

    channel配下のtitle等、およびimage/hints等の兄弟オブジェクトを含む
    brandingSettings全体を現状値のまま保持し、channel.keywordsだけ
    空白区切り(スペースを含む語は引用符で囲む)の1文字列にして上書きする。
    YouTubeのkeywords引用符構文にエスケープ機構は無いため、語に含まれる
    `"` はそのまま送ると構文が壊れる。エスケープではなく除去することで
    安全側に倒す。
    """
    if not keywords:
        raise ValueError("keywords を空にはできません")
    sanitized = [kw.replace('"', "") for kw in keywords]
    joined = " ".join(f'"{kw}"' if " " in kw else kw for kw in sanitized)
    if len(joined) > _MAX_CHANNEL_KEYWORDS_LENGTH:
        raise ValueError(
            f"keywords は{_MAX_CHANNEL_KEYWORDS_LENGTH}文字以内にしてください: {len(joined)}文字"
        )
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=MANAGE_SCOPES,
    )
    service = _build_service(creds)
    current = service.channels().list(part="brandingSettings", mine=True).execute()
    items = current.get("items") or []
    if len(items) != 1:
        raise RuntimeError("YouTubeチャンネルが見つかりません")
    branding = dict(items[0].get("brandingSettings") or {})
    channel_branding = dict(branding.get("channel") or {})
    channel_branding["keywords"] = joined
    branding["channel"] = channel_branding
    service.channels().update(
        part="brandingSettings",
        body={"id": items[0]["id"], "brandingSettings": branding},
    ).execute()
    return "updated"


def post_comment(
    video_id: str,
    text: str,
    *,
    token_file: Path | None = None,
    client_secret_file: Path | None = None,
) -> str:
    """動画へトップレベルコメントを投稿し、コメントIDを返す(issue #86)。

    YouTube Data APIにコメントを「固定」するエンドポイントは無いため、
    投稿後の固定はYouTube Studioから手動で行う必要がある。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        raise ValueError(f"invalid YouTube video id: {video_id!r}")
    if not text.strip():
        raise ValueError("text を空文字にはできません")
    creds = _load_credentials(
        interactive=False,
        token_file=token_file,
        client_secret_file=client_secret_file,
        scopes=MANAGE_SCOPES,
    )
    service = _build_service(creds)
    resp = (
        service.commentThreads()
        .insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": text}},
                }
            },
        )
        .execute()
    )
    return resp["id"]


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube アップロード")
    ap.add_argument("--auth", action="store_true", help="初回OAuth同意してtokenを保存")
    analytics_mode = ap.add_mutually_exclusive_group()
    analytics_mode.add_argument(
        "--analytics",
        action="store_true",
        help="--auth時にYouTube Analytics読み取りscopeも要求",
    )
    analytics_mode.add_argument(
        "--analytics-readonly",
        action="store_true",
        help=(
            "--auth時にAnalytics用read-only scopeだけを要求し、"
            "分析専用tokenへ保存（upload権限なし）"
        ),
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
    ap.add_argument(
        "--update-video",
        metavar="VIDEO_ID",
        help="公開済み動画のタイトル・説明欄を更新（新値未指定なら現状表示のみ）",
    )
    ap.add_argument("--new-title", help="--update-video で設定する新タイトル")
    ap.add_argument(
        "--new-description-file",
        help="--update-video で設定する新説明欄全文のファイルパス",
    )
    ap.add_argument(
        "--expected-title",
        help="現在のタイトルがこれと一致するときだけ更新する（競合検出）",
    )
    ap.add_argument(
        "--ensure-playlist",
        metavar="TITLE",
        help="同名の再生リストのIDを表示する。無ければ作成する",
    )
    ap.add_argument(
        "--add-to-playlist",
        metavar="PLAYLIST_ID",
        help="--video-id で指定した動画をこの再生リストに追加する",
    )
    ap.add_argument("--video-id", help="--add-to-playlist で追加する対象動画ID")
    ap.add_argument(
        "--set-channel-keywords",
        metavar="KEYWORDS",
        help="カンマ区切りのキーワードでチャンネルのbrandingSettingsを更新する",
    )
    ap.add_argument(
        "--post-comment",
        metavar="VIDEO_ID",
        help="指定した動画にトップレベルコメントを投稿する（--comment-text必須）。固定は手動",
    )
    ap.add_argument("--comment-text", help="--post-comment で投稿する本文")
    args = ap.parse_args()
    privacy = config.YOUTUBE_PRIVACY
    token_file = Path(config.YOUTUBE_TOKEN_FILE)
    analytics_token_file = Path(config.YOUTUBE_ANALYTICS_TOKEN_FILE)
    client_secret_file = Path(config.YOUTUBE_CLIENT_SECRET_FILE)
    if args.channel:
        from . import channel

        spec = channel.load(args.channel)
        privacy = spec.publish.youtube.privacy
        token_file = spec.publish.youtube.token
        analytics_token_file = spec.publish.youtube.analytics_token
        client_secret_file = spec.publish.youtube.client_secret
    if args.auth:
        if args.analytics_readonly and args.manage:
            ap.error("--analytics-readonly cannot be combined with --manage")
        if args.analytics_readonly:
            if analytics_token_file.resolve() == token_file.resolve():
                ap.error(
                    "Analytics read-only token path must differ from "
                    "the publish token path"
                )
            auth_scopes = list(ANALYTICS_READONLY_SCOPES)
            token_file = analytics_token_file
        else:
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
            exact_scopes=args.analytics_readonly,
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
    if args.update_video:
        new_description = None
        if args.new_description_file:
            new_description = Path(args.new_description_file).read_text(
                encoding="utf-8"
            )
        if args.new_title is None and new_description is None:
            snippet = video_snippet(
                args.update_video,
                token_file=token_file,
                client_secret_file=client_secret_file,
            )
            print(f"title: {snippet.get('title', '')}")
            print(f"description: {snippet.get('description', '')}")
            return
        result = update_title_description(
            args.update_video,
            title=args.new_title,
            description=new_description,
            expected_title=args.expected_title,
            token_file=token_file,
            client_secret_file=client_secret_file,
        )
        print(f"{args.update_video}: {result}")
        return
    if args.ensure_playlist:
        playlist_id = ensure_playlist(
            args.ensure_playlist,
            token_file=token_file,
            client_secret_file=client_secret_file,
        )
        print(f"playlist_id={playlist_id}")
        return
    if args.add_to_playlist:
        if not args.video_id:
            raise SystemExit("--add-to-playlist には --video-id が必要です")
        result = add_video_to_playlist(
            args.add_to_playlist,
            args.video_id,
            token_file=token_file,
            client_secret_file=client_secret_file,
        )
        print(f"{args.video_id} -> {args.add_to_playlist}: {result}")
        return
    if args.set_channel_keywords:
        keywords = [kw.strip() for kw in args.set_channel_keywords.split(",") if kw.strip()]
        result = set_channel_keywords(
            keywords,
            token_file=token_file,
            client_secret_file=client_secret_file,
        )
        print(f"keywords: {result}")
        return
    if args.post_comment:
        if not args.comment_text:
            raise SystemExit("--post-comment には --comment-text が必要です")
        comment_id = post_comment(
            args.post_comment,
            args.comment_text,
            token_file=token_file,
            client_secret_file=client_secret_file,
        )
        print(f"comment_id={comment_id}（固定はYouTube Studioで手動操作してください）")
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
