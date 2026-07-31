"""チャンネル別の生成履歴（重複回避・コーナーローテーション用）。"""
from __future__ import annotations

import fcntl
import json
import os
import re
import unicodedata
import uuid
from collections.abc import Mapping
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


def _latest_reservation_rows(rows: list[dict], now: datetime) -> dict[str, dict]:
    """現在時刻までに確定した予約状態だけで最新行を決める。

    壊れた時刻のterminal行や未来のterminal行が、結果不明のpublishingを
    見えなくして再利用を許さないようにする。時刻不明のpublishingだけは
    fail-closedで最新状態として残す。
    """
    latest: dict[str, tuple[int, datetime, dict]] = {}
    invalid_publishing: dict[str, tuple[int, dict]] = {}
    for index, row in enumerate(rows):
        reservation_id = str(row.get("reservation_id") or "")
        if not reservation_id:
            continue
        status = str(row.get("status") or "")
        timestamp = _parse_ts(row.get("ts"))
        if timestamp is None:
            if status == "publishing":
                invalid_publishing[reservation_id] = (index, row)
            continue
        if timestamp > now and status not in {"queued", "publishing"}:
            continue
        current = latest.get(reservation_id)
        if current is None or index > current[0]:
            latest[reservation_id] = (index, timestamp, row)
    for reservation_id, (index, row) in invalid_publishing.items():
        current = latest.get(reservation_id)
        if current is None or index > current[0]:
            latest[reservation_id] = (
                index,
                datetime.min.replace(tzinfo=timezone.utc),
                row,
            )
    return {
        reservation_id: state[2]
        for reservation_id, state in latest.items()
    }


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
    "scarcity": (
        r"物不足",
        r"食料不足",
        r"供給不足",
        r"日用品不足",
        r"品不足",
        r"欠乏",
        r"配給",
    ),
    "planned_economy": (
        r"計画経済",
        r"配給制度",
        r"統制経済",
        r"価格統制",
    ),
    "invisible_hand": (r"見えざる手", r"見えない手", r"アダムスミス"),
    "growth_worship": (r"成長", r"gdp", r"列車", r"エンジン", r"神様", r"宗教"),
    "wealth_inequality": (r"富の集中", r"上位1"),
    "inequality": (r"格差", r"不平等", r"貧富", r"階級差"),
    "tech_giant": (r"テック巨人", r"テック企業"),
    "more_desire": (r"もっと欲しい", r"衝動"),
    "consumption_desire": (r"消費欲", r"消費社会", r"欲望", r"贅沢", r"広告"),
}
_STRONG_TOPIC_CONCEPTS = frozenset(
    {"scarcity", "planned_economy", "inequality", "consumption_desire"}
)
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

_CONTINUATION_TYPES = frozenset(
    {"sequel", "opposing_view", "audience_adaptation"}
)
_TOPIC_METADATA_FIELDS = (
    "canonical_theme",
    "angle",
    "audience",
    "format",
    "novelty_type",
    "parent_topic",
    "parent_topic_id",
    "novelty_reason",
    "source",
    "viewpoint",
    "novelty_axis",
    "comparison_key",
)
_GENERIC_CANONICAL_THEMES = frozenset(
    {
        "youtube",
        "youtube運用",
        "youtube運営",
        "youtube攻略",
        "youtube成長",
        "youtubeの運用",
        "youtubeの成長",
        "youtubeショート",
        "動画",
        "動画制作",
        "動画運用",
        "動画改善",
        "コンテンツ",
        "コンテンツ制作",
        "歴史",
        "思想",
        "哲学",
        "政治",
        "経済",
        "社会",
        "文化",
        "制度",
        "資本主義",
        "資本主義ネタ",
        "共産主義",
        "社会主義",
    }
)
_GENERIC_CANONICAL_PARTS = (
    "youtube",
    "ユーチューブ",
    "チャンネル",
    "動画",
    "ショート",
    "コンテンツ",
    "運用",
    "運営",
    "攻略",
    "成長",
    "改善",
    "方法",
    "設計",
    "成功",
    "伸ばし方",
)
_GENERIC_DESCRIPTOR_PARTS = _GENERIC_CANONICAL_PARTS + (
    "解説",
    "確認",
    "比較",
    "理由",
    "ポイント",
    "コツ",
    "秘訣",
    "について",
    "とは",
    "する",
)


