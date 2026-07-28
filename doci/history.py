"""チャンネル別の生成履歴（重複回避・コーナーローテーション用）。"""
from __future__ import annotations

import fcntl
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .channel import ChannelSpec


@dataclass(frozen=True)
class TopicMatch:
    topic: str
    ts: str
    similarity: float
    source: str


class TopicCooldownSkip(RuntimeError):
    """直近の公開済み/キュー済み題材と重複したため、今回の制作をスキップする。"""

    def __init__(self, topic: str, match: TopicMatch, cooldown_days: int):
        self.topic = topic
        self.match = match
        self.cooldown_days = cooldown_days
        self.reason = (
            f"題材「{topic}」は過去{cooldown_days}日以内の"
            f"{match.source}題材「{match.topic}」と実質的に重複"
            f"（類似度 {match.similarity:.2f}）"
        )
        super().__init__(self.reason)


def _read_path(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_all(spec: ChannelSpec) -> list[dict]:
    return _read_path(spec.history_file)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_topic(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"\bctr\b", "クリック率", text)
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠]+", "", text)


_CONCEPT_PATTERNS = {
    "click_through_rate": (r"クリック率", r"clickthroughrate"),
    "thumbnail": (r"サムネ", r"thumbnail"),
    "title": (r"タイトル",),
    "retention": (
        r"視聴維持",
        r"平均視聴時間",
        r"冒頭30秒",
        r"冒頭三十秒",
        r"離脱",
        r"retention",
    ),
    "traffic_source": (r"流入元", r"トラフィックソース"),
    "impressions": (r"インプレッション",),
    "analytics": (r"アナリティクス", r"studio"),
    "shorts": (r"ショート", r"shorts"),
    "related_video": (r"関連動画",),
    "subscriber": (r"登録者", r"チャンネル登録"),
    "ab_test": (r"abテスト", r"テストと比較"),
    # ideologyチャンネル(資本主義/共産主義)向け。比喩を変えた使い回しは技術用語を含まないため、
    # YouTube向け概念だけでは一致せず素通りしてしまう（実測で確認済み）。
    "utopian_ideal": (
        r"楽園", r"理想郷", r"完璧な平等", r"みんなで幸せ", r"全員が同じ",
        r"誰もが.{0,4}平等", r"誰か.{0,6}平等", r"よき社会", r"青図", r"設計図", r"天国",
    ),
    "tragedy_sacrifice": (r"犠牲", r"悲劇", r"地獄", r"暴落", r"泥濘", r"喰らう"),
    "nordic_comparison": (r"北欧",),
    "planned_economy": (r"計画経済",),
    "invisible_hand": (r"見えざる手", r"見えない手", r"アダムスミス"),
    "growth_worship": (r"成長", r"gdp", r"列車", r"エンジン", r"神様", r"宗教"),
    "wealth_inequality": (r"格差", r"富の集中", r"上位1"),
    "tech_giant": (r"テック巨人", r"テック企業"),
    "more_desire": (r"もっと欲しい", r"欲望", r"衝動"),
}
_BOILERPLATE = (
    "初心者向け",
    "youtube",
    "ユーチューブ",
    "チャンネル",
    "動画",
    "伸ばし方",
    "改善",
    "方法",
    "設計",
    "使い方",
    "解説",
    "本当の理由",
)


def topic_concepts(value: str) -> list[str]:
    normalised = _normalise_topic(value)
    return sorted(
        concept
        for concept, patterns in _CONCEPT_PATTERNS.items()
        if any(re.search(pattern, normalised) for pattern in patterns)
    )


def _topic_fingerprint(value: str) -> str:
    normalised = _normalise_topic(value)
    canonical_groups = {
        "retention": (
            "視聴維持率",
            "視聴維持",
            "平均視聴時間",
            "冒頭30秒",
            "冒頭三十秒",
            "離脱",
        ),
        "clickrate": ("クリック率", "clickthroughrate"),
        "trafficsource": ("トラフィックソース", "流入元"),
        "relatedvideo": ("関連動画",),
    }
    for canonical, phrases in canonical_groups.items():
        for phrase in phrases:
            normalised = normalised.replace(phrase, canonical)
    for phrase in _BOILERPLATE:
        normalised = normalised.replace(phrase, "")
    return normalised


