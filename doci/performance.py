"""YouTube実績のreadbackと、次回生成へ渡す保守的なフィードバック。"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import channel, history, youtube
from .channel import ChannelSpec

SCHEMA_VERSION = 1
MIN_ELIGIBLE_VIDEOS = 8
MIN_ANALYTICS_VIEWS = 20
MIN_PUBLIC_VIEWS = 50
MIN_GROUP_SIZE = 2
MIN_TRAIT_SUPPORT = 2
MIN_EVAL_PEERS = 4  # 2 * MIN_GROUP_SIZE。medianとの相対比較に最低限必要なpeer数


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _history_by_video(spec: ChannelSpec) -> dict[str, dict]:
    rows = _read_jsonl(spec.history_file)
    return {
        str(row["video_id"]): row
        for row in rows
        if row.get("video_id")
    }


def _snapshot_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / "performance.jsonl"


def _decision_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / "performance_decision.json"


def _snapshot_signature(snapshot: dict) -> str:
    stable = {
        "analytics": snapshot.get("analytics", {}),
        # issue #164: トラフィックreadbackのavailable/reason変化も署名へ含め、
        # statusだけの変化でも新しいsnapshot行を追記できるようにする。
        "traffic_sources": snapshot.get("traffic_sources", {}),
        "search_terms": snapshot.get("search_terms", {}),
        "retention_curve": snapshot.get("retention_curve", {}),
        "videos": snapshot.get("videos", []),
    }
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _duration_bucket(value: object) -> str:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return "under_60s"
    if seconds < 180:
        return "60_to_179s"
    return "180s_or_more"


def _format_traits(spec: ChannelSpec, recorded: dict) -> list[str]:
    """題材語を含めず、生成物の再利用可能な形式属性だけを抽出する。"""
    traits: list[str] = []
    tier = str(recorded.get("tier") or "")
    if tier:
        traits.append(f"tier:{tier}")
    duration = _duration_bucket(recorded.get("duration_sec"))
    if duration:
        traits.append(f"duration:{duration}")

    workdir_raw = recorded.get("workdir")
    if not workdir_raw:
        return traits
    workdir = Path(str(workdir_raw)).resolve()
    output_root = spec.output_dir.resolve()
    if not workdir.is_relative_to(output_root):
        return traits
    try:
        script = json.loads((workdir / "script.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return traits
    scenes = script.get("scenes")
    if not isinstance(scenes, list):
        return traits
    scene_count = len(scenes)
    if scene_count:
        if scene_count <= 4:
            scene_bucket = "1_to_4"
        elif scene_count <= 8:
            scene_bucket = "5_to_8"
        else:
            scene_bucket = "9_or_more"
        traits.append(f"scenes:{scene_bucket}")
    traits.append(
        "chart:present"
        if any(isinstance(scene, dict) and scene.get("chart") for scene in scenes)
        else "chart:absent"
    )
    return traits


def _scene_time_windows(script: dict, total_seconds: float) -> list[dict]:
    """scenesの時間窓をnarration文字数比で按分する（compose.pyと同じ方式）。

    各sceneに対し start/end 秒を返す。文字数が取得できない場合は均等割。
    これは合成時の実境界（文末スナップ）とは異なる近似であり、照合の目安。
    """
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return []
    narration = str(script.get("narration") or "")
    weights = [max(len(str(s.get("caption") or "")) + 1, 1) for s in scenes]
    total_weight = sum(weights)
    if total_weight <= 0:
        return []
    if total_seconds <= 0:
        total_seconds = 1.0
    windows: list[dict] = []
    cursor = 0.0
    for index, weight in enumerate(weights):
        span = total_seconds * weight / total_weight
        start = cursor
        end = cursor + span
        windows.append(
            {
                "index": index,
                "caption": str(scenes[index].get("caption") or ""),
                "start": round(start, 3),
                "end": round(end, 3),
            }
        )
        cursor = end
    return windows


def retention_moments(
    curve: list[dict],
    *,
    threshold: float = 8.0,
    min_delta: float = 2.0,
) -> list[dict]:
    """維持率カーブからスパイク（山）とディップ（谷）を検出する（issue #149）。

    各点のwatch_ratioを前後の点と比較し、前後より`threshold`以上高い点を
    spike、低い点を dip とする。端点やデータが少なすぎる場合は検出しない
    （山=成功・谷=失敗と断定しないために、形状だけから結論を出さない）。
    返り値は `{elapsed_ratio, watch_ratio, kind}` のリスト。
    """
    if not curve or len(curve) < 5:
        return []
    moments: list[dict] = []
    for index in range(1, len(curve) - 1):
        prev = curve[index - 1]["watch_ratio"]
        curr = curve[index]["watch_ratio"]
        nxt = curve[index + 1]["watch_ratio"]
        if abs(prev - curr) < min_delta and abs(nxt - curr) < min_delta:
            continue
        if curr > prev + threshold and curr > nxt + threshold:
            moments.append(
                {
                    "elapsed_ratio": curve[index]["elapsed_ratio"],
                    "watch_ratio": curr,
                    "kind": "spike",
                }
            )
        elif curr < prev - threshold and curr < nxt - threshold:
            moments.append(
                {
                    "elapsed_ratio": curve[index]["elapsed_ratio"],
                    "watch_ratio": curr,
                    "kind": "dip",
                }
            )
    return moments


def retention_moment_scenes(
    moments: list[dict],
    script: dict,
    total_seconds: float,
) -> list[dict]:
    """検出した山/谷を、台本のscenesと照合して「何秒付近・どのシーン」を返す。

    該当sceneが特定できない場合は `scene_index=None` のまま（推測しない）。
    """
    windows = _scene_time_windows(script, total_seconds)
    if not windows:
        return [
            {
                **moment,
                "elapsed_seconds": round(moment["elapsed_ratio"] * total_seconds, 1),
                "scene_index": None,
                "scene_caption": "",
            }
            for moment in moments
        ]
    annotated: list[dict] = []
    for moment in moments:
        second = moment["elapsed_ratio"] * total_seconds
        scene = next(
            (
                win
                for win in windows
                if win["start"] <= second <= win["end"]
            ),
            None,
        )
        annotated.append(
            {
                **moment,
                "elapsed_seconds": round(second, 1),
                "scene_index": scene["index"] if scene else None,
                "scene_caption": scene["caption"] if scene else "",
            }
        )
    return annotated


def sync(
    spec: ChannelSpec,
    *,
    now: datetime | None = None,
    lookback_days: int = 90,
) -> dict:
    """履歴にある投稿動画のread-only指標を取得し、変化時だけsnapshotを追記する。"""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    history_rows = _history_by_video(spec)
    video_ids = list(history_rows)
    details = youtube.video_details(
        video_ids,
        token_file=spec.publish.youtube.token,
        client_secret_file=spec.publish.youtube.client_secret,
    )
    analytics_status: dict = {
        "available": False,
        "source": "youtube_analytics_api_v2",
    }
    analytics_rows: list[dict] = []
    traffic_status: dict = {"available": False, "source": "youtube_analytics_api_v2"}
    search_status: dict = {"available": False, "source": "youtube_analytics_api_v2"}
    retention_status: dict = {
        "available": False,
        "source": "youtube_analytics_api_v2",
    }
    traffic_by_id: dict[str, dict[str, int]] = {}
    search_by_id: dict[str, list[dict]] = {}
    retention_by_id: dict[str, list[dict]] = {}
    if youtube._token_has_scopes(spec.publish.youtube.token, youtube.ANALYTICS_SCOPES):
        start = (current.date() - timedelta(days=lookback_days)).isoformat()
        end = current.date().isoformat()
        try:
            analytics_rows = youtube.video_analytics(
                video_ids,
                start_date=start,
                end_date=end,
                token_file=spec.publish.youtube.token,
                client_secret_file=spec.publish.youtube.client_secret,
            )
            analytics_status.update(
                {
                    "available": True,
                    "start_date": start,
                    "end_date": end,
                }
            )
            # issue #164: トラフィックソースと検索語句はAnalytics APIが
            # 返せる範囲だけ取得する。取得できない動画・種別は0や「なし」と
            # 推測せず、空のまま（fail-closed）。両者は別々のtry/statusで
            # 管理し、片方の失敗が他方の実データを「取得不可」にしない。
            try:
                traffic_by_id = youtube.video_traffic_sources(
                    video_ids,
                    start_date=start,
                    end_date=end,
                    token_file=spec.publish.youtube.token,
                    client_secret_file=spec.publish.youtube.client_secret,
                )
                traffic_status.update({"available": True})
            except Exception as exc:
                traffic_status["reason"] = (
                    "トラフィックソースreadback失敗。retention指標のみ保存: "
                    f"{str(exc)[:400]}"
                )
            try:
                # 検索語句は動画ごとにAPIを呼ぶため、コンテンツギャップ企画
                # （gap_query記録）の動画だけへ照会対象を絞る。さらに今回の
                # snapshot出力（details）で使われる動画へ積集合で絞り、
                # 削除済み等でData APIが返さない古い動画へ無駄にAPIを呼ばない
                # （Claude review指摘）。
                sync_ids = {str(detail.get("video_id") or "") for detail in details}
                gap_video_ids = [
                    video_id
                    for video_id, row in history_rows.items()
                    if video_id in sync_ids
                    and str(
                        (history._row_topic_metadata(row)).get("gap_query") or ""
                    ).strip()
                ]
                search_failures: dict[str, str] = {}
                if gap_video_ids:
                    search_by_id, search_failures = youtube.video_search_terms(
                        gap_video_ids,
                        start_date=start,
                        end_date=end,
                        token_file=spec.publish.youtube.token,
                        client_secret_file=spec.publish.youtube.client_secret,
                    )
                search_status.update({"available": True})
                if search_failures:
                    search_status["failed_video_ids"] = sorted(search_failures)
                    search_status["failures"] = search_failures
            except Exception as exc:
                search_status["reason"] = (
                    "検索語句readback失敗。traffic sourceは保存: "
                    f"{str(exc)[:400]}"
                )
            try:
                # issue #149: 維持率カーブはShorts等でAPIが返さない場合が
                # あるため、取得できる範囲だけ保存する（fail-closed）。
                retention_by_id = youtube.video_retention_curves(
                    video_ids,
                    start_date=start,
                    end_date=end,
                    token_file=spec.publish.youtube.token,
                    client_secret_file=spec.publish.youtube.client_secret,
                )
                retention_status.update({"available": True})
            except Exception as exc:
                retention_status["reason"] = (
                    "維持率カーブreadback失敗。平均指標のみ保存: "
                    f"{str(exc)[:400]}"
                )
        except Exception as exc:  # API無効・一時障害でもData API snapshotは残す
            analytics_status["reason"] = (
                "Analytics readback失敗。Data API snapshotのみ保存: "
                f"{str(exc)[:400]}"
            )
    else:
        analytics_status["reason"] = (
            "YouTube Analytics APIをOAuthクライアントのGoogle Cloud projectで"
            "有効化し、yt-analytics.readonly scopeを明示的に許可する必要があります。"
            "有効化後に `python -m doci.youtube --auth --analytics "
            f"--channel {spec.id}` で再認証してください"
        )
    analytics_by_id = {
        str(row.get("video_id")): row for row in analytics_rows if row.get("video_id")
    }
    videos: list[dict] = []
    for detail in details:
        video_id = str(detail.get("video_id") or "")
        recorded = history_rows.get(video_id, {})
        topic = str(recorded.get("topic") or history._row_topic(recorded))
        analytics = analytics_by_id.get(video_id)
        if isinstance(analytics, dict):
            analytics = {
                **analytics,
                "traffic_sources": traffic_by_id.get(video_id, {}),
                "search_terms": search_by_id.get(video_id, []),
                "retention_curve": retention_by_id.get(video_id, []),
            }
        videos.append(
            {
                "video_id": video_id,
                "title": str(recorded.get("title") or detail.get("title") or ""),
                "corner": str(recorded.get("corner") or ""),
                "topic": topic,
                "topic_metadata": history._row_topic_metadata(recorded),
                "format_traits": _format_traits(spec, recorded),
                "history_ts": str(recorded.get("ts") or ""),
                "published_at": str(detail.get("published_at") or ""),
                "privacy_status": str(detail.get("privacy_status") or ""),
                "data_api": {
                    "views": int(detail.get("views", 0) or 0),
                    "likes": int(detail.get("likes", 0) or 0),
                    "comments": int(detail.get("comments", 0) or 0),
                    "duration": str(detail.get("duration") or ""),
                },
                "analytics": analytics,
            }
        )
    videos.sort(key=lambda row: (row["history_ts"], row["video_id"]))
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "channel": spec.id,
        "collected_at": current.isoformat(),
        "source": "youtube_data_api_v3",
        "analytics": analytics_status,
        "traffic_sources": traffic_status,
        "search_terms": search_status,
        "retention_curve": retention_status,
        "videos": videos,
    }
    path = _snapshot_path(spec)
    previous = _read_jsonl(path)
    if previous and _snapshot_signature(previous[-1]) == _snapshot_signature(snapshot):
        return previous[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return snapshot


def _format_cohort(row: dict) -> str:
    duration = next(
        (
            str(feature)
            for feature in row.get("format_traits") or []
            if str(feature).startswith("duration:")
        ),
        "",
    )
    tier = next(
        (
            str(feature)
            for feature in row.get("format_traits") or []
            if str(feature).startswith("tier:")
        ),
        "",
    )
    return f"{duration}|{tier}" if duration and tier else ""


def _largest_format_cohort(rows: list[dict]) -> tuple[list[dict], str]:
    cohorts: dict[str, list[dict]] = {}
    for row in rows:
        cohort = _format_cohort(row)
        if cohort:
            cohorts.setdefault(cohort, []).append(row)
    if not cohorts:
        return [], ""
    cohort, members = max(
        cohorts.items(),
        key=lambda item: (len(item[1]), item[0]),
    )
    return members, cohort


def _ranked_rows(
    snapshot: dict,
    *,
    corner_key: str | None = None,
) -> tuple[list[dict], str, str]:
    videos = [
        row
        for row in snapshot.get("videos", [])
        if not corner_key or row.get("corner") == corner_key
    ]
    analytics = [
        {
            **row,
            "_score": float(row["analytics"]["average_view_percentage"]),
        }
        for row in videos
        if isinstance(row.get("analytics"), dict)
        and int(row["analytics"].get("views", 0) or 0) >= MIN_ANALYTICS_VIEWS
        and row["analytics"].get("average_view_percentage") is not None
    ]
    analytics_cohort, cohort = _largest_format_cohort(analytics)
    if len(analytics_cohort) >= MIN_ELIGIBLE_VIDEOS:
        return (
            sorted(analytics_cohort, key=lambda row: row["_score"]),
            "youtube_analytics_api_v2.average_view_percentage",
            cohort,
        )
    public = []
    for row in videos:
        metrics = row.get("data_api") or {}
        views = int(metrics.get("views", 0) or 0)
        if row.get("privacy_status") != "public" or views < MIN_PUBLIC_VIEWS:
            continue
        published = history._parse_ts(row.get("published_at") or row.get("history_ts"))
        collected = history._parse_ts(snapshot.get("collected_at"))
        age_days = max(
            1.0,
            ((collected - published).total_seconds() / 86400)
            if collected and published
            else 1.0,
        )
        public.append({**row, "_score": views / age_days})
    public_cohort, public_format = _largest_format_cohort(public)
    if analytics_cohort and len(analytics_cohort) >= len(public_cohort):
        return (
            sorted(analytics_cohort, key=lambda row: row["_score"]),
            "youtube_analytics_api_v2.average_view_percentage",
            cohort,
        )
    return (
        sorted(public_cohort, key=lambda row: row["_score"]),
        "youtube_data_api_v3.views_per_day",
        public_format,
    )


def _traits(rows: list[dict]) -> Counter[str]:
    traits: Counter[str] = Counter()
    for row in rows:
        for feature in row.get("format_traits") or []:
            traits[str(feature)] += 1
    return traits


def _single_trait(upper: list[dict], lower: list[dict]) -> tuple[str, str]:
    upper_traits = _traits(upper)
    lower_traits = _traits(lower)
    candidates: list[tuple[int, str, str]] = []
    for trait in set(upper_traits) | set(lower_traits):
        upper_count = upper_traits[trait]
        lower_count = lower_traits[trait]
        if upper_count >= MIN_TRAIT_SUPPORT and lower_count == 0:
            candidates.append((upper_count, "positive", trait))
        elif lower_count >= MIN_TRAIT_SUPPORT and upper_count == 0:
            candidates.append((lower_count, "negative", trait))
    if not candidates:
        return "", ""
    _support, direction, trait = max(
        candidates,
        key=lambda item: (item[0], item[1] == "positive", item[2]),
    )
    return direction, trait


def _has_evaluation_result(snapshot: dict, video_id: str) -> bool:
    row = next(
        (
            video
            for video in snapshot.get("videos", [])
            if str(video.get("video_id") or "") == video_id
        ),
        None,
    )
    if not row:
        return False
    analytics = row.get("analytics")
    if (
        isinstance(analytics, dict)
        and int(analytics.get("views", 0) or 0) >= MIN_ANALYTICS_VIEWS
        and analytics.get("average_view_percentage") is not None
    ):
        return True
    metrics = row.get("data_api") or {}
    return (
        row.get("privacy_status") == "public"
        and int(metrics.get("views", 0) or 0) >= MIN_PUBLIC_VIEWS
    )


def _experiment_result(
    snapshot: dict,
    corner_key: str | None,
    video_id: str,
) -> dict:
    """適用動画の指標を同一cohort内peerのmedianと比較し、有効性を判定する。

    `build_decision`の仮説生成と同じcohort・同じ指標選択(`_ranked_rows`)で
    比較することで、「同じ指標で再評価する」というguidanceと整合させる。
    比較材料が不足する場合はfail-closed(`effective: False`)とし、
    誤って逆効果の施策を横展開する事故を避ける。

    既知の制約: cohortは`duration:*`/`tier:*`で定義されるため、
    まさにこれらを変更する実験は適用動画がcohort外に出て
    `insufficient_comparison`(fail-closed)に必ずなる。安全側だが、
    duration/tier仮説は横展開元にはなれない。
    """
    ranked, metric, format_cohort = _ranked_rows(snapshot, corner_key=corner_key)
    applied = next(
        (row for row in ranked if str(row.get("video_id") or "") == video_id),
        None,
    )
    if applied is None:
        return {
            "effective": False,
            "metric": metric,
            "format_cohort": format_cohort,
            "score": None,
            "peer_median": None,
            "peers": 0,
            "reason": "insufficient_comparison",
        }
    peer_scores = [
        row["_score"]
        for row in ranked
        if str(row.get("video_id") or "") != video_id
    ]
    if len(peer_scores) < MIN_EVAL_PEERS:
        return {
            "effective": False,
            "metric": metric,
            "format_cohort": format_cohort,
            "score": applied["_score"],
            "peer_median": None,
            "peers": len(peer_scores),
            "reason": "insufficient_comparison",
        }
    peer_median = statistics.median(peer_scores)
    return {
        "effective": applied["_score"] > peer_median,
        "metric": metric,
        "format_cohort": format_cohort,
        "score": applied["_score"],
        "peer_median": peer_median,
        "peers": len(peer_scores),
        "reason": "",
    }


def build_decision(
    spec: ChannelSpec,
    snapshot: dict,
    *,
    corner_key: str | None = None,
) -> dict:
    """小標本・非公開ゼロ値を学習させず、相対差だけを仮説へ変換する。"""
    ranked, metric, format_cohort = _ranked_rows(
        snapshot,
        corner_key=corner_key,
    )
    decision_seed = {
        "channel": spec.id,
        "corner": corner_key,
        "snapshot_at": snapshot.get("collected_at"),
        "metric": metric,
        "format_cohort": format_cohort,
        "eligible": [row.get("video_id") for row in ranked],
    }
    decision_id = hashlib.sha256(
        json.dumps(decision_seed, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    decision = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "channel": spec.id,
        "corner": corner_key,
        "snapshot_at": snapshot.get("collected_at"),
        "metric": metric,
        "format_cohort": format_cohort,
        "eligible_video_ids": decision_seed["eligible"],
        "min_samples": MIN_ELIGIBLE_VIDEOS,
        "min_group_size": MIN_GROUP_SIZE,
        "min_trait_support": MIN_TRAIT_SUPPORT,
        "source_status": {
            "data_api": {
                "available": True,
                "source": snapshot.get("source"),
            },
            "analytics": snapshot.get("analytics") or {"available": False},
        },
        "guardrails": [
            "相関を因果と断定しない",
            "高実績動画の題材そのものを再利用しない",
            "topic cooldownを常に優先する",
            "一度に試す変数は1つ",
        ],
    }
    analytics_reason = str(
        (snapshot.get("analytics") or {}).get("reason") or ""
    )
    source_suffix = f" / {analytics_reason}" if analytics_reason else ""
    if len(ranked) < MIN_ELIGIBLE_VIDEOS:
        decision.update(
            {
                "status": "insufficient_data",
                "reason": (
                    f"比較可能な動画が{len(ranked)}本。"
                    f"最低{MIN_ELIGIBLE_VIDEOS}本必要"
                    f"{source_suffix}"
                ),
                "guidance": "",
            }
        )
    else:
        group_size = max(MIN_GROUP_SIZE, len(ranked) // 4)
        lower = ranked[:group_size]
        upper = ranked[-group_size:]
        direction, trait = _single_trait(upper, lower)
        if not trait:
            decision.update(
                {
                    "status": "insufficient_signal",
                    "reason": (
                        "上位・下位群を2本以上で分ける単一の形式特性がない"
                        f"{source_suffix}"
                    ),
                    "guidance": "",
                }
            )
        else:
            positive = [trait] if direction == "positive" else []
            negative = [trait] if direction == "negative" else []
            decision.update(
                {
                    "status": "active",
                    "reason": (
                        "同一corner・同一尺・同一tier cohortの相対上位・下位群から"
                        "単一の形式仮説を作成"
                    ),
                    "top_video_ids": [row["video_id"] for row in upper],
                    "bottom_video_ids": [row["video_id"] for row in lower],
                    "positive_traits": positive,
                    "negative_traits": negative,
                    "guidance": (
                        f"実績snapshot {snapshot.get('collected_at')} / decision "
                        f"{decision_id} / 指標 {metric}。"
                        f"比較cohort: {corner_key or 'all'} / {format_cohort}。"
                        f"相対上位に固有の形式特性: {', '.join(positive) or 'なし'}。"
                        f"相対下位に固有の形式特性: {', '.join(negative) or 'なし'}。"
                        "これは因果ではなく次回1本の実験仮説としてのみ使う。"
                        "上位動画の題材は再利用せず、30日cooldownに通る新しい題材を選ぶ。"
                        "変更変数は1つに絞り、同じ指標で再評価する。"
                    ),
                }
            )
    path = _decision_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return decision


def refresh(spec: ChannelSpec, *, corner_key: str | None = None) -> dict:
    snapshot = sync(spec)
    return build_decision(spec, snapshot, corner_key=corner_key)


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube投稿実績のread-only同期")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--corner")
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    spec = channel.load(args.channel)
    if args.sync:
        decision = refresh(spec, corner_key=args.corner)
    else:
        snapshots = _read_jsonl(_snapshot_path(spec))
        if not snapshots:
            raise RuntimeError(
                f"performance snapshotがありません。先に --sync --channel {spec.id}"
            )
        decision = build_decision(spec, snapshots[-1], corner_key=args.corner)
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