def _is_generic_descriptor(value: str) -> bool:
    normalised = _normalise_topic(value)
    remainder = normalised
    for part in _GENERIC_DESCRIPTOR_PARTS:
        remainder = remainder.replace(part, "")
    remainder = re.sub(
        r"^[のにをがはでと]+|[のにをがはでと]+$", "", remainder
    )
    return (
        normalised in _GENERIC_CANONICAL_THEMES
        or len(remainder) < 5
    )


def topic_concepts(value: str) -> list[str]:
    normalised = _normalise_topic(value)
    return sorted(
        concept
        for concept, patterns in _CONCEPT_PATTERNS.items()
        if any(re.search(pattern, normalised) for pattern in patterns)
    )


def topic_metadata(
    topic: str, metadata: Mapping[str, object] | None = None
) -> dict[str, str]:
    """題材台帳へ保存する bounded なメタデータを正規化する。"""
    raw = metadata if isinstance(metadata, Mapping) else {}

    def text(key: str, limit: int) -> str:
        value = raw.get(key)
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:limit]

    canonical_theme = text("canonical_theme", 300) or topic.strip()[:300]
    if _is_generic_descriptor(canonical_theme):
        canonical_theme = ""
    novelty_type = text("novelty_type", 40)
    if novelty_type not in {"new", *_CONTINUATION_TYPES}:
        # 欠落・不正な構造化出力を新規題材とみなすと、同じ題材を
        # 「新規」として通してしまう。未知のまま保存し、続編許可にも使わない。
        novelty_type = "unknown"
    return {
        "canonical_theme": canonical_theme,
        "angle": text("angle", 500),
        "audience": text("audience", 160)
        or text("youtube_creator_audience", 160),
        "format": text("format", 80),
        "novelty_type": novelty_type,
        "parent_topic": text("parent_topic", 300),
        "parent_topic_id": text("parent_topic_id", 120),
        "novelty_reason": text("novelty_reason", 500),
        "source": text("source", 80) or "research",
        "viewpoint": text("viewpoint", 160),
        "novelty_axis": text("novelty_axis", 40),
        "comparison_key": text("comparison_key", 200),
    }


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
        shared_concepts = left_concepts & right_concepts
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


def _row_topic_metadata(
    row: dict,
    topic: str | None = None,
    *,
    cache: dict[int, dict[str, str]] | None = None,
) -> dict[str, str]:
    """現行行・旧workdirのresearchから題材メタデータを復元する。"""
    cache_key = id(row)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    resolved_topic = topic or _row_topic(row)
    raw: dict[str, object] = {}
    nested = row.get("topic_metadata")
    if isinstance(nested, Mapping):
        raw.update(nested)
    for key in _TOPIC_METADATA_FIELDS:
        if key not in raw and key in row:
            raw[key] = row[key]
    workdir = row.get("workdir")
    if workdir:
        try:
            script = json.loads(
                (Path(str(workdir)) / "script.json").read_text(encoding="utf-8")
            )
            research = script.get("_research")
            if isinstance(research, Mapping):
                for source_key, target_key in (
                    ("canonical_theme", "canonical_theme"),
                    ("angle", "angle"),
                    ("youtube_creator_audience", "audience"),
                    ("format", "format"),
                    ("novelty_type", "novelty_type"),
                    ("parent_topic", "parent_topic"),
                    ("parent_topic_id", "parent_topic_id"),
                    ("novelty_reason", "novelty_reason"),
                    ("viewpoint", "viewpoint"),
                    ("novelty_axis", "novelty_axis"),
                    ("comparison_key", "comparison_key"),
                ):
                    if (
                        source_key in research
                        and (
                            target_key not in raw
                            or not str(raw.get(target_key) or "").strip()
                        )
                    ):
                        raw[target_key] = research[source_key]
        except (OSError, ValueError, TypeError):
            pass
    resolved = topic_metadata(resolved_topic, raw)
    if cache is not None:
        cache[cache_key] = resolved
    return resolved