def topic_similarity(left: str, right: str) -> float:
    """YouTube領域の概念タグと表記揺れを併用する類似度。"""
    a = _topic_fingerprint(left)
    b = _topic_fingerprint(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if min(len(a), len(b)) >= 10 and (a in b or b in a):
        return 0.98
    if min(len(a), len(b)) < 8:
        return SequenceMatcher(None, a, b).ratio()
    a_grams = {a[index : index + 2] for index in range(len(a) - 1)}
    b_grams = {b[index : index + 2] for index in range(len(b) - 1)}
    overlap = len(a_grams & b_grams)
    containment = overlap / min(len(a_grams), len(b_grams))
    sequence = SequenceMatcher(None, a, b).ratio()
    left_concepts = set(topic_concepts(left))
    right_concepts = set(topic_concepts(right))
    concept_score = 0.0
    if left_concepts and right_concepts:
        concept_overlap = len(left_concepts & right_concepts)
        if concept_overlap and len(left_concepts | right_concepts) >= 2:
            # 共通する一般概念が1つあるだけで別題材を止めない。集合全体に占める
            # 一致率（Jaccard）で、同じ主題構造のときだけ強く判定する。
            concept_score = concept_overlap / len(left_concepts | right_concepts)
    return max(containment, sequence, concept_score)


def _row_topic(row: dict) -> str:
    topic = str(row.get("topic") or "").strip()
    if topic:
        return topic
    workdir = row.get("workdir")
    if workdir:
        try:
            script = json.loads((Path(str(workdir)) / "script.json").read_text(encoding="utf-8"))
            topic = str((script.get("_research") or {}).get("topic") or "").strip()
            if topic:
                return topic
        except (OSError, ValueError, TypeError):
            pass
    title = str(row.get("title") or "").strip()
    description = str(row.get("description") or "").split("\n", 1)[0].strip()
    return f"{title} {description}".strip()


def _cooldown_candidates(
    rows: list[dict], *, now: datetime, cooldown_days: int
) -> list[tuple[dict, str, str]]:
    cutoff = now - timedelta(days=cooldown_days)
    candidates: list[tuple[dict, str, str]] = []
    latest_reservations: dict[str, dict] = {}
    for row in rows:
        reservation_id = str(row.get("reservation_id") or "")
        if reservation_id:
            latest_reservations[reservation_id] = row
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < cutoff or ts > now:
            continue
        status = str(row.get("status") or "")
        reservation_id = str(row.get("reservation_id") or "")
        if reservation_id and latest_reservations.get(reservation_id) is not row:
            continue
        is_published = status == "published" or (not status and bool(row.get("video_id")))
        is_queued = status == "queued"
        if not (is_published or is_queued):
            continue
        topic = _row_topic(row)
        if topic:
            candidates.append((row, topic, "公開済み" if is_published else "キュー済み"))
    return candidates


def reserve_topic(
    spec: ChannelSpec,
    corner: str,
    topic: str,
    *,
    cooldown_days: int,
    reserve: bool = True,
    now: datetime | None = None,
    similarity_threshold: float = 0.55,
    semantic_check: (
        Callable[[str, list[str]], TopicMatch | None] | None
    ) = None,
) -> str | None:
    """題材を原子的に照合し、実投稿runならキューとして予約する。

    重複時はスキップ行も同じロック内で追記するため、並行runでも
    「照合は双方通過したが同じ題材を予約した」という競合を起こさない。
    semantic_check は語彙が一致しない言い換え重複（比喩を変えただけ等）を
    LLMで補助判定するための差し込み口。文字列照合が0件のときだけ呼ぶ。
    """
    if cooldown_days <= 0:
        return None
    topic = topic.strip()
    if not topic:
        raise ValueError("cooldown判定に使う題材が空です")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = spec.history_file
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        file.seek(0)
        rows: list[dict] = []
        for line in file:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        candidates = _cooldown_candidates(
            rows, now=current, cooldown_days=cooldown_days
        )
        best: TopicMatch | None = None
        for row, previous_topic, source in candidates:
            similarity = topic_similarity(topic, previous_topic)
            if similarity < similarity_threshold:
                continue
            candidate = TopicMatch(
                topic=previous_topic,
                ts=str(row.get("ts") or ""),
                similarity=similarity,
                source=source,
            )
            if best is None or candidate.similarity > best.similarity:
                best = candidate
        if best is None and semantic_check is not None:
            seen_topics: set[str] = set()
            recent_topics: list[str] = []
            for _row, previous_topic, _source in reversed(candidates):
                if previous_topic in seen_topics:
                    continue
                seen_topics.add(previous_topic)
                recent_topics.append(previous_topic)
            best = semantic_check(topic, recent_topics)
        if best is not None:
            exc = TopicCooldownSkip(topic, best, cooldown_days)
            if reserve:
                row = {
                    "ts": current.isoformat(),
                    "channel": spec.id,
                    "corner": corner,
                    "title": "",
                    "video_id": None,
                    "status": "skipped",
                    "topic": topic,
                    "topic_concepts": topic_concepts(topic),
                    "skip_reason": exc.reason,
                    "matched_topic": best.topic,
                    "matched_ts": best.ts,
                    "similarity": round(best.similarity, 4),
                }
                file.seek(0, 2)
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
                file.flush()
            raise exc
        if not reserve:
            return None
        reservation_id = uuid.uuid4().hex
        row = {
            "ts": current.isoformat(),
            "channel": spec.id,
            "corner": corner,
            "title": "",
            "video_id": None,
            "status": "queued",
            "topic": topic,
            "topic_concepts": topic_concepts(topic),
            "reservation_id": reservation_id,
        }
        file.seek(0, 2)
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        return reservation_id


def cancel_topic(
    spec: ChannelSpec,
    corner: str,
    topic: str,
    reservation_id: str,
    reason: str,
) -> None:
    """制作失敗または投稿なしの予約を無効化する状態遷移を追記する。"""
    record(
        spec,
        corner,
        "",
        extra={
            "status": "cancelled",
            "topic": topic,
            "topic_concepts": topic_concepts(topic),
            "reservation_id": reservation_id,
            "cancel_reason": reason[:500],
        },
    )


def _performance_decision_used_rows(rows: list[dict], decision_id: str) -> bool:
    latest: dict[str, dict] = {}
    for row in rows:
        if str(row.get("performance_decision_id") or "") != decision_id:
            continue
        application_id = str(row.get("performance_application_id") or "")
        if application_id:
            latest[application_id] = row
    return any(
        str(row.get("status") or "")
        in {
            "performance_queued",
            "performance_applied",
            "performance_evaluated",
            "generated",
            "published",
        }
        for row in latest.values()
    )


def _active_performance_experiment_rows(
    rows: list[dict],
    corner: str,
) -> dict | None:
    seen: set[str] = set()
    for row in reversed(rows):
        application_id = str(row.get("performance_application_id") or "")
        if not application_id or application_id in seen:
            continue
        seen.add(application_id)
        if row.get("corner") != corner:
            continue
        if str(row.get("status") or "") in {
            "performance_queued",
            "performance_applied",
            "published",
        }:
            return row
    return None


def active_performance_experiment(
    spec: ChannelSpec,
    corner: str,
) -> dict | None:
    """cornerで適用中または評価待ちの実験を返す。"""
    return _active_performance_experiment_rows(_read_all(spec), corner)


def performance_decision_used(spec: ChannelSpec, decision_id: str) -> bool:
    """同じ実績snapshot由来の仮説が予約済みまたは1本へ適用済みか返す。"""
    return _performance_decision_used_rows(_read_all(spec), decision_id)


def reserve_performance_decision(
    spec: ChannelSpec,
    corner: str,
    decision_id: str,
) -> str | None:
    """同一decisionを複数runへ適用しないよう、原子的に適用枠を予約する。"""
    path = spec.history_file
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        file.seek(0)
        rows: list[dict] = []
        for line in file:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if (
            _performance_decision_used_rows(rows, decision_id)
            or _active_performance_experiment_rows(rows, corner)
        ):
            return None
        application_id = uuid.uuid4().hex
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": spec.id,
            "corner": corner,
            "title": "",
            "video_id": None,
            "status": "performance_queued",
            "performance_decision_id": decision_id,
            "performance_application_id": application_id,
        }
        file.seek(0, 2)
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        return application_id


