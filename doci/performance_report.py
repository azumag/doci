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
        if total_views > 0 and search_views > 0:
            share = search_views * 100.0 / total_views
            gap_status = _gap_match_status(gap_query, terms)
            gap_line = {
                "matched": f"（狙った検索語「{gap_query}」と完全一致）",
                "not_confirmed": (
                    f"（取得できた上位語句に「{gap_query}」の完全一致なし。"
                    "意味的一致・上位25件外は未評価）"
                ),
                "not_evaluated": "（gap_queryとの一致判定は材料不足で保留）",
            }[gap_status]
            discovery_lines.append(
                f"- `{video_id}`: YouTube検索からの視聴 {search_views} 回"
                f"（全体の {share:.1f}%）{gap_line}"
            )
            if terms:
                top_terms = ", ".join(
                    f"「{t.get('term')}」({int(t.get('views', 0) or 0)}回)"
                    for t in sorted(
                        terms,
                        key=lambda item: int(item.get("views", 0) or 0),
                        reverse=True,
                    )[:3]
                )
                discovery_lines.append(f"  - 検索語句: {top_terms}")
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
    if isinstance(snapshot, dict):
        section_corners = {section["corner"] for section in sections}
        has_gap_discovery = any(
            str((row.get("topic_metadata") or {}).get("gap_query") or "").strip()
            and str(row.get("corner") or "") in section_corners
            for row in snapshot.get("videos", [])
        )
    has_content = has_section_content or has_gap_discovery
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
