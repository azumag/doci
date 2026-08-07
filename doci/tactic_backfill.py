"""過去動画のnarrationからYouTube運用施策(viewer_action)を抽出して保存する(issue #106)。

`doci.tactic_issues`(issue #90)はresearch段階で保存された`viewer_action`のみを
対象にしており、施策抽出が導入される前に公開された過去動画は候補にならない。
このモジュールは過去動画のnarration(script.jsonに残存)から施策をLLM抽出し、
専用のJSONL(`output/<channel>/tactic_backfill.jsonl`)へ保存する。

保存した抽出結果は`tactic_issues --backfill <file>`で候補として取り込み、
既存の重複防止(fingerprint恒久ブロック・cooldown・GitHub照合)を通して
issue化される。本モジュールは外部状態(GitHub・history.jsonl)を変更しない。

抽出対象のスキップ条件:
- history.jsonlに`published`行が無い動画
- 既にviewer_actionを保持する動画(research段階で抽出済み)
- 既にbackfill済み(status=extracted/empty)の動画。`--only`で明示指定すれば
  再抽出できる。`error`は`--retry-errors`で再試行できる
- narrationが取得できない動画(workdir/script.json欠落)

CLI:
    python -m doci.tactic_backfill --channel youtube-growth
    python -m doci.tactic_backfill --channel youtube-growth --limit 10
    python -m doci.tactic_backfill --channel youtube-growth --only <video_id>
    python -m doci.tactic_backfill --channel youtube-growth --retry-errors
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import ai_text, channel, config, history, llm
from .channel import ChannelSpec

_SCHEMA_VERSION = 1
_BACKFILL_FILENAME = "tactic_backfill.jsonl"
# LLM1回あたりのタイムアウト(秒)。
_DEFAULT_LLM_TIMEOUT = 120
# 比喩・歴史のたとえだけで具体操作が含まれない動画を空文字とみなすための
# 指示。research.pyのviewer_action定義(research.py:189-190)と整合させる。
_EXTRACT_PROMPT = """\
あなたはYouTube運用施策の抽出担当です。以下はYouTubeショート動画のナレーション
(本編テキスト)です。この動画が視聴者に提示した「YouTube運用施策」を抽出してください。

施策の定義:
- viewer_action: 視聴後にYouTube Studioまたは次の動画制作で実行できる具体的な操作(1文)。
  該当しなければ空文字。比喩・歴史のたとえ・一般的な心構えだけで、実際に取れる操作が
  無い場合は空文字にしてください。
- youtube_creator_problem: この動画が解決しようとしているYouTube制作者の具体的な
  課題または指標(1文)。該当しなければ空文字。

出力は有効なJSONオブジェクトのみ(前後に説明やコードフェンスを付けない):
{{"viewer_action": "...", "youtube_creator_problem": "..."}}

