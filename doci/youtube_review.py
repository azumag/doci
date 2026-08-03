"""YouTube攻略Chの主題ガード（企画の主題適合を判定するだけの純粋関数群）。

自動公開の可否は厳格な企画項目だけで判定し、時間経過を公開可否の理由には
しない。GitHub Issueでの人手承認・ラベル待ち・reconcileの仕組みは存在しない。
`review.enabled = false` のチャンネルは常に `publish.youtube.privacy` の
静的な値をそのまま使う。`review.enabled = true` のチャンネルは、この主題判定
（`assess()`）の結果だけでpublic/unlistedを都度決める。
`youtube-growth` の公開判定は `pipeline.performance_gated_publish` により
`doci/run_daily.py` 側で行い、この主題ガードは経由しない。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .channel import ChannelSpec

_SUBJECT_REJECTION_MARKERS = (
    "youtubeとは関係ない",
    "youtubeと関係ない",
    "youtubeとは無関係",
    "youtubeと無関係",
    "youtubeが主題ではない",
    "youtubeは主題ではない",
    "youtube制作者向けではない",
    "youtube向けではない",
    "youtube運用向けではない",
    "youtube制作者は対象外",
    "youtube制作者を対象としない",
    "youtubeが対象ではない",
    "youtubeショートとは関係ない",
    "youtubeショートと関係ない",
    "youtube動画とは関係ない",
    "youtube動画と関係ない",
    "youtube動画とは無関係",
)
_YOUTUBE_CONTEXT_MARKERS = (
    "youtube",
    "ショート",
    "shorts",
)
_YOUTUBE_OPERATION_MARKERS = (
    "ctr",
    "サムネ",
    "クリック率",
    "視聴維持",
    "維持率",
    "離脱",
    "平均視聴時間",
    "再生数",
    "登録者",
    "インプレッション",
    "youtube studio",
    "アナリティクス",
    "チャンネル登録",
    "関連動画",
    "流入元",
    "タイトル",
    "冒頭",
)
_PROBLEM_SIGNAL_MARKERS = (
    "ctr",
    "クリック率",
    "視聴維持",
    "維持率",
    "離脱",
    "平均視聴時間",
    "再生数",
    "登録者",
    "インプレッション",
    "流入元",
    "関連動画",
    "低い",
    "下が",
    "伸びない",
    "増えない",
    "減る",
    "不足",
    "届かない",
    "クリックされない",
    "視聴されない",
    "できない",
    "わからない",
    "迷う",
    "失敗",
)
_ACTION_TARGET_MARKERS = (
    "youtube studio",
    "次の動画",
    "次の一本",
    "次のショート",
    "タイトル",
    "サムネ",
    "冒頭",
    "説明欄",
    "視聴維持",
    "アナリティクス",
)
_ACTION_MARKERS = (
    "確認",
    "変更",
    "比較",
    "記録",
    "設定",
    "編集",
    "作成",
    "試す",
    "測る",
    "調整",
    "開く",
    "選ぶ",
    "削る",
    "追加",
)


@dataclass(frozen=True)
class ThemeAssessment:
    audience: str
    problem: str
    viewer_action: str
    theme_fit: str
    theme_fit_reason: str
    subject_clear: bool
    eligible_for_public: bool
    reasons: tuple[str, ...]

    @property
    def privacy(self) -> str:
        return "public" if self.eligible_for_public else "unlisted"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["privacy"] = self.privacy
        return data


def _text(value: object, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _rejects_youtube_subject(*values: str) -> bool:
    folded = " ".join(values).casefold()
    return any(marker in folded for marker in _SUBJECT_REJECTION_MARKERS)


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in markers)


def _matched_markers(value: str, markers: tuple[str, ...]) -> set[str]:
    folded = value.casefold()
    return {marker for marker in markers if marker in folded}


def assess(script: dict) -> ThemeAssessment:
    """3明示項目と主題適合が全て厳格に確認できる場合だけ自動公開可とする。"""
    research = script.get("_research")
    research = research if isinstance(research, dict) else {}
    audience = _text(research.get("youtube_creator_audience"))
    problem = _text(research.get("youtube_creator_problem"))
    viewer_action = _text(research.get("viewer_action"))
    theme_fit = _text(research.get("theme_fit"), limit=40).casefold()
    theme_fit_reason = _text(research.get("theme_fit_reason"))
    title = _text(script.get("title"))
    topic = _text(research.get("topic"))
    angle = _text(research.get("angle"))
    description = _text(script.get("description"))
    narration = _text(script.get("narration"), limit=5000)

    audience_clear = audience.casefold() == "youtube制作者".casefold()
    problem_markers = _matched_markers(problem, _YOUTUBE_OPERATION_MARKERS)
    problem_clear = (
        len(problem) >= 8
        and bool(problem_markers)
        and _contains_any(problem, _PROBLEM_SIGNAL_MARKERS)
        and not _rejects_youtube_subject(problem)
    )
    action_clear = (
        len(viewer_action) >= 8
        and _contains_any(viewer_action, _ACTION_TARGET_MARKERS)
        and _contains_any(viewer_action, _ACTION_MARKERS)
        and not _rejects_youtube_subject(viewer_action)
    )
    planned_subject = " ".join((topic, angle))
    generated_subject = " ".join((title, description, narration))
    context_clear = all(
        _contains_any(value, _YOUTUBE_CONTEXT_MARKERS)
        for value in (planned_subject, generated_subject)
    )
    focus_consistent = bool(problem_markers) and all(
        bool(problem_markers.intersection(_matched_markers(value, _YOUTUBE_OPERATION_MARKERS)))
        for value in (
            planned_subject,
            title,
            " ".join((description, narration)),
            theme_fit_reason,
        )
    )
    subject_clear = (
        context_clear
        and focus_consistent
        and not _rejects_youtube_subject(
            audience,
            problem,
            viewer_action,
            topic,
            angle,
            title,
            description,
            narration,
            theme_fit_reason,
        )
    )

    reasons: list[str] = []
    if not audience_clear:
        reasons.append("対象者がYouTube制作者と厳密に明記されていない")
    if not problem_clear:
        reasons.append("解決する具体的なYouTube上の課題または指標がない")
    if not action_clear:
        reasons.append("視聴後に取れる具体的なYouTube操作がない")
    if theme_fit != "clear":
        reasons.append("主題適合がclearではない")
    if not theme_fit_reason:
        reasons.append("主題適合の理由がない")
    if not subject_clear:
        reasons.append("企画・タイトルからYouTube主題を明確に確認できない")

    return ThemeAssessment(
        audience=audience,
        problem=problem,
        viewer_action=viewer_action,
        theme_fit=theme_fit or "missing",
        theme_fit_reason=theme_fit_reason,
        subject_clear=subject_clear,
        eligible_for_public=not reasons,
        reasons=tuple(reasons),
    )


def choose_privacy(
    spec: ChannelSpec,
    script: dict,
) -> tuple[str, ThemeAssessment | None]:
    """確認運用が有効なチャンネルだけ、主題適合の自動判定で公開設定を決める。"""
    if not spec.publish.youtube.review.enabled:
        return spec.publish.youtube.privacy, None
    assessment = assess(script)
    return assessment.privacy, assessment
