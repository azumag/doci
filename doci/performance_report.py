"""実績フィードバックの3日毎レポートissueサイクル(issue #92)。

`doci.run_daily`の投稿フローとは完全に分離した独立ジョブ。チャンネル単位で
実績を分析し、「調査内容」「実験内容（新しい単一trait仮説）」「前回提案の
効果検証」を1つのissueへまとめて報告する。仮説を実際の生成へ反映する作業は
運用者が手動で行う（このモジュールは一切自動適用しない）。

自動適用を撤去したため「どの動画に適用されたか」を明示的な予約では
追跡できない。代わりに、前回提案したtraitがその後同じcornerで投稿された
動画のformat_traitsに実際に出現したかを自動検知し、出現した最初の動画を
適用済みとみなして事後評価する(`_detect_applied_video`)。

既定はdry-run。実績readback（`performance.jsonl`。既存の`python -m
doci.performance --sync`と同じ副作用）と仮説生成の候補表示は行うが、
実験状態(`performance_experiments.jsonl`)の記録・intervalタイマーの更新・
GitHub issueの作成は一切行わない。`--apply`を明示した場合だけ、これらを
実際に行う。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import channel, config, feedback_issues, history, performance
from .channel import ChannelSpec

_LOCK_WAIT_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.25


def _lock_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / ".performance_report.lock"


@contextmanager
def _operation_lock(spec: ChannelSpec) -> Iterator[None]:
    """`performance_experiments.jsonl`の状態遷移を1チャンネル1プロセスに
    直列化する（`feedback_issues._operation_lock`とは別ファイル・別ロック。
    `submit_candidate`が内部で同じロックを再取得すると同一プロセス内で
    自己デッドロックするため、意図的に分離している）。"""
    path = _lock_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_TIMEOUT_SECONDS
    with path.open("a+", encoding="utf-8") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"performance_report処理lockを"
                        f"{_LOCK_WAIT_TIMEOUT_SECONDS:g}秒以内に取得できません"
                    )
                time.sleep(_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


# --- 実験状態: output/<channel>/performance_experiments.jsonl ---


def _experiments_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / "performance_experiments.jsonl"


def _read_experiments(spec: ChannelSpec) -> list[dict]:
    return performance._read_jsonl(_experiments_path(spec))


def _append_experiment(spec: ChannelSpec, row: dict) -> None:
    path = _experiments_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


def _latest_experiment_rows(rows: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        experiment_id = str(row.get("experiment_id") or "")
        if experiment_id:
            latest[experiment_id] = row
    return latest


def experiment_id(
    channel_id: str, corner: str, trait: str, direction: str, decision_id: str
) -> str:
    seed = {
        "channel": channel_id,
        "corner": corner,
        "trait": trait,
        "direction": direction,
        "decision_id": decision_id,
    }
    digest = hashlib.sha256(
        json.dumps(seed, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"px-{digest}"


# --- trait出現の自動検知 ---


def _detect_applied_video(experiment: dict, snapshot: dict) -> dict | None:
    """提案traitが、提案後に同じcornerで投稿された動画へ出現したかを検知する。

    positiveなtraitはそのまま出現有無、negativeなtraitは「同じfamily
    (`chart:`等プレフィックス)の特徴を持ちつつ対象traitは持たない」動画
    だけを適用済みとみなす(script.json欠落等でtraitが単に欠けた動画を
    誤って「適用」と判定しないためのガード)。複数候補があれば、
    提案後に最初に投稿された動画を返す。
    """
    corner = str(experiment.get("corner") or "")
    trait = str(experiment.get("trait") or "")
    direction = str(experiment.get("direction") or "")
    proposed_at = history._parse_ts(experiment.get("proposed_at"))
    if not corner or not trait or proposed_at is None:
        return None
    family = trait.split(":", 1)[0] + ":" if ":" in trait else ""
    candidates: list[tuple[datetime, dict]] = []
    for row in snapshot.get("videos", []):
        if row.get("corner") != corner:
            continue
        published = history._parse_ts(
            row.get("published_at") or row.get("history_ts")
        )
        if published is None or published <= proposed_at:
            continue
        candidates.append((published, row))
    candidates.sort(key=lambda item: item[0])
    for _published, row in candidates:
        traits = row.get("format_traits") or []
        if direction == "positive":
            if trait in traits:
                return row
        elif direction == "negative":
            if family and any(t.startswith(family) for t in traits) and trait not in traits:
                return row
    return None


def _progress_experiments(
    spec: ChannelSpec, snapshot: dict, now: datetime, *, apply: bool
) -> dict[str, list[dict]]:
    """corner毎に、pending実験の状態遷移(applied/evaluated/expired)を進め、
    今回のサイクルで報告すべき(未reportedのevaluated)行をcorner毎に返す。

    `apply=False`（dry-run）では、issue本文プレビューに必要な遷移後の内容を
    計算するだけで`performance_experiments.jsonl`へは一切書き込まない
    （dry-runが「候補表示のみ・外部状態を一切変更しない」という契約を守るため。
    以前は`apply`に関わらず無条件で追記しており、dry-runで実行しただけで
    `applied`/`expired`へ状態が進んでしまう副作用があった）。

    `applied`状態にも`proposed`と同じ`max_age`を適用する。指標が育たない
    まま(Analytics未許可・非公開のまま等)`applied`に無期限で滞留すると、
    「前回提案の効果検証」が永久に「判定材料不足」のまま残ってしまうため。
    """
    latest = _latest_experiment_rows(_read_experiments(spec))
    max_age = timedelta(days=config.PERFORMANCE_EXPERIMENT_MAX_AGE_DAYS)

    def emit(row: dict) -> None:
        if apply:
            _append_experiment(spec, row)

    by_corner: dict[str, list[dict]] = {}
    for row in latest.values():
        corner = str(row.get("corner") or "")
        status = str(row.get("status") or "")
        if status == "proposed":
            applied_video = _detect_applied_video(row, snapshot)
            if applied_video is not None:
                row = {
                    **row,
                    "ts": now.isoformat(),
                    "status": "applied",
                    "video_id": applied_video.get("video_id"),
                    "video_published_at": (
                        applied_video.get("published_at")
                        or applied_video.get("history_ts")
                    ),
                }
                emit(row)
            else:
                proposed_at = history._parse_ts(row.get("proposed_at"))
                if proposed_at is not None and now - proposed_at > max_age:
                    emit(
                        {
                            **row,
                            "ts": now.isoformat(),
                            "status": "expired",
                            "reason": "trait_not_detected_within_max_age",
                        }
                    )
                continue
        if row.get("status") == "applied":
            proposed_at = history._parse_ts(row.get("proposed_at"))
            if proposed_at is not None and now - proposed_at > max_age:
                emit(
                    {
                        **row,
                        "ts": now.isoformat(),
                        "status": "expired",
                        "reason": "evaluation_threshold_not_reached_within_max_age",
                    }
                )
                continue
            video_id = str(row.get("video_id") or "")
            if video_id and performance._has_evaluation_result(snapshot, video_id):
                result = performance._experiment_result(snapshot, corner, video_id)
                row = {
                    **row,
                    "ts": now.isoformat(),
                    "status": "evaluated",
                    "result": result,
                }
                emit(row)
        if row.get("status") == "evaluated":
            by_corner.setdefault(corner, []).append(row)
    return by_corner


def _mark_reported(spec: ChannelSpec, rows: list[dict], issue_number: int, now: datetime) -> None:
    for row in rows:
        _append_experiment(
            spec,
            {
                **row,
                "ts": now.isoformat(),
                "status": "reported",
                "report_issue_number": issue_number,
            },
        )


def _record_proposed_experiments(
    spec: ChannelSpec, sections: list[dict], issue: dict, now: datetime
) -> None:
    for section in sections:
        hyp = section.get("proposal")
        if hyp is None:
            continue
        decision = section["decision"]
        row = {
            "ts": now.isoformat(),
            "schema_version": 1,
            "experiment_id": hyp["experiment_id"],
            "channel": spec.id,
            "corner": section["corner"],
            "status": "proposed",
            "direction": hyp["direction"],
            "trait": hyp["trait"],
            "metric": decision.get("metric"),
            "format_cohort": decision.get("format_cohort"),
            "decision_id": decision.get("decision_id"),
            "issue_number": issue.get("number"),
            "issue_url": issue.get("url"),
            "proposed_at": now.isoformat(),
        }
        _append_experiment(spec, row)


# --- corner毎のセクション組み立て（純関数） ---


def hypothesis_key(channel_id: str, corner: str, decision: dict) -> str:
    traits = sorted(
        (decision.get("positive_traits") or []) + (decision.get("negative_traits") or [])
    )
    return f"{channel_id}|{corner}|{decision.get('metric')}|{','.join(traits)}"


def _primary_trait(decision: dict) -> tuple[str, str] | None:
    positive = decision.get("positive_traits") or []
    negative = decision.get("negative_traits") or []
    if positive:
        return "positive", positive[0]
    if negative:
        return "negative", negative[0]
    return None


def build_corner_section(
    spec: ChannelSpec,
    corner_key: str,
    decision: dict,
    evaluations: list[dict],
    recent_hyp_keys: set[str],
) -> dict:
    proposal = None
    key = None
    if decision.get("status") == "active":
        direction_trait = _primary_trait(decision)
        key = hypothesis_key(spec.id, corner_key, decision)
        if direction_trait is not None and key not in recent_hyp_keys:
            direction, trait = direction_trait
            proposal = {
                "experiment_id": experiment_id(
                    spec.id, corner_key, trait, direction, str(decision.get("decision_id") or "")
                ),
                "direction": direction,
                "trait": trait,
                "hypothesis_key": key,
            }
    return {
        "corner": corner_key,
        "decision": decision,
        "proposal": proposal,
        "evaluations": evaluations,
    }


# --- issue本文組み立て ---


def _investigation_text(decision: dict) -> str:
    status = decision.get("status")
    if status in ("insufficient_data", "insufficient_signal"):
        return f"- {decision.get('reason', '')}"
    metric = decision.get("metric", "")
    cohort = decision.get("format_cohort", "")
    eligible = decision.get("eligible_video_ids") or []
    top = ", ".join(f"`{v}`" for v in decision.get("top_video_ids") or []) or "なし"
    bottom = ", ".join(f"`{v}`" for v in decision.get("bottom_video_ids") or []) or "なし"
    return (
        f"- 指標: {metric}（対象{len(eligible)}本）\n"
        f"- 比較cohort: {cohort}\n"
        f"- 相対上位群: {top}\n"
        f"- 相対下位群: {bottom}"
    )


def _hypothesis_text(decision: dict, section: dict) -> str:
    if decision.get("status") != "active":
        return f"- 新仮説なし（{decision.get('reason', '')}）"
    if section.get("proposal") is None:
        return "- 新仮説なし（同じ仮説を直近のcooldown期間内に提案済み）"
    positive = ", ".join(decision.get("positive_traits") or []) or "なし"
    negative = ", ".join(decision.get("negative_traits") or []) or "なし"
    return (
        f"- 相対上位群に固有の形式特性: {positive}\n"
        f"- 相対下位群に固有の形式特性: {negative}\n"
        "- これは相関にもとづく実験仮説であり、因果の証明ではない。次回1本の生成で"
        "この形式変数だけを反映し、上位動画の題材は再利用しない（反映作業は運用者が手動で行う）。"
    )


def _evaluation_text(evaluations: list[dict]) -> str:
    if not evaluations:
        return "- 判定材料不足（前回提案の適用動画が未検出、または指標が十分に育っていません）"
    lines = [
        "- 注意: 「適用動画」は運用者が実際に仮説を反映した確証ではなく、"
        "提案traitがその後投稿された動画に出現しただけの自動検知（偶然の一致を含みうる）。"
    ]
    for row in evaluations:
        result = row.get("result") or {}
        verdict = "effective" if result.get("effective") else "ineffective"
        lines.append(
            f"- trait `{row.get('trait')}` / 適用動画 `{row.get('video_id')}`: "
            f"**{verdict}**（score={result.get('score')} / "
            f"peer_median={result.get('peer_median')} / "
            f"reason={result.get('reason') or 'なし'}）"
        )
    return "\n".join(lines)


def fingerprint(channel_id: str, sections: list[dict]) -> str:
    seed = {
        "channel": channel_id,
        "corners": [
            {
                "corner": s["corner"],
                "decision_id": s["decision"].get("decision_id"),
                "metric": s["decision"].get("metric"),
                "format_cohort": s["decision"].get("format_cohort"),
                "positive_traits": sorted(s["decision"].get("positive_traits") or []),
                "negative_traits": sorted(s["decision"].get("negative_traits") or []),
                "proposal": s["proposal"]["hypothesis_key"] if s.get("proposal") else None,
                "reported_experiment_ids": sorted(
                    str(e.get("experiment_id") or "") for e in s.get("evaluations") or []
                ),
            }
            for s in sections
        ],
    }
    return hashlib.sha256(
        json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _cycle_title(spec: ChannelSpec, now: datetime, fp: str) -> str:
    return f"[feedback] {spec.id} 実績レポート {now.date().isoformat()} ({fp[:8]})"


def _normalise_term(value: str) -> str:
    """検索語句の正規化（空白畳み込み＋小文字化）。完全一致判定に使う。"""
    return " ".join(str(value or "").split()).casefold()


def _gap_match_status(gap_query: str, terms: list[dict]) -> str:
    """gap_query と実検索語句の対応を判定する（issue #164）。

    - `matched`: gap_query が実検索語句と正規化完全一致
    - `not_confirmed`: 取得できた上位語句に完全一致がない。意味的一致や上位25件
      外に存在する可能性を否定しない（「流入語句なし」と断定しない）
    - `not_evaluated`: gap_query または実検索語句が取得できていない（推測しない）
    """
    gap = _normalise_term(gap_query)
    if not gap or not terms:
        return "not_evaluated"
    actual = {_normalise_term(item.get("term")) for item in terms}
    return "matched" if gap in actual else "not_confirmed"


def _clean_text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _discovery_satisfaction_text(snapshot: dict | None, corner: str) -> str:
    """検索発見（Discovery）と視聴後評価（Satisfaction）を分離して表示する。

    issue #164: コンテンツギャップ企画の検証は「狙った検索需要から見つけられたか」
    （検索流入）と「見つけた視聴者が視聴を続けたか」（維持率）を別々に扱う。
    Analytics APIが返さない指標は0や「なし」と断定せず、取得不可と明記する。
    """
    if not isinstance(snapshot, dict):
        return "- Discovery / Satisfaction: snapshot未取得のため評価しません"
    corner_videos = [
        row
        for row in snapshot.get("videos", [])
        if str(row.get("corner") or "") == corner
    ]
    gap_videos: list[tuple[dict, str]] = []
    for row in corner_videos:
        metadata = row.get("topic_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        gap_query = _clean_text(metadata.get("gap_query"), limit=200)
        if gap_query:
            gap_videos.append((row, gap_query))
    if not corner_videos:
        return "- Discovery / Satisfaction: このcornerの動画がsnapshotにありません"
    if not gap_videos:
        return (
            "- Discovery / Satisfaction: このcornerにコンテンツギャップ企画"
            "（gap_query記録）の動画がありません。通常動画は対象外です"
        )
    discovery_lines: list[str] = []
    satisfaction_lines: list[str] = []

    def format_terms(terms: list[dict]) -> str:
        if not terms:
            return ""
        top_terms = ", ".join(
            f"「{t.get('term')}」({int(t.get('views', 0) or 0)}回)"
            for t in sorted(
                terms,
                key=lambda item: int(item.get("views", 0) or 0),
                reverse=True,
            )[:3]
        )
        return f"  - 検索語句: {top_terms}"

    for row, gap_query in gap_videos:
        video_id = str(row.get("video_id") or "")
        analytics = row.get("analytics")
        analytics = analytics if isinstance(analytics, dict) else {}
        traffic = analytics.get("traffic_sources")
        traffic = traffic if isinstance(traffic, dict) else {}
        terms = analytics.get("search_terms")
        terms = terms if isinstance(terms, list) else []
        total_views = int(analytics.get("views", 0) or 0)
        search_views = int(traffic.get("YT_SEARCH", 0) or 0)
        gap_line = ""
        gap_status = _gap_match_status(gap_query, terms)
        if gap_query:
            gap_line = {
                "matched": f"（狙った検索語「{gap_query}」と完全一致）",
                "not_confirmed": (
                    f"（取得できた上位語句に「{gap_query}」の完全一致なし。"
                    "意味的一致・上位25件外は未評価）"
                ),
                "not_evaluated": "（gap_queryとの一致判定は材料不足で保留）",
            }[gap_status]
        if total_views > 0 and search_views > 0:
            share = search_views * 100.0 / total_views
            discovery_lines.append(
                f"- `{video_id}`: YouTube検索からの視聴 {search_views} 回"
                f"（全体の {share:.1f}%）{gap_line}"
            )
            term_line = format_terms(terms)
            if term_line:
                discovery_lines.append(term_line)
        elif terms:
            # traffic sourceのバッチ取得が失敗しても、検索語句（動画個別取得）
            # が成功していれば実データを表示する。流入回数は不明として
            # 0や「なし」と断定しない（Claude review指摘）。
            discovery_lines.append(
                f"- `{video_id}`: YouTube検索からの流入回数は取得できませんでした"
                f"（traffic source取得不可）。検索語句は取得済み{gap_line}"
            )
            term_line = format_terms(terms)
            if term_line:
                discovery_lines.append(term_line)
        else:
            discovery_lines.append(
                f"- `{video_id}`: YouTube検索からの流入を取得できませんでした"
                "（Analytics APIが返さない場合は推測で補いません）"
            )
        avg_percent = analytics.get("average_view_percentage")
        if avg_percent is not None:
            satisfaction_lines.append(
                f"- `{video_id}`: 平均視聴維持率 {float(avg_percent):.1f}%"
            )
        else:
            satisfaction_lines.append(
                f"- `{video_id}`: 維持率を取得できませんでした（推測で補いません）"
            )
    return (
        "### 検索発見（Discovery）\n\n"
        + "\n".join(discovery_lines)
        + "\n\n### 視聴後評価（Satisfaction）\n\n"
        + "\n".join(satisfaction_lines)
    )


def _opening_signal_for_row(row: dict) -> dict | None:
    analytics = row.get("analytics")
    analytics = analytics if isinstance(analytics, dict) else {}
    curve = analytics.get("retention_curve")
    if not isinstance(curve, list) or not curve:
        return None
    data_api = row.get("data_api")
    data_api = data_api if isinstance(data_api, dict) else {}
    duration_iso = str(data_api.get("duration") or "")
    return performance.opening_retention_signal(
        curve,
        duration_iso,
        _script_for_video(row),
    )


_OPENING_REPORT_LIMIT = 10


def _opening_retention_text(snapshot: dict | None, corner: str) -> str:
    """冒頭30秒の維持率低下と、次の1本で試す変更を表示する（issue #142）。"""
    if not isinstance(snapshot, dict):
        return "- 冒頭30秒: snapshot未取得のため評価しません"
    retention_status = snapshot.get("retention_curve")
    retention_status = (
        retention_status if isinstance(retention_status, dict) else {}
    )
    if not retention_status.get("available"):
        reason = str(retention_status.get("reason") or "取得不可")
        return (
            "- 冒頭30秒: 維持率カーブの取得に失敗しました"
            f"（{reason}。推測で補いません）"
        )

    corner_videos = [
        row
        for row in snapshot.get("videos", [])
        if str(row.get("corner") or "") == corner
    ]
    if not corner_videos:
        return "- 冒頭30秒: このcornerの動画がsnapshotにありません"

    failed = set(retention_status.get("failed_video_ids") or [])
    analysed = 0
    unavailable = 0
    actionable: list[tuple[dict, dict]] = []
    for row in corner_videos:
        video_id = str(row.get("video_id") or "")
        if video_id in failed:
            unavailable += 1
            continue
        analytics = row.get("analytics")
        analytics = analytics if isinstance(analytics, dict) else {}
        # Analytics対象外の動画は「取得失敗」に数えない。
        if "retention_curve" not in analytics:
            continue
        signal = _opening_signal_for_row(row)
        if signal is None:
            unavailable += 1
            continue
        analysed += 1
        if signal.get("actionable"):
            actionable.append((row, signal))

    if analysed == 0:
        suffix = f"（判定材料不足 {unavailable}本）" if unavailable else ""
        return (
            "- 冒頭30秒: 分析可能な維持率カーブがありません"
            f"{suffix}。推測で補いません"
        )

    def priority(item: tuple[dict, dict]) -> tuple[float, float, str]:
        row, signal = item
        published = history._parse_ts(
            row.get("history_ts") or row.get("published_at")
        )
        timestamp = published.timestamp() if published is not None else float("-inf")
        severity = max(
            float(signal.get("cumulative_drop_ratio") or 0.0),
            float(signal.get("largest_step_drop_ratio") or 0.0),
        )
        return timestamp, severity, str(row.get("video_id") or "")

    # 次の1本の判断には新しい実績を優先し、同時刻なら低下幅が大きい順にする。
    # snapshot自体はhistory_ts昇順なので、入力順のまま先頭10本を採ると
    # 最新動画が常に省略される（Sol review指摘）。
    actionable.sort(key=priority, reverse=True)

    lines = [
        f"- 分析対象 {analysed}本 / 冒頭低下シグナル {len(actionable)}本"
        + (f" / 判定材料不足 {unavailable}本" if unavailable else "")
    ]
    for row, signal in actionable[:_OPENING_REPORT_LIMIT]:
        video_id = str(row.get("video_id") or "")
        cumulative_points = signal["cumulative_drop_ratio"] * 100
        step_points = signal["largest_step_drop_ratio"] * 100
        lines.append(
            f"- `{video_id}`: 最初の観測点 約{signal['start_seconds']:.1f}秒 "
            f"{signal['start_watch_ratio'] * 100:.1f}% → "
            f"冒頭{signal['window_seconds']:.1f}秒内の最終観測点 "
            f"約{signal['end_seconds']:.1f}秒 {signal['end_watch_ratio'] * 100:.1f}%"
            f"（累計 {cumulative_points:.1f}ポイント低下）"
        )
        drop_from = signal.get("drop_from_seconds")
        drop_to = signal.get("drop_to_seconds")
        scene = " ".join(str(signal.get("scene_caption") or "").split())[:120]
        if drop_from is not None and drop_to is not None:
            location = f"約{drop_from:.1f}→{drop_to:.1f}秒"
            if scene:
                location += f"（シーン: {scene}）"
            lines.append(
                f"  - 最大区間低下: {location}で {step_points:.1f}ポイント"
            )
    if len(actionable) > _OPENING_REPORT_LIMIT:
        lines.append(
            f"- 他にも{len(actionable) - _OPENING_REPORT_LIMIT}本ありますが、"
            f"詳細は先頭{_OPENING_REPORT_LIMIT}本まで表示します"
        )
    if actionable:
        lines.append(
            "- 次の1本: 上記区間の実際の映像・台本を確認し、同じcorner・近い尺/tierで"
            "冒頭フックだけを変更する。他の中心変数は固定し、同じ冒頭ウィンドウの"
            "累計低下と最大区間低下を比較する（反映は運用者が手動で行う）。"
        )
    else:
        lines.append(
            "- 8ポイント以上の冒頭低下を検出しなかったため、冒頭フック変更を"
            "このデータだけから提案しません。"
        )
    lines.append(
        "- 8ポイントはレポート対象を絞る検知閾値であり、万能な合格ラインではありません。"
        "低下の原因は動画内容と照合し、相関を因果と断定しません。"
    )
    return "\n".join(lines)