ナレーション:
{narration}
"""


# --- パス ---


def _backfill_path(spec: ChannelSpec) -> Path:
    return spec.output_dir / _BACKFILL_FILENAME


# --- 読み取り ---


def _read_backfill(spec: ChannelSpec) -> list[dict]:
    path = _backfill_path(spec)
    return history._read_path(path)


def _published_rows(spec: ChannelSpec) -> list[dict]:
    return [
        row
        for row in history._read_all(spec)
        if str(row.get("status") or "") == "published"
    ]


def _narration(row: dict) -> str:
    workdir = row.get("workdir")
    if not workdir:
        return ""
    try:
        script = json.loads(
            (Path(str(workdir)) / "script.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return ""
    return " ".join(str(script.get("narration") or "").split())


def _already_extracted(spec: ChannelSpec) -> set[str]:
    """既にviewer_actionを持つpublished動画のvideo_id集合(research経由)。"""
    cache: dict[int, dict[str, str]] = {}
    extracted: set[str] = set()
    for row in _published_rows(spec):
        metadata = history._row_topic_metadata(row, cache=cache)
        if metadata.get("viewer_action"):
            extracted.add(str(row.get("video_id") or ""))
    return extracted


# --- LLM抽出 ---


def _extract(narration: str, *, backend: str, timeout: int) -> dict:
    """narrationから施策をLLM抽出する。JSONのみを返す(検証済み)。

    未対応バックエンド・JSON解析失敗はRuntimeError(呼び出し側でerror扱い)。
    """
    if backend not in {"codex", "opencode_go"}:
        raise RuntimeError(f"未対応のバックフィル抽出バックエンドです: {backend}")
    prompt = _EXTRACT_PROMPT.format(narration=narration[:4000])
    if backend == "codex":
        raw = llm.run_codex(
            prompt,
            config.CODEX_MODEL,
            timeout=timeout,
            min_web_fetches=0,  # 外部Web不要。ネットワーク経路を塞ぐ
        )
    else:
        raw = ai_text._run_opencode_go(
            prompt,
            ai_text._opencode_go_model(config.RESEARCH_MODEL),
            timeout=timeout,
        )
    data = llm.extract_json(raw)
    if not isinstance(data, dict):
        raise RuntimeError("抽出結果がJSONオブジェクトではありません")
    for key in ("viewer_action", "youtube_creator_problem"):
        value = str(data.get(key) or "").strip()
        if len(value) > 400:
            raise RuntimeError(f"{key}が上限(400字)を超えています")
        data[key] = value
    return {"viewer_action": data["viewer_action"], "youtube_creator_problem": data["youtube_creator_problem"]}


def _backfill_row(
    row: dict,
    *,
    narration: str,
    result: dict,
    status: str,
    backend: str,
    error: str = "",
) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "schema_version": _SCHEMA_VERSION,
        "video_id": str(row.get("video_id") or ""),
        "workdir": str(row.get("workdir") or ""),
        "corner": str(row.get("corner") or ""),
        "video_title": str(row.get("title") or ""),
        "topic": str(row.get("topic") or ""),
        "narration_len": len(narration),
        "viewer_action": result.get("viewer_action", ""),
        "youtube_creator_problem": result.get("youtube_creator_problem", ""),
        "status": status,
        "backend": backend,
        "error": error,
    }


def _append(spec: ChannelSpec, row: dict) -> None:
    path = _backfill_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


# --- オーケストレーション ---


def run(
    spec: ChannelSpec,
    *,
    limit: int | None = None,
    only_video_ids: Iterable[str] | None = None,
    retry_errors: bool = False,
    backend: str | None = None,
) -> dict:
    """未抽出の過去動画から施策を抽出し、JSONLへ追記する(冪等・レジューム可)。"""
    backend = backend or config.RESEARCH_BACKEND
    if backend not in {"codex", "opencode_go"}:
        # 未対応バックエンドで45本全てにstatus=error行を残す前に一括拒否する。
        raise RuntimeError(
            f"未対応のバックフィル抽出バックエンドです: {backend}"
            "（codex または opencode_go）"
        )
    only = set(only_video_ids or ())
    previous = {str(row.get("video_id") or ""): row for row in _read_backfill(spec)}
    extracted = _already_extracted(spec)
    rows: list[dict] = []
    for row in _published_rows(spec):
        video_id = str(row.get("video_id") or "")
        if not video_id:
            continue
        if only and video_id not in only:
            continue
        if video_id in extracted:
            continue
        prior = previous.get(video_id)
        if prior is not None:
            # `--only`で明示指定された動画は、terminal扱いの行でも再抽出する
            # (empty判定の見直しやnarration復旧後に再実行できるようにする)。
            if prior.get("status") == "error" and not retry_errors and not only:
                continue
            if prior.get("status") in ("extracted", "empty") and not only:
                continue
        narration = _narration(row)
        if not narration:
            rows.append(
                {
                    "video_id": video_id,
                    "title": str(row.get("title") or ""),
                    "status": "empty",
                    "reason": "narrationを取得できません",
                }
            )
            continue
        rows.append(
            {
                "video_id": video_id,
                "title": str(row.get("title") or ""),
                "narration": narration,
                "row": row,
            }
        )

    processed: list[dict] = []
    errors: list[dict] = []
    skipped: list[dict] = []
    for item in rows:
        if limit is not None and len(processed) + len(errors) >= limit:
            skipped.append(
                {"video_id": item["video_id"], "reason": "limit_reached"}
            )
            continue
        if "row" not in item:
            # narrationが取れない動画: 実質施策なしとみなしてempty記録
            _append(
                spec,
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "schema_version": _SCHEMA_VERSION,
                    "video_id": item["video_id"],
                    "workdir": "",
                    "corner": "",
                    "video_title": item["title"],
                    "topic": "",
                    "narration_len": 0,
                    "viewer_action": "",
                    "youtube_creator_problem": "",
                    "status": "empty",
                    "backend": backend,
                    "error": item["reason"],
                },
            )
            errors.append(
                {"video_id": item["video_id"], "status": "empty", "error": item["reason"]}
            )
            continue
        try:
            result = _extract(item["narration"], backend=backend, timeout=_DEFAULT_LLM_TIMEOUT)
        except Exception as exc:  # 1本の失敗で全体を止めない
            row = _backfill_row(
                item["row"],
                narration=item["narration"],
                result={},
                status="error",
                backend=backend,
                error=str(exc)[:300],
            )
            _append(spec, row)
            errors.append(
                {"video_id": item["video_id"], "status": "error", "error": str(exc)[:300]}
            )
            continue
        status = "extracted" if result["viewer_action"] else "empty"
        row = _backfill_row(
            item["row"],
            narration=item["narration"],
            result=result,
            status=status,
            backend=backend,
        )
        _append(spec, row)
        processed.append(
            {
                "video_id": item["video_id"],
                "status": status,
                "viewer_action": result["viewer_action"],
            }
        )

    return {
        "mode": "extract",
        "channel": spec.id,
        "backend": backend,
        "processed": processed,
        "errors": errors,
        "skipped": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="過去動画のnarrationから施策(viewer_action)を抽出してJSONLへ保存"
    )
    parser.add_argument("--channel", required=True)
    parser.add_argument("--limit", type=int, default=None, help="処理する最大件数")
    parser.add_argument(
        "--only",
        metavar="VIDEO_ID",
        action="append",
        default=None,
        help="指定したvideo_idだけを抽出(複数指定可)",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="status=errorの動画を再試行する",
    )
    parser.add_argument(
        "--backend",
        choices=("codex", "opencode_go"),
        default=None,
        help="LLMバックエンド(既定はRESEARCH_BACKEND)",
    )
    args = parser.parse_args()
    spec = channel.load(args.channel)
    result = run(
        spec,
        limit=args.limit,
        only_video_ids=args.only,
        retry_errors=args.retry_errors,
        backend=args.backend,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