def cancel_performance_decision(
    spec: ChannelSpec,
    corner: str,
    decision_id: str,
    application_id: str,
    reason: str,
) -> None:
    """動画が完成しなかったdecision適用予約を再利用可能に戻す。"""
    record(
        spec,
        corner,
        "",
        extra={
            "status": "performance_cancelled",
            "performance_decision_id": decision_id,
            "performance_application_id": application_id,
            "cancel_reason": reason[:500],
        },
    )


def apply_performance_decision(
    spec: ChannelSpec,
    corner: str,
    decision_id: str,
    application_id: str,
    video_id: str,
) -> None:
    """decision適用先のYouTube動画を、通常履歴保存とは独立して確定する。"""
    record(
        spec,
        corner,
        "",
        video_id,
        extra={
            "status": "performance_applied",
            "performance_decision_id": decision_id,
            "performance_application_id": application_id,
        },
    )


def complete_performance_evaluation(
    spec: ChannelSpec,
    applied: dict,
) -> None:
    """評価閾値に到達した実験を完了し、cornerの次実験を解禁する。"""
    record(
        spec,
        str(applied.get("corner") or ""),
        "",
        str(applied.get("video_id") or "") or None,
        extra={
            "status": "performance_evaluated",
            "performance_decision_id": applied.get("performance_decision_id"),
            "performance_application_id": applied.get(
                "performance_application_id"
            ),
        },
    )