def _retention_curve_text(snapshot: dict | None, corner: str) -> str:
    """維持率カーブの山/谷とシーン照合を表示する（issue #149）。

    山=成功・谷=失敗と断定せず、「何秒付近・どのシーン」を事実として並べ、
    理由の確認は運用者が動画内容と照合して行う、と明記する。
    """
    if not isinstance(snapshot, dict):
        return "- 維持率カーブ: snapshot未取得のため評価しません"
    retention_status = snapshot.get("retention_curve")
    retention_status = (
        retention_status if isinstance(retention_status, dict) else {}
    )
    if not retention_status.get("available"):
        reason = str(retention_status.get("reason") or "取得不可")
        return (
            "- 維持率カーブ: 取得に失敗しました"
            f"（{reason}。推測で補いません）"
        )
    corner_videos = [
        row
        for row in snapshot.get("videos", [])
        if str(row.get("corner") or "") == corner
    ]
    if not corner_videos:
        return "- 維持率カーブ: このcornerの動画がsnapshotにありません"
    failed = retention_status.get("failed_video_ids") or []
    lines: list[str] = []
    reported_moments = 0
    truncated = False
    for row in corner_videos:
        video_id = str(row.get("video_id") or "")
        if video_id in failed:
            lines.append(
                f"- `{video_id}`: 維持率カーブを取得できませんでした"
                "（動画固有エラー。推測で補いません）"
            )
            continue
        analytics = row.get("analytics")
        analytics = analytics if isinstance(analytics, dict) else {}
        curve = analytics.get("retention_curve")
        # 照会対象外（Analytics実績が無い古い動画等）はanalyticsに
        # retention_curve キー自体が無いため、取得不可と誤表示しない。
        if "retention_curve" not in analytics:
            continue
        curve = curve if isinstance(curve, list) else []
        if not curve:
            lines.append(
                f"- `{video_id}`: 維持率カーブを取得できませんでした"
                "（Shorts等ではAPIが返さない場合があります。推測で補いません）"
            )
            continue
        moments = performance.retention_moments(curve)
        script = _script_for_video(row)
        data_api = row.get("data_api")
        data_api = data_api if isinstance(data_api, dict) else {}
        duration_iso = str(data_api.get("duration") or "")
        annotated = performance.retention_moment_scenes(
            moments, script, duration_iso
        )
        if not annotated:
            lines.append(
                f"- `{video_id}`: 維持率カーブに明瞭な山/谷を検出しませんでした"
                "（形状だけで成功・失敗は断定しません）"
            )
            continue
        if reported_moments >= 10:
            truncated = True
            break
        lines.append(f"- `{video_id}`: 維持率カーブの山/谷")
        for moment in annotated:
            if reported_moments >= 10:
                truncated = True
                break
            reported_moments += 1
            kind = "山（spike）" if moment["kind"] == "spike" else "谷（dip）"
            second = moment.get("elapsed_seconds")
            scene = moment.get("scene_caption")
            if second is None:
                lines.append(
                    f"  - {kind}: 位置不明（動画長を取得できませんでした）。"
                    "該当箇所の内容と照合して理由を確認してください"
                )
            elif scene:
                lines.append(
                    f"  - {kind}: 約{second}秒付近（シーン: {scene}）。"
                    "該当箇所の内容と照合して理由を確認してください"
                )
            else:
                lines.append(
                    f"  - {kind}: 約{second}秒付近。"
                    "該当箇所の内容と照合して理由を確認してください"
                )
        if truncated:
            break
    if truncated:
        lines.append(
            "- 他にも山/谷がありますが、レポートは先頭10件まで表示します"
        )
    lines.append(
        "- 山/谷は再視聴・巻き戻し・スキップ・離脱のいずれかが起きた場所の手がかり"
        "であり、それだけで成功・失敗を判定しません。"
    )
    return "\n".join(lines)


