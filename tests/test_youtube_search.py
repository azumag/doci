from __future__ import annotations

import json
import unittest
from googleapiclient.errors import HttpError
from types import SimpleNamespace
import tempfile
from pathlib import Path
from unittest import mock

from doci import performance, youtube


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


def _analytics_headers(*names: str) -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "columnType": (
                "DIMENSION"
                if name in {"day", "video", "insightTrafficSourceType"}
                else "METRIC"
            ),
            "dataType": (
                "STRING"
                if name in {"day", "video", "insightTrafficSourceType"}
                else "INTEGER"
            ),
        }
        for name in names
    ]


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

    def test_owned_video_details_readonly_filters_to_authenticated_channel(self) -> None:
        channels = mock.Mock()
        channels.list.return_value.execute.return_value = {
            "items": [{"id": "owned-channel"}]
        }
        videos = mock.Mock()
        videos.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "owned123456",
                    "snippet": {
                        "channelId": "owned-channel",
                        "title": "アプリ投稿Short",
                        "publishedAt": "2026-08-10T18:00:00Z",
                    },
                    "contentDetails": {"duration": "PT58S"},
                    "statistics": {"viewCount": "100", "commentCount": "3"},
                    "status": {"privacyStatus": "public"},
                },
                {
                    "id": "foreign12345",
                    "snippet": {
                        "channelId": "foreign-channel",
                        "title": "他チャンネル",
                        "publishedAt": "2026-08-10T18:00:00Z",
                    },
                    "contentDetails": {"duration": "PT30S"},
                    "statistics": {},
                    "status": {"privacyStatus": "public"},
                },
            ]
        }
        service = mock.Mock()
        service.channels.return_value = channels
        service.videos.return_value = videos
        with (
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            result = youtube.owned_video_details_readonly(
                ["owned123456", "foreign12345"]
            )

        self.assertEqual(result["channel_id"], "owned-channel")
        self.assertEqual(
            [item["video_id"] for item in result["videos"]], ["owned123456"]
        )
        self.assertEqual(result["videos"][0]["duration"], "PT58S")
        self.assertEqual(result["videos"][0]["comments"], 3)
        self.assertEqual(
            credentials.call_args.kwargs["scopes"],
            youtube.ANALYTICS_READONLY_SCOPES,
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])
        self.assertEqual(channels.list.call_args.kwargs, {"part": "id", "mine": True})

    def test_owned_video_details_readonly_requires_one_authenticated_channel(self) -> None:
        channels = mock.Mock()
        channels.list.return_value.execute.return_value = {"items": []}
        service = mock.Mock()
        service.channels.return_value = channels
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                youtube.owned_video_details_readonly(["owned123456"])

    def test_comment_reply_short_metrics_reads_availability_and_video_windows(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {
                "columnHeaders": _analytics_headers(
                    "day",
                    "views",
                    "comments",
                    "subscribersGained",
                    "subscribersLost",
                ),
                "rows": [
                    ["2026-08-16", 100, 2, 1, 0],
                    ["2026-08-17", 120, 3, 2, 1],
                ],
            },
            {
                "columnHeaders": _analytics_headers(
                    "video",
                    "views",
                    "comments",
                    "subscribersGained",
                    "subscribersLost",
                ),
                "rows": [["reply12345", 1000, 20, 8, 2]],
            },
            {
                "columnHeaders": _analytics_headers(
                    "video",
                    "views",
                    "comments",
                    "subscribersGained",
                    "subscribersLost",
                ),
                "rows": [["base123456", 800, 8, 4, 2]],
            },
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        windows = [
            {
                "video_id": "reply12345",
                "start_date": "2026-08-11",
                "end_date": "2026-08-17",
            },
            {
                "video_id": "base123456",
                "start_date": "2026-08-08",
                "end_date": "2026-08-14",
            },
        ]
        with (
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            result = youtube.comment_reply_short_metrics(
                windows,
                availability_start_date="2026-08-11",
                availability_end_date="2026-08-18",
            )

        self.assertEqual(result["data_through_date"], "2026-08-17")
        self.assertEqual(result["videos"][0]["comments"], 20)
        self.assertEqual(result["videos"][0]["net_subscribers"], 6)
        self.assertEqual(result["videos"][1]["net_subscribers"], 2)
        calls = reports.query.call_args_list
        self.assertEqual(calls[0].kwargs["dimensions"], "day")
        self.assertNotIn("filters", calls[0].kwargs)
        self.assertEqual(calls[1].kwargs["filters"], "video==reply12345")
        self.assertEqual(calls[1].kwargs["sort"], "-views")
        self.assertEqual(calls[2].kwargs["startDate"], "2026-08-08")
        self.assertEqual(calls[0].kwargs["startIndex"], 1)
        self.assertEqual(
            calls[0].kwargs["metrics"],
            "views,comments,subscribersGained,subscribersLost",
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])

    def test_comment_reply_short_metrics_keeps_absent_columns_as_none(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {"columnHeaders": _analytics_headers("day"), "rows": []},
            {
                "columnHeaders": _analytics_headers("video", "views"),
                "rows": [["reply12345", 50]],
            },
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            result = youtube.comment_reply_short_metrics(
                [
                    {
                        "video_id": "reply12345",
                        "start_date": "2026-08-11",
                        "end_date": "2026-08-17",
                    }
                ],
                availability_start_date="2026-08-11",
                availability_end_date="2026-08-18",
            )

        self.assertIsNone(result["data_through_date"])
        self.assertIsNone(result["videos"][0]["comments"])
        self.assertIsNone(result["videos"][0]["subscribers_gained"])
        self.assertIsNone(result["videos"][0]["net_subscribers"])

    def test_comment_reply_short_metrics_rejects_wrong_video_provenance(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {
                "columnHeaders": _analytics_headers("day"),
                "rows": [["2026-08-17"]],
            },
            {
                "columnHeaders": _analytics_headers(
                    "video", "views", "comments"
                ),
                "rows": [["wrongVideo123", 10, 3]],
            },
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaisesRegex(RuntimeError, "provenance"):
                youtube.comment_reply_short_metrics(
                    [
                        {
                            "video_id": "reply12345",
                            "start_date": "2026-08-11",
                            "end_date": "2026-08-17",
                        }
                    ],
                    availability_start_date="2026-08-11",
                    availability_end_date="2026-08-18",
                )

    def test_comment_reply_short_metrics_rejects_invalid_count_values(self) -> None:
        for value in (10.9, True, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                reports = mock.Mock()
                reports.query.return_value.execute.side_effect = [
                    {
                        "columnHeaders": _analytics_headers("day"),
                        "rows": [["2026-08-17"]],
                    },
                    {
                        "columnHeaders": _analytics_headers("video", "views"),
                        "rows": [["reply12345", value]],
                    },
                ]
                service = mock.Mock()
                service.reports.return_value = reports
                with (
                    mock.patch.object(
                        youtube, "_load_credentials", return_value=object()
                    ),
                    mock.patch(
                        "googleapiclient.discovery.build", return_value=service
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "non-negative integer"
                    ):
                        youtube.comment_reply_short_metrics(
                            [
                                {
                                    "video_id": "reply12345",
                                    "start_date": "2026-08-11",
                                    "end_date": "2026-08-17",
                                }
                            ],
                            availability_start_date="2026-08-11",
                            availability_end_date="2026-08-18",
                        )

    def test_comment_reply_short_metrics_rejects_invalid_header_type(self) -> None:
        headers = _analytics_headers("day", "views")
        headers[1]["dataType"] = "FLOAT"
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": headers,
            "rows": [["2026-08-17", 10]],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaisesRegex(RuntimeError, "column type"):
                youtube.comment_reply_short_metrics(
                    [
                        {
                            "video_id": "reply12345",
                            "start_date": "2026-08-11",
                            "end_date": "2026-08-17",
                        }
                    ],
                    availability_start_date="2026-08-11",
                    availability_end_date="2026-08-18",
                )

    def test_comment_reply_short_metrics_paginates_availability_days(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {
                "columnHeaders": _analytics_headers("day"),
                "rows": [["2026-08-16"]] * 200,
            },
            {
                "columnHeaders": _analytics_headers("day"),
                "rows": [["2026-08-17"]],
            },
            {
                "columnHeaders": _analytics_headers(
                    "video",
                    "views",
                    "comments",
                    "subscribersGained",
                    "subscribersLost",
                ),
                "rows": [["reply12345", 50, 1, 1, 0]],
            },
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            result = youtube.comment_reply_short_metrics(
                [
                    {
                        "video_id": "reply12345",
                        "start_date": "2026-08-11",
                        "end_date": "2026-08-17",
                    }
                ],
                availability_start_date="2026-08-11",
                availability_end_date="2026-08-18",
            )

        self.assertEqual(result["data_through_date"], "2026-08-17")
        self.assertEqual(reports.query.call_args_list[0].kwargs["startIndex"], 1)
        self.assertEqual(reports.query.call_args_list[1].kwargs["startIndex"], 201)
        self.assertEqual(reports.query.call_args_list[2].kwargs["dimensions"], "video")

    def test_channel_page_share_metrics_uses_complete_fixed_windows(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {
                "columnHeaders": _analytics_headers(
                    "day", "insightTrafficSourceType", "views"
                ),
                "rows": [["2026-07-31", "YT_CHANNEL", 20]],
            },
            {
                "columnHeaders": _analytics_headers("video", "views"),
                "rows": [["complete123", 100]],
            },
            {
                "columnHeaders": _analytics_headers(
                    "video", "insightTrafficSourceType", "views"
                ),
                "rows": [
                    ["complete123", "YT_CHANNEL", 10],
                    ["complete123", "YT_SEARCH", 40],
                ],
            },
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            result = youtube.video_channel_page_share_metrics(
                [
                    {
                        "video_id": "complete123",
                        "start_date": "2026-07-20",
                        "end_date": "2026-07-26",
                    },
                    {
                        "video_id": "incomplete456",
                        "start_date": "2026-07-29",
                        "end_date": "2026-08-04",
                    },
                ],
                availability_start_date="2026-07-01",
                availability_end_date="2026-08-01",
            )

        self.assertEqual(result["data_through_date"], "2026-07-31")
        self.assertEqual(
            result["videos"][0],
            {
                "video_id": "complete123",
                "start_date": "2026-07-20",
                "end_date": "2026-07-26",
                "window_days": 7,
                "data_through_date": "2026-07-31",
                "status": "available",
                "views": 100,
                "channel_page_views": 10,
            },
        )
        self.assertEqual(result["videos"][1]["status"], "window_incomplete")
        self.assertIsNone(result["videos"][1]["views"])
        self.assertEqual(len(reports.query.call_args_list), 3)
        self.assertEqual(
            reports.query.call_args_list[0].kwargs["dimensions"],
            "day,insightTrafficSourceType",
        )
        self.assertEqual(
            reports.query.call_args_list[0].kwargs["sort"],
            "day,insightTrafficSourceType",
        )
        self.assertEqual(
            reports.query.call_args_list[1].kwargs["dimensions"], "video"
        )
        self.assertEqual(
            reports.query.call_args_list[2].kwargs["dimensions"],
            "video,insightTrafficSourceType",
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])

    def test_channel_page_share_metrics_isolates_wrong_video_provenance(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {
                "columnHeaders": _analytics_headers(
                    "day", "insightTrafficSourceType", "views"
                ),
                "rows": [["2026-07-31", "YT_CHANNEL", 20]],
            },
            {
                "columnHeaders": _analytics_headers("video", "views"),
                "rows": [["wrongVideo", 100]],
            },
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            result = youtube.video_channel_page_share_metrics(
                [
                    {
                        "video_id": "requested123",
                        "start_date": "2026-07-20",
                        "end_date": "2026-07-26",
                    }
                ],
                availability_start_date="2026-07-01",
                availability_end_date="2026-08-01",
            )

        self.assertEqual(result["videos"][0]["status"], "invalid_response")
        self.assertIn("provenance", result["videos"][0]["reason"])

    def test_channel_page_share_metrics_keeps_good_video_on_specific_400(
        self,
    ) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {
                "columnHeaders": _analytics_headers(
                    "day", "insightTrafficSourceType", "views"
                ),
                "rows": [["2026-07-31", "YT_CHANNEL", 20]],
            },
            {
                "columnHeaders": _analytics_headers("video", "views"),
                "rows": [["good123", 100]],
            },
            {
                "columnHeaders": _analytics_headers(
                    "video", "insightTrafficSourceType", "views"
                ),
                "rows": [["good123", "YT_CHANNEL", 10]],
            },
            _google_http_error(400, "videoNotFound"),
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            result = youtube.video_channel_page_share_metrics(
                [
                    {
                        "video_id": video_id,
                        "start_date": "2026-07-20",
                        "end_date": "2026-07-26",
                    }
                    for video_id in ("good123", "missing456")
                ],
                availability_start_date="2026-07-01",
                availability_end_date="2026-08-01",
            )

        self.assertEqual(result["videos"][0]["status"], "available")
        self.assertEqual(result["videos"][1]["status"], "query_failed")
        self.assertIn("videonotfound", result["videos"][1]["reason"])

    def test_channel_page_share_metrics_reraises_global_invalid_filters(
        self,
    ) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [
            {
                "columnHeaders": _analytics_headers(
                    "day", "insightTrafficSourceType", "views"
                ),
                "rows": [["2026-07-31", "YT_CHANNEL", 20]],
            },
            _google_http_error(400, "invalidFilters"),
        ]
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaises(HttpError):
                youtube.video_channel_page_share_metrics(
                    [
                        {
                            "video_id": "requested123",
                            "start_date": "2026-07-20",
                            "end_date": "2026-07-26",
                        }
                    ],
                    availability_start_date="2026-07-01",
                    availability_end_date="2026-08-01",
                )

    def test_channel_page_share_metrics_rejects_fractional_views(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": _analytics_headers(
                "day", "insightTrafficSourceType", "views"
            ),
            "rows": [["2026-07-31", "YT_CHANNEL", 10.5]],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(youtube, "_load_credentials", return_value=object()),
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            with self.assertRaisesRegex(RuntimeError, "non-negative integer"):
                youtube.video_channel_page_share_metrics(
                    [
                        {
                            "video_id": "requested123",
                            "start_date": "2026-07-20",
                            "end_date": "2026-07-26",
                        }
                    ],
                    availability_start_date="2026-07-01",
                    availability_end_date="2026-08-01",
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
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
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
        self.assertEqual(
            credentials.call_args.kwargs["scopes"],
            youtube.ANALYTICS_READONLY_SCOPES,
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])

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
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
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
        self.assertEqual(
            credentials.call_args.kwargs["scopes"],
            youtube.ANALYTICS_READONLY_SCOPES,
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])

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
        両方の結果が結合される。第2バッチの対象IDも実IDであることを検証する。"""
        ids = [f"id-{index:03d}" for index in range(201)]
        reports = mock.Mock()

        def _query(**kwargs):
            filters = str(kwargs["filters"])
            requested = [
                part
                for part in filters.replace("video==", "").split(",")
                if part
            ]
            rows = [
                [video_id, 100, 1]
                for video_id in requested
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
        # 第2バッチの照会対象は末尾の id-200 だけ。
        self.assertEqual(
            reports.query.call_args_list[1].kwargs["filters"],
            "video==id-200",
        )
        # 結果ID集合は入力と一致する。
        self.assertEqual(
            {row["video_id"] for row in results},
            set(ids),
        )

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
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
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
        self.assertEqual(
            credentials.call_args.kwargs["scopes"],
            youtube.ANALYTICS_READONLY_SCOPES,
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])

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
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
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
        self.assertEqual(
            credentials.call_args.kwargs["scopes"],
            youtube.ANALYTICS_READONLY_SCOPES,
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])

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

    def test_video_retention_curves_filters_subscribed_status_and_reads_impressions(
        self,
    ) -> None:
        """issue #128: 購読状態filterとsegment observationsを同じ点へ保存する。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "elapsedVideoTimeRatio"},
                {"name": "audienceWatchRatio"},
                {"name": "totalSegmentImpressions"},
            ],
            "rows": [
                [0.2, 0.8, 35],
                [0.4, 0.7, 20],
                [0.6, 0.6, 21],
            ],
        }
        service = mock.Mock()
        service.reports.return_value = reports
        with (
            mock.patch.object(
                youtube, "_load_credentials", return_value=object()
            ) as credentials,
            mock.patch("googleapiclient.discovery.build", return_value=service),
        ):
            curves, failed = youtube.video_retention_curves(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
                subscribed_status="SUBSCRIBED",
                include_segment_impressions=True,
            )

        self.assertEqual(
            curves["abc123"],
            [
                {
                    "elapsed_ratio": 0.2,
                    "watch_ratio": 0.8,
                    "segment_impressions": 35,
                },
                {
                    "elapsed_ratio": 0.4,
                    "watch_ratio": 0.7,
                    "segment_impressions": 20,
                },
                {
                    "elapsed_ratio": 0.6,
                    "watch_ratio": 0.6,
                    "segment_impressions": 21,
                },
            ],
        )
        self.assertEqual(failed, {})
        query = reports.query.call_args.kwargs
        self.assertEqual(
            query["filters"],
            "video==abc123;subscribedStatus==SUBSCRIBED",
        )
        self.assertEqual(
            query["metrics"],
            "audienceWatchRatio,totalSegmentImpressions",
        )
        self.assertEqual(
            credentials.call_args.kwargs["scopes"],
            youtube.ANALYTICS_READONLY_SCOPES,
        )
        self.assertTrue(credentials.call_args.kwargs["exact_scopes"])

    def test_video_retention_curves_rejects_unknown_subscribed_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "subscribed_status"):
            youtube.video_retention_curves(
                ["abc123"],
                start_date="2026-07-01",
                end_date="2026-07-26",
                subscribed_status="RETURNING",
            )

    def test_video_retention_curves_by_subscribed_status_keeps_segments_separate(
        self,
    ) -> None:
        def curves(_ids, **kwargs):
            status = kwargs["subscribed_status"]
            value = 0.8 if status == "SUBSCRIBED" else 0.6
            return (
                {
                    "abc123": [
                        {
                            "elapsed_ratio": 0.5,
                            "watch_ratio": value,
                            "segment_impressions": 30,
                        }
                    ]
                },
                {},
            )

        with mock.patch.object(
            youtube,
            "video_retention_curves",
            side_effect=curves,
        ) as retention:
            by_video, failed = (
                youtube.video_retention_curves_by_subscribed_status(
                    ["abc123"],
                    start_date="2026-07-01",
                    end_date="2026-07-26",
                )
            )

        self.assertEqual(set(by_video["abc123"]), {"SUBSCRIBED", "UNSUBSCRIBED"})
        self.assertEqual(
            by_video["abc123"]["SUBSCRIBED"][0]["watch_ratio"], 0.8
        )
        self.assertEqual(
            by_video["abc123"]["UNSUBSCRIBED"][0]["watch_ratio"], 0.6
        )
        self.assertEqual(failed, {})
        self.assertEqual(retention.call_count, 2)
        for call in retention.call_args_list:
            self.assertTrue(call.kwargs["include_segment_impressions"])

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

    def test_video_retention_curves_rejects_whole_curve_with_invalid_row(self) -> None:
        """issue #125/#149: 不正点だけを捨てて部分曲線を返さない。"""
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "elapsedVideoTimeRatio"},
                {"name": "audienceWatchRatio"},
            ],
            "rows": [
                [0.25, 0.90],
                [0.30, 0.88],
                [0.35, 0.86],
                [0.40, "bad"],
                [0.45, 0.84],
                [0.50, 0.82],
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
        self.assertEqual(failed, {"abc123": "invalid retention response row"})

    def test_video_retention_curves_rejects_each_malformed_curve_shape(self) -> None:
        """issue #125: 欠落、非有限、負値、範囲外、異値重複を動画単位で拒否する。"""
        valid_headers = [
            {"name": "elapsedVideoTimeRatio"},
            {"name": "audienceWatchRatio"},
        ]
        cases = {
            "missing_column": (
                [{"name": "elapsedVideoTimeRatio"}],
                [[0.25], [0.50]],
            ),
            "missing_row_value": (valid_headers, [[0.25, 0.90], [0.40]]),
            "nan_watch_ratio": (valid_headers, [[0.25, 0.90], [0.40, "NaN"]]),
            "negative_watch_ratio": (valid_headers, [[0.25, 0.90], [0.40, -0.1]]),
            "out_of_range_elapsed": (valid_headers, [[0.25, 0.90], [1.1, 0.80]]),
            "conflicting_duplicate": (
                valid_headers,
                [[0.25, 0.90], [0.40, 0.85], [0.40, 0.80], [0.50, 0.82]],
            ),
            "non_string_extra_header": (
                [*valid_headers, {"name": 123}],
                [[0.25, 0.90, "unused"], [0.50, 0.82, "unused"]],
            ),
            "missing_extra_header_name": (
                [*valid_headers, {}],
                [[0.25, 0.90, "unused"], [0.50, 0.82, "unused"]],
            ),
        }
        for name, (headers, rows) in cases.items():
            with self.subTest(name=name):
                reports = mock.Mock()
                reports.query.return_value.execute.return_value = {
                    "columnHeaders": headers,
                    "rows": rows,
                }
                service = mock.Mock()
                service.reports.return_value = reports
                with (
                    mock.patch.object(
                        youtube, "_load_credentials", return_value=object()
                    ),
                    mock.patch(
                        "googleapiclient.discovery.build", return_value=service
                    ),
                ):
                    curves, failed = youtube.video_retention_curves(
                        ["abc123"],
                        start_date="2026-07-01",
                        end_date="2026-07-26",
                    )

                self.assertEqual(curves, {})
                self.assertIn("abc123", failed)

    def test_video_retention_curves_deduplicates_identical_points(self) -> None:
        reports = mock.Mock()
        reports.query.return_value.execute.return_value = {
            "columnHeaders": [
                {"name": "elapsedVideoTimeRatio"},
                {"name": "audienceWatchRatio"},
            ],
            "rows": [[0.25, 0.90], [0.25, 0.90], [0.50, 0.82]],
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
            curves,
            {
                "abc123": [
                    {"elapsed_ratio": 0.25, "watch_ratio": 0.90},
                    {"elapsed_ratio": 0.50, "watch_ratio": 0.82},
                ]
            },
        )
        self.assertEqual(failed, {})

    def test_video_retention_curves_separates_invalid_video_from_dense_boundary(
        self,
    ) -> None:
        """issue #125: 不正動画を拒否しても、別動画の8pp境界は保持する。"""
        headers = [
            {"name": "elapsedVideoTimeRatio"},
            {"name": "audienceWatchRatio"},
        ]
        invalid_page = {
            "columnHeaders": headers,
            "rows": [[0.25, 0.90], [0.40, "bad"], [0.50, 0.82]],
        }
        valid_page = {
            "columnHeaders": headers,
            "rows": [
                [0.25, 0.90],
                [0.30, 0.88],
                [0.35, 0.86],
                [0.40, 0.85],
                [0.45, 0.84],
                [0.50, 0.82],
            ],
        }
        reports = mock.Mock()
        reports.query.return_value.execute.side_effect = [invalid_page, valid_page]
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

        self.assertNotIn("bad-id", curves)
        self.assertIn("bad-id", failed)
        self.assertEqual(len(curves["ok-id"]), 6)
        signal = performance.opening_to_midpoint_retention_signal(
            curves["ok-id"],
            "PT2M",
        )
        self.assertIsNotNone(signal)
        self.assertTrue(signal["actionable"])
        self.assertAlmostEqual(signal["decline_ratio"], 0.08)


if __name__ == "__main__":
    unittest.main()
