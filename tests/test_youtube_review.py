from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    review = review or YouTubeReviewSpec(
        enabled=True,
        repository="owner/repo",
        publish_label="公開承認",
        hold_label="保留",
        keep_unlisted_label="限定公開で保持",
    )
    youtube = SimpleNamespace(
        privacy="unlisted",
        token=root / "youtube-token.json",
        client_secret=root / "client-secret.json",
        review=review,
    )
    return SimpleNamespace(
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


class GithubIdentityTest(unittest.TestCase):
    def tearDown(self) -> None:
        youtube_review._current_gh_login.cache_clear()

    def test_current_login_comes_from_gh_authenticated_user(self) -> None:
        youtube_review._current_gh_login.cache_clear()
        with mock.patch.object(
            youtube_review,
            "_run_gh",
            return_value="review-operator",
        ) as gh_mock:
            login = youtube_review._current_gh_login()

        self.assertEqual(login, "review-operator")
        gh_mock.assert_called_once_with(["api", "user", "--jq", ".login"])

    def test_invalid_current_login_fails_closed(self) -> None:
        youtube_review._current_gh_login.cache_clear()
        with mock.patch.object(
            youtube_review,
            "_run_gh",
            return_value="unsafe login",
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "gh認証ユーザー名を安全に確認できません",
            ):
                youtube_review._current_gh_login()


class IssueWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = _spec(self.root)
        self.review = self.spec.publish.youtube.review
        self.assessment = youtube_review.assess(
            _assessment_script(viewer_action="")
        )
        login_patch = mock.patch.object(
            youtube_review,
            "_current_gh_login",
            return_value="owner",
        )
        login_patch.start()
        self.addCleanup(login_patch.stop)
        self.list_open_impl = youtube_review._list_open_tracking_issues
        issue_list_patch = mock.patch.object(
            youtube_review,
            "_list_open_tracking_issues",
            return_value={},
        )
        self.issue_list_mock = issue_list_patch.start()
        self.addCleanup(issue_list_patch.stop)

    def _issue(
        self,
        *,
        labels: tuple[str, ...] = (),
        state: str = "OPEN",
        video_id: str = "abc123XYZ",
        number: int = 42,
        author: str = "owner",
    ) -> youtube_review.TrackingIssue:
        return youtube_review.TrackingIssue(
            number=number,
            video_id=video_id,
            title="確認",
            body=f"<!-- doci-youtube-review video_id={video_id} -->",
            labels=labels,
            url=f"https://github.com/owner/repo/issues/{number}",
            state=state,
            author=author,
        )

    def _queue(self, video_id: str = "abc123XYZ") -> None:
        youtube_review.queue_pending(
            self.spec,
            video_id,
            "YouTubeショート改善",
            self.assessment,
        )

    def test_issue_body_documents_labels_and_no_time_based_publish(self) -> None:
        body = youtube_review._issue_body(
            "abc123XYZ",
            "YouTubeショート改善",
            self.assessment,
            self.review,
        )

        self.assertIn("公開承認", body)
        self.assertIn("保留", body)
        self.assertIn("限定公開で保持", body)
        self.assertIn("経過時間だけを理由に、自動公開することはありません", body)
        self.assertNotIn("token", body.casefold())

    def test_ensure_issue_requires_local_outbox_provenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "not registered"):
            youtube_review.ensure_issue(self.spec, "abc123XYZ")

    def test_operation_lock_times_out_instead_of_waiting_forever(self) -> None:
        with (
            mock.patch.object(
                youtube_review.fcntl,
                "flock",
                side_effect=BlockingIOError,
            ),
            mock.patch.object(
                youtube_review.time,
                "monotonic",
                side_effect=[0.0, 1.0],
            ),
            mock.patch.object(youtube_review.time, "sleep") as sleep_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "0秒以内に取得できません"):
                with youtube_review._operation_lock(
                    self.spec,
                    timeout_seconds=0,
                ):
                    self.fail("lock must not be acquired")

        sleep_mock.assert_not_called()

    def test_retry_plan_is_scoped_to_one_cron_cycle(self) -> None:
        youtube_review.save_retry_plan(
            self.spec,
            "cron-123",
            ("broken123", "broken123"),
        )
        youtube_review.save_retry_plan(
            self.spec,
            "cron-124",
            ("second456",),
        )

        self.assertEqual(
            youtube_review.load_retry_plan(self.spec, "cron-123"),
            ("broken123",),
        )
        self.assertEqual(
            youtube_review.load_retry_plan(self.spec, "cron-124"),
            ("second456",),
        )
        self.assertNotEqual(
            youtube_review._retry_plan_path(self.spec, "cron-123"),
            youtube_review._retry_plan_path(self.spec, "cron-124"),
        )
        self.assertIsNone(youtube_review.load_retry_plan(self.spec, ""))

    def test_retry_plan_count_is_bounded_without_removing_current_cycle(self) -> None:
        with mock.patch.object(youtube_review, "_MAX_RETRY_PLAN_FILES", 2):
            for index in range(3):
                youtube_review.save_retry_plan(
                    self.spec,
                    f"cron-{index}",
                    (f"video{index}99",),
                )

        plan_dir = self.spec.output_dir / ".youtube_review_retry"
        self.assertLessEqual(len(list(plan_dir.glob("*.json"))), 2)
        self.assertEqual(
            youtube_review.load_retry_plan(self.spec, "cron-2"),
            ("video299",),
        )

    def test_outbox_compaction_preserves_latest_state_per_video(self) -> None:
        self._queue()
        record = youtube_review._latest_records(self.spec)["abc123XYZ"]
        youtube_review._append_record(
            self.spec,
            youtube_review._with_status(record, "hold"),
        )
        youtube_review._append_record(
            self.spec,
            youtube_review._with_status(record, "keep_unlisted"),
        )

        with mock.patch.object(
            youtube_review,
            "_OUTBOX_COMPACT_MIN_EVENTS",
            2,
        ):
            youtube_review._compact_outbox_locked(self.spec)

        lines = youtube_review._outbox_path(self.spec).read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            youtube_review._latest_records(self.spec)["abc123XYZ"].status,
            "keep_unlisted",
        )

    def test_queue_is_fsynced_before_issue_creation_and_reused(self) -> None:
        self._queue()
        existing = self._issue(state="CLOSED")
        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                return_value=existing,
            ),
            mock.patch.object(youtube_review, "_create_issue") as create_mock,
        ):
            result = youtube_review.ensure_issue(self.spec, existing.video_id)

        self.assertEqual(result, existing)
        create_mock.assert_not_called()
        row = json.loads(
            youtube_review._outbox_path(self.spec)
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        self.assertEqual(row["status"], "pending")

    def test_publish_label_changes_only_unlisted_then_closes_issue(self) -> None:
        issue = self._issue(labels=("公開承認",))
        self._queue(issue.video_id)
        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                return_value=issue,
            ),
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=issue,
            ),
            mock.patch(
                "doci.youtube.privacy_status",
                return_value="unlisted",
            ),
            mock.patch("doci.youtube.set_privacy") as privacy_mock,
            mock.patch.object(
                youtube_review,
                "_close_published_issue",
            ) as close_mock,
        ):
            events = youtube_review.reconcile(self.spec)

        privacy_mock.assert_called_once_with(
            issue.video_id,
            "public",
            expected_privacy="unlisted",
            token_file=self.spec.publish.youtube.token,
            client_secret_file=self.spec.publish.youtube.client_secret,
        )
        close_mock.assert_called_once_with(self.review, issue)
        self.assertIn("公開完了", events[0])

    def test_private_video_is_never_changed_even_with_publish_label(self) -> None:
        issue = self._issue(labels=("公開承認",))
        self._queue(issue.video_id)
        with (
            mock.patch.object(youtube_review, "_find_issue", return_value=issue),
            mock.patch.object(youtube_review, "_get_issue", return_value=issue),
            mock.patch("doci.youtube.privacy_status", return_value="private"),
            mock.patch("doci.youtube.set_privacy") as privacy_mock,
            mock.patch.object(
                youtube_review,
                "_close_published_issue",
            ) as close_mock,
        ):
            events = youtube_review.reconcile(self.spec)

        privacy_mock.assert_not_called()
        close_mock.assert_not_called()
        self.assertIn("公開変更を拒否", events[0])

    def test_closed_issue_recovers_after_terminal_append_failure(self) -> None:
        approved = self._issue(labels=("公開承認",))
        closed = self._issue(labels=("公開承認",), state="CLOSED")
        self._queue(approved.video_id)
        original_append = youtube_review._append_record
        failed_once = False

        def fail_first_terminal(spec, record):
            nonlocal failed_once
            if record.status == "published" and not failed_once:
                failed_once = True
                raise OSError("terminal fsync failed")
            return original_append(spec, record)

        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                return_value=approved,
            ),
            mock.patch.object(
                youtube_review,
                "_get_issue",
                side_effect=[approved, closed, closed],
            ),
            mock.patch(
                "doci.youtube.privacy_status",
                return_value="public",
            ) as privacy_mock,
            mock.patch.object(
                youtube_review,
                "_close_published_issue",
            ),
            mock.patch.object(
                youtube_review,
                "_append_record",
                side_effect=fail_first_terminal,
            ),
        ):
            first_events = youtube_review.reconcile(self.spec)
            self.assertEqual(
                youtube_review._latest_records(self.spec)[approved.video_id].status,
                "public_confirmed",
            )
            second_events = youtube_review.reconcile(self.spec)

        self.assertTrue(any("terminal fsync failed" in event for event in first_events))
        self.assertTrue(any("公開完了状態を復旧" in event for event in second_events))
        self.assertEqual(
            youtube_review._latest_records(self.spec)[approved.video_id].status,
            "published",
        )
        privacy_mock.assert_called_once()

    def test_public_confirmed_closes_even_if_approval_label_was_removed(self) -> None:
        approved = self._issue(labels=("公開承認",))
        withdrawn = self._issue(labels=())
        self._queue(approved.video_id)
        failed_once = False

        def fail_first_close(review, issue):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise RuntimeError("close failed")

        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                return_value=approved,
            ),
            mock.patch.object(
                youtube_review,
                "_get_issue",
                side_effect=[approved, withdrawn, withdrawn],
            ),
            mock.patch(
                "doci.youtube.privacy_status",
                return_value="public",
            ) as privacy_mock,
            mock.patch.object(
                youtube_review,
                "_close_published_issue",
                side_effect=fail_first_close,
            ) as close_mock,
        ):
            first_events = youtube_review.reconcile(self.spec)
            self.assertEqual(
                youtube_review._latest_records(self.spec)[approved.video_id].status,
                "public_confirmed",
            )
            second_events = youtube_review.reconcile(self.spec)

        self.assertTrue(any("close failed" in event for event in first_events))
        self.assertTrue(any("公開完了状態を復旧" in event for event in second_events))
        self.assertEqual(
            youtube_review._latest_records(self.spec)[approved.video_id].status,
            "published",
        )
        self.assertEqual(close_mock.call_count, 2)
        privacy_mock.assert_called_once()

    def test_forged_issue_without_outbox_is_ignored(self) -> None:
        with (
            mock.patch.object(youtube_review, "_find_issue") as find_mock,
            mock.patch("doci.youtube.privacy_status") as privacy_mock,
        ):
            events = youtube_review.reconcile(self.spec)

        self.assertEqual(events, [])
        find_mock.assert_not_called()
        privacy_mock.assert_not_called()

    def test_fresh_label_withdrawal_prevents_publication(self) -> None:
        approved = self._issue(labels=("公開承認",))
        withdrawn = self._issue(labels=())
        self._queue(approved.video_id)
        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                return_value=approved,
            ),
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=withdrawn,
            ),
            mock.patch("doci.youtube.privacy_status") as privacy_mock,
        ):
            events = youtube_review.reconcile(self.spec)

        self.assertEqual(events, [])
        privacy_mock.assert_not_called()

    def test_hold_keep_and_conflicting_labels_never_change_youtube(self) -> None:
        cases = (
            ("hold123", ("保留",), "保留"),
            ("keep123", ("限定公開で保持",), "限定公開で保持"),
            ("conflict123", ("公開承認", "保留"), "競合"),
        )
        for index, (video_id, labels, expected) in enumerate(cases, start=1):
            self._queue(video_id)
            issue = self._issue(
                labels=labels,
                video_id=video_id,
                number=index,
            )
            with (
                self.subTest(video_id=video_id),
                mock.patch.object(
                    youtube_review,
                    "_find_issue",
                    return_value=issue,
                ),
                mock.patch.object(
                    youtube_review,
                    "_get_issue",
                    return_value=issue,
                ),
                mock.patch("doci.youtube.privacy_status") as privacy_mock,
            ):
                events = youtube_review.reconcile(self.spec)

            privacy_mock.assert_not_called()
            self.assertTrue(any(expected in event for event in events))

    def test_linked_hold_issue_is_fetched_once_per_reconcile(self) -> None:
        issue = self._issue(labels=("保留",))
        self._queue(issue.video_id)
        record = youtube_review._latest_records(self.spec)[issue.video_id]
        youtube_review._append_record(
            self.spec,
            youtube_review._with_issue(record, issue),
        )
        before_lines = youtube_review._outbox_path(self.spec).read_text(
            encoding="utf-8"
        ).splitlines()
        self.issue_list_mock.return_value = {issue.number: issue}
        with (
            mock.patch.object(
                youtube_review,
                "_get_issue",
            ) as get_mock,
            mock.patch("doci.youtube.privacy_status") as privacy_mock,
        ):
            outcome = youtube_review.reconcile_result(self.spec)

        self.assertEqual(outcome.failed_count, 0)
        self.assertIn("保留（変更なし）", outcome.events[0])
        self.issue_list_mock.assert_called_once_with(self.review, "owner")
        get_mock.assert_not_called()
        privacy_mock.assert_not_called()
        after_lines = youtube_review._outbox_path(self.spec).read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(after_lines, before_lines)

    def test_linked_issues_are_batched_for_each_recorded_actor(self) -> None:
        actor_a = self._issue(
            labels=("保留",),
            video_id="actorA11",
            number=11,
            author="actor-a",
        )
        actor_b = self._issue(
            video_id="actorB22",
            number=22,
            author="actor-b",
        )
        for issue in (actor_a, actor_b):
            self._queue(issue.video_id)
            record = youtube_review._latest_records(self.spec)[issue.video_id]
            youtube_review._append_record(
                self.spec,
                youtube_review._with_issue(record, issue),
            )
        by_author = {
            "actor-a": {actor_a.number: actor_a},
            "actor-b": {actor_b.number: actor_b},
        }
        self.issue_list_mock.side_effect = (
            lambda _review, author: by_author[author]
        )
        with mock.patch.object(youtube_review, "_get_issue") as get_mock:
            outcome = youtube_review.reconcile_result(self.spec)

        self.assertEqual(outcome.failed_count, 0)
        self.assertCountEqual(
            self.issue_list_mock.call_args_list,
            [
                mock.call(self.review, "actor-a"),
                mock.call(self.review, "actor-b"),
            ],
        )
        get_mock.assert_not_called()

    def test_actor_batch_failure_happens_before_any_video_change(self) -> None:
        actor_a = self._issue(
            labels=("公開承認",),
            video_id="actorA11",
            number=11,
            author="actor-a",
        )
        actor_b = self._issue(
            labels=("公開承認",),
            video_id="actorB22",
            number=22,
            author="actor-b",
        )
        for issue in (actor_a, actor_b):
            self._queue(issue.video_id)
            record = youtube_review._latest_records(self.spec)[issue.video_id]
            youtube_review._append_record(
                self.spec,
                youtube_review._with_issue(record, issue),
            )

        def list_for_actor(_review, author):
            if author == "actor-b":
                raise RuntimeError("ページ上限")
            return {actor_a.number: actor_a}

        self.issue_list_mock.side_effect = list_for_actor
        with (
            mock.patch.object(youtube_review, "_get_issue") as get_mock,
            mock.patch("doci.youtube.privacy_status") as privacy_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "ページ上限"):
                youtube_review.reconcile_result(self.spec)

        get_mock.assert_not_called()
        privacy_mock.assert_not_called()

    def test_retry_targets_only_failed_video_and_does_not_refetch_hold(self) -> None:
        hold = self._issue(labels=("保留",), video_id="hold123", number=1)
        self._queue(hold.video_id)
        hold_record = youtube_review._latest_records(self.spec)[hold.video_id]
        youtube_review._append_record(
            self.spec,
            youtube_review._with_issue(hold_record, hold),
        )
        self._queue("broken123")

        def fail_broken(_review, video_id, _expected_author):
            if video_id == "broken123":
                raise RuntimeError("boom")
            self.fail(f"unexpected issue lookup: {video_id}")

        with (
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=hold,
            ) as get_mock,
            mock.patch.object(
                youtube_review,
                "_find_issue",
                side_effect=fail_broken,
            ),
        ):
            first = youtube_review.reconcile_result(self.spec)
            youtube_review.save_retry_plan(
                self.spec,
                "cron-cycle-a",
                first.failed_video_ids,
            )
            youtube_review.save_retry_plan(
                self.spec,
                "cron-cycle-b",
                (),
            )
            retry_ids = youtube_review.load_retry_plan(
                self.spec,
                "cron-cycle-a",
            )
            self.assertEqual(retry_ids, ("broken123",))
            self.assertEqual(
                youtube_review.load_retry_plan(self.spec, "cron-cycle-b"),
                (),
            )
            second = youtube_review.reconcile_result(
                self.spec,
                only_video_ids=set(retry_ids),
            )

        self.assertEqual(first.failed_video_ids, ("broken123",))
        self.assertEqual(second.failed_video_ids, ("broken123",))
        get_mock.assert_called_once_with(self.review, hold.number)

    def test_missing_issue_is_retried_from_unlisted_history(self) -> None:
        row = {
            "video_id": "retry123",
            "title": "確認待ち",
            "youtube_privacy": "unlisted",
            "youtube_theme_review": self.assessment.to_dict(),
        }
        self.spec.history_file.write_text(
            json.dumps(row, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        created = self._issue(video_id="retry123")
        with (
            mock.patch.object(youtube_review, "_find_issue", return_value=None),
            mock.patch.object(
                youtube_review,
                "_create_issue",
                return_value=created,
            ) as create_mock,
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=created,
            ),
        ):
            youtube_review.reconcile(self.spec)

        create_mock.assert_called_once()
        records = youtube_review._latest_records(self.spec)
        self.assertEqual(records["retry123"].issue_number, 42)

    def test_one_broken_record_does_not_stop_the_next_record(self) -> None:
        self._queue("broken123")
        self._queue("healthy123")
        healthy = self._issue(video_id="healthy123")

        def find_issue(_review, video_id, _expected_author):
            if video_id == "broken123":
                raise RuntimeError("boom")
            return healthy

        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                side_effect=find_issue,
            ),
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=healthy,
            ),
        ):
            outcome = youtube_review.reconcile_result(self.spec)

        self.assertEqual(outcome.failed_count, 1)
        self.assertEqual(outcome.failed_video_ids, ("broken123",))
        self.assertTrue(any("broken123" in event for event in outcome.events))
        self.assertEqual(
            youtube_review._latest_records(self.spec)["healthy123"].issue_number,
            42,
        )

    def test_issue_lookup_is_authenticated_user_scoped_and_direct(self) -> None:
        with mock.patch.object(
            youtube_review,
            "_run_gh",
            return_value="[]",
        ) as gh_mock:
            youtube_review._find_issue(self.review, "abc123XYZ")

        args = gh_mock.call_args.args[0]
        self.assertIn("api", args)
        self.assertIn("creator=owner", args)
        self.assertIn("per_page=100", args)
        self.assertIn("page=1", args)
        self.assertNotIn("--search", args)

    def test_issue_lookup_checks_multiple_pages(self) -> None:
        unrelated = [
            {
                "number": number,
                "title": "unrelated",
                "body": "",
                "labels": [],
                "html_url": f"https://github.com/owner/repo/issues/{number}",
                "state": "open",
                "user": {"login": "owner"},
            }
            for number in range(1, 101)
        ]
        target = {
            "number": 101,
            "title": "target",
            "body": "<!-- doci-youtube-review video_id=abc123XYZ -->",
            "labels": [],
            "html_url": "https://github.com/owner/repo/issues/101",
            "state": "open",
            "user": {"login": "owner"},
        }
        with mock.patch.object(
            youtube_review,
            "_run_gh",
            side_effect=[
                json.dumps(unrelated),
                json.dumps([target]),
            ],
        ) as gh_mock:
            issue = youtube_review._find_issue(self.review, "abc123XYZ")

        self.assertIsNotNone(issue)
        self.assertEqual(issue.number, 101)
        self.assertEqual(gh_mock.call_count, 2)
        self.assertIn("page=2", gh_mock.call_args.args[0])

    def test_issue_lookup_fails_closed_at_page_limit(self) -> None:
        full_page = [
            {
                "number": number,
                "title": "unrelated",
                "body": "",
                "labels": [],
                "html_url": f"https://github.com/owner/repo/issues/{number}",
                "state": "open",
                "user": {"login": "owner"},
            }
            for number in range(1, 101)
        ]
        with (
            mock.patch.object(youtube_review, "_MAX_ISSUE_LIST_PAGES", 2),
            mock.patch.object(
                youtube_review,
                "_run_gh",
                return_value=json.dumps(full_page),
            ) as gh_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "ページ上限"):
                youtube_review._find_issue(self.review, "abc123XYZ")

        self.assertEqual(gh_mock.call_count, 2)

    def test_open_tracking_issues_are_fetched_as_one_page(self) -> None:
        row = {
            "number": 42,
            "title": "target",
            "body": "<!-- doci-youtube-review video_id=abc123XYZ -->",
            "labels": [{"name": "保留"}],
            "html_url": "https://github.com/owner/repo/issues/42",
            "state": "open",
            "user": {"login": "owner"},
        }
        with mock.patch.object(
            youtube_review,
            "_run_gh",
            return_value=json.dumps([row]),
        ) as gh_mock:
            issues = self.list_open_impl(self.review, "owner")

        self.assertEqual(issues[42].video_id, "abc123XYZ")
        args = gh_mock.call_args.args[0]
        self.assertIn("state=open", args)
        self.assertIn("creator=owner", args)

    def test_issue_lookup_ignores_matching_marker_from_another_author(self) -> None:
        row = {
            "number": 42,
            "title": "forged",
            "body": "<!-- doci-youtube-review video_id=abc123XYZ -->",
            "labels": [],
            "html_url": "https://github.com/owner/repo/issues/42",
            "state": "open",
            "user": {"login": "attacker"},
        }
        with mock.patch.object(
            youtube_review,
            "_run_gh",
            return_value=json.dumps([row]),
        ):
            issue = youtube_review._find_issue(self.review, "abc123XYZ")

        self.assertIsNone(issue)

    def test_issue_creation_intent_prevents_duplicate_after_link_fsync_failure(
        self,
    ) -> None:
        self._queue()
        created = self._issue()
        original_append = youtube_review._append_record
        failed_once = False

        def fail_link_once(spec, record):
            nonlocal failed_once
            if record.issue_number is not None and not failed_once:
                failed_once = True
                raise OSError("link fsync failed")
            return original_append(spec, record)

        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                side_effect=[None, created],
            ),
            mock.patch.object(
                youtube_review,
                "_create_issue",
                return_value=created,
            ) as create_mock,
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=created,
            ),
            mock.patch.object(
                youtube_review,
                "_append_record",
                side_effect=fail_link_once,
            ),
        ):
            with self.assertRaisesRegex(OSError, "link fsync failed"):
                youtube_review.ensure_issue(self.spec, created.video_id)
            self.assertEqual(
                youtube_review._latest_records(self.spec)[created.video_id].status,
                "issue_creating",
            )
            self.assertEqual(
                youtube_review._latest_records(self.spec)[created.video_id].issue_author,
                "owner",
            )
            recovered = youtube_review.ensure_issue(self.spec, created.video_id)

        self.assertEqual(recovered, created)
        create_mock.assert_called_once()

    def test_actor_switch_after_create_loss_never_recreates_issue(self) -> None:
        self._queue()
        created = self._issue(author="actor-a")
        original_append = youtube_review._append_record
        failed_once = False

        def fail_link_once(spec, record):
            nonlocal failed_once
            if record.issue_number is not None and not failed_once:
                failed_once = True
                raise OSError("link fsync failed")
            return original_append(spec, record)

        with (
            mock.patch.object(
                youtube_review,
                "_current_gh_login",
                return_value="actor-a",
            ),
            mock.patch.object(youtube_review, "_find_issue", return_value=None),
            mock.patch.object(
                youtube_review,
                "_create_issue",
                return_value=created,
            ) as first_create,
            mock.patch.object(youtube_review, "_get_issue", return_value=created),
            mock.patch.object(
                youtube_review,
                "_append_record",
                side_effect=fail_link_once,
            ),
        ):
            with self.assertRaisesRegex(OSError, "link fsync failed"):
                youtube_review.ensure_issue(self.spec, created.video_id)

        record = youtube_review._latest_records(self.spec)[created.video_id]
        self.assertEqual(record.status, "issue_creating")
        self.assertEqual(record.issue_author, "actor-a")
        first_create.assert_called_once()

        with (
            mock.patch.object(
                youtube_review,
                "_current_gh_login",
                return_value="actor-b",
            ),
            mock.patch.object(
                youtube_review,
                "_find_issue",
                return_value=None,
            ) as find_mock,
            mock.patch.object(youtube_review, "_create_issue") as retry_create,
        ):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "gh認証ユーザーが変更"):
                    youtube_review.ensure_issue(self.spec, created.video_id)

        retry_create.assert_not_called()
        self.assertEqual(find_mock.call_count, 2)
        for call in find_mock.call_args_list:
            self.assertEqual(call.args, (self.review, created.video_id, "actor-a"))
        latest = youtube_review._latest_records(self.spec)[created.video_id]
        self.assertEqual(latest.status, "issue_creating")
        self.assertEqual(latest.issue_author, "actor-a")

    def test_new_issue_author_is_verified_before_linking(self) -> None:
        self._queue()
        created = self._issue()
        forged = self._issue(author="attacker")
        with (
            mock.patch.object(youtube_review, "_find_issue", return_value=None),
            mock.patch.object(
                youtube_review,
                "_create_issue",
                return_value=created,
            ),
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=forged,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "作成者"):
                youtube_review.ensure_issue(self.spec, created.video_id)

        record = youtube_review._latest_records(self.spec)[created.video_id]
        self.assertEqual(record.status, "issue_creating")
        self.assertIsNone(record.issue_number)

    def test_missing_marker_on_recorded_issue_never_creates_a_duplicate(
        self,
    ) -> None:
        self._queue()
        existing = self._issue()
        with mock.patch.object(
            youtube_review,
            "_find_issue",
            return_value=existing,
        ):
            youtube_review.ensure_issue(self.spec, existing.video_id)

        with (
            mock.patch.object(
                youtube_review,
                "_get_issue",
                return_value=None,
            ),
            mock.patch.object(youtube_review, "_create_issue") as create_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "自動再作成しません"):
                youtube_review.ensure_issue(self.spec, existing.video_id)

        create_mock.assert_not_called()

    def test_failed_creation_is_retried_only_after_a_direct_lookup_run(
        self,
    ) -> None:
        self._queue()
        with (
            mock.patch.object(
                youtube_review,
                "_find_issue",
                side_effect=[None, None],
            ),
            mock.patch.object(
                youtube_review,
                "_create_issue",
                side_effect=RuntimeError("create failed"),
            ) as create_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "create failed"):
                youtube_review.ensure_issue(self.spec, "abc123XYZ")
            self.assertEqual(
                youtube_review._latest_records(self.spec)["abc123XYZ"].status,
                "issue_creating",
            )
            with self.assertRaisesRegex(RuntimeError, "次回run"):
                youtube_review.ensure_issue(self.spec, "abc123XYZ")

        self.assertEqual(
            youtube_review._latest_records(self.spec)["abc123XYZ"].status,
            "pending",
        )
        create_mock.assert_called_once()

    def test_error_redaction_removes_github_token_shapes(self) -> None:
        value = "failed with ghp_abcdefghijklmnopqrstuvwxyz123456"
        self.assertNotIn("ghp_", youtube_review._redact(value))


if __name__ == "__main__":
    unittest.main()