def _subscribed_status_comparison_for_row(row: dict) -> dict:
    analytics = row.get("analytics")
    analytics = analytics if isinstance(analytics, dict) else {}
    data_api = row.get("data_api")
    data_api = data_api if isinstance(data_api, dict) else {}
    return performance.subscribed_status_retention_comparison(
        analytics.get("retention_by_subscribed_status"),
        str(data_api.get("duration") or ""),
        _script_for_video(row),
    )


def _subscribed_status_retention_text(snapshot: dict | None, corner: str) -> str:
    """購読者/非購読者の維持率差と流入元viewsを分離表示する（issue #128）。"""
    if not isinstance(snapshot, dict):
        return "- 購読状態別の維持率: snapshot未取得のため評価しません"
    status = snapshot.get("retention_by_subscribed_status")
    status = status if isinstance(status, dict) else {}
    if not status.get("available"):
        reason = str(status.get("reason") or "取得不可")
        return (
            "- 購読状態別の維持率: 取得に失敗しました"
            f"（{reason}。推測で補いません）"
        )
    queried = set(status.get("queried_video_ids") or [])
    rows = [
        row
        for row in snapshot.get("videos", [])
        if str(row.get("corner") or "") == corner
        and str(row.get("video_id") or "") in queried
    ]
    if not rows:
        return (
            "- 購読状態別の維持率: このcornerに比較対象の最新動画がありません"
        )

    def priority(row: dict) -> tuple[float, str]:
        published = history._parse_ts(
            row.get("published_at") or row.get("history_ts")
        )
        return (
            published.timestamp() if published is not None else float("-inf"),
            str(row.get("video_id") or ""),
        )

    rows.sort(key=priority, reverse=True)
    failed = set(status.get("failed_video_ids") or [])
    traffic_status = snapshot.get("traffic_sources")
    traffic_status = traffic_status if isinstance(traffic_status, dict) else {}
    traffic_available = bool(traffic_status.get("available"))
    lines: list[str] = []
    if not traffic_available:
        reason = str(traffic_status.get("reason") or "取得不可")
        lines.append(
            "- 流入元views: 取得に失敗しました"
            f"（{reason}。維持率とは結合せず、推測で補いません）"
        )
    actionable = 0
    for row in rows:
        video_id = str(row.get("video_id") or "")
        analytics = row.get("analytics")
        analytics = analytics if isinstance(analytics, dict) else {}
        traffic = analytics.get("traffic_sources")
        traffic = traffic if isinstance(traffic, dict) else {}
        if traffic_available:
            sources = sorted(
                (
                    (str(source), int(views))
                    for source, views in traffic.items()
                    if isinstance(views, int)
                    and not isinstance(views, bool)
                    and views >= 0
                ),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )[:3]
            if sources:
                lines.append(
                    f"- `{video_id}` 流入元views（維持率とは結合しません）: "
                    + ", ".join(f"{source}={views}" for source, views in sources)
                )
            else:
                lines.append(
                    f"- `{video_id}` 流入元views: API取得は成功しましたが、"
                    "この動画の内訳データが返らず未評価です"
                    "（0とはみなしません）"
                )
        if video_id in failed:
            lines.append(
                f"- `{video_id}`: 購読状態別カーブを取得できませんでした"
                "（動画またはsegment固有。推測で補いません）"
            )
            continue
        comparison = _subscribed_status_comparison_for_row(row)
        if comparison["status"] == "insufficient_data":
            lines.append(
                f"- `{video_id}`: 判定材料不足"
                f"（信頼可能な共通点 {comparison['reliable_point_count']} / "
                f"必要 {comparison['min_common_points']}、各segment "
                f"{comparison['min_segment_impressions']} observations以上）"
            )
            continue
        gap_points = abs(float(comparison["gap_ratio"])) * 100
        subscribed_percent = float(comparison["subscribed_watch_ratio"]) * 100
        unsubscribed_percent = float(comparison["unsubscribed_watch_ratio"]) * 100
        second = comparison.get("elapsed_seconds")
        scene = _clean_text(comparison.get("scene_caption"), limit=120)
        location = (
            f"約{float(second):.1f}秒付近" if second is not None else "位置不明"
        )
        if scene:
            location += f"（シーン: {scene}）"
        lines.append(
            f"- `{video_id}` {location}: 購読者 {subscribed_percent:.1f}%"
            f"（{comparison['subscribed_segment_impressions']} observations） / "
            f"非購読者 {unsubscribed_percent:.1f}%"
            f"（{comparison['unsubscribed_segment_impressions']} observations） / "
            f"差 {gap_points:.1f}ポイント"
        )
        if comparison.get("actionable"):
            actionable += 1

    if actionable:
        lines.append(
            "- 次の1本: 差が大きい箇所の実際の内容を確認し、低い側に必要だった"
            "前提・導入・説明順の仮説を1つだけ選ぶ。同じcorner・近い尺/tierで"
            "他の中心変数を固定し、同じ購読状態別カーブで再確認する"
            "（反映は運用者が手動で行う）。"
        )
    else:
        lines.append(
            "- 8ポイント以上の明瞭なsegment差を検出しなかったため、"
            "このデータだけから次の変更を提案しません。"
        )
    lines.append(
        "- SUBSCRIBED/UNSUBSCRIBEDは閲覧時の購読状態であり、"
        "リピーター/新規視聴者ではありません。流入元viewsとも直接結合せず、"
        "差を因果と断定しません。"
    )
    return "\n".join(lines)


