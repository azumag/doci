from __future__ import annotations

import unittest
from types import SimpleNamespace
import tempfile
from pathlib import Path
from unittest import mock

from doci import youtube


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
                {"name": "estimatedMinutesWatched"},
                {"name": "averageViewDuration"},
                {"name": "averageViewPercentage"},
                {"name": "likes"},
                {"name": "comments"},
            ],
            "rows": [["abc123", 80, 120.5, 90.0, 72.4, 5, 2]],
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
        self.assertEqual(reports.query.call_args.kwargs["dimensions"], "video")
        self.assertEqual(reports.query.call_args.kwargs["filters"], "video==abc123")
        self.assertEqual(reports.query.call_args.kwargs["sort"], "-views")
        self.assertEqual(reports.query.call_args.kwargs["maxResults"], 200)


if __name__ == "__main__":
    unittest.main()
