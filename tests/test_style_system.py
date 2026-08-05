from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from doci import channel, charts, chart_seq, compose, run_daily, style_themes, thumbnail
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

_STAT_SPEC = {
    "type": "stat",
    "title": "テーマ比較",
    "value": "42",
    "caption": "説明文",
}


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
        self.assertEqual(style.subtitle.box_radius, 0.35)
        self.assertEqual(style.thumbnail.title_color, "#f6efe1")
        self.assertEqual(style.thumbnail.theme, "classic")
        self.assertEqual(style.chart.theme, "classic")
        self.assertEqual(style.theme, "classic")
        self.assertEqual(style.video.pad_color, "0x0a0a0c")
        self.assertEqual(style.bgm.rotation, "fixed")

    def test_classic_theme_output_is_byte_identical_to_no_style(self) -> None:
        """issue #76: `classic`テーマ(既定)導入前後で出力が変わらないことの回帰テスト。"""
        baseline_chart = charts.chart_html(_STAT_SPEC)
        classic_chart = charts.chart_html(_STAT_SPEC, style=ChartStyle(theme="classic"))
        self.assertEqual(baseline_chart, classic_chart)

        baseline_thumb = thumbnail._html_doc("見出し", None)
        classic_thumb = thumbnail._html_doc(
            "見出し", None, ThumbnailStyle(theme="classic")
        )
        self.assertEqual(baseline_thumb, classic_thumb)

        baseline_overlay = chart_seq._overlay_html(
            {"events": [{"year": "1", "label": "出来事"}]}, 1080, 1920
        )
        classic_overlay = chart_seq._overlay_html(
            {"events": [{"year": "1", "label": "出来事"}]},
            1080,
            1920,
            ChartStyle(theme="classic"),
        )
        self.assertEqual(baseline_overlay, classic_overlay)

    def test_tech_theme_differentiates_chart_thumbnail_and_overlay(self) -> None:
        """issue #76: `tech`テーマが構造要素の非表示・書体・レイアウトを差し替える。"""
        tech_chart = charts.chart_html(_STAT_SPEC, style=ChartStyle(theme="tech"))
        self.assertIn(".grain,.frame,.frame::after,.star-bg{display:none}", tech_chart)
        # classicの明朝見出しには無いtech固有の書体指定(極太ゴシック)で判定する。
        # ("Hiragino Kaku Gothic ProN"はclassicのbody font-familyにも既に含まれ
        # 判定に使えないため避ける)
        self.assertIn("font-weight:900;letter-spacing:0;color:#f2f6fb", tech_chart)

        classic_thumb = thumbnail._html_doc("見出し", None, ThumbnailStyle(theme="classic"))
        tech_thumb = thumbnail._html_doc("見出し", None, ThumbnailStyle(theme="tech"))
        self.assertNotEqual(classic_thumb, tech_thumb)
        self.assertIn("justify-content:flex-end", tech_thumb)

        tech_overlay = chart_seq._overlay_html(
            {"events": [{"year": "1", "label": "出来事"}]},
            1080,
            1920,
            ChartStyle(theme="tech"),
        )
        self.assertIn(".frame{display:none}", tech_overlay)

    def test_tech_theme_timeline_head_overrides_inline_border_color(self) -> None:
        """自己レビュー指摘: `_timeline`ビルダーは`.tl-head`のborder-topを
        インラインstyleで書くため、テーマCSSの通常上書きでは負ける。
        `!important`で確実に上書きされることを検証する。"""
        timeline_spec = {
            "type": "timeline",
            "title": "年表",
            "events": [
                {"year": "1", "label": "出来事1"},
                {"year": "2", "label": "出来事2"},
            ],
        }
        tech_chart = charts.chart_html(timeline_spec, style=ChartStyle(theme="tech"))
        self.assertIn(".tl-head{border-top-color:#f4c25c!important}", tech_chart)
        # インラインstyleの旧ゴールド値がまだ残っている(!importantで上書きされる前提)ことを確認。
        self.assertIn("border-top:", tech_chart)

    def test_tech_theme_palette_recolors_theme_css_literals(self) -> None:
        """テーマCSSが`_DONUT_COLORS`リテラルを使う箇所は、channelパレットで
        自動的に再着色される(`charts._apply_style_html`の既存置換機構を再利用)。"""
        chart_html = charts.chart_html(
            _STAT_SPEC,
            style=ChartStyle(theme="tech", palette=("#00aa00", "#1122ff")),
        )
        self.assertIn(".trule{height:.55vh;width:8vw;border-radius:0;background:#00aa00", chart_html)
        self.assertNotIn("#f4c25c", chart_html)

    def test_unknown_theme_falls_back_to_classic(self) -> None:
        self.assertIs(style_themes.get("no-such-theme"), style_themes.THEMES["classic"])
        self.assertIs(style_themes.get(None), style_themes.THEMES["classic"])

    def test_subtitle_box_radius_default_and_square(self) -> None:
        rounded = self.root / "rounded.png"
        square = self.root / "square.png"
        base = StyleSpec(
            subtitle=SubtitleStyle(box_color="#0000ff", box_alpha=1.0, position_ratio=0.1)
        )
        if not compose._render_caption_png("角丸比較", rounded, 400, 800, base):
            self.skipTest("Japanese font or Pillow unavailable")
        square_style = StyleSpec(
            subtitle=SubtitleStyle(
                box_color="#0000ff", box_alpha=1.0, position_ratio=0.1, box_radius=0.0
            )
        )
        compose._render_caption_png("角丸比較", square, 400, 800, square_style)

        from PIL import Image

        # 角丸(既定0.35)は左上隅が透明、角形(0.0)は左上隅まで不透明になる。
        r_img = Image.open(rounded).convert("RGBA")
        s_img = Image.open(square).convert("RGBA")
        r_bbox = r_img.getchannel("A").getbbox()
        s_bbox = s_img.getchannel("A").getbbox()
        self.assertEqual(r_img.getpixel((r_bbox[0], r_bbox[1]))[3], 0)
        self.assertEqual(s_img.getpixel((s_bbox[0], s_bbox[1]))[3], 255)

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

    def test_chart_animation_failure_falls_back_to_static_chart(self) -> None:
        scene = compose.Scene(
            path=self.root / "unused.png",
            is_video=False,
            chart_spec={"type": "bar", "title": "比較", "data": []},
        )
        commands: list[list[str]] = []
        with (
            patch("doci.charts.render_chart_video", side_effect=RuntimeError("CDP pipe closed")),
            patch("doci.charts.render_chart", return_value=self.root / "chart_00.png") as static_mock,
            patch.object(compose, "_run", side_effect=lambda cmd, **_kwargs: commands.append(cmd)),
        ):
            out = compose._build_scene_clip(
                scene, 2.0, 0, self.root, 400, 800, StyleSpec()
            )

        static_mock.assert_called_once()
        self.assertEqual(out, self.root / "scene_00.mp4")
        self.assertIn("-loop", commands[0])

    def test_chart_chrome_failure_falls_back_to_pillow_png(self) -> None:
        scene = compose.Scene(
            path=self.root / "unused.png",
            is_video=False,
            chart_spec={
                "type": "timeline",
                "title": "改善の順序",
                "events": [{"year": "1", "label": "指標を見る"}],
                "source": "YouTube公式ヘルプ",
            },
        )
        commands: list[list[str]] = []
        with (
            patch("doci.charts.render_chart_video", side_effect=RuntimeError("CDP pipe closed")),
            patch("doci.charts.render_chart", side_effect=RuntimeError("Chrome failed")),
            patch.object(compose, "_run", side_effect=lambda cmd, **_kwargs: commands.append(cmd)),
        ):
            compose._build_scene_clip(
                scene, 2.0, 0, self.root, 400, 800, StyleSpec()
            )

        fallback = self.root / "chart_00.png"
        self.assertTrue(fallback.exists())
        self.assertGreater(fallback.stat().st_size, 0)
        self.assertIn(str(fallback), commands[0])

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
