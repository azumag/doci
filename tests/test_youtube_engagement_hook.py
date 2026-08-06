"""issue #86: アップロード後の再生リスト追加・エンゲージメントコメント投稿フックのテスト。
チャンネル別方式（issue #98）: youtube_engagement_comment_modeの配線も対象。

対象: run_daily._apply_youtube_engagement_actions。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from doci import ai_text, run_daily, youtube
from doci.channel import CornerSpec


def _spec(pipeline: dict) -> SimpleNamespace:
    spec = SimpleNamespace(
        id="youtube-growth",
        pipeline=pipeline,
        publish=SimpleNamespace(
            youtube=SimpleNamespace(token=Path("token.json"), client_secret=Path("secret.json"))
        ),
    )
    spec.pipeline_get = lambda key, default=None: spec.pipeline.get(key, default)
    return spec


def _corner() -> CornerSpec:
    return CornerSpec(
        key="shorts",
        label="ショート攻略",
        persona_path=Path(__file__),
        corner_path=Path(__file__),
        voice_key="narrator",
    )


class ApplyYoutubeEngagementActionsTest(unittest.TestCase):
    def test_both_flags_off_does_nothing(self) -> None:
        spec = _spec({})
        with (
            mock.patch.object(youtube, "ensure_playlist") as ensure_mock,
            mock.patch.object(youtube, "post_comment") as comment_mock,
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        ensure_mock.assert_not_called()
        comment_mock.assert_not_called()

    def test_playlist_flag_adds_video_to_corner_playlist(self) -> None:
        spec = _spec({"youtube_auto_playlist": True})
        with (
            mock.patch.object(youtube, "ensure_playlist", return_value="PL1") as ensure_mock,
            mock.patch.object(
                youtube, "add_video_to_playlist", return_value="added"
            ) as add_mock,
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        ensure_mock.assert_called_once()
        self.assertEqual(ensure_mock.call_args.args[0], "ショート攻略")
        add_mock.assert_called_once_with(
            "PL1",
            "vid123",
            token_file=spec.publish.youtube.token,
            client_secret_file=spec.publish.youtube.client_secret,
        )

    def test_playlist_failure_is_swallowed(self) -> None:
        spec = _spec({"youtube_auto_playlist": True})
        with (
            mock.patch.object(
                youtube, "ensure_playlist", side_effect=RuntimeError("quota")
            ),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        self.assertTrue(
            any("再生リスト追加失敗" in call.args[0] for call in log_mock.call_args_list)
        )

    def test_comment_flag_posts_generated_comment(self) -> None:
        spec = _spec({"youtube_auto_engagement_comment": True})
        with (
            mock.patch.object(
                ai_text, "generate_engagement_comment", return_value="議論を誘発する一言"
            ) as generate_mock,
            mock.patch.object(youtube, "post_comment", return_value="c1") as post_mock,
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        post_mock.assert_called_once_with(
            "vid123",
            "議論を誘発する一言",
            token_file=spec.publish.youtube.token,
            client_secret_file=spec.publish.youtube.client_secret,
        )
        self.assertEqual(generate_mock.call_args.kwargs["mode"], "debate")

    def test_comment_mode_is_passed_from_pipeline_setting(self) -> None:
        spec = _spec(
            {
                "youtube_auto_engagement_comment": True,
                "youtube_engagement_comment_mode": "call_to_action",
            }
        )
        with (
            mock.patch.object(
                ai_text, "generate_engagement_comment", return_value="コメント"
            ) as generate_mock,
            mock.patch.object(youtube, "post_comment", return_value="c1"),
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        self.assertEqual(generate_mock.call_args.kwargs["mode"], "call_to_action")

    def test_comment_mode_defaults_to_debate_when_unset(self) -> None:
        spec = _spec({"youtube_auto_engagement_comment": True})
        with (
            mock.patch.object(
                ai_text, "generate_engagement_comment", return_value="コメント"
            ) as generate_mock,
            mock.patch.object(youtube, "post_comment", return_value="c1"),
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        self.assertEqual(generate_mock.call_args.kwargs["mode"], "debate")

    def test_log_label_reflects_call_to_action_mode(self) -> None:
        spec = _spec(
            {
                "youtube_auto_engagement_comment": True,
                "youtube_engagement_comment_mode": "call_to_action",
            }
        )
        with (
            mock.patch.object(
                ai_text, "generate_engagement_comment", return_value="コメント"
            ),
            mock.patch.object(youtube, "post_comment", return_value="c1"),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        self.assertTrue(
            any("行動喚起コメント投稿" in call.args[0] for call in log_mock.call_args_list)
        )

    def test_comment_generation_failure_skips_posting_without_raising(self) -> None:
        spec = _spec({"youtube_auto_engagement_comment": True})
        with (
            mock.patch.object(ai_text, "generate_engagement_comment", return_value=None),
            mock.patch.object(youtube, "post_comment") as post_mock,
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        post_mock.assert_not_called()

    def test_comment_posting_failure_is_swallowed(self) -> None:
        spec = _spec({"youtube_auto_engagement_comment": True})
        with (
            mock.patch.object(
                ai_text, "generate_engagement_comment", return_value="コメント"
            ),
            mock.patch.object(
                youtube, "post_comment", side_effect=RuntimeError("api error")
            ),
            mock.patch.object(run_daily, "_log") as log_mock,
        ):
            run_daily._apply_youtube_engagement_actions(
                spec, _corner(), {"title": "t", "narration": "n"}, "vid123"
            )
        self.assertTrue(
            any("コメント投稿失敗" in call.args[0] for call in log_mock.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
