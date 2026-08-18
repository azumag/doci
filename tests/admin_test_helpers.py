"""tests/test_admin_*.py 共通のフィクスチャヘルパー。

`doci/config.py` の `ROOT`/`PROMPTS`/`OUTPUT` 等はモジュール読込時に一度だけ
計算される定数で、`ROOT` を `mock.patch.object` しても連動しない（
`tests/test_channel_spec.py` が `config.OUTPUT` を別途patchしているのと同じ理由）。
このため `doci.admin` 配下のテストは、使う定数をそれぞれ個別にpatchすること。
"""
from __future__ import annotations

import json
from pathlib import Path


def write_minimal_repo(root: Path) -> None:
    """`config.ROOT`/`config.PROMPTS`/`config.OUTPUT` 相当の骨格を作る。"""
    (root / "doci" / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "doci" / "prompts" / "output_rules.md").write_text(
        "# 共通出力規則(テスト用)\nですます調で書くこと。\n", encoding="utf-8"
    )
    (root / "output").mkdir(parents=True, exist_ok=True)
    (root / "channels").mkdir(parents=True, exist_ok=True)


def write_channel(
    root: Path,
    channel_id: str,
    *,
    corners: dict[str, dict] | None = None,
    extra_toml: str = "",
) -> Path:
    """`channels/<id>/` 配下に persona/corner/voices.json/channel.toml 一式を作る。"""
    corners = corners or {"main": {"label": "メイン", "voice": "narrator"}}
    channel_dir = root / "channels" / channel_id
    prompts_dir = channel_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    voice_keys = sorted({spec.get("voice", "narrator") for spec in corners.values()})
    voices_payload = {key: {"voicevox_speaker": 1} for key in voice_keys}
    (channel_dir / "voices.json").write_text(
        json.dumps(voices_payload, ensure_ascii=False), encoding="utf-8"
    )

    corner_blocks = []
    for key, spec in corners.items():
        persona_name = f"persona_{key}.md"
        corner_name = f"corner_{key}.md"
        (prompts_dir / persona_name).write_text(
            f"あなたは{spec['label']}の案内人です。\n", encoding="utf-8"
        )
        (prompts_dir / corner_name).write_text(
            "## コーナー\nテーマ: {date}\n過去の題材: {past_topics}\n", encoding="utf-8"
        )
        corner_blocks.append(
            f'[corners.{key}]\n'
            f'label = "{spec["label"]}"\n'
            f'persona = "prompts/{persona_name}"\n'
            f'corner = "prompts/{corner_name}"\n'
            f'voice = "{spec.get("voice", "narrator")}"\n'
        )

    rotation = ", ".join(f'"{k}"' for k in corners)
    toml_text = (
        'voices = "voices.json"\n\n'
        "[channel]\n"
        f'id = "{channel_id}"\n'
        'name = "テストチャンネル"\n'
        f"rotation = [{rotation}]\n\n"
        + "\n".join(corner_blocks)
        + '\n[publish]\nplatforms = ["youtube"]\n\n'
        + extra_toml
    )
    (channel_dir / "channel.toml").write_text(toml_text, encoding="utf-8")
    return channel_dir
