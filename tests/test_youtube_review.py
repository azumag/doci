from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from doci import youtube_review
from doci.channel import YouTubeReviewSpec


def _assessment_script(**research_overrides) -> dict:
    research = {
        "topic": "YouTubeショートの冒頭離脱を減らす",
        "angle": "視聴者維持率から冒頭を診断する",
        "youtube_creator_audience": "YouTube制作者",
        "youtube_creator_problem": "YouTubeショートの冒頭離脱を視聴者維持率で特定する",
        "viewer_action": "YouTube Studioで冒頭の維持率を確認し、次の一本の冒頭だけを変更する",
        "theme_fit": "clear",
        "theme_fit_reason": "YouTubeショートの視聴者維持率改善が主題の中心だから",
    }
    research.update(research_overrides)
    return {
        "title": "YouTubeショートの冒頭離脱を直す",
        "description": "視聴者維持率を使った改善手順",
        "narration": (
            "YouTubeショートの視聴者維持率を確認し、"
            "冒頭離脱が起きる位置を次の一本で変更します。"
        ),
        "_research": research,
    }


def _spec(
    root: Path,
    *,
    review: YouTubeReviewSpec | None = None,
) -> SimpleNamespace:
    review = review or YouTubeReviewSpec(enabled=True)
    youtube = SimpleNamespace(
        privacy="unlisted",
        token=root / "youtube-token.json",
        client_secret=root / "client-secret.json",
        review=review,
    )
    return SimpleNamespace(
        id="youtube-growth",
        publish=SimpleNamespace(youtube=youtube),
        history_file=root / "history.jsonl",
        output_dir=root / "output",
    )