_SHARE_DISPLAY_LIMIT = 20


def _share_metrics(row: dict) -> tuple[int, int] | None:
    """共有率計算用に shares/views を厳格に正規化する（issue #144）。

    `share_30d`（過去30日集計）から取り、欠落・不正・負数・views<=0 は
    None を返す（共有率を算出しない。fail-closed）。表示と候補判定の両方で
    この関数を使うことで、欠損時の挙動を一致させる。
    """
    share_30d = row.get("share_30d")
    if not isinstance(share_30d, dict):
        return None
    shares = share_30d.get("shares")
    views = share_30d.get("views")
    if shares is None or views is None:
        return None
    if isinstance(shares, bool) or isinstance(views, bool):
        return None
    try:
        shares_float = float(shares)
        views_float = float(views)
    except (TypeError, ValueError):
        return None
    # 小数は整数値であることを確認（100.9 を100へ切り捨てない）。
    if not shares_float.is_integer() or not views_float.is_integer():
        return None
    shares_int = int(shares_float)
    views_int = int(views_float)
    if shares_int < 0 or views_int <= 0:
        return None
    return shares_int, views_int


def _share_text(snapshot: dict | None, corner: str) -> str:
    """共有率（shares/views）と1%超動画の構造を表示する（issue #144）。

    issue #144 の対象は shorts のみ。`share_30d`（過去30日集計）を使って
    共有率を算出し、1%超の動画の構造（format_traits）を優先表示する。
    構造付きの1%超動画が1本もない場合は、1%以下の動画を最大5本まで参考表示する
    （構造未記録の1%超動画がある場合は件数要約のみ）。表示上限
    （`_SHARE_DISPLAY_LIMIT`）を超えない。再生数偏重の評価を避けるための補助指標。
    """
    if corner != "shorts":
        return "- 共有率: この節は shorts のみ対象です"
    if not isinstance(snapshot, dict):
        return "- 共有率: snapshot未取得のため評価しません"
    corner_videos = [
        row
        for row in snapshot.get("videos", [])
        if str(row.get("corner") or "") == corner
    ]
    if not corner_videos:
        return "- 共有率: このcornerの動画がsnapshotにありません"
    missing_count = 0
    below_or_missing: list[str] = []
    scored: list[tuple[float, str]] = []
    no_trait_over_one_percent = 0
    for row in corner_videos:
        video_id = str(row.get("video_id") or "")
        metrics = _share_metrics(row)
        if metrics is None:
            missing_count += 1
            continue
        shares_int, views_int = metrics
        rate = shares_int * 100.0 / views_int
        line = f"- `{video_id}`: 共有率 {rate:.3f}%（共有 {shares_int} / 再生 {views_int}）"
        if shares_int * 100 > views_int:
            traits = row.get("format_traits") or []
            if traits:
                trait_text = ", ".join(str(t) for t in traits)
                scored.append(
                    (
                        -rate,
                        f"{line} / 構造: {trait_text}",
                    )
                )
            else:
                no_trait_over_one_percent += 1
        else:
            below_or_missing.append(line)
    lines: list[str] = []
    shown = 0
    # 構造付き（format_traitsあり）を共有率降順で並べる。構造未記録の
    # 1%超動画は個別表示せず件数要約（Sol review指摘）。
    for _neg_rate, line in sorted(scored, key=lambda item: item[0]):
        if shown >= _SHARE_DISPLAY_LIMIT:
            lines.append(
                f"- 他にも共有率1%超の動画があります（先頭{_SHARE_DISPLAY_LIMIT}件のみ表示）"
            )
            break
        lines.append(line)
        shown += 1
    if no_trait_over_one_percent:
        lines.append(
            f"- 構造未記録の共有率1%超: {no_trait_over_one_percent} 本"
        )
    if not scored and not no_trait_over_one_percent and below_or_missing:
        for line in below_or_missing[:5]:
            lines.append(line)
        remaining = len(below_or_missing) - 5
        if remaining > 0:
            lines.append(f"- 他 {remaining} 本は共有率1%以下")
    elif below_or_missing:
        lines.append(f"- 他 {len(below_or_missing)} 本は共有率1%以下（一覧省略）")
    if missing_count:
        lines.append(
            f"- {missing_count} 本は共有率を算出できませんでした"
            "（30日データが無いか不正。推測で補いません）"
        )
    if scored:
        lines.insert(0, "共有率1%超の動画の構造（次の企画の材料）:")
    lines.append(
        "- 共有率は視聴者の能動的な評価の一つの手がかりであり、"
        "再生数だけの評価を避けるための補助指標です。"
    )
    return "\n".join(lines)


