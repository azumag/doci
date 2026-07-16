"""ChannelSpec ベースのコーナーローテーションとプロンプト組み立て。"""
from __future__ import annotations

from pathlib import Path

from . import config
from .channel import ChannelSpec, CornerSpec


def pick_corner(spec: ChannelSpec, last_corner: str | None) -> CornerSpec:
    """チャンネルの rotation に従い、前回の次のコーナーを返す。"""
    if last_corner in spec.rotation:
        idx = (spec.rotation.index(last_corner) + 1) % len(spec.rotation)
        return spec.corners[spec.rotation[idx]]
    return spec.corners[spec.rotation[0]]


def _output_rules_path(spec: ChannelSpec) -> Path:
    override = spec.root / "prompts" / "output_rules.md"
    return override if override.is_file() else config.PROMPTS / "output_rules.md"


def build_prompt(
    spec: ChannelSpec,
    corner: CornerSpec,
    date: str,
    past_topics: list[str],
    research: dict | None = None,
    plan: dict | None = None,
) -> str:
    """チャンネルの persona / corner と共通または上書き規則を結合する。"""
    persona = corner.persona_path.read_text(encoding="utf-8")
    rules = _output_rules_path(spec).read_text(encoding="utf-8")
    corner_tpl = corner.corner_path.read_text(encoding="utf-8")
    past = "、".join(past_topics[-20:]) if past_topics else "（まだありません）"
    corner_body = corner_tpl.replace("{date}", date).replace("{past_topics}", past)
    prompt = f"{persona}\n\n{rules}\n\n{corner_body}\n"
    if research:
        from . import research as research_mod

        prompt += "\n" + research_mod.brief_for_prompt(research) + "\n"
    if plan:
        from . import plan as plan_mod

        prompt += "\n" + plan_mod.brief_for_prompt(plan) + "\n"
    return prompt
