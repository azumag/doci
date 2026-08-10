"""YouTube攻略Chの主題ガード（企画の主題適合を判定するだけの純粋関数群）。

自動公開の可否は厳格な企画項目だけで判定し、時間経過を公開可否の理由には
しない。GitHub Issueでの人手承認・ラベル待ち・reconcileの仕組みは存在しない。
`review.enabled = false` のチャンネルは常に `publish.youtube.privacy` の
静的な値をそのまま使う。`review.enabled = true` のチャンネルは、この主題判定
（`assess()`）の結果だけでpublic/unlistedを都度決める。
"""
from __future__ import annotations

import re
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
_CONTENT_GAP_MARKERS = (
    "コンテンツギャップ",
    "コンテンツ ギャップ",
    "content gap",
)
_CONTENT_GAP_MISLEADING_MARKERS = (
    "検索されていないテーマ",
    "検索されていない答え",
    "検索されてないテーマ",
    "検索されてない答え",
    "検索されていない話題",
    "検索されてない話題",
    "検索されていない題材",
    "検索されてない題材",
)
_PAUSE_SEQUENCE_RE = re.compile(r"[…—―]+")
_SHORTS_REQUIRED_PAUSE_COUNT = 3
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
    "検索流入",
    "検索語句",
    "コンテンツギャップ",
    "コンテンツ ギャップ",
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
    "検索流入",
    "検索語句",
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
    "選び",
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


def _misleading_content_gap(gap_query: str, *values: str) -> bool:
    """コンテンツギャップを「検索されていないテーマ」と誤解する表現を検出する。

    issue #164: 公式の説明ではコンテンツギャップは「検索されているのに十分な
    結果がない検索領域」であり、「検索されていないテーマ」ではない。誤解表現が
    タイトル等に含まれる場合は自動公開にしない（推測・断定を公開経路へ通さない）。
    `gap_query` が非空の企画はコンテンツギャップ企画として扱い、本文に
    「コンテンツギャップ」という語が無くても誤解表現を検出する。
    """
    joined = " ".join(values).casefold()
    gap_context = _contains_any(joined, _CONTENT_GAP_MARKERS) or bool(
        str(gap_query or "").strip()
    )
    if not gap_context:
        return False
    return any(marker in joined for marker in _CONTENT_GAP_MISLEADING_MARKERS)


def _pause_count(narration: str) -> int:
    """narrationに含まれる「情報を留める間」の箇所数を数える（issue #150）。

    連続した休止記号（…—―の並び）を1箇所として数え、本文を挟んだ別々の
    一致だけを加算する。`……` 1回が「3箇所」にならないよう、記号の文字数では
    数えない。
    """
    if not narration:
        return 0
    return len(_PAUSE_SEQUENCE_RE.findall(narration))


def _subject_reason(
    gap_query: str,
    shorts_pause_clear: bool,
    problem: str,
    viewer_action: str,
    topic: str,
    angle: str,
    title: str,
    description: str,
    narration: str,
) -> str:
    if _misleading_content_gap(
        gap_query,
        problem,
        viewer_action,
        topic,
        angle,
        title,
        description,
        narration,
    ):
        return "コンテンツギャップを「検索されていないテーマ」と誤解する表現がある"
    if not shorts_pause_clear:
        return "ショート台本に情報を留める間（休止表現）が3箇所ありません"
    return "企画・タイトルからYouTube主題を明確に確認できない"


def assess(script: dict, corner_key: str | None = None) -> ThemeAssessment:
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
    gap_query = _text(research.get("gap_query"), limit=200)
    shorts_pause_clear = True
    if corner_key == "shorts":
        shorts_pause_clear = _pause_count(narration) >= _SHORTS_REQUIRED_PAUSE_COUNT

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
        and shorts_pause_clear
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
        and not _misleading_content_gap(
            gap_query,
            problem,
            viewer_action,
            topic,
            angle,
            title,
            description,
            narration,
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
        reasons.append(_subject_reason(
            gap_query,
            shorts_pause_clear,
            problem,
            viewer_action,
            topic,
            angle,
            title,
            description,
            narration,
        ))

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
    corner_key: str | None = None,
) -> tuple[str, ThemeAssessment | None]:
    """確認運用が有効なチャンネルだけ、主題適合の自動判定で公開設定を決める。"""
    if not spec.publish.youtube.review.enabled:
        return spec.publish.youtube.privacy, None
    assessment = assess(script, corner_key=corner_key)
    return assessment.privacy, assessment