def _is_share_over_one_percent(row: dict) -> bool:
    """共有率（shares/views）が1%を超えるかを判定する（issue #144）。

    共有率は視聴者の能動的な評価の一つの手がかり。`share_30d` の欠落・不正・
    views<=0 は False（共有率を算出しない）。1%ちょうどは超えない
    （整数比較 `shares*100 > views`）。"""
    metrics = _share_metrics(row)
    if metrics is None:
        return False
    shares_int, views_int = metrics
    return shares_int * 100 > views_int


def _script_for_video(row: dict) -> dict:
    """snapshotの動画行からscript.jsonを読み込む（無ければ空dict）。"""
    workdir = row.get("workdir")
    if not workdir:
        return {}
    try:
        data = json.loads(
            (Path(str(workdir)) / "script.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cycle_body(
    spec: ChannelSpec,
    sections: list[dict],
    fp: str,
    now: datetime,
    snapshot: dict | None = None,
) -> str:
    lines: list[str] = [
        feedback_issues.feedback_marker(fp),
        feedback_issues.channel_marker(spec.id),
    ]
    for section in sections:
        if section.get("proposal") is not None:
            lines.append(
                feedback_issues.hypothesis_marker(section["proposal"]["hypothesis_key"])
            )
    lines += ["", f"# {spec.id} 実績レポート {now.date().isoformat()}"]
    for section in sections:
        decision = section["decision"]
        lines += [
            "",
            f"## corner: {section['corner']}",
            "",
            "### 調査内容",
            "",
            _investigation_text(decision),
            "",
            "### 実験内容（1回に変更する形式変数）",
            "",
            _hypothesis_text(decision, section),
            "",
            "### 前回提案の効果検証",
            "",
            _evaluation_text(section.get("evaluations") or []),
            "",
            _discovery_satisfaction_text(snapshot, section["corner"]),
            "",
            "### 冒頭30秒の維持率と次の1本（issue #142）",
            "",
            _opening_retention_text(snapshot, section["corner"]),
            "",
            "### 維持率カーブの山/谷とシーン照合（issue #149）",
            "",
            _retention_curve_text(snapshot, section["corner"]),
            "",
            "### 購読状態別の維持率と流入元（issue #128）",
            "",
            _subscribed_status_retention_text(snapshot, section["corner"]),
        ]
        if section["corner"] == "shorts":
            lines += [
                "",
                "### 共有率と共有される動画の構造（issue #144）",
                "",
                _share_text(snapshot, section["corner"]),
            ]
    lines += [
        "",
        "## ガードレール",
        "",
        "- 相関を因果と断定しない",
        "- 高実績動画の題材そのものを再利用しない",
        "- topic cooldownを常に優先する",
        "- 一度に試す変数は1つ",
        "- この仮説を実際の生成へ反映する作業は運用者が手動で行う（システムは自動適用しない）",
    ]
    return "\n".join(lines) + "\n"


def build_cycle_candidate(
    spec: ChannelSpec,
    sections: list[dict],
    now: datetime,
    snapshot: dict | None = None,
) -> dict | None:
    has_section_content = any(
        section.get("proposal") is not None or section.get("evaluations")
        for section in sections
    )
    # issue #164: 形式仮説の有無に関わらず、gap動画の検索発見・視聴後評価
    # が揃っていれば報告候補として扱う（snapshot未指定の従来呼び出しは
    # 従来どおりsection内容だけで判定）。
    # 表示対象はsectionのcornerと一致する動画だけ。cornerがどのsectionにも
    # 存在しない動画のgap_queryは、無内容issueを防ぐため候補判定に含めない
    # （Claude review指摘）。
    has_gap_discovery = False
    has_opening_content = False
    has_retention_content = False
    has_subscribed_retention_content = False
    has_share_content = False
    if isinstance(snapshot, dict):
        section_corners = {section["corner"] for section in sections}
        has_gap_discovery = any(
            str((row.get("topic_metadata") or {}).get("gap_query") or "").strip()
            and str(row.get("corner") or "") in section_corners
            for row in snapshot.get("videos", [])
        )
        # issue #142/#149: 形式仮説・gap動画が無くても、matching cornerに
        # 冒頭低下シグナルまたは明瞭な山/谷があれば候補として報告する。
        # 無内容issueは防ぎつつ、分析結果を次の1本の手動施策へつなぐ。
        retention_status = snapshot.get("retention_curve")
        retention_status = (
            retention_status if isinstance(retention_status, dict) else {}
        )
        failed = set(retention_status.get("failed_video_ids") or [])
        if retention_status.get("available"):
            for row in snapshot.get("videos", []):
                if str(row.get("corner") or "") not in section_corners:
                    continue
                if str(row.get("video_id") or "") in failed:
                    continue
                analytics = row.get("analytics")
                analytics = analytics if isinstance(analytics, dict) else {}
                curve = analytics.get("retention_curve")
                if not isinstance(curve, list) or not curve:
                    continue
                opening_signal = _opening_signal_for_row(row)
                if opening_signal and opening_signal.get("actionable"):
                    has_opening_content = True
                if performance.retention_moments(curve):
                    has_retention_content = True
                if has_opening_content and has_retention_content:
                    break
        subscribed_status = snapshot.get("retention_by_subscribed_status")
        subscribed_status = (
            subscribed_status if isinstance(subscribed_status, dict) else {}
        )
        queried = set(subscribed_status.get("queried_video_ids") or [])
        failed_subscribed = set(subscribed_status.get("failed_video_ids") or [])
        if subscribed_status.get("available"):
            for row in snapshot.get("videos", []):
                video_id = str(row.get("video_id") or "")
                if str(row.get("corner") or "") not in section_corners:
                    continue
                if video_id not in queried or video_id in failed_subscribed:
                    continue
                comparison = _subscribed_status_comparison_for_row(row)
                if comparison.get("actionable"):
                    has_subscribed_retention_content = True
                    break
        # issue #144: 共有率1%超の動画がmatching cornerにあれば報告候補とする。
        # 対象はshorts cornerのみ。1%超でも構造（format_traits）が未記録なら
        # 次の企画の材料にならないため候補にしない（Sol review指摘）。
        has_share_content = any(
            str(row.get("corner") or "") == "shorts"
            and str(row.get("corner") or "") in section_corners
            and _is_share_over_one_percent(row)
            and bool(row.get("format_traits"))
            for row in snapshot.get("videos", [])
        )
    has_content = (
        has_section_content
        or has_gap_discovery
        or has_opening_content
        or has_retention_content
        or has_subscribed_retention_content
        or has_share_content
    )
    if not has_content:
        return None
    fp = fingerprint(spec.id, sections)
    hypothesis_keys = [
        section["proposal"]["hypothesis_key"]
        for section in sections
        if section.get("proposal") is not None
    ]
    return {
        "fingerprint": fp,
        "hypothesis_keys": hypothesis_keys,
        "title": _cycle_title(spec, now, fp),
        "body": _cycle_body(spec, sections, fp, now, snapshot),
    }


# --- 起動間隔ゲート: output/<channel>/performance_report_state.json ---


def _state_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / "performance_report_state.json"


def _load_state(spec: ChannelSpec) -> dict:
    path = _state_path(spec)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(spec: ChannelSpec, state: dict) -> None:
    path = _state_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _interval_elapsed(spec: ChannelSpec, now: datetime) -> bool:
    """直近runから`PERFORMANCE_REPORT_MIN_INTERVAL_HOURS`時間経過していればTrue。"""
    last_run_at = _load_state(spec).get("last_run_at")
    if not last_run_at:
        return True
    last_ts = history._parse_ts(last_run_at)
    if last_ts is None:
        return True
    elapsed_hours = (now - last_ts).total_seconds() / 3600.0
    return elapsed_hours >= config.PERFORMANCE_REPORT_MIN_INTERVAL_HOURS


# --- オーケストレーション ---


def run_channel(
    spec: ChannelSpec, *, now: datetime | None = None, apply: bool = False
) -> dict:
    reference = now or datetime.now(timezone.utc)
    if not spec.pipeline_get("performance_feedback", False):
        return {"channel": spec.id, "status": "skipped", "reason": "performance_feedback_disabled"}
    repository = spec.pipeline_get("feedback_repository", "")
    if not repository:
        return {"channel": spec.id, "status": "skipped", "reason": "no_repository"}
    if apply and not _interval_elapsed(spec, reference):
        return {"channel": spec.id, "status": "skipped", "reason": "interval_not_elapsed"}

    if not apply:
        return _run_channel_body(spec, reference, apply=False)
    # apply時のみ、このチャンネルの実験状態遷移・issue投稿・intervalタイマー
    # 更新を1プロセスに直列化する（同一チャンネルへの並行`--apply`実行が
    # performance_experiments.jsonlへ重複した状態遷移行を書く事故を防ぐ）。
    # `feedback_issues.submit_candidate`が内部で取得する別ファイルのロック
    # とは独立しているため、ここでのロック保持中に呼んでも自己デッドロック
    # しない。
    with _operation_lock(spec):
        return _run_channel_body(spec, reference, apply=True)


def _run_channel_body(spec: ChannelSpec, reference: datetime, *, apply: bool) -> dict:
    snapshot = performance.sync(spec)
    recent_hyp_keys = feedback_issues.recent_hypothesis_keys(spec, now=reference)
    # 全corner分の実験状態遷移を1回だけ進める（corner毎に呼ぶと同じ実験を
    # 複数回遷移させ、JSONLへ重複行を書いてしまうため）。
    evaluations_by_corner = _progress_experiments(spec, snapshot, reference, apply=apply)

    sections = []
    for corner_key in sorted(spec.corners):
        decision = performance.build_decision(spec, snapshot, corner_key=corner_key)
        evaluations = evaluations_by_corner.get(corner_key, [])
        sections.append(
            build_corner_section(spec, corner_key, decision, evaluations, recent_hyp_keys)
        )

    candidate = build_cycle_candidate(spec, sections, reference, snapshot)
    if candidate is None:
        result = {
            "channel": spec.id,
            "status": "no_content",
            "corners": [s["corner"] for s in sections],
        }
    else:
        submission = feedback_issues.submit_candidate(spec, candidate, apply=apply)
        created = submission.get("created")
        if not apply:
            status = "dry_run"
        elif created:
            status = "submitted"
        else:
            status = "skipped"
        result = {"channel": spec.id, "status": status, "submission": submission}
        if apply and created:
            _record_proposed_experiments(spec, sections, created, reference)
            for section in sections:
                if section.get("evaluations"):
                    _mark_reported(spec, section["evaluations"], created["number"], reference)

    if apply:
        _save_state(spec, {"last_run_at": reference.isoformat()})
    return result


def run_all(*, apply: bool = False, now: datetime | None = None) -> tuple[dict, int]:
    reference = now or datetime.now(timezone.utc)
    results: list[dict] = []
    for channel_id in channel.discover():
        try:
            spec = channel.load(channel_id)
            result = run_channel(spec, now=reference, apply=apply)
            results.append(result)
        except Exception as exc:  # 1チャンネル失敗でも残りを逐次実行する
            _log(f"channel={channel_id} ERROR: {exc}")
            results.append({"channel": channel_id, "status": "error", "error": str(exc)})
    succeeded = sum(
        item.get("status") in ("submitted", "no_content", "dry_run") for item in results
    )
    skipped = sum(item.get("status") == "skipped" for item in results)
    failed = len(results) - succeeded - skipped
    summary = {
        "mode": "all_channels",
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "channels": results,
    }
    return summary, 0 if (succeeded or skipped) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="実績フィードバックの3日毎レポートissueサイクル"
    )
    parser.add_argument("--channel", help="チャンネルID（省略時は全チャンネル）")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実験状態の記録・GitHub issue作成を実際に行う（未指定時はdry-run）",
    )
    args = parser.parse_args()
    if args.channel:
        spec = channel.load(args.channel)
        result = run_channel(spec, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") != "error" else 1
    summary, exit_code = run_all(apply=args.apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
