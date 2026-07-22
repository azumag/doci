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
                    "statistics": {"viewCount": "12000", "likeCount": "430"},
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


if __name__ == "__main__":
    unittest.main()
