"""生成履歴（重複回避・コーナーローテーション用）。output/history.jsonl に追記。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config

HISTORY_FILE = config.OUTPUT / "history.jsonl"


def _read_all() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows: list[dict] = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def last_corner() -> str | None:
    rows = _read_all()
    return rows[-1].get("corner") if rows else None


def recent_topics(limit: int = 30) -> list[str]:
    rows = _read_all()
    topics = [r.get("title", "") for r in rows if r.get("title")]
    return topics[-limit:]


def record(corner: str, title: str, video_id: str | None = None, extra: dict | None = None) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "corner": corner,
        "title": title,
        "video_id": video_id,
    }
    if extra:
        row.update(extra)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
