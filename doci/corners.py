"""コーナー定義・ローテーション・プロンプト組み立て。

v1 は communism / capitalism の2コーナーのみ。
将来のコーナーは CORNERS に追加するだけで拡張できる。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Corner:
    key: str
    label: str
    persona_file: str
    corner_file: str
    voice_key: str  # "chinese_ai" | "american_ai"

    @property
    def voice(self):  # -> voices.VoiceCfg（話者＋速度/ピッチ/抑揚/音量）
        from . import voices

        return voices.get(self.voice_key)

    @property
    def speaker(self) -> int:
        return self.voice.speaker


CORNERS: dict[str, Corner] = {
    "communism": Corner(
        key="communism",
        label="共産主義ネタ",
        persona_file="persona_chinese.md",
        corner_file="corner_communism.md",
        voice_key="chinese_ai",
    ),
    "capitalism": Corner(
        key="capitalism",
        label="資本主義ネタ",
        persona_file="persona_american.md",
        corner_file="corner_capitalism.md",
        voice_key="american_ai",
    ),
}

# v1 のローテーション順（交互）
ROTATION = ["capitalism", "communism"]


def pick_corner(last_corner: str | None) -> Corner:
    """前回と違うコーナーを選ぶ（交互）。履歴が無ければ ROTATION 先頭。"""
    if last_corner in ROTATION:
        idx = (ROTATION.index(last_corner) + 1) % len(ROTATION)
        return CORNERS[ROTATION[idx]]
    return CORNERS[ROTATION[0]]


def _read_prompt(name: str) -> str:
    return (config.PROMPTS / name).read_text(encoding="utf-8")


def build_prompt(
    corner: Corner,
    date: str,
    past_topics: list[str],
    research: dict | None = None,
    plan: dict | None = None,
) -> str:
    """persona + output_rules + corner を結合した最終プロンプトを返す。

    research(issue #6)があれば検証済み事実を、plan(issue #2)があれば起承転結＋図表の
    構成プランを末尾に足す（題材選定はせず、その具体・構成に沿わせる）。
    """
    persona = _read_prompt(corner.persona_file)
    rules = _read_prompt("output_rules.md")
    corner_tpl = _read_prompt(corner.corner_file)
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