def topic_match_similarity(
    topic: str,
    metadata: Mapping[str, object] | None,
    previous_topic: str,
    previous_row: dict,
    *,
    metadata_cache: dict[int, dict[str, str]] | None = None,
) -> float:
    """題材本文と大テーマの両方を使い、言い換えを見逃さない。"""
    current = topic_metadata(topic, metadata)
    previous = _row_topic_metadata(
        previous_row,
        previous_topic,
        cache=metadata_cache,
    )
    topic_score = topic_similarity(topic, previous_topic)
    score = topic_score
    current_theme = current["canonical_theme"]
    previous_theme = previous["canonical_theme"]
    theme_score = 0.0
    if len(current_theme) >= 4 and len(previous_theme) >= 4:
        theme_score = topic_similarity(current_theme, previous_theme)
    current_angle = current["angle"]
    previous_angle = previous["angle"]
    angle_score = 0.0
    if (
        len(current_angle) >= 4
        and len(previous_angle) >= 4
        and not _is_generic_descriptor(current_angle)
        and not _is_generic_descriptor(previous_angle)
    ):
        angle_score = topic_similarity(current_angle, previous_angle)
    # 同じ分野の強い概念が1語あるだけでは、異なる対象・切り口を
    # canonical_themeの重複として昇格させない。
    theme_supported = topic_score >= 0.55 or angle_score >= 0.55
    if theme_score >= 0.55 and theme_supported:
        score = max(score, theme_score)
    # angleはLLMが似た定型文を返しやすいため、本文またはcanonical_themeの
    # 裏付けなしに単独で重複扱いへ昇格させない。
    if angle_score >= 0.55 and (topic_score >= 0.55 or theme_score >= 0.55):
        score = max(score, angle_score)
    return score


def _stable_topic_id(row: dict) -> str:
    return str(
        row.get("video_id")
        or row.get("topic_ledger_reservation_id")
        or row.get("reservation_id")
        or ""
    ).strip()


def _resolve_parent_topic_id(
    metadata: Mapping[str, object] | None,
    candidates: list[tuple[dict, str, str]],
    similarity_threshold: float,
) -> str | None:
    """公開済み候補から親IDを内部解決する。曖昧なら続編を許可しない。"""
    current = topic_metadata("", metadata)
    if current["novelty_type"] not in _CONTINUATION_TYPES:
        return ""
    if current["parent_topic_id"]:
        return current["parent_topic_id"]
    if not current["parent_topic"]:
        return None
    matches: set[str] = set()
    for row, previous_topic, _source in candidates:
        status = str(row.get("status") or "")
        if not (
            status == "published"
            or (not status and bool(row.get("video_id")))
        ):
            continue
        stable_id = _stable_topic_id(row)
        if stable_id and topic_similarity(
            current["parent_topic"], previous_topic
        ) >= similarity_threshold:
            matches.add(stable_id)
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _continuation_allowed(
    topic: str,
    metadata: Mapping[str, object] | None,
    previous_row: dict,
    previous_topic: str,
    similarity_threshold: float,
    *,
    metadata_cache: dict[int, dict[str, str]] | None = None,
) -> bool:
    """明示された続編だけを、元題材との新規性が確認できる場合に許可する。"""
    current = topic_metadata(topic, metadata)
    novelty_type = current["novelty_type"]
    if novelty_type not in _CONTINUATION_TYPES:
        return False
    previous_status = str(previous_row.get("status") or "")
    previous_is_published = previous_status == "published" or (
        not previous_status and bool(previous_row.get("video_id"))
    )
    if not previous_is_published:
        # 制作中の親を続編扱いすると、親の失敗前に別動画を通せてしまう。
        return False
    previous_id = _stable_topic_id(previous_row)
    if not previous_id:
        return False
    if not current["parent_topic_id"]:
        return False
    if current["parent_topic_id"] and current["parent_topic_id"] != previous_id:
        return False
    if len(current["parent_topic"]) < 4 or len(current["novelty_reason"]) < 12:
        return False
    previous = _row_topic_metadata(
        previous_row,
        previous_topic,
        cache=metadata_cache,
    )
    if novelty_type == "opposing_view":
        if (
            current["novelty_axis"] != "stance"
            or not current["viewpoint"]
            or not current["comparison_key"]
            or not previous["comparison_key"]
        ):
            return False
        if previous["viewpoint"] and (
            _normalise_topic(current["viewpoint"])
            == _normalise_topic(previous["viewpoint"])
        ):
            return False
    elif novelty_type == "sequel":
        if (
            current["novelty_axis"] not in {"time", "case", "mechanism", "metric"}
            or not current["comparison_key"]
            or not previous["comparison_key"]
        ):
            return False
    if novelty_type in {"opposing_view", "sequel"} and topic_similarity(
        current["comparison_key"], previous["comparison_key"]
    ) >= similarity_threshold:
        return False
    if topic_similarity(current["parent_topic"], previous_topic) < similarity_threshold:
        return False
    previous_angle = previous["angle"]
    if current["angle"] and previous_angle:
        if topic_similarity(current["angle"], previous_angle) >= similarity_threshold:
            return False
    elif not current["angle"]:
        return False
    if novelty_type == "audience_adaptation":
        if not current["audience"] or not previous["audience"]:
            return False
        if topic_similarity(current["audience"], previous["audience"]) >= similarity_threshold:
            return False
    return True


