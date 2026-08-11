from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from doci import config, instagram, publish, routing, tiktok, youtube
from doci.channel import (
    InstagramPublishSpec,
    PublishSpec,
    TikTokPublishSpec,
    YouTubePublishSpec,
)


class PublishAccountsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.route = routing.Route(
            tier="longform",
            is_youtube_short=False,
            platforms=["youtube"],
            hashtag="",
            landscape=True,
        )

    def _youtube_spec(self, name: str, *, platforms=("youtube",)) -> PublishSpec:
        token = self.root / name / "youtube_token.json"
        token.parent.mkdir(parents=True)
        token.write_text("{}", encoding="utf-8")
        return PublishSpec(
            platforms=platforms,
            youtube=YouTubePublishSpec(
                privacy="unlisted",
                client_secret=self.root / name / "client_secret.json",
                token=token,
            ),
        )

    def test_youtube_token_scope_check_uses_stored_grants(self) -> None:
        token = self.root / "youtube_token.json"
        token.write_text(json.dumps({"scopes": youtube.SCOPES}), encoding="utf-8")

        self.assertTrue(youtube._token_has_scopes(token, youtube.SCOPES))
        self.assertFalse(
            youtube._token_has_scopes(token, youtube.ACCOUNT_SCOPES)
        )
        self.assertFalse(
            youtube._token_has_scopes(token, youtube.MANAGE_SCOPES)
        )

    def test_enabled_requires_channel_platform_token_and_global_switch(self) -> None:
        spec = self._youtube_spec("alpha")
        with patch.object(config, "PUBLISH_YOUTUBE", True):
            self.assertEqual(publish._enabled("youtube", spec), (True, ""))

        with patch.object(config, "PUBLISH_YOUTUBE", False):
            enabled, why = publish._enabled("youtube", spec)
        self.assertFalse(enabled)
        self.assertIn("PUBLISH_YOUTUBE=0", why)

        excluded = self._youtube_spec("beta", platforms=())
        with patch.object(config, "PUBLISH_YOUTUBE", True):
            enabled, why = publish._enabled("youtube", excluded)
        self.assertFalse(enabled)
        self.assertIn("publish.platforms", why)

        spec.youtube.token.unlink()
        with patch.object(config, "PUBLISH_YOUTUBE", True):
            enabled, why = publish._enabled("youtube", spec)
        self.assertFalse(enabled)
        self.assertIn(str(spec.youtube.token), why)

    def test_tiktok_and_instagram_require_channel_credentials(self) -> None:
        tik_token = self.root / "tiktok.json"
        tik_token.write_text("{}", encoding="utf-8")
        spec = PublishSpec(
            platforms=("tiktok", "instagram"),
            tiktok=TikTokPublishSpec(token=tik_token, privacy="SELF_ONLY"),
            instagram=InstagramPublishSpec(
                user_id="ig-user", access_token_env="IG_TOKEN_TEST"
            ),
        )
        with (
            patch.object(config, "PUBLISH_TIKTOK", True),
            patch.object(config, "TIKTOK_CLIENT_KEY", "key"),
            patch.object(config, "TIKTOK_CLIENT_SECRET", "secret"),
        ):
            self.assertEqual(publish._enabled("tiktok", spec), (True, ""))

        with (
            patch.object(config, "PUBLISH_INSTAGRAM", True),
            patch.dict(os.environ, {"IG_TOKEN_TEST": "token"}),
        ):
            self.assertEqual(publish._enabled("instagram", spec), (True, ""))

        with patch.object(config, "PUBLISH_INSTAGRAM", True), patch.dict(
            os.environ, {}, clear=True
        ):
            enabled, why = publish._enabled("instagram", spec)
        self.assertFalse(enabled)
        self.assertIn("IG_TOKEN_TEST", why)

    def test_dry_run_identifies_each_channel_token_path(self) -> None:
        alpha = SimpleNamespace(publish=self._youtube_spec("alpha"))
        beta = SimpleNamespace(publish=self._youtube_spec("beta"))

        with patch.object(config, "PUBLISH_YOUTUBE", True):
            alpha_result = publish.publish(
                self.root / "video.mp4",
                title="Title",
                description="Description",
                tags=[],
                route=self.route,
                spec=alpha,
                dry_run=True,
            )[0]
            beta_result = publish.publish(
                self.root / "video.mp4",
                title="Title",
                description="Description",
                tags=[],
                route=self.route,
                spec=beta,
                dry_run=True,
            )[0]

        self.assertEqual(alpha_result.status, "dry_run")
        self.assertIn(str(alpha.publish.youtube.token), alpha_result.detail)
        self.assertIn(str(beta.publish.youtube.token), beta_result.detail)
        self.assertNotEqual(alpha_result.detail, beta_result.detail)

    def test_global_dry_run_switch_prevents_upload(self) -> None:
        spec = SimpleNamespace(publish=self._youtube_spec("alpha"))
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(config, "PUBLISH_DRY_RUN", True),
            patch.object(publish, "_do_upload") as upload_mock,
        ):
            result = publish.publish(
                self.root / "video.mp4",
                title="Title",
                description="Description",
                tags=[],
                route=self.route,
                spec=spec,
            )[0]

        self.assertEqual(result.status, "dry_run")
        upload_mock.assert_not_called()

    def test_upload_exception_is_classified_as_unknown(self) -> None:
        spec = SimpleNamespace(publish=self._youtube_spec("alpha"))
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(publish, "_do_upload", side_effect=TimeoutError("timeout")),
        ):
            result = publish.publish(
                self.root / "video.mp4",
                title="Title",
                description="Description",
                tags=[],
                route=self.route,
                spec=spec,
            )[0]

        self.assertEqual(result.status, "unknown")
        self.assertIn("投稿結果不明", result.detail)

    def test_youtube_preflight_error_is_reusable_same_day(self) -> None:
        spec = SimpleNamespace(publish=self._youtube_spec("alpha"))
        with (
            patch.object(config, "PUBLISH_YOUTUBE", True),
            patch.object(
                youtube,
                "upload",
                side_effect=youtube.UploadPreflightError("scope不足"),
            ),
        ):
            result = publish.publish(
                self.root / "video.mp4",
                title="Title",
                description="Description",
                tags=[],
                route=self.route,
                spec=spec,
            )[0]

        self.assertEqual(result.status, "error")
        self.assertIn("投稿前検証失敗", result.detail)

    def test_tiktok_and_instagram_preflight_errors_are_reusable(self) -> None:
        tik_token = self.root / "tiktok-preflight.json"
        tik_token.write_text("{}", encoding="utf-8")
        spec = SimpleNamespace(
            publish=PublishSpec(
                platforms=("tiktok", "instagram"),
                tiktok=TikTokPublishSpec(token=tik_token, privacy="SELF_ONLY"),
                instagram=InstagramPublishSpec(
                    user_id="ig-user", access_token_env="IG_PREFLIGHT_TOKEN"
                ),
            )
        )
        tiktok_route = routing.Route(
            tier="short",
            is_youtube_short=False,
            platforms=["tiktok"],
            hashtag="",
            landscape=False,
        )
        instagram_route = routing.Route(
            tier="short",
            is_youtube_short=False,
            platforms=["instagram"],
            hashtag="",
            landscape=False,
        )
        with (
            patch.object(config, "PUBLISH_TIKTOK", True),
            patch.object(config, "TIKTOK_CLIENT_KEY", "key"),
            patch.object(config, "TIKTOK_CLIENT_SECRET", "secret"),
            patch.object(
                publish,
                "_do_upload",
                side_effect=tiktok.TikTokUploadPreflightError("token不正"),
            ),
        ):
            tiktok_result = publish.publish(
                self.root / "video.mp4",
                title="Title",
                description="Description",
                tags=[],
                route=tiktok_route,
                spec=spec,
            )[0]

        with (
            patch.object(config, "PUBLISH_INSTAGRAM", True),
            patch.dict(os.environ, {"IG_PREFLIGHT_TOKEN": "token"}),
            patch.object(
                publish,
                "_do_upload",
                side_effect=instagram.InstagramUploadPreflightError("公開ホスト未実装"),
            ),
        ):
            instagram_result = publish.publish(
                self.root / "video.mp4",
                title="Title",
                description="Description",
                tags=[],
                route=instagram_route,
                spec=spec,
            )[0]

        self.assertEqual(tiktok_result.status, "error")
        self.assertIn("投稿前検証失敗", tiktok_result.detail)
        self.assertEqual(instagram_result.status, "error")
        self.assertIn("投稿前検証失敗", instagram_result.detail)

    def test_youtube_upload_and_thumbnail_receive_channel_credentials(self) -> None:
        settings = self._youtube_spec("alpha")
        thumbnail = self.root / "thumbnail.png"
        with (
            patch.object(youtube, "upload", return_value="video-id") as upload_mock,
            patch.object(youtube, "set_thumbnail") as thumbnail_mock,
        ):
            result = publish._do_upload(
                "youtube",
                self.root / "video.mp4",
                "Title",
                "Description",
                ["tag"],
                self.route,
                settings,
                thumbnail,
            )

        self.assertEqual(result.id, "video-id")
        self.assertEqual(upload_mock.call_args.args[4], "unlisted")
        self.assertEqual(
            upload_mock.call_args.kwargs["token_file"], settings.youtube.token
        )
        self.assertEqual(
            upload_mock.call_args.kwargs["client_secret_file"],
            settings.youtube.client_secret,
        )
        self.assertEqual(
            thumbnail_mock.call_args.kwargs["token_file"], settings.youtube.token
        )

    def test_youtube_privacy_override_takes_precedence_for_one_upload(self) -> None:
        settings = self._youtube_spec("alpha")
        with patch.object(
            youtube,
            "upload",
            return_value="video-id",
        ) as upload_mock:
            publish._do_upload(
                "youtube",
                self.root / "video.mp4",
                "Title",
                "Description",
                [],
                self.route,
                settings,
                youtube_privacy="public",
            )

        self.assertEqual(settings.youtube.privacy, "unlisted")
        self.assertEqual(upload_mock.call_args.args[4], "public")

    def test_set_privacy_preserves_existing_youtube_status_fields(self) -> None:
        service = MagicMock()
        videos = service.videos.return_value
        videos.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {
                        "privacyStatus": "unlisted",
                        "license": "youtube",
                        "selfDeclaredMadeForKids": False,
                        "uploadStatus": "processed",
                    }
                }
            ]
        }
        settings = self._youtube_spec("alpha")
        with (
            patch.object(
                youtube,
                "_load_credentials",
                return_value=object(),
            ) as credentials_mock,
            patch.object(
                youtube,
                "_build_service",
                return_value=service,
            ),
        ):
            youtube.set_privacy(
                "video123",
                "public",
                expected_privacy="unlisted",
                token_file=settings.youtube.token,
                client_secret_file=settings.youtube.client_secret,
            )

        body = videos.update.call_args.kwargs["body"]
        self.assertEqual(body["status"]["privacyStatus"], "public")
        self.assertEqual(body["status"]["license"], "youtube")
        self.assertNotIn("uploadStatus", body["status"])
        self.assertEqual(
            credentials_mock.call_args.kwargs["scopes"],
            youtube.MANAGE_SCOPES,
        )

    def test_set_privacy_refuses_an_unexpected_private_video(self) -> None:
        service = MagicMock()
        videos = service.videos.return_value
        videos.list.return_value.execute.return_value = {
            "items": [{"status": {"privacyStatus": "private"}}]
        }
        settings = self._youtube_spec("alpha")
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            with self.assertRaisesRegex(RuntimeError, "actual=private"):
                youtube.set_privacy(
                    "video123",
                    "public",
                    expected_privacy="unlisted",
                    token_file=settings.youtube.token,
                    client_secret_file=settings.youtube.client_secret,
                )

        videos.update.assert_not_called()

    def test_privacy_status_reads_without_updating(self) -> None:
        service = MagicMock()
        videos = service.videos.return_value
        videos.list.return_value.execute.return_value = {
            "items": [{"status": {"privacyStatus": "unlisted"}}]
        }
        settings = self._youtube_spec("alpha")
        with (
            patch.object(youtube, "_load_credentials", return_value=object()),
            patch.object(youtube, "_build_service", return_value=service),
        ):
            current = youtube.privacy_status(
                "video123",
                token_file=settings.youtube.token,
                client_secret_file=settings.youtube.client_secret,
            )

        self.assertEqual(current, "unlisted")
        videos.update.assert_not_called()

    def test_tiktok_token_helpers_use_requested_path(self) -> None:
        path = self.root / "nested" / "tiktok_token.json"
        token = {"access_token": "channel-token", "expires_at": 99999999999}

        tiktok._save_token(token, path)

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), token)
        self.assertEqual(tiktok._load_token(path), token)
        self.assertEqual(tiktok._access_token(path), "channel-token")

    def test_tiktok_and_instagram_uploads_receive_channel_settings(self) -> None:
        tik_token = self.root / "tiktok_token.json"
        settings = PublishSpec(
            platforms=("tiktok", "instagram"),
            tiktok=TikTokPublishSpec(token=tik_token, privacy="SELF_ONLY"),
            instagram=InstagramPublishSpec(
                user_id="ig-channel", access_token_env="IG_CHANNEL_TOKEN"
            ),
        )
        short_route = routing.Route(
            tier="short",
            is_youtube_short=True,
            platforms=["tiktok", "instagram"],
            hashtag="#Shorts",
            landscape=False,
        )
        with patch.object(
            tiktok,
            "upload",
            return_value={"publish_id": "tik-id", "status": "PUBLISH_COMPLETE"},
        ) as tik_upload:
            publish._do_upload(
                "tiktok",
                self.root / "video.mp4",
                "Title",
                "Description",
                [],
                short_route,
                settings,
            )
        self.assertEqual(tik_upload.call_args.kwargs["token_file"], tik_token)
        self.assertEqual(tik_upload.call_args.kwargs["privacy"], "SELF_ONLY")

        with (
            patch.dict(os.environ, {"IG_CHANNEL_TOKEN": "secret-value"}),
            patch.object(
                instagram,
                "upload",
                return_value={"id": "ig-id", "permalink": "https://example.test/ig"},
            ) as ig_upload,
        ):
            publish._do_upload(
                "instagram",
                self.root / "video.mp4",
                "Title",
                "Description",
                [],
                short_route,
                settings,
            )
        self.assertEqual(ig_upload.call_args.kwargs["user_id"], "ig-channel")
        self.assertEqual(
            ig_upload.call_args.kwargs["access_token"], "secret-value"
        )

    def test_tiktok_terminal_statuses_are_not_all_success(self) -> None:
        tik_token = self.root / "tiktok-status-token.json"
        settings = PublishSpec(
            platforms=("tiktok",),
            tiktok=TikTokPublishSpec(token=tik_token, privacy="SELF_ONLY"),
        )
        cases = (
            ("PUBLISH_COMPLETE", "ok"),
            ("FAILED", "error"),
            ("PROCESSING", "unknown"),
            ("", "unknown"),
        )
        for api_status, expected in cases:
            with self.subTest(api_status=api_status):
                with patch.object(
                    tiktok,
                    "upload",
                    return_value={"publish_id": "tik-id", "status": api_status},
                ):
                    result = publish._do_upload(
                        "tiktok",
                        self.root / "video.mp4",
                        "Title",
                        "Description",
                        [],
                        self.route,
                        settings,
                    )
                self.assertEqual(result.status, expected)
                self.assertEqual(result.detail, api_status)

    def test_youtube_auth_cli_uses_selected_channel_paths(self) -> None:
        settings = self._youtube_spec("alpha")
        fake_spec = SimpleNamespace(publish=settings)
        with (
            patch("sys.argv", ["doci.youtube", "--auth", "--channel", "alpha"]),
            patch("doci.channel.load", return_value=fake_spec),
            patch.object(youtube, "_load_credentials") as load_mock,
            patch("builtins.print"),
        ):
            youtube.main()

        self.assertTrue(load_mock.call_args.kwargs["interactive"])
        self.assertEqual(
            load_mock.call_args.kwargs["scopes"], youtube.ACCOUNT_SCOPES
        )
        self.assertEqual(
            load_mock.call_args.kwargs["token_file"], settings.youtube.token
        )
        self.assertEqual(
            load_mock.call_args.kwargs["client_secret_file"],
            settings.youtube.client_secret,
        )

    def test_youtube_analytics_readonly_auth_excludes_upload_scope(self) -> None:
        settings = self._youtube_spec("alpha")
        fake_spec = SimpleNamespace(publish=settings)
        with (
            patch(
                "sys.argv",
                [
                    "doci.youtube",
                    "--auth",
                    "--channel",
                    "alpha",
                    "--analytics-readonly",
                ],
            ),
            patch("doci.channel.load", return_value=fake_spec),
            patch.object(youtube, "_load_credentials") as load_mock,
            patch("builtins.print"),
        ):
            youtube.main()

        scopes = load_mock.call_args.kwargs["scopes"]
        self.assertEqual(scopes, youtube.ANALYTICS_READONLY_SCOPES)
        self.assertNotIn(youtube.SCOPES[0], scopes)

    def test_youtube_manage_auth_requests_write_scope(self) -> None:
        settings = self._youtube_spec("alpha")
        fake_spec = SimpleNamespace(publish=settings)
        with (
            patch(
                "sys.argv",
                [
                    "doci.youtube",
                    "--auth",
                    "--channel",
                    "alpha",
                    "--analytics",
                    "--manage",
                ],
            ),
            patch("doci.channel.load", return_value=fake_spec),
            patch.object(youtube, "_load_credentials") as load_mock,
            patch("builtins.print"),
        ):
            youtube.main()

        scopes = load_mock.call_args.kwargs["scopes"]
        self.assertIn(youtube.MANAGE_SCOPE, scopes)
        self.assertEqual(
            youtube.MANAGE_SCOPE,
            "https://www.googleapis.com/auth/youtube.force-ssl",
        )
        self.assertNotIn(
            "https://www.googleapis.com/auth/youtube",
            scopes,
        )
        self.assertIn(
            "https://www.googleapis.com/auth/yt-analytics.readonly",
            scopes,
        )

    def test_youtube_whoami_cli_uses_selected_channel_paths(self) -> None:
        settings = self._youtube_spec("alpha")
        fake_spec = SimpleNamespace(publish=settings)
        with (
            patch("sys.argv", ["doci.youtube", "--whoami", "--channel", "alpha"]),
            patch("doci.channel.load", return_value=fake_spec),
            patch.object(
                youtube,
                "account_info",
                return_value=[{"id": "UC-test", "title": "Alpha Channel"}],
            ) as info_mock,
            patch("builtins.print") as print_mock,
        ):
            youtube.main()

        self.assertEqual(
            info_mock.call_args.kwargs["token_file"], settings.youtube.token
        )
        self.assertEqual(
            info_mock.call_args.kwargs["client_secret_file"],
            settings.youtube.client_secret,
        )
        print_mock.assert_called_with("channel_id=UC-test title=Alpha Channel")


if __name__ == "__main__":
    unittest.main()
