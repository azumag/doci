"""issue #86: 再生リスト作成・動画追加・チャンネルキーワード・コメント投稿のテスト。

対象: youtube.ensure_playlist / playlist_video_ids / add_video_to_playlist /
set_channel_keywords / post_comment。
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from doci import youtube


class EnsurePlaylistTest(unittest.TestCase):
    def test_returns_existing_playlist_id_when_title_matches(self) -> None:
        service = MagicMock()
        service.playlists.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "PL_existing", "snippet": {"title": "ショート攻略"}}],
            "nextPageToken": None,
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            playlist_id = youtube.ensure_playlist(
                "ショート攻略", token_file=None, client_secret_file=None
            )
        self.assertEqual(playlist_id, "PL_existing")
        service.playlists.return_value.insert.assert_not_called()

    def test_paginates_across_multiple_pages_before_matching(self) -> None:
        service = MagicMock()
        service.playlists.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "PL_a", "snippet": {"title": "他の再生リスト"}}], "nextPageToken": "p2"},
            {"items": [{"id": "PL_b", "snippet": {"title": "分析・改善"}}], "nextPageToken": None},
        ]
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            playlist_id = youtube.ensure_playlist(
                "分析・改善", token_file=None, client_secret_file=None
            )
        self.assertEqual(playlist_id, "PL_b")

    def test_creates_playlist_when_no_title_matches(self) -> None:
        service = MagicMock()
        service.playlists.return_value.list.return_value.execute.return_value = {
            "items": [],
            "nextPageToken": None,
        }
        service.playlists.return_value.insert.return_value.execute.return_value = {
            "id": "PL_new"
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            playlist_id = youtube.ensure_playlist(
                "共産主義ネタ", token_file=None, client_secret_file=None
            )
        self.assertEqual(playlist_id, "PL_new")
        body = service.playlists.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["title"], "共産主義ネタ")
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")

    def test_rejects_empty_title(self) -> None:
        with self.assertRaises(ValueError):
            youtube.ensure_playlist("   ")


class PlaylistVideoIdsTest(unittest.TestCase):
    def test_collects_video_ids_across_pages(self) -> None:
        service = MagicMock()
        service.playlistItems.return_value.list.return_value.execute.side_effect = [
            {
                "items": [{"contentDetails": {"videoId": "v1"}}],
                "nextPageToken": "p2",
            },
            {
                "items": [{"contentDetails": {"videoId": "v2"}}],
                "nextPageToken": None,
            },
        ]
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            ids = youtube.playlist_video_ids(
                "PL1", token_file=None, client_secret_file=None
            )
        self.assertEqual(ids, {"v1", "v2"})


class AddVideoToPlaylistTest(unittest.TestCase):
    def test_skips_insert_when_video_already_present(self) -> None:
        service = MagicMock()
        service.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "item1"}]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            result = youtube.add_video_to_playlist(
                "PL1", "abcdefghijk", token_file=None, client_secret_file=None
            )
        self.assertEqual(result, "already_present")
        service.playlistItems.return_value.insert.assert_not_called()
        list_kwargs = service.playlistItems.return_value.list.call_args.kwargs
        self.assertEqual(list_kwargs["playlistId"], "PL1")
        self.assertEqual(list_kwargs["videoId"], "abcdefghijk")

    def test_inserts_when_video_not_present(self) -> None:
        service = MagicMock()
        service.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": []
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            result = youtube.add_video_to_playlist(
                "PL1", "abcdefghijk", token_file=None, client_secret_file=None
            )
        self.assertEqual(result, "added")
        list_kwargs = service.playlistItems.return_value.list.call_args.kwargs
        self.assertEqual(list_kwargs["playlistId"], "PL1")
        self.assertEqual(list_kwargs["videoId"], "abcdefghijk")
        body = service.playlistItems.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["playlistId"], "PL1")
        self.assertEqual(body["snippet"]["resourceId"]["videoId"], "abcdefghijk")

    def test_rejects_invalid_video_id(self) -> None:
        with self.assertRaises(ValueError):
            youtube.add_video_to_playlist("PL1", "bad id!")


class SetChannelKeywordsTest(unittest.TestCase):
    def test_preserves_existing_branding_and_joins_keywords(self) -> None:
        service = MagicMock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "UC123",
                    "brandingSettings": {"channel": {"title": "既存タイトル"}},
                }
            ]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            result = youtube.set_channel_keywords(
                ["YouTube攻略", "登録者を増やす"],
                token_file=None,
                client_secret_file=None,
            )
        self.assertEqual(result, "updated")
        body = service.channels.return_value.update.call_args.kwargs["body"]
        self.assertEqual(body["id"], "UC123")
        self.assertEqual(
            body["brandingSettings"]["channel"]["keywords"],
            "YouTube攻略 登録者を増やす",
        )
        self.assertEqual(body["brandingSettings"]["channel"]["title"], "既存タイトル")

    def test_preserves_sibling_branding_objects_like_image_and_hints(self) -> None:
        service = MagicMock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "UC123",
                    "brandingSettings": {
                        "channel": {"title": "既存タイトル"},
                        "image": {"bannerExternalUrl": "https://example.com/banner.png"},
                    },
                }
            ]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            youtube.set_channel_keywords(
                ["YouTube攻略"], token_file=None, client_secret_file=None
            )
        body = service.channels.return_value.update.call_args.kwargs["body"]
        self.assertEqual(
            body["brandingSettings"]["image"]["bannerExternalUrl"],
            "https://example.com/banner.png",
        )

    def test_strips_embedded_double_quotes_from_keywords(self) -> None:
        service = MagicMock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC123", "brandingSettings": {"channel": {}}}]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            youtube.set_channel_keywords(
                ['a b"c'], token_file=None, client_secret_file=None
            )
        body = service.channels.return_value.update.call_args.kwargs["body"]
        self.assertEqual(body["brandingSettings"]["channel"]["keywords"], '"a bc"')

    def test_quotes_keywords_containing_spaces(self) -> None:
        service = MagicMock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC123", "brandingSettings": {"channel": {}}}]
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            youtube.set_channel_keywords(
                ["YouTube 攻略"], token_file=None, client_secret_file=None
            )
        body = service.channels.return_value.update.call_args.kwargs["body"]
        self.assertEqual(
            body["brandingSettings"]["channel"]["keywords"], '"YouTube 攻略"'
        )

    def test_rejects_empty_keywords(self) -> None:
        with self.assertRaises(ValueError):
            youtube.set_channel_keywords([])

    def test_rejects_overlong_keywords(self) -> None:
        with self.assertRaises(ValueError):
            youtube.set_channel_keywords(["a" * 600])


class PostCommentTest(unittest.TestCase):
    def test_posts_top_level_comment_and_returns_id(self) -> None:
        service = MagicMock()
        service.commentThreads.return_value.insert.return_value.execute.return_value = {
            "id": "comment123"
        }
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            comment_id = youtube.post_comment(
                "abcdefghijk",
                "議論を誘発するコメント",
                token_file=None,
                client_secret_file=None,
            )
        self.assertEqual(comment_id, "comment123")
        body = service.commentThreads.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["videoId"], "abcdefghijk")
        self.assertEqual(
            body["snippet"]["topLevelComment"]["snippet"]["textOriginal"],
            "議論を誘発するコメント",
        )

    def test_rejects_invalid_video_id(self) -> None:
        with self.assertRaises(ValueError):
            youtube.post_comment("bad id!", "text")

    def test_rejects_empty_text(self) -> None:
        with self.assertRaises(ValueError):
            youtube.post_comment("abcdefghijk", "   ")


class EngagementCliTest(unittest.TestCase):
    def test_ensure_playlist_flag_prints_playlist_id(self) -> None:
        with (
            patch("sys.argv", ["doci.youtube", "--ensure-playlist", "ショート攻略"]),
            patch.object(youtube, "ensure_playlist", return_value="PL1") as ensure_mock,
            patch("builtins.print") as print_mock,
        ):
            youtube.main()
        ensure_mock.assert_called_once_with(
            "ショート攻略", token_file=unittest.mock.ANY, client_secret_file=unittest.mock.ANY
        )
        print_mock.assert_called_with("playlist_id=PL1")

    def test_add_to_playlist_flag_requires_video_id(self) -> None:
        with patch(
            "sys.argv", ["doci.youtube", "--add-to-playlist", "PL1"]
        ):
            with self.assertRaises(SystemExit):
                youtube.main()

    def test_add_to_playlist_flag_calls_library_function(self) -> None:
        with (
            patch(
                "sys.argv",
                ["doci.youtube", "--add-to-playlist", "PL1", "--video-id", "vid123"],
            ),
            patch.object(
                youtube, "add_video_to_playlist", return_value="added"
            ) as add_mock,
            patch("builtins.print"),
        ):
            youtube.main()
        self.assertEqual(add_mock.call_args.args, ("PL1", "vid123"))

    def test_set_channel_keywords_flag_splits_on_comma(self) -> None:
        with (
            patch(
                "sys.argv",
                ["doci.youtube", "--set-channel-keywords", "a, b ,c"],
            ),
            patch.object(
                youtube, "set_channel_keywords", return_value="updated"
            ) as keywords_mock,
            patch("builtins.print"),
        ):
            youtube.main()
        self.assertEqual(keywords_mock.call_args.args[0], ["a", "b", "c"])

    def test_post_comment_flag_requires_comment_text(self) -> None:
        with patch("sys.argv", ["doci.youtube", "--post-comment", "vid123"]):
            with self.assertRaises(SystemExit):
                youtube.main()

    def test_post_comment_flag_calls_library_function(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    "doci.youtube",
                    "--post-comment",
                    "vid123",
                    "--comment-text",
                    "コメント本文",
                ],
            ),
            patch.object(youtube, "post_comment", return_value="c1") as comment_mock,
            patch("builtins.print"),
        ):
            youtube.main()
        self.assertEqual(comment_mock.call_args.args, ("vid123", "コメント本文"))


if __name__ == "__main__":
    unittest.main()