class ThemeAssessmentTest(unittest.TestCase):
    def test_all_explicit_fields_and_clear_subject_allow_public(self) -> None:
        result = youtube_review.assess(_assessment_script())

        self.assertTrue(result.eligible_for_public)
        self.assertEqual(result.privacy, "public")
        self.assertEqual(result.reasons, ())

    def test_missing_field_keeps_generation_on_unlisted_path(self) -> None:
        result = youtube_review.assess(_assessment_script(viewer_action=""))

        self.assertFalse(result.eligible_for_public)
        self.assertEqual(result.privacy, "unlisted")
        self.assertIn(
            "視聴後に取れる具体的なYouTube操作がない",
            result.reasons,
        )

    def test_ambiguous_theme_never_auto_publishes(self) -> None:
        result = youtube_review.assess(
            _assessment_script(theme_fit="ambiguous")
        )

        self.assertEqual(result.privacy, "unlisted")

    def test_misleading_content_gap_wording_is_not_auto_published(self) -> None:
        """issue #164: コンテンツギャップを「検索されていないテーマ」と誤解する
        表現は、他の項目が揃っていても自動公開しない。"""
        script = _assessment_script(
            topic="検索されていないテーマから次のショートを作る",
            viewer_action=(
                "YouTube Studioのコンテンツギャップで検索語を1つ選び、"
                "次のショートで不足を埋める"
            ),
        )
        script["title"] = "検索されていない答えから作るショート企画"

        result = youtube_review.assess(script)

        self.assertFalse(result.eligible_for_public)
        self.assertEqual(result.privacy, "unlisted")
        self.assertTrue(
            any(
                "誤解する表現" in reason
                for reason in result.reasons
            )
        )

    def test_correct_content_gap_wording_can_be_public(self) -> None:
        """issue #164: 需要と供給不足が伝わる正しい表現なら誤解表現ガードは
        発動しない（他の条件が揃えばpublic判定を妨げない）。"""
        script = _assessment_script(
            topic="コンテンツギャップから検索流入を狙うショート企画",
            youtube_creator_problem=(
                "検索結果が不足している領域をコンテンツギャップで埋めたい課題"
            ),
            viewer_action=(
                "YouTube Studioのコンテンツギャップで検索語を1つ選び、"
                "次のショートで不足を埋める"
            ),
            theme_fit_reason=(
                "コンテンツギャップから検索流入を狙うショート企画が主題の中心だから"
            ),
        )
        script["title"] = "コンテンツギャップから検索流入を狙うショート"
        script["description"] = "コンテンツギャップから検索流入を狙う企画の立て方"
        script["narration"] = (
            "検索されているのに結果が足りない領域をコンテンツギャップで選び、"
            "次のショートで不足を埋める手順を説明します。"
        )

        result = youtube_review.assess(script)

        self.assertTrue(result.eligible_for_public)
        self.assertEqual(result.privacy, "public")

    def test_misleading_title_with_gap_query_but_no_gap_word_is_unlisted(self) -> None:
        """issue #164: gap_queryが非空でも、本文に「コンテンツギャップ」という
        語が無ければ誤解表現ガードは発動しない（Sol review指摘2の回帰）。"""
        script = _assessment_script(
            topic="検索されていない答えから作るYouTubeショート",
            youtube_creator_problem=(
                "検索されているのに十分な結果がない領域をショート企画で埋めたい課題"
            ),
            viewer_action=(
                "YouTube Studioで検索語を1つ選び、次のショートで不足を埋める"
            ),
            gap_query="ネタ切れ 解消",
        )
        script["title"] = "検索されていない答えから作るショート企画"

        result = youtube_review.assess(script)

        self.assertFalse(result.eligible_for_public)
        self.assertEqual(result.privacy, "unlisted")
        self.assertTrue(
            any(
                "誤解する表現" in reason
                for reason in result.reasons
            )
        )

    def test_plain_search_topic_without_operation_signal_is_not_public(self) -> None:
        """issue #164: 裸の「検索」だけでYouTube運用改善と判定しない（Sol
        review指摘2の回帰）。運用シグナル（検索流入・検索語句・コンテンツ
        ギャップ等）が無い企画は従来どおりunlisted。"""
        script = _assessment_script(
            topic="YouTubeで検索した歴史資料を紹介する",
            youtube_creator_problem=(
                "YouTubeで検索した歴史資料を動画で紹介したい課題"
            ),
            viewer_action=(
                "YouTubeで検索して見つけた資料を次の動画で紹介する"
            ),
        )
        script["title"] = "YouTube検索で見つけた歴史資料"
        script["description"] = "検索で見つけた資料の紹介"
        script["narration"] = (
            "YouTubeで検索して見つけた歴史資料を紹介する動画です。"
        )

        result = youtube_review.assess(script)

        self.assertFalse(result.eligible_for_public)
        self.assertEqual(result.privacy, "unlisted")

    def test_shorts_without_three_pauses_is_not_public(self) -> None:
        """issue #150: shorts台本は情報を留める間（休止表現）が3箇所ないと
        自動公開しない。"""
        script = _assessment_script()
        script["narration"] = (
            "YouTubeショートの視聴者維持率を確認し、冒頭離脱の位置を変えます。"
            "情報を詰め込みすぎると急落します。"
        )

        result = youtube_review.assess(script, corner_key="shorts")

        self.assertFalse(result.eligible_for_public)
        self.assertEqual(result.privacy, "unlisted")
        self.assertTrue(
            any("3箇所ありません" in reason for reason in result.reasons)
        )

    def test_single_ellipsis_sequence_counts_as_one_pause(self) -> None:
        """issue #150 (Sol review指摘): `……` 1回や `………` 1回は1箇所として
        数え、3箇所と誤判定しない。"""
        for narration in (
            "本文……本文",
            "本文………本文",
        ):
            with self.subTest(narration=narration):
                self.assertEqual(youtube_review._pause_count(narration), 1)
                script = _assessment_script()
                script["narration"] = narration
                result = youtube_review.assess(script, corner_key="shorts")
                self.assertFalse(result.eligible_for_public)

    def test_separated_three_pause_sequences_count_as_three(self) -> None:
        """issue #150: 本文で分離された3種類の休止表現は3箇所として数える。"""
        narration = "一文。……二文。…三文。——四文。"
        self.assertEqual(youtube_review._pause_count(narration), 3)

    def test_shorts_with_three_pauses_is_public_when_other_fields_ok(self) -> None:
        """issue #150: shorts台本に休止表現が3箇所あればpauseガードは発動しない。"""
        script = _assessment_script()
        script["narration"] = (
            "YouTubeショートの視聴者維持率を確認します。……"
            "冒頭離脱が起きる位置を変えます。……"
            "情報を詰め込みすぎると急落します。……"
            "次の一本で緩急を試します。"
        )

        result = youtube_review.assess(script, corner_key="shorts")

        self.assertTrue(result.eligible_for_public)
        self.assertEqual(result.privacy, "public")

    def test_non_shorts_corner_ignores_pause_gate(self) -> None:
        """issue #150: video等の他のcornerにはpauseガードを適用しない。"""
        script = _assessment_script()
        script["narration"] = "休止表現のない通常の説明文です。"

        result = youtube_review.assess(script, corner_key="video")

        self.assertTrue(result.eligible_for_public)
        self.assertEqual(result.privacy, "public")

    def test_no_research_is_safe_unlisted_fallback(self) -> None:
        result = youtube_review.assess(
            {"title": "幸福の正体", "description": "睡眠データの話"}
        )

        self.assertEqual(result.privacy, "unlisted")
        self.assertGreaterEqual(len(result.reasons), 4)

    def test_clear_metadata_cannot_publish_an_off_theme_title(self) -> None:
        script = _assessment_script()
        script["title"] = "なぜ私たちは眠らないのか"

        result = youtube_review.assess(script)

        self.assertEqual(result.privacy, "unlisted")
        self.assertIn(
            "企画・タイトルからYouTube主題を明確に確認できない",
            result.reasons,
        )

    def test_negated_youtube_fields_cannot_pass_by_substring(self) -> None:
        result = youtube_review.assess(
            _assessment_script(
                youtube_creator_problem=(
                    "YouTubeショートとは関係ない睡眠の悩みを改善する"
                ),
                theme_fit_reason=(
                    "YouTubeショートとは関係ない企画だが語を含めた"
                ),
            )
        )

        self.assertEqual(result.privacy, "unlisted")

    def test_explanatory_contrast_in_narration_does_not_force_unlisted(
        self,
    ) -> None:
        script = _assessment_script()
        script["narration"] = (
            "YouTubeショートの離脱はアルゴリズムの問題ではなく、"
            "冒頭の視聴維持率を確認して次の一本で変更する課題です。"
        )

        result = youtube_review.assess(script)

        self.assertEqual(result.privacy, "public")

    def test_explicit_subject_rejection_anywhere_keeps_video_unlisted(
        self,
    ) -> None:
        cases = (
            ("topic", "YouTube動画とは関係ない視聴維持率の話"),
            ("title", "YouTubeが主題ではない視聴維持率の話"),
            (
                "narration",
                "YouTubeが主題ではない睡眠企画ですが、"
                "YouTubeショートの視聴維持率と冒頭離脱を確認します。",
            ),
        )
        for field, value in cases:
            script = _assessment_script()
            if field == "topic":
                script["_research"]["topic"] = value
            else:
                script[field] = value

            with self.subTest(field=field):
                result = youtube_review.assess(script)

            self.assertEqual(result.privacy, "unlisted")

    def test_off_topic_narration_cannot_pass_on_youtube_self_declaration(self) -> None:
        script = _assessment_script(
            topic="YouTubeショートで睡眠の幸福を語る",
            angle="睡眠日誌の良さを紹介する",
            youtube_creator_problem=(
                "YouTubeショートで睡眠の幸福を語る企画を作る"
            ),
            viewer_action="次の動画の冒頭に睡眠日誌を追加する",
            theme_fit_reason="YouTubeショートを使った睡眠の幸福が主題",
        )
        script["title"] = "YouTubeショートで睡眠の幸福を語る"
        script["description"] = "睡眠日誌で幸福を見つける方法"
        script["narration"] = "幸福と睡眠だけの話"

        result = youtube_review.assess(script)

        self.assertEqual(result.privacy, "unlisted")
        self.assertIn(
            "解決する具体的なYouTube上の課題または指標がない",
            result.reasons,
        )

    def test_operation_target_without_problem_or_metric_is_unlisted(self) -> None:
        script = _assessment_script(
            topic="YouTube動画のタイトルで睡眠の幸福を語る",
            angle="タイトルに睡眠の幸福を入れる",
            youtube_creator_problem=(
                "YouTube動画のタイトルに睡眠の幸福を入れる企画を作る"
            ),
            viewer_action="次の動画のタイトルに睡眠の幸福を追加する",
            theme_fit_reason="YouTube動画のタイトルで睡眠の幸福を語る主題",
        )
        script["title"] = "YouTube動画のタイトルで睡眠の幸福を語る"
        script["description"] = "タイトルに睡眠の幸福を入れる"
        script["narration"] = "YouTube動画のタイトルに睡眠の幸福を追加します"

        result = youtube_review.assess(script)

        self.assertEqual(result.privacy, "unlisted")
        self.assertIn(
            "解決する具体的なYouTube上の課題または指標がない",
            result.reasons,
        )

    def test_generated_narration_must_retain_the_planned_youtube_focus(self) -> None:
        script = _assessment_script()
        script["description"] = "睡眠日誌で幸福を見つける方法"
        script["narration"] = "幸福と睡眠だけの話"

        result = youtube_review.assess(script)

        self.assertEqual(result.privacy, "unlisted")
        self.assertIn(
            "企画・タイトルからYouTube主題を明確に確認できない",
            result.reasons,
        )

    def test_vague_action_cannot_pass_on_a_generic_verb(self) -> None:
        result = youtube_review.assess(
            _assessment_script(viewer_action="YouTubeを見る")
        )

        self.assertEqual(result.privacy, "unlisted")

    def test_disabled_review_preserves_existing_channel_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(
                Path(tmp),
                review=YouTubeReviewSpec(enabled=False),
            )

            privacy, assessment = youtube_review.choose_privacy(
                spec,
                _assessment_script(),
            )

        self.assertEqual(privacy, "unlisted")
        self.assertIsNone(assessment)

    def test_enabled_review_decides_privacy_from_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(Path(tmp), review=YouTubeReviewSpec(enabled=True))

            privacy, assessment = youtube_review.choose_privacy(
                spec,
                _assessment_script(),
            )

        self.assertEqual(privacy, "public")
        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.eligible_for_public)


if __name__ == "__main__":
    unittest.main()