def _completed_rows(spec: ChannelSpec) -> list[dict]:
    completed = []
    for row in _read_all(spec):
        status = str(row.get("status") or "")
        if status in {"published", "generated"} or (
            not status and bool(row.get("title"))
        ):
            completed.append(row)
    return completed


def last_corner(spec: ChannelSpec) -> str | None:
    rows = _completed_rows(spec)
    return rows[-1].get("corner") if rows else None


def last_run(spec: ChannelSpec) -> dict | None:
    """チャンネルの直近実行レコードを返す。履歴なしなら None。"""
    rows = _completed_rows(spec)
    return rows[-1] if rows else None


def recent_topics(spec: ChannelSpec, limit: int = 30) -> list[str]:
    rows = _read_all(spec)
    topics: list[str] = []
    for row in rows:
        title = row.get("title", "")
        if not title:
            continue
        desc_line = (row.get("description") or "").split("\n", 1)[0].strip()
        topics.append(f"{title}（{desc_line}）" if desc_line else title)
    return topics[-limit:]


def recent_titles(
    spec: ChannelSpec,
    limit: int = 30,
    *,
    cooldown_days: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """重複回避プロンプト用に題名だけを返す。

    description まで全件結合するとOpenCode Goゲートウェイが長い入力をHTTP 500にするため、
    題材の重複判定に必要なtitleだけを本文生成へ渡す。cooldown_daysを渡すと、
    件数ではなくcooldown判定と同じ日数窓（既定30日）で絞ってからlimit件に切り詰める。
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current - timedelta(days=cooldown_days) if cooldown_days else None
    titles: list[str] = []
    for row in _read_all(spec):
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        if cutoff is not None:
            ts = _parse_ts(row.get("ts"))
            if ts is not None and (ts < cutoff or ts > current):
                continue
        titles.append(title)
    return titles[-limit:]


def cooldown_window_topics(
    spec: ChannelSpec,
    *,
    cooldown_days: int,
    now: datetime | None = None,
) -> list[str]:
    """過去cooldown_days日以内の公開済み/キュー済み題材を新しい順・重複なしで返す。

    語彙一致に頼らない意味的重複判定（LLM）へ渡す候補一覧として使う。
    """
    if cooldown_days <= 0:
        return []
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = _cooldown_candidates(
        _read_all(spec), now=current, cooldown_days=cooldown_days
    )
    seen: set[str] = set()
    topics: list[str] = []
    for _row, topic, _source in reversed(candidates):
        if topic in seen:
            continue
        seen.add(topic)
        topics.append(topic)
    return topics


def record(
    spec: ChannelSpec,
    corner: str,
    title: str,
    video_id: str | None = None,
    extra: dict | None = None,
) -> None:
    path = spec.history_file
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": spec.id,
        "corner": corner,
        "title": title,
        "video_id": video_id,
    }
    if extra:
        row.update(extra)
    with path.open("a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