def _semantic_match_allows_continuation(
    topic: str,
    metadata: Mapping[str, object] | None,
    match: TopicMatch,
    candidates: list[tuple[dict, str, str]],
    similarity_threshold: float,
    *,
    metadata_cache: dict[int, dict[str, str]] | None = None,
) -> bool:
    """意味判定の一致先が明示的な続編の親なら、重複扱いにしない。"""
    matched_key = _normalise_topic(match.topic)
    matching = [
        candidate
        for candidate in candidates
        if matched_key
        and _normalise_topic(candidate[1]) == matched_key
    ]
    if not matching and matched_key:
        # 意味判定側が入力を短縮して返す実装にも対応する。ただし、
        # 類似度の低い別候補まで続編の親と誤認しない。
        ranked = sorted(
            candidates,
            key=lambda candidate: topic_similarity(match.topic, candidate[1]),
            reverse=True,
        )
        if ranked and topic_similarity(match.topic, ranked[0][1]) >= 0.9:
            matching = [ranked[0]]
    return bool(matching) and all(
        _continuation_allowed(
            topic,
            metadata,
            row,
            previous_topic,
            similarity_threshold,
            metadata_cache=metadata_cache,
        )
        for row, previous_topic, _source in matching
    )


def _cooldown_candidates(
    rows: list[dict], *, now: datetime, cooldown_days: int
) -> list[tuple[dict, str, str]]:
    cutoff = now - timedelta(days=cooldown_days)
    candidates: list[tuple[dict, str, str]] = []
    latest_reservations = _latest_reservation_rows(rows, now)
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        status = str(row.get("status") or "")
        if ts is None:
            if status != "publishing":
                continue
        elif ts > now and status not in {"queued", "publishing"}:
            continue
        elif status != "publishing" and ts < cutoff:
            continue
        reservation_id = str(row.get("reservation_id") or "")
        if reservation_id and latest_reservations.get(reservation_id) is not row:
            continue
        is_published = status == "published" or (not status and bool(row.get("video_id")))
        is_queued = status in {"queued", "publishing"}
        if status == "queued" and not _queued_reservation_is_active(row, ts, now):
            continue
        if not (is_published or is_queued):
            continue
        topic = _row_topic(row)
        if topic:
            label = (
                "公開済み"
                if is_published
                else "投稿処理中"
                if status == "publishing"
                else "キュー済み"
            )
            candidates.append((row, topic, label))
    return candidates


