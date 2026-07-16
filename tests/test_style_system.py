from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from doci import channel, charts, compose, run_daily, thumbnail
from doci.channel import (
    BgmStyle,
    ChannelSpec,
    ChartStyle,
    CornerSpec,
    CreditsStyle,
    StyleSpec,
    SubtitleStyle,
    ThumbnailStyle,
    VideoStyle,
)


class StyleSystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.persona = self.root / "persona.md"
        self.corner_prompt = self.root / "corner.md"
        self.persona.write_text("persona", encoding="utf-8")
        self.corner_prompt.write_text("corner", encoding="utf-8")
        self.voices_path = self.root / "voices.json"
        self.voices_path.write_text(
            json.dumps(
                {
                    "voice": {
                        "voicevox_speaker": 7,
                        "label": "案内AI (四国めたん/ノーマル)",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.corner = CornerSpec(
            key="main",
            label="Main",
            persona_path=self.persona,
            corner_path=self.corner_prompt,
            voice_key="voice",
        )

    def _spec(self, style: StyleSpec) -> ChannelSpec:
        return ChannelSpec(
            id="sample",
            name="Sample",
            root=self.root,
            corners={"main": self.corner},
            rotation=["main"],
            voices_path=self.voices_path,
            style=style,
        )

    def _touch_tracks(self, directory: Path, names: list[str]) -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in names:
            path = directory / name
            path.write_bytes(b"audio")
            paths.append(path)
        return paths

    def test_default_style_matches_existing_values(self) -> None:
        style = StyleSpec()

        self.assertEqual(style.subtitle.fill, "#ffffff")
        self.assertEqual(style.subtitle.stroke, "#000000")
        self.assertEqual(style.subtitle.position_ratio, 0.64)
        self.assertEqual(style.thumbnail.title_color, "#f6efe1")
        self.assertEqual(style.video.pad_color, "0x0a0a0c")
        self.assertEqual(style.bgm.rotation, "fixed")

    def test_bgm_fixed_daily_and_per_corner_are_deterministic(self) -> None:
        bgm_dir = self.root / "bgm"
        tracks = self._touch_tracks(bgm_dir, ["a.mp3", "b.ogg", "c.wav"])
        fixed = self._spec(StyleSpec(bgm=BgmStyle(dir=bgm_dir, rotation="fixed")))
        daily = self._spec(StyleSpec(bgm=BgmStyle(dir=bgm_dir, rotation="daily")))

        self.assertEqual(channel.bgm_path(fixed, self.corner, "2026-07-16"), tracks[0])
        selected = [
            channel.bgm_path(daily, self.corner, f"2026-07-{day:02d}")
            for day in range(1, 21)
        ]
        self.assertEqual(
            channel.bgm_path(daily, self.corner, "2026-07-16"),
            channel.bgm_path(daily, self.corner, "2026-07-16"),
        )
        self.assertGreater(len(set(selected)), 1)

        corner_tracks = self._touch_tracks(
            bgm_dir / "main", ["corner_b.mp3", "corner_a.mp3"]
        )
        per_corner = self._spec(
            StyleSpec(bgm=BgmStyle(dir=bgm_dir, rotation="per_corner"))
        )
        self.assertEqual(
            channel.bgm_path(per_corner, self.corner, "2026-07-16"),
            sorted(corner_tracks)[0],
        )

    def test_thumbnail_and_chart_html_apply_theme(self) -> None:
        thumb_style = ThumbnailStyle(font_family="Georgia,serif", title_color="#12ab34")
        thumb_html = thumbnail._html_doc("Theme title", None, thumb_style)
        self.assertIn("font-family:Georgia,serif", thumb_html)
        self.assertIn("color:#12ab34", thumb_html)

        font = self.root / "font.ttf"
        font.write_bytes(b"font")
        chart_style = ChartStyle(palette=("#00aa00", "#aa00aa"), font=font)
        chart_html = charts.chart_html(
            {
                "type": "stat",
                "title": "Theme chart",
                "value": "42",
                "caption": "Answer",
            },
            style=chart_style,
        )
        self.assertIn("#00aa00", chart_html)
        self.assertIn("#aa00aa", chart_html)
        self.assertIn("DociChannelChart", chart_html)
        self.assertIn(font.as_uri(), chart_html)

    def test_subtitle_png_uses_custom_colors_and_position(self) -> None:
        out = self.root / "subtitle.png"
        style = StyleSpec(
            subtitle=SubtitleStyle(
                fill="#ff0000",
                stroke="#00ff00",
                box_color="#0000ff",
                box_alpha=1.0,
                position_ratio=0.2,
            )
        )
        if not compose._render_caption_png("テーマ字幕", out, 400, 800, style):
            self.skipTest("Japanese font or Pillow unavailable")

        from PIL import Image

        image = Image.open(out).convert("RGBA")
        colors = [color for _count, color in image.getcolors(maxcolors=400 * 800)]
        self.assertTrue(any(r > 200 and g < 80 and b < 80 for r, g, b, _a in colors))
        self.assertTrue(any(b > 200 and r < 80 and g < 80 for r, g, b, _a in colors))
        alpha = image.getchannel("A")
        bbox = alpha.getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreaterEqual(bbox[1], 150)
        self.assertLess(bbox[1], 220)

    def test_compose_applies_video_filter_pad_color_and_bgm_volume(self) -> None:
        style = StyleSpec(
            video=VideoStyle(pad_color="0x123456", filter="eq=saturation=0.8"),
            bgm=BgmStyle(dir=self.root, volume=0.07),
        )
        scene = compose.Scene(path=self.root / "scene.png", is_video=False, static=True)
        commands: list[list[str]] = []

        with patch.object(compose, "_run", side_effect=lambda cmd, **_kwargs: commands.append(cmd)):
            compose._build_scene_clip(scene, 2.0, 0, self.root, 400, 800, style)
        self.assertTrue(any("color=0x123456" in arg for arg in commands[0]))

        commands.clear()
        with (
            patch.object(compose, "_build_scene_clip", return_value=self.root / "clip.mp4"),
            patch.object(compose, "_concat", return_value=self.root / "silent.mp4"),
            patch.object(compose, "_run", side_effect=lambda cmd, **_kwargs: commands.append(cmd)),
        ):
            compose.compose(
                [compose.Scene(path=self.root / "scene.png", is_video=False)],
                self.root / "narration.wav",
                2.0,
                self.root / "out.mp4",
                bgm=self.root / "music.mp3",
                style=style,
            )
        filter_graph = commands[-1][commands[-1].index("-filter_complex") + 1]
        self.assertIn("eq=saturation=0.8", filter_graph)
        self.assertIn("volume=0.07", filter_graph)

    def test_credit_template_cannot_remove_voicevox_credit(self) -> None:
        spec = self._spec(
            StyleSpec(credits=CreditsStyle(template="Credits\n{asset_credit}"))
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            credits = run_daily._credits(spec, self.corner)

        self.assertIn("Pexels", credits)
        self.assertIn("VOICEVOX:四国めたん", credits)
        self.assertTrue(any("required credit appended" in str(w.message) for w in caught))


if __name__ == "__main__":
    unittest.main()
