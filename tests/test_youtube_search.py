from __future__ import annotations

import json
import unittest
from googleapiclient.errors import HttpError
from types import SimpleNamespace
import tempfile
from pathlib import Path
from unittest import mock

from doci import youtube


class _FakeResp:
    """httplib2 Responseと同様にdict-likeアクセスを持つ最小スタブ。"""

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason

    def get(self, key: str, default=None):
        return {"content-type": "application/json"}.get(key, default)


def _google_http_error(status: int, reason: str) -> HttpError:
    """Google APIの実際のJSONエラー形状からHttpErrorを生成する。"""
    body = json.dumps(
        {
            "error": {
                "code": status,
                "message": reason,
                "errors": [{"reason": reason, "message": reason}],
            }
        }
    ).encode("utf-8")
    return HttpError(
        _FakeResp(status, "Bad Request" if status == 400 else "Not Found"),
        body,
    )


class _Request:
    def execute(self) -> dict:
        return {
            "items": [
                {
                    "id": {"videoId": "abc123"},
                    "snippet": {
                        "title": "ショートの伸ばし方",
                        "channelTitle": "運営者チャンネル",
                        "publishedAt": "2026-07-01T00:00:00Z",
                        "description": "実例を使って説明します",
                    },
                },
                {"id": {}, "snippet": {"title": "動画ではない結果"}},
            ]
        }


class _Search:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def list(self, **kwargs):
        self.kwargs = kwargs
        return _Request()


class _VideosRequest:
    def execute(self) -> dict:
        return {
            "items": [
                {
                    "id": "abc123",
                    "snippet": {"description": "章立てを含む全文説明欄"},
                    "contentDetails": {"duration": "PT8M12S"},
                    "statistics": {
                        "viewCount": "12000",
                        "likeCount": "430",
                        "commentCount": "12",
                    },
                    "status": {"privacyStatus": "public"},
                }
            ]
        }


class _Videos:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def list(self, **kwargs):
        self.kwargs = kwargs
        return _VideosRequest()


class _Service:
    def __init__(self) -> None:
        self.search_resource = _Search()
        self.videos_resource = _Videos()

    def search(self) -> _Search:
        return self.search_resource

    def videos(self) -> _Videos:
        return self.videos_resource