def _queued_reservation_is_active(
    row: dict,
    timestamp: datetime | None,
    now: datetime,
) -> bool:
    """孤児予約を無期限に題材cooldownへ残さず、PID名前空間にも依存しない。"""
    if timestamp is None:
        return True
    from . import config

    if str(row.get("status") or "") == "publishing":
        return True
    ttl_hours = config.TOPIC_RESERVATION_TTL_HOURS
    if ttl_hours > 0 and timestamp < now - timedelta(hours=ttl_hours):
        return False
    # 旧履歴にowner_pidが残っていても、共有コンテナ・ホスト間でPIDを
    # 解釈しない。稼働中runを早期に孤児扱いして重複予約を許すより、
    # 明示的なTTLでのみqueued予約を解放する。
    return True


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
    metadata: Mapping[str, object] | None = None,
    topic_ledger_reservation_id: str | None = None,
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
    raw_metadata = metadata if isinstance(metadata, dict) else None
    metadata = topic_metadata(topic, metadata)
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
        resolved_parent_id = _resolve_parent_topic_id(
            metadata,
            candidates,
            similarity_threshold,
        )
        if resolved_parent_id and not metadata["parent_topic_id"]:
            if raw_metadata is not None:
                raw_metadata["parent_topic_id"] = resolved_parent_id
            metadata = {**metadata, "parent_topic_id": resolved_parent_id}
        best: TopicMatch | None = None
        metadata_cache: dict[int, dict[str, str]] = {}
        for row, previous_topic, source in candidates:
            similarity = topic_match_similarity(
                topic,
                metadata,
                previous_topic,
                row,
                metadata_cache=metadata_cache,
            )
            if similarity < similarity_threshold:
                continue
            if _continuation_allowed(
                topic,
                metadata,
                row,
                previous_topic,
                similarity_threshold,
                metadata_cache=metadata_cache,
            ):
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
                key = _normalise_topic(previous_topic)
                if not key or key in seen_topics:
                    continue
                seen_topics.add(key)
                recent_topics.append(previous_topic)
            semantic_match = semantic_check(topic, recent_topics)
            if semantic_match is not None and not _semantic_match_allows_continuation(
                topic,
                metadata,
                semantic_match,
                candidates,
                similarity_threshold,
                metadata_cache=metadata_cache,
            ):
                best = semantic_match
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
                    "topic_metadata": metadata,
                    **metadata,
                    "skip_reason": exc.reason,
                    "matched_topic": best.topic,
                    "matched_ts": best.ts,
                    "similarity": round(best.similarity, 4),
                }
                file.seek(0, 2)
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
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
                    "topic_metadata": metadata,
                    **metadata,
                    "reservation_id": reservation_id,
                    "topic_ledger_reservation_id": topic_ledger_reservation_id,
        }
        file.seek(0, 2)
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())
        return reservation_id


def cancel_topic(
    spec: ChannelSpec,
    corner: str,
    topic: str,
    reservation_id: str,
    reason: str,
    metadata: Mapping[str, object] | None = None,
    topic_ledger_reservation_id: str | None = None,
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
            "topic_metadata": topic_metadata(topic, metadata),
            **topic_metadata(topic, metadata),
            "reservation_id": reservation_id,
            "topic_ledger_reservation_id": topic_ledger_reservation_id,
            "cancel_reason": reason[:500],
        },
    )


def mark_topic_publishing(
    spec: ChannelSpec,
    corner: str,
    topic: str,
    reservation_id: str,
    metadata: Mapping[str, object] | None = None,
    topic_ledger_reservation_id: str | None = None,
) -> None:
    """外部投稿を開始する前に、結果不明でも題材をfail-closedにする。"""
    if not reservation_id:
        return
    record(
        spec,
        corner,
        "",
        extra={
            "status": "publishing",
            "topic": topic,
            "topic_concepts": topic_concepts(topic),
            "topic_metadata": topic_metadata(topic, metadata),
            **topic_metadata(topic, metadata),
            "reservation_id": reservation_id,
            "topic_ledger_reservation_id": topic_ledger_reservation_id,
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
        os.fsync(file.fileno())
        os.fsync(file.fileno())
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
        os.fsync(file.fileno())
