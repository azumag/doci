"""チャンネル別の生成履歴（重複回避・コーナーローテーション用）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .channel import ChannelSpec


def _read_all(spec: ChannelSpec) -> list[dict]:
    path = spec.history_file
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


def last_corner(spec: ChannelSpec) -> str | None:
    rows = _read_all(spec)
    return rows[-1].get("corner") if rows else None


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
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
