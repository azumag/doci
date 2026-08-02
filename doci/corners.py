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


def _output_rules_addendum_path(spec: ChannelSpec) -> Path:
    return spec.root / "prompts" / "output_rules_addendum.md"


def build_prompt(
    spec: ChannelSpec,
    corner: CornerSpec,
    date: str,
    past_topics: list[str],
    research: dict | None = None,
    plan: dict | None = None,
    performance_guidance: str = "",
    recent_openings: list[str] | None = None,
) -> str:
    """persona / 出力規則 / チャンネル追加規則 / corner を結合する。"""
    persona = corner.persona_path.read_text(encoding="utf-8")
    rules = _output_rules_path(spec).read_text(encoding="utf-8")
    corner_tpl = corner.corner_path.read_text(encoding="utf-8")
    past = "、".join(past_topics[-20:]) if past_topics else "（まだありません）"
    corner_body = corner_tpl.replace("{date}", date).replace("{past_topics}", past)
    addendum_path = _output_rules_addendum_path(spec)
    if addendum_path.is_file():
        addendum = addendum_path.read_text(encoding="utf-8")
        prompt = f"{persona}\n\n{rules}\n\n{addendum}\n\n{corner_body}\n"
    else:
        # 追加規則がないチャンネルでは従来のプロンプトを1バイトも変えない。
        prompt = f"{persona}\n\n{rules}\n\n{corner_body}\n"
    if recent_openings:
        # issue #70: 書き出しの型が動画をまたいで同型化するのを防ぐため、直近の書き出しを
        # 明示して避けさせる。recent_openings が空/Noneのチャンネルでは出力を1バイトも変えない。
        numbered = "\n".join(f"- {o}" for o in recent_openings)
        prompt += (
            "\n## 直近の書き出し（これらと同じ型で始めない）\n"
            f"{numbered}\n"
            "上記と同じ修辞の型（反語疑問・定型的な前置き等）を連続させないこと。\n"
        )
    if performance_guidance:
        prompt += (
            "\n## このチャンネル自身の実績から得た形式仮説\n"
            f"{performance_guidance}\n"
            "題材の再利用ではなく、次回1本の形式実験にだけ使うこと。"
            "最近の題材とtopic cooldownを必ず優先する。\n"
        )
    if research:
        from . import research as research_mod

        prompt += "\n" + research_mod.brief_for_prompt(research) + "\n"
    if plan:
        from . import plan as plan_mod

        prompt += "\n" + plan_mod.brief_for_prompt(plan) + "\n"
    return prompt