class YouTubeSearchTest(unittest.TestCase):
    def test_upload_only_classifies_session_start_4xx_as_preflight(self) -> None:
        class FakeHttpError(Exception):
            def __init__(self) -> None:
                super().__init__("HTTP 400")
                self.resp = SimpleNamespace(status=400)

        class Request:
            def __init__(self, resumable_uri) -> None:
                self.resumable_uri = resumable_uri

            def next_chunk(self):
                raise FakeHttpError()

        class Videos:
            def __init__(self, resumable_uri) -> None:
                self.resumable_uri = resumable_uri

            def insert(self, **_kwargs):
                return Request(self.resumable_uri)

        class Service:
            def __init__(self, resumable_uri) -> None:
                self.resource = Videos(resumable_uri)

            def videos(self):
                return self.resource

        for resumable_uri, expected in (
            (None, youtube.UploadPreflightError),
            ("https://upload.example/session", FakeHttpError),
        ):
            with self.subTest(resumable_uri=resumable_uri):
                with (
                    mock.patch.object(youtube, "_load_credentials", return_value=object()),
                    mock.patch(
                        "googleapiclient.discovery.build",
                        return_value=Service(resumable_uri),
                    ),
                    mock.patch(
                        "googleapiclient.http.MediaFileUpload",
                        return_value=object(),
                    ),
                ):
                    with self.assertRaises(expected):
                        youtube.upload(
                            Path("/tmp/video.mp4"),
                            "Title",
                            "Description",
                            [],
                        )

    def test_search_public_videos_returns_real_video_metadata(self) -> None:
        service = _Service()
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            results = youtube.search_public_videos("YouTube ショート 伸ばし方")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["channel"], "運営者チャンネル")
        self.assertEqual(results[0]["url"], "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(results[0]["description"], "章立てを含む全文説明欄")
        self.assertEqual(results[0]["view_count"], "12000")
        self.assertEqual(results[0]["duration"], "PT8M12S")
        self.assertEqual(service.search_resource.kwargs["type"], "video")
        self.assertEqual(service.search_resource.kwargs["relevanceLanguage"], "ja")

    def test_fetch_public_transcript_uses_embedded_caption_track(self) -> None:
        api = mock.Mock()
        api.fetch.return_value = [
            SimpleNamespace(text="冒頭で結論を見せる"),
            SimpleNamespace(text="\n次に実例を説明する"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with mock.patch(
                "youtube_transcript_api.YouTubeTranscriptApi", return_value=api
            ):
                transcript = youtube.fetch_public_transcript(
                    "https://www.youtube.com/watch?v=abc", cache_dir=cache_dir
                )
                cached = youtube.fetch_public_transcript(
                    "https://www.youtube.com/watch?v=abc", cache_dir=cache_dir
                )

        self.assertEqual(transcript, "冒頭で結論を見せる 次に実例を説明する")
        self.assertEqual(cached, transcript)
        api.fetch.assert_called_once_with("abc", languages=["ja"])

    def test_video_details_returns_read_only_performance_fields(self) -> None:
        service = _Service()
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            results = youtube.video_details(["abc123"])

        self.assertEqual(results[0]["views"], 12000)
        self.assertEqual(results[0]["likes"], 430)
        self.assertEqual(results[0]["comments"], 12)
        self.assertEqual(results[0]["privacy_status"], "public")
        self.assertEqual(
            service.videos_resource.kwargs["part"],
            "snippet,contentDetails,statistics,status",
        )

    def test_video_analytics_maps_column_headers(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "video"},
                {"name": "views"},
                {"name": "engagedViews"},
                {"name": "estimatedMinutesWatched"},
                {"name": "averageViewDuration"},
                {"name": "averageViewPercentage"},
                {"name": "likes"},
                {"name": "comments"},
            ],
            "rows": [["abc123", 80, 65, 120.5, 90.0, 72.4, 5, 2]],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            results = youtube.video_analytics(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(results[0]["average_view_percentage"], 72.4)
        self.assertEqual(results[0]["views"], 80)
        self.assertEqual(results[0]["engaged_views"], 65)
        self.assertEqual(reports.query.call_args.kwargs["dimensions"], "video")
        self.assertEqual(reports.query.call_args.kwargs["filters"], "video==abc123")
        self.assertEqual(reports.query.call_args.kwargs["sort"], "-views")
        self.assertEqual(reports.query.call_args.kwargs["maxResults"], 200)
        self.assertIn("engagedViews", reports.query.call_args.kwargs["metrics"])

    def test_video_analytics_defaults_engaged_views_to_zero_when_absent(self) -> None:
        """`engagedViews`列がレスポンスに含まれない場合でもKeyErrorにならず
        0を返すことを固定する（他のメトリクスと同じ`.get(key, 0)`防御パターン
        の確認。無効なメトリクス指定自体はAPI側がHTTP 400で拒否するため、
        実運用でこの経路に入るのは想定外の応答形状の場合のみ）。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [{"name": "video"}, {"name": "views"}],
            "rows": [["abc123", 80]],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            results = youtube.video_analytics(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )
        self.assertEqual(results[0]["engaged_views"], 0)

    def test_video_share_metrics_queries_only_views_and_shares(self) -> None:
        """issue #144: 共有率用に views/shares だけを取得し、shares欠落は
        None（0にしない）。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "video"},
                {"name": "views"},
                {"name": "shares"},
            ],
            "rows": [["abc123", 500, 8]],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            results = youtube.video_share_metrics(
                ["abc123"],
                start_date="2026-06-25",
                end_date="2026-07-24",
            )
        self.assertEqual(results[0]["video_id"], "abc123")
        self.assertEqual(results[0]["views"], 500)
        self.assertEqual(results[0]["shares"], 8)
        self.assertEqual(
            reports.query.call_args.kwargs["metrics"], "views,shares"
        )
        self.assertEqual(
            reports.query.call_args.kwargs["startDate"], "2026-06-25"
        )
        self.assertEqual(
            reports.query.call_args.kwargs["endDate"], "2026-07-24"
        )

    def test_video_share_metrics_keeps_missing_shares_as_none(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [{"name": "video"}, {"name": "views"}],
            "rows": [["abc123", 500]],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            results = youtube.video_share_metrics(
                ["abc123"],
                start_date="2026-06-25",
                end_date="2026-07-24",
            )
        self.assertIsNone(results[0]["shares"])

    def test_video_share_metrics_batches_beyond_200_ids(self) -> None:
        """issue #144 (Sol review指摘): 201件では2リクエストへ分割され、
        両方の結果が結合される。"""
        ids = [f"id-{index:03d}" for index in range(201)]
        reports = mock.Mock()

        def _query(**kwargs):
            filters = str(kwargs["filters"])
            count = filters.count(",") + 1
            rows = [
                [f"id-{index:03d}", count, 1]
                for index in range(count)
            ]
            return mock.Mock(
                execute=lambda: {
                    "columnHeaders": [
                        {"name": "video"},
                        {"name": "views"},
                        {"name": "shares"},
                    ],
                    "rows": rows,
                }
            )

        reports.query.side_effect = _query
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            results = youtube.video_share_metrics(
                ids,
                start_date="2026-06-25",
                end_date="2026-07-24",
            )
        self.assertEqual(len(results), 201)
        self.assertEqual(reports.query.call_count, 2)

    def test_video_share_metrics_empty_input_returns_without_api(self) -> None:
        """issue #144 (Sol review指摘): 空IDでは認証・API buildを行わない。"""
        with (
            mock.patch.object(
                youtube, "_load_credentials", side_effect=AssertionError
            ),
            mock.patch(
                "googleapiclient.discovery.build", side_effect=AssertionError
            ),
        ):
            results = youtube.video_share_metrics(
                [],
                start_date="2026-06-25",
                end_date="2026-07-24",
            )
        self.assertEqual(results, [])

    def test_video_traffic_sources_maps_source_type_views(self) -> None:
        """issue #164: トラフィックソース種別ごとのviewsをvideo_idで返す。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "video"},
                {"name": "insightTrafficSourceType"},
                {"name": "views"},
            ],
            "rows": [
                ["abc123", "YT_SEARCH", 42],
                ["abc123", "SHORTS", 10],
                ["def456", "YT_SEARCH", 7],
            ],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            by_video = youtube.video_traffic_sources(
                ["abc123", "def456"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(
            by_video,
            {
                "abc123": {"YT_SEARCH": 42, "SHORTS": 10},
                "def456": {"YT_SEARCH": 7},
            },
        )
        self.assertEqual(reports.query.call_args.kwargs["dimensions"], "video,insightTrafficSourceType")
        self.assertEqual(reports.query.call_args.kwargs["metrics"], "views")

    def test_video_traffic_sources_drops_zero_and_empty_rows(self) -> None:
        """issue #164: views0や空の種別は欠落を0と断定せず結果から除く。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "video"},
                {"name": "insightTrafficSourceType"},
                {"name": "views"},
            ],
            "rows": [
                ["abc123", "YT_SEARCH", 0],
                ["abc123", "", 5],
                ["", "YT_SEARCH", 9],
            ],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            by_video = youtube.video_traffic_sources(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )
        self.assertEqual(by_video, {})

    def test_video_traffic_sources_paginates_beyond_200_rows(self) -> None:
        """issue #164 (Sol review指摘5): 200行を超えるsourceが複数ページに
        分かれても、startIndexページングで全行を結合する。"""
        first_page = {
            "columnHeaders": [
                {"name": "video"},
                {"name": "insightTrafficSourceType"},
                {"name": "views"},
            ],
            "rows": [
                ["abc123", f"TYPE_{index}", index + 1] for index in range(200)
            ],
        }
        second_page = {
            "columnHeaders": [
                {"name": "video"},
                {"name": "insightTrafficSourceType"},
                {"name": "views"},
            ],
            "rows": [["abc123", "EXTRA", 999]],
        }
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [first_page, second_page]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            by_video = youtube.video_traffic_sources(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(len(by_video["abc123"]), 201)
        self.assertEqual(by_video["abc123"]["EXTRA"], 999)
        calls = reports.query.call_args_list
        self.assertEqual(calls[0].kwargs["startIndex"], 1)
        self.assertEqual(calls[1].kwargs["startIndex"], 201)

    def test_video_search_terms_maps_terms_and_views(self) -> None:
        """issue #164: 具体的な検索語句をviews付きで返す。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "insightTrafficSourceDetail"},
                {"name": "views"},
            ],
            "rows": [
                ["ショート 企画", 30],
                ["コンテンツギャップ", 12],
            ],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            by_video, failed = youtube.video_search_terms(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(
            by_video,
            {
                "abc123": [
                    {"term": "ショート 企画", "views": 30},
                    {"term": "コンテンツギャップ", "views": 12},
                ]
            },
        )
        self.assertEqual(failed, {})
        self.assertEqual(
            reports.query.call_args.kwargs["dimensions"],
            "insightTrafficSourceDetail",
        )
        self.assertEqual(
            reports.query.call_args.kwargs["filters"],
            "video==abc123;insightTrafficSourceType==YT_SEARCH",
        )
        self.assertEqual(reports.query.call_args.kwargs["maxResults"], 25)

    def test_video_search_terms_keeps_partial_results_on_individual_failure(self) -> None:
        """issue #164 (Sol review指摘): 1動画の取得不能が他動画の結果へ
        波及しない。成功した動画の検索語句は保持する。"""
        reports = mock.Mock()
        ok_page = {
            "columnHeaders": [
                {"name": "insightTrafficSourceDetail"},
                {"name": "views"},
            ],
            "rows": [["コンテンツギャップ", 12]],
        }
        http_error = _google_http_error(400, "privacy")
        reports.query.return_value.execute.side_effect = [http_error, ok_page]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            by_video, failed = youtube.video_search_terms(
                ["fail-1", "ok-2"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(
            by_video,
            {"ok-2": [{"term": "コンテンツギャップ", "views": 12}]},
        )
        self.assertIn("HTTP 400", failed["fail-1"])

    def test_video_search_terms_raises_immediately_on_global_error(self) -> None:
        """issue #164: 認証・権限・クォータ等の全体障害（HTTP 403等）は
        動画固有エラーと区別して即時中断し、残りを照会し続けない。"""
        reports = mock.Mock()
        http_error = _google_http_error(403, "quotaExceeded")
        reports.query.return_value.execute.side_effect = http_error
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaises(HttpError):
                youtube.video_search_terms(
                    ["a", "b", "c", "d"],
                    start_date="2026-07-01",
                    end_date="2026-07-26",
                )

        # 1件目で即時中断（2件目以降へは進まない）。
        self.assertEqual(reports.query.call_count, 1)

    def test_video_search_terms_invalid_filters_is_global_error(self) -> None:
        """issue #164 (Sol review指摘): invalidFilters等のリクエスト構造不備
        （HTTP 400）は動画固有エラーとせず、全体障害として1件目で中断する。"""
        reports = mock.Mock()
        http_error = _google_http_error(400, "invalidFilters")
        reports.query.return_value.execute.side_effect = http_error
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaises(HttpError):
                youtube.video_search_terms(
                    ["a", "b"],
                    start_date="2026-07-01",
                    end_date="2026-07-26",
                )
        self.assertEqual(reports.query.call_count, 1)

    def test_video_search_terms_raises_when_all_videos_fail_video_specific(self) -> None:
        """issue #164: 全動画が動画固有エラー（HTTP 400 privacy等）で失敗した
        場合は、部分取得成功とせず例外を再送出する。"""
        reports = mock.Mock()
        http_error = _google_http_error(400, "privacy")
        reports.query.return_value.execute.side_effect = http_error
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaises(HttpError):
                youtube.video_search_terms(
                    ["a", "b"],
                    start_date="2026-07-01",
                    end_date="2026-07-26",
                )
        # 全動画が失敗するまでは照会する（部分成功と区別するため）。
        self.assertEqual(reports.query.call_count, 2)

    def test_video_search_terms_empty_input_returns_tuple(self) -> None:
        """issue #164 (Sol review指摘): 空入力でも通常時と同じタプルを返す。"""
        by_video, failed = youtube.video_search_terms(
            [],
            start_date="2026-07-01",
            end_date="2026-07-26",
        )
        self.assertEqual(by_video, {})
        self.assertEqual(failed, {})

    def test_video_retention_curves_maps_and_sorts_points(self) -> None:
        """issue #149: 維持率カーブを elapsedVideoTimeRatio で取得し、
        経過比率順にソートして返す。動画ごとに単一クエリを発行する。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "elapsedVideoTimeRatio"},
                {"name": "audienceWatchRatio"},
            ],
            "rows": [
                ["0.9", 0.30],
                ["0.1", 0.95],
                ["0.5", 0.60],
            ],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            curves, failed = youtube.video_retention_curves(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(
            curves["abc123"],
            [
                {"elapsed_ratio": 0.1, "watch_ratio": 0.95},
                {"elapsed_ratio": 0.5, "watch_ratio": 0.60},
                {"elapsed_ratio": 0.9, "watch_ratio": 0.30},
            ],
        )
        self.assertEqual(failed, {})
        self.assertEqual(
            reports.query.call_args.kwargs["dimensions"],
            "elapsedVideoTimeRatio",
        )
        self.assertEqual(reports.query.call_args.kwargs["filters"], "video==abc123")
        self.assertIn("audienceWatchRatio", reports.query.call_args.kwargs["metrics"])

    def test_video_retention_curves_issues_one_query_per_video(self) -> None:
        """issue #149: 複数IDではID数分の単一動画クエリになり、filterに
        カンマが入らない。動画固有reasonのHTTP 400は他動画の成功を妨げない。"""
        reports = mock.Mock()
        http_error = _google_http_error(400, "privacy")
        ok_page = {
            "columnHeaders": [
                {"name": "elapsedVideoTimeRatio"},
                {"name": "audienceWatchRatio"},
            ],
            "rows": [["0.5", 0.60]],
        }
        reports.query.return_value.execute.side_effect = [http_error, ok_page]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            curves, failed = youtube.video_retention_curves(
                ["bad-id", "ok-id"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(curves, {"ok-id": [{"elapsed_ratio": 0.5, "watch_ratio": 0.60}]})
        self.assertIn("bad-id", failed)
        self.assertEqual(reports.query.call_count, 2)
        for call in reports.query.call_args_list:
            self.assertNotIn(",", call.kwargs["filters"])
            self.assertEqual(call.kwargs["dimensions"], "elapsedVideoTimeRatio")

    def test_video_retention_curves_invalid_filters_is_global_error(self) -> None:
        """issue #149 (Sol review指摘): invalidFilters等のリクエスト構造不備
        （HTTP 400）は動画固有とせず、全体障害として1件目で即時raiseする。"""
        reports = mock.Mock()
        http_error = _google_http_error(400, "invalidFilters")
        reports.query.return_value.execute.side_effect = http_error
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaises(HttpError):
                youtube.video_retention_curves(
                    ["a", "b"],
                    start_date="2026-07-01",
                    end_date="2026-07-26",
                )
        self.assertEqual(reports.query.call_count, 1)

    def test_video_retention_curves_empty_input_returns_tuple(self) -> None:
        """issue #149: 空入力でも通常時と同じタプルを返す。"""
        curves, failed = youtube.video_retention_curves(
            [],
            start_date="2026-07-01",
            end_date="2026-07-26",
        )
        self.assertEqual(curves, {})
        self.assertEqual(failed, {})

    def test_video_retention_curves_drops_out_of_range_and_empty_rows(self) -> None:
        """issue #149: 経過比率が0〜1の範囲外・欠落行は除外する（fail-closed）。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "elapsedVideoTimeRatio"},
                {"name": "audienceWatchRatio"},
            ],
            "rows": [
                ["1.5", 0.50],
                ["", 0.50],
                ["0.2", "bad"],
            ],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            curves, failed = youtube.video_retention_curves(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
            )

        self.assertEqual(curves, {})
        self.assertEqual(failed, {})


if __name__ == "__main__":
    unittest.main()
