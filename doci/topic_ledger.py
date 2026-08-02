"""全チャネル共通の題材台帳。

チャネル別 history.jsonl を置き換えず、公開済み・キュー済み題材を横断して
照合する。新しい予約だけを追記し、既存履歴は読み取り時に統合する。
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import channel, config, history


@dataclass(frozen=True)
class LedgerCandidate:
    row: dict
    topic: str
    source: str


class TopicLedgerCorruptError(RuntimeError):
    """台帳のJSONLが壊れており、重複判定を安全に続けられない。"""


class DailyUploadLimitSkip(RuntimeError):
    """チャンネルの日次実投稿枠を使い切ったため、生成をスキップする。"""

    def __init__(self, channel_id: str, limit: int, local_day: str):
        self.channel_id = channel_id
        self.limit = limit
        self.local_day = local_day
        self.reason = (
            f"channel={channel_id} はJST {local_day} の実投稿枠"
            f"（{limit}本）を使用済みのため、追加生成をスキップ"
        )
        super().__init__(self.reason)


_JST = ZoneInfo("Asia/Tokyo")


def ledger_path() -> Path:
    """環境変数・テスト用patch後のOUTPUTを毎回解決する。"""
    return config.OUTPUT / "topic_ledger.jsonl"


def _lock_path() -> Path:
    return config.OUTPUT / ".topic_ledger.lock"


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TopicLedgerCorruptError(
                f"共通題材台帳のJSONLが壊れています: {path}:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise TopicLedgerCorruptError(
                f"共通題材台帳の行がオブジェクトではありません: {path}:{line_number}"
            )
        rows.append(row)
    return rows


def _ledger_candidates(
    rows: list[dict], *, now: datetime, cooldown_days: int
) -> list[LedgerCandidate]:
    cutoff = now - timedelta(days=cooldown_days)
    latest_reservations = history._latest_reservation_rows(rows, now)
    candidates: list[LedgerCandidate] = []
    for row in rows:
        timestamp = history._parse_ts(row.get("ts"))
        status = str(row.get("status") or "")
        if timestamp is None:
            if status != "publishing":
                continue
        elif timestamp > now and status not in {"queued", "publishing"}:
            continue
        elif status != "publishing" and timestamp < cutoff:
            continue
        reservation_id = str(row.get("reservation_id") or "")
        if reservation_id and latest_reservations.get(reservation_id) is not row:
            continue
        is_published = status == "published" or (
            not status and bool(row.get("video_id"))
        )
        if status not in {"queued", "publishing"} and not is_published:
            continue
        if status == "queued" and not history._queued_reservation_is_active(
            row, timestamp, now
        ):
            continue
        topic = history._row_topic(row)
        if not topic:
            continue
        channel_id = str(row.get("channel") or "unknown")
        label = (
            "公開済み"
            if is_published
            else "投稿処理中"
            if status == "publishing"
            else "キュー済み"
        )
        candidates.append(
            LedgerCandidate(row, topic, f"共通台帳({channel_id}/{label})")
        )
    return candidates


def _legacy_candidates(*, now: datetime, cooldown_days: int) -> list[LedgerCandidate]:
    """既存チャネル履歴を壊さず、共通照合用の候補として読む。"""
    candidates: list[LedgerCandidate] = []
    for channel_id in channel.discover():
        try:
            spec = channel.load(channel_id)
            rows = history._read_path(spec.history_file)
        except (OSError, ValueError, TypeError):
            continue
        for row, topic, status_label in history._cooldown_candidates(
            rows, now=now, cooldown_days=cooldown_days
        ):
            candidates.append(
                LedgerCandidate(
                    row,
                    topic,
                    f"既存履歴({channel_id}/{status_label})",
                )
            )
    return candidates


def recent_topics(
    *,
    limit: int = 20,
    cooldown_days: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """プロンプトへ渡す、全チャネルの直近題材を重複なく返す。"""
    if limit <= 0:
        return []
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window = (
        config.TOPIC_COOLDOWN_DAYS
        if cooldown_days is None
        else cooldown_days
    )
    if window <= 0:
        return []
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        candidates = _ledger_candidates(
            _read_rows(ledger_path()), now=current, cooldown_days=window
        )
        candidates.extend(_legacy_candidates(now=current, cooldown_days=window))
    candidates.sort(
        key=lambda candidate: history._parse_ts(candidate.row.get("ts"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    topics: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = history._normalise_topic(candidate.topic)
        if not key or key in seen:
            continue
        seen.add(key)
        topics.append(candidate.topic)
        if len(topics) >= limit:
            break
    return topics


def _append(file, row: dict) -> None:  # type: ignore[no-untyped-def]
    file.seek(0, 2)
    file.write(json.dumps(row, ensure_ascii=False) + "\n")
    file.flush()
    os.fsync(file.fileno())


def _max_uploads_per_day(spec: channel.ChannelSpec) -> int:
    getter = getattr(spec, "pipeline_get", None)
    if callable(getter):
        value = getter("max_uploads_per_day", 0)
    else:
        pipeline = getattr(spec, "pipeline", {})
        value = (
            pipeline.get("max_uploads_per_day", 0)
            if isinstance(pipeline, Mapping)
            else 0
        )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value


def _daily_upload_key(row: dict) -> str:
    video_id = str(row.get("video_id") or "").strip()
    if video_id:
        return f"video:{video_id}"
    ledger_reservation_id = str(
        row.get("topic_ledger_reservation_id") or ""
    ).strip()
    if ledger_reservation_id:
        return f"ledger:{ledger_reservation_id}"
    reservation_id = str(row.get("reservation_id") or "").strip()
    if reservation_id:
        # 共通台帳では reservation_id、チャネル履歴では
        # topic_ledger_reservation_id が同じ相関IDになる。
        return f"ledger:{reservation_id}"
    topic = history._row_topic(row)
    normalised_topic = history._normalise_topic(topic)
    if normalised_topic:
        return f"topic:{normalised_topic}"
    return "row:" + str(id(row))


def _daily_upload_correlation_ids(row: dict) -> tuple[str, ...]:
    values = (
        str(row.get("topic_ledger_reservation_id") or "").strip(),
        str(row.get("reservation_id") or "").strip(),
    )
    return tuple(dict.fromkeys(value for value in values if value))


def _daily_upload_days(rows: list[dict]) -> dict[str, str]:
    days: dict[str, str] = {}
    for row in rows:
        day = str(row.get("daily_upload_day") or "").strip()
        if not day:
            continue
        for correlation_id in _daily_upload_correlation_ids(row):
            days.setdefault(correlation_id, day)
    return days


def _daily_upload_keys(
    rows: list[dict],
    *,
    channel_id: str,
    current: datetime,
    reservation_days: Mapping[str, str] | None = None,
) -> set[str]:
    local_day = current.astimezone(_JST).date()
    latest_reservations = history._latest_reservation_rows(rows, current)
    keys: set[str] = set()
    for row in rows:
        if str(row.get("channel") or "") != channel_id:
            continue
        timestamp = history._parse_ts(row.get("ts"))
        status = str(row.get("status") or "")
        if (
            timestamp is not None
            and timestamp > current
            and status not in {"queued", "publishing"}
        ):
            continue
        reservation_id = str(row.get("reservation_id") or "")
        if reservation_id and latest_reservations.get(reservation_id) is not row:
            continue
        active = status in {"published", "publishing"} or (
            not status and bool(row.get("video_id"))
        )
        if status == "queued":
            active = history._queued_reservation_is_active(row, timestamp, current)
        if status in {"queued", "publishing"}:
            # 日付をまたいで制作・投稿中の予約も、結果が確定するまで
            # 次のJST枠を使わせない。外部結果不明のpublishingは手動確認まで保持する。
            if active:
                keys.add(_daily_upload_key(row))
            continue
        if timestamp is None:
            continue
        reservation_day = str(row.get("daily_upload_day") or "").strip()
        # 新しいpublished行は公開時点の日付を持つ。旧形式のpublished行も
        # 予約日の引継ぎではなく終端行の時刻へフォールバックさせる。
        if not reservation_day and reservation_days is not None and status != "published":
            reservation_day = next(
                (
                    reservation_days[correlation_id]
                    for correlation_id in _daily_upload_correlation_ids(row)
                    if correlation_id in reservation_days
                ),
                "",
            )
        effective_day = reservation_day or timestamp.astimezone(_JST).date().isoformat()
        if effective_day != local_day.isoformat():
            continue
        if active:
            keys.add(_daily_upload_key(row))
    return keys


def _daily_upload_keys_for_spec(
    ledger_rows: list[dict],
    spec: channel.ChannelSpec,
    *,
    current: datetime,
) -> set[str]:
    history_file = getattr(spec, "history_file", None)
    legacy_rows: list[dict] = []
    if isinstance(history_file, Path):
        try:
            legacy_rows = history._read_path(history_file)
        except OSError:
            legacy_rows = []
    reservation_days = _daily_upload_days(ledger_rows + legacy_rows)
    keys = _daily_upload_keys(
        ledger_rows,
        channel_id=spec.id,
        current=current,
        reservation_days=reservation_days,
    )
    if legacy_rows:
        keys.update(
            _daily_upload_keys(
                legacy_rows,
                channel_id=spec.id,
                current=current,
                reservation_days=reservation_days,
            )
        )
    return keys


def _candidate_sort_key(candidate: LedgerCandidate) -> datetime:
    return history._parse_ts(candidate.row.get("ts")) or datetime.min.replace(
        tzinfo=timezone.utc
    )


def _collect_candidates(
    rows: list[dict], *, now: datetime, cooldown_days: int
) -> list[LedgerCandidate]:
    candidates = _ledger_candidates(rows, now=now, cooldown_days=cooldown_days)
    candidates.extend(_legacy_candidates(now=now, cooldown_days=cooldown_days))
    candidates.sort(key=_candidate_sort_key, reverse=True)
    return candidates


def _resolve_topic_metadata(
    topic: str,
    topic_data: dict[str, str],
    raw_metadata: dict[str, object] | None,
    candidates: list[LedgerCandidate],
    similarity_threshold: float,
) -> dict[str, str]:
    resolved_parent_id = history._resolve_parent_topic_id(
        topic_data,
        [
            (candidate.row, candidate.topic, candidate.source)
            for candidate in candidates
        ],
        similarity_threshold,
    )
    if resolved_parent_id and not topic_data["parent_topic_id"]:
        if raw_metadata is not None:
            raw_metadata["parent_topic_id"] = resolved_parent_id
        return {**topic_data, "parent_topic_id": resolved_parent_id}
    return topic_data


def _lexical_match(
    topic: str,
    topic_data: Mapping[str, object],
    candidates: list[LedgerCandidate],
    similarity_threshold: float,
    *,
    metadata_cache: dict[int, dict[str, str]] | None = None,
) -> history.TopicMatch | None:
    cache = metadata_cache if metadata_cache is not None else {}
    best: history.TopicMatch | None = None
    for candidate in candidates:
        similarity = history.topic_match_similarity(
            topic,
            topic_data,
            candidate.topic,
            candidate.row,
            metadata_cache=cache,
        )
        if similarity < similarity_threshold:
            continue
        if history._continuation_allowed(
            topic,
            topic_data,
            candidate.row,
            candidate.topic,
            similarity_threshold,
            metadata_cache=cache,
        ):
            continue
        match = history.TopicMatch(
            topic=candidate.topic,
            ts=str(candidate.row.get("ts") or ""),
            similarity=similarity,
            source=candidate.source,
        )
        if best is None or match.similarity > best.similarity:
            best = match
    return best


def _recent_candidate_topics(candidates: list[LedgerCandidate]) -> list[str]:
    topics: list[str] = []
    seen_topics: set[str] = set()
    for candidate in candidates:
        key = history._normalise_topic(candidate.topic)
        if not key or key in seen_topics:
            continue
        seen_topics.add(key)
        topics.append(candidate.topic)
    return topics


def _semantic_match_is_blocking(
    topic: str,
    topic_data: Mapping[str, object],
    match: history.TopicMatch,
    candidates: list[LedgerCandidate],
    similarity_threshold: float,
    *,
    metadata_cache: dict[int, dict[str, str]] | None = None,
) -> bool:
    matched_key = history._normalise_topic(match.topic)
    matching = [
        candidate
        for candidate in candidates
        if matched_key
        and history._normalise_topic(candidate.topic) == matched_key
    ]
    if not matching and matched_key:
        ranked = sorted(
            candidates,
            key=lambda candidate: history.topic_similarity(
                match.topic, candidate.topic
            ),
            reverse=True,
        )
        if ranked and history.topic_similarity(match.topic, ranked[0].topic) >= 0.9:
            matching = [ranked[0]]
    if not matching:
        # 意味判定のスナップショットにだけ存在した候補は、再検証後に
        # 取消済み・期限切れになった可能性があるため採用しない。
        return False
    return not history._semantic_match_allows_continuation(
        topic,
        topic_data,
        match,
        [(candidate.row, candidate.topic, candidate.source) for candidate in matching],
        similarity_threshold,
        metadata_cache=metadata_cache,
    )


def _append_daily_limit_skip(
    path: Path,
    spec: channel.ChannelSpec,
    corner: str,
    topic: str,
    topic_data: Mapping[str, object],
    current: datetime,
    daily_limit: int,
) -> DailyUploadLimitSkip:
    local_day = current.astimezone(_JST).date().isoformat()
    exc = DailyUploadLimitSkip(spec.id, daily_limit, local_day)
    with path.open("a", encoding="utf-8") as file:
        _append(
            file,
            {
                "ts": current.isoformat(),
                "channel": spec.id,
                "corner": corner,
                "topic": topic,
                "status": "daily_limit_skipped",
                "daily_upload_limit": daily_limit,
                "daily_upload_day": local_day,
                "topic_metadata": dict(topic_data),
                **topic_data,
                "skip_reason": exc.reason,
            },
        )
    return exc


def ensure_daily_capacity(
    spec: channel.ChannelSpec,
    *,
    now: datetime | None = None,
) -> None:
    """実投稿runの重い生成へ進む前に、日次枠を読み取り専用で確認する。"""
    daily_limit = _max_uploads_per_day(spec)
    if daily_limit <= 0:
        return
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        used_keys = _daily_upload_keys_for_spec(
            _read_rows(ledger_path()),
            spec,
            current=current,
        )
    if len(used_keys) >= daily_limit:
        local_day = current.astimezone(_JST).date().isoformat()
        raise DailyUploadLimitSkip(spec.id, daily_limit, local_day)


def reserve(
    spec: channel.ChannelSpec,
    corner: str,
    topic: str,
    *,
    cooldown_days: int,
    metadata: Mapping[str, object] | None = None,
    reserve: bool = True,
    now: datetime | None = None,
    similarity_threshold: float = 0.55,
    semantic_check: Callable[[str, list[str]], history.TopicMatch | None]
    | None = None,
) -> str | None:
    """全チャネルを照合し、実投稿runだけ共通台帳へ予約する。"""
    daily_limit = _max_uploads_per_day(spec)
    if cooldown_days <= 0 and daily_limit <= 0:
        return None
    topic = topic.strip()
    if not topic:
        raise ValueError("共通題材台帳の照合対象が空です")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_metadata = metadata if isinstance(metadata, dict) else None
    topic_data = history.topic_metadata(topic, metadata)
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        metadata_cache: dict[int, dict[str, str]] = {}
        def read_and_match(*, advance_clock: bool = False) -> tuple[
            list[dict], list[LedgerCandidate], dict[str, str], history.TopicMatch | None
        ]:
            nonlocal current
            rows = _read_rows(path)
            if advance_clock:
                active_timestamps = [
                    timestamp
                    for row in rows
                    if str(row.get("status") or "") in {"queued", "publishing"}
                    for timestamp in [history._parse_ts(row.get("ts"))]
                    if timestamp is not None
                ]
                if active_timestamps:
                    current = max(current, max(active_timestamps))
            if reserve and daily_limit > 0:
                used_keys = _daily_upload_keys_for_spec(
                    rows,
                    spec,
                    current=current,
                )
                if len(used_keys) >= daily_limit:
                    raise _append_daily_limit_skip(
                        path,
                        spec,
                        corner,
                        topic,
                        topic_data,
                        current,
                        daily_limit,
                    )
            candidates = _collect_candidates(
                rows,
                now=current,
                cooldown_days=cooldown_days,
            )
            current_topic_data = _resolve_topic_metadata(
                topic,
                topic_data,
                raw_metadata,
                candidates,
                similarity_threshold,
            )
            # semantic再判定で台帳を読み直すと行dictも作り直される。古い
            # id(row)キャッシュを残すと、CPythonのID再利用で別行のメタデータを
            # 誤って参照し得るため、読込単位でだけキャッシュを有効にする。
            metadata_cache.clear()
            best = _lexical_match(
                topic,
                current_topic_data,
                candidates,
                similarity_threshold,
                metadata_cache=metadata_cache,
            )
            return rows, candidates, current_topic_data, best

        rows, candidates, topic_data, best = read_and_match()
        semantic_match: history.TopicMatch | None = None
        if best is None and semantic_check is not None:
            semantic_topics = _recent_candidate_topics(candidates)
            # LLM判定は最大60秒かかるため、共通台帳の排他ロックを
            # 保持したまま実行しない。候補が並行追加された場合は最大3回まで
            # 時刻・候補を更新して再判定し、最後まで変化する場合はfail-closedにする。
            for semantic_attempt in range(3):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                wait_started = datetime.now(timezone.utc)
                try:
                    semantic_match = semantic_check(topic, semantic_topics)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                # LLM呼び出しに実際にかかった経過時間だけcurrentを進める。
                # datetime.now()へ直接max()すると、注入されたnow(テストの仮想時計や
                # 将来のバックフィル)から実時計へ一気に飛んでしまい、その飛び幅が
                # TOPIC_RESERVATION_TTL_HOURSを超えるとqueued予約が突然「期限切れ」に
                # 見えて再判定の母集団から消え、fail-closedのはずが素通りしてしまう。
                current = current + (datetime.now(timezone.utc) - wait_started)
                rows, candidates, topic_data, best = read_and_match(
                    advance_clock=True
                )
                fresh_topics = _recent_candidate_topics(candidates)
                if best is not None:
                    break
                if fresh_topics != semantic_topics and semantic_attempt < 2:
                    semantic_topics = fresh_topics
                    continue
                if fresh_topics != semantic_topics:
                    if fresh_topics:
                        best = history.TopicMatch(
                            topic=fresh_topics[0],
                            ts="",
                            similarity=similarity_threshold,
                            source="共通台帳(並行予約のため再確認不能)",
                        )
                    break
                if semantic_match is not None and _semantic_match_is_blocking(
                    topic,
                    topic_data,
                    semantic_match,
                    candidates,
                    similarity_threshold,
                    metadata_cache=metadata_cache,
                ):
                    best = semantic_match
                break
        if best is not None:
            exc = history.TopicCooldownSkip(topic, best, cooldown_days)
            if reserve:
                with path.open("a", encoding="utf-8") as file:
                    _append(
                        file,
                        {
                            "ts": current.isoformat(),
                            "channel": spec.id,
                            "corner": corner,
                            "topic": topic,
                            "status": "skipped",
                            "topic_metadata": topic_data,
                            **topic_data,
                            "skip_reason": exc.reason,
                            "matched_topic": best.topic,
                            "matched_ts": best.ts,
                            "matched_source": best.source,
                            "similarity": round(best.similarity, 4),
                        },
                    )
            raise exc
        if not reserve:
            return None
        reservation_id = uuid.uuid4().hex
        with path.open("a", encoding="utf-8") as file:
            _append(
                file,
                {
                    "ts": current.isoformat(),
                    "channel": spec.id,
                    "corner": corner,
                    "topic": topic,
                    "status": "queued",
                    "reservation_id": reservation_id,
                    "daily_upload_limit": daily_limit or None,
                    "daily_upload_day": (
                        current.astimezone(_JST).date().isoformat()
                        if daily_limit > 0
                        else None
                    ),
                    "topic_metadata": topic_data,
                    **topic_data,
                },
            )
        return reservation_id


def _append_event(
    spec: channel.ChannelSpec,
    corner: str,
    topic: str,
    reservation_id: str,
    status: str,
    *,
    metadata: Mapping[str, object] | None = None,
    video_id: str | None = None,
    cancel_reason: str | None = None,
    publish_results: list[Mapping[str, object]] | None = None,
    now: datetime | None = None,
) -> None:
    if not reservation_id:
        return
    topic_data = history.topic_metadata(topic, metadata)
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        # 呼び出し元のreserve()/ensure_daily_capacity()と同じ時計を使う。実時計に固定すると、
        # テストや将来のバックフィルで注入したnowより後の「未来」timestampとして扱われ、
        # _latest_reservation_rowsの「未来のterminal行は無視する」ガードに弾かれて、この
        # cancelled/published行がqueued予約を上書きできず、古いqueuedが有効のまま残る。
        event_ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        row = {
            "ts": event_ts.isoformat(),
            "channel": spec.id,
            "corner": corner,
            "topic": topic,
            "status": status,
            "reservation_id": reservation_id,
            "video_id": video_id,
            "topic_metadata": topic_data,
            **topic_data,
        }
        if status == "published":
            # 予約日ではなく、実際に終端化した時点のJST日を日次枠へ
            # 計上する。23:59予約→00:01公開を前日の枠へ戻さない。
            row["daily_upload_day"] = event_ts.astimezone(_JST).date().isoformat()
        if publish_results is not None:
            row["publish_results"] = [
                {
                    "platform": str(result.get("platform") or "")[:40],
                    "status": str(result.get("status") or "")[:40],
                    "id": str(result.get("id") or "")[:200] or None,
                    "detail": str(result.get("detail") or "")[:240],
                }
                for result in publish_results
                if isinstance(result, Mapping)
            ][:12]
        if cancel_reason:
            row["cancel_reason"] = cancel_reason[:500]
        with path.open("a", encoding="utf-8") as file:
            _append(
                file,
                row,
            )


def mark_publishing(
    spec: channel.ChannelSpec,
    corner: str,
    topic: str,
    reservation_id: str,
    *,
    metadata: Mapping[str, object] | None = None,
    publish_results: list[Mapping[str, object]] | None = None,
    now: datetime | None = None,
) -> None:
    """外部投稿開始前に、結果不明でも題材をfail-closedにする。"""
    _append_event(
        spec,
        corner,
        topic,
        reservation_id,
        "publishing",
        metadata=metadata,
        publish_results=publish_results,
        now=now,
    )


def complete(
    spec: channel.ChannelSpec,
    corner: str,
    topic: str,
    reservation_id: str,
    *,
    status: str,
    metadata: Mapping[str, object] | None = None,
    video_id: str | None = None,
    publish_results: list[Mapping[str, object]] | None = None,
    now: datetime | None = None,
) -> None:
    """予約を最終状態へ進める。generatedは次回の重複候補にしない。"""
    if status not in {"published", "generated"}:
        raise ValueError(f"invalid topic ledger completion status: {status}")
    _append_event(
        spec,
        corner,
        topic,
        reservation_id,
        status,
        metadata=metadata,
        video_id=video_id,
        publish_results=publish_results,
        now=now,
    )


def cancel(
    spec: channel.ChannelSpec,
    corner: str,
    topic: str,
    reservation_id: str,
    reason: str,
    *,
    metadata: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> None:
    """制作失敗時に共通予約を再利用可能へ戻す。"""
    _append_event(
        spec,
        corner,
        topic,
        reservation_id,
        "cancelled",
        metadata=metadata,
        cancel_reason=reason,
        now=now,
    )


def recover_publishing(
    reservation_id: str,
    *,
    status: str = "cancelled",
    video_id: str | None = None,
    reason: str = "運用者が外部投稿の結果を確認し、未完了予約を復旧",
) -> dict[str, object]:
    """プロセス消失後のpublishing予約を、運用者確認付きで終端化する。

    自動で取消して重複投稿を招かないよう、通常runからは呼ばない。運用者が
    YouTube等の外部状態を確認した後、未投稿ならcancelled、投稿済みなら
    video_id付きpublishedを明示して実行する。
    """
    reservation_id = reservation_id.strip()
    if not reservation_id:
        raise ValueError("reservation_idが空です")
    if status not in {"cancelled", "published"}:
        raise ValueError("statusはcancelledまたはpublishedです")
    video_id = str(video_id or "").strip() or None
    if status == "published" and not video_id:
        raise ValueError("published復旧にはvideo_idが必要です")
    if status == "cancelled" and video_id:
        raise ValueError("cancelled復旧にvideo_idは指定できません")
    current = datetime.now(timezone.utc)
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    local_recovered = False
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        rows = _read_rows(path)
        latest = history._latest_reservation_rows(rows, current)
        active = latest.get(reservation_id)
        if active is None:
            raise ValueError(f"共通題材台帳に予約がありません: {reservation_id}")
        active_status = str(active.get("status") or "")
        if active_status not in {"publishing", status}:
            raise ValueError(
                f"publishing以外の予約は復旧できません: {active.get('status')}"
            )
        channel_id = str(active.get("channel") or "")
        corner = str(active.get("corner") or "")
        topic = history._row_topic(active)
        if not topic:
            raise ValueError("復旧対象の題材が空です")
        if active_status == status:
            existing_video_id = str(active.get("video_id") or "").strip() or None
            if status == "published" and existing_video_id != video_id:
                raise ValueError(
                    "published復旧済みですが、指定されたvideo_idが異なります"
                )
            # 同じ終端内容の再実行は監査行を増やさず成功扱いにする。
            return {
                "channel": channel_id,
                "corner": corner,
                "topic": topic,
                "reservation_id": reservation_id,
                "status": status,
                "video_id": existing_video_id if status == "published" else None,
                "local_history_recovered": False,
                "idempotent": True,
            }
        metadata = history._row_topic_metadata(active, topic)
        active_publish_results = active.get("publish_results")

        spec = channel.load(channel_id)
        local_rows = history._read_path(spec.history_file)
        local_active: dict | None = None
        for row in reversed(local_rows):
            if str(row.get("topic_ledger_reservation_id") or "") != reservation_id:
                continue
            if str(row.get("status") or "") == "publishing":
                local_active = row
                break
        if local_active is not None:
            local_reservation_id = str(local_active.get("reservation_id") or "")
            local_topic = history._row_topic(local_active) or topic
            local_metadata = history._row_topic_metadata(local_active, local_topic)
            if status == "cancelled":
                history.cancel_topic(
                    spec,
                    corner,
                    local_topic,
                    local_reservation_id or reservation_id,
                    reason,
                    metadata=local_metadata,
                    topic_ledger_reservation_id=reservation_id,
                )
            else:
                history.record(
                    spec,
                    corner,
                    str(local_active.get("title") or ""),
                    video_id,
                    extra={
                        "status": "published",
                        "topic": local_topic,
                        "topic_concepts": history.topic_concepts(local_topic),
                        "topic_metadata": local_metadata,
                        **local_metadata,
                        "reservation_id": local_reservation_id or reservation_id,
                        "topic_ledger_reservation_id": reservation_id,
                        "recovery_reason": reason[:500],
                        "publish_results": active_publish_results,
                    },
                )
            local_recovered = True

        event_ts = datetime.now(timezone.utc)
        row = {
            "ts": event_ts.isoformat(),
            "channel": channel_id,
            "corner": corner,
            "topic": topic,
            "status": status,
            "reservation_id": reservation_id,
            "video_id": video_id if status == "published" else None,
            "topic_metadata": metadata,
            **metadata,
            "recovery_reason": reason[:500],
        }
        if status == "published":
            row["daily_upload_day"] = event_ts.astimezone(_JST).date().isoformat()
        if isinstance(active_publish_results, list):
            row["publish_results"] = active_publish_results[:12]
        if status == "cancelled" and active.get("daily_upload_day"):
            row["daily_upload_day"] = active["daily_upload_day"]
        with path.open("a", encoding="utf-8") as file:
            _append(file, row)

    return {
        "channel": channel_id,
        "corner": corner,
        "topic": topic,
        "reservation_id": reservation_id,
        "status": status,
        "video_id": video_id if status == "published" else None,
        "local_history_recovered": local_recovered,
    }
