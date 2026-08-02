"""チャネル別の実投稿枠と、投稿結果不明時のfail-closed状態を全チャネル共通で管理する台帳。

題材内容そのものの重複判定はチャネル別 history.reserve_topic() が担う。チャネル間で
扱うテーマは十分に異なるため、この台帳は題材の跨ぎ照合は行わない。ここでは
pipeline.max_uploads_per_day のJST日次実投稿枠と、外部投稿結果が確定するまでの
安全な状態遷移（queued→publishing→published/cancelled）だけを、ファイルロックで
原子的に扱う。
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import channel, config, history


class TopicLedgerCorruptError(RuntimeError):
    """台帳のJSONLが壊れており、日次枠・投稿状態の判定を安全に続けられない。"""


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
    metadata: Mapping[str, object] | None = None,
    reserve: bool = True,
    now: datetime | None = None,
) -> str | None:
    """pipeline.max_uploads_per_dayのJST日次実投稿枠だけを原子的に確認・予約する。

    題材内容の重複判定は行わない(チャネル別history.reserve_topic()の役割)。
    枠設定が無いチャンネルは即Noneを返し、台帳を汚さない。
    """
    daily_limit = _max_uploads_per_day(spec)
    if daily_limit <= 0:
        return None
    topic = topic.strip()
    if not topic:
        raise ValueError("共通題材台帳の予約対象が空です")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    topic_data = history.topic_metadata(topic, metadata)
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if reserve:
            rows = _read_rows(path)
            used_keys = _daily_upload_keys_for_spec(rows, spec, current=current)
            if len(used_keys) >= daily_limit:
                raise _append_daily_limit_skip(
                    path, spec, corner, topic, topic_data, current, daily_limit
                )
        else:
            _read_rows(path)  # 台帳破損はdry-run照合でも検出する。
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
                    "daily_upload_limit": daily_limit,
                    "daily_upload_day": current.astimezone(_JST).date().isoformat(),
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
                        **{
                            key: local_active[key]
                            for key in (
                                "workdir",
                                "description",
                                "duration_sec",
                                "tier",
                                "platforms",
                                "youtube_privacy",
                            )
                            if local_active.get(key) is not None
                        },
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
