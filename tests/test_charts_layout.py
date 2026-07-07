"""doci.charts のレイアウト計算・出典整形・新図表型の純関数テスト（ネットワーク/Chrome不要）。

- _clean_source: 「裏取り済み(の)?(事実|情報)?(より|から)?」除去、40字切り詰め。
- _avail_vh: タイトル行数・unit有無によるbudgetの増減。
- 各ビルダー: 項目数が多い時に scale が1未満かつフロア以上になり、レンダリング結果の
  インライン style にも反映されていること。
- chart_html("donut"/"line"): 例外なくHTML文字列を返し、data-donut / data-line 属性を含む。
"""
from __future__ import annotations

import re
import unittest

from doci import charts


class CleanSourceTest(unittest.TestCase):
    def test_strips_meta_phrase_with_no_and_yori(self) -> None:
        s = "裏取り済みの事実より（総務省統計局の家計調査）"
        self.assertEqual(charts._clean_source(s), "総務省統計局の家計調査")

    def test_strips_meta_phrase_without_no(self) -> None:
        s = "裏取り済み事実より（総務省統計局の家計調査）"
        self.assertEqual(charts._clean_source(s), "総務省統計局の家計調査")

    def test_no_dangling_yori_left_over(self) -> None:
        for s in [
            "裏取り済みの事実より（Wikipedia）",
            "裏取り済み事実より（Wikipedia）",
            "裏取り済みの情報から（Wikipedia）",
        ]:
            cleaned = charts._clean_source(s)
            self.assertFalse(cleaned.startswith("より"), cleaned)
            self.assertFalse(cleaned.startswith("から"), cleaned)

    def test_short_result_treated_as_empty(self) -> None:
        self.assertEqual(charts._clean_source("裏取り済みのより"), "")
        self.assertEqual(charts._clean_source(""), "")
        self.assertEqual(charts._clean_source("裏取り済み"), "")

    def test_long_source_truncated_to_40_chars_with_ellipsis(self) -> None:
        long_src = "あ" * 60
        cleaned = charts._clean_source(long_src)
        self.assertEqual(len(cleaned), 41)  # 40文字＋省略記号
        self.assertTrue(cleaned.endswith("…"))
        self.assertEqual(cleaned[:40], "あ" * 40)

    def test_normal_short_source_untouched(self) -> None:
        self.assertEqual(charts._clean_source("総務省統計局"), "総務省統計局")


class AvailVhTest(unittest.TestCase):
    def test_longer_title_yields_smaller_budget(self) -> None:
        short_spec = {"title": "短い見出し"}
        long_spec = {
            "title": "これはとても長いタイトルでレイアウトの縮小具合を確認するためのテストケースです長め"
        }
        self.assertLess(charts._avail_vh(long_spec, 1080, 1920), charts._avail_vh(short_spec, 1080, 1920))

    def test_unit_present_yields_smaller_budget(self) -> None:
        with_unit = {"title": "テスト", "unit": "単位：億個"}
        without_unit = {"title": "テスト"}
        self.assertLess(charts._avail_vh(with_unit, 1080, 1920), charts._avail_vh(without_unit, 1080, 1920))


def _style_value(html: str, cls: str, prop: str) -> float:
    """レンダリング済HTMLから、指定classの最初の要素が持つ style 内の数値プロパティ(vh)を取り出す。"""
    m = re.search(rf'class="{cls}"[^>]*style="[^"]*{prop}:([\d.]+)vh', html)
    assert m, f"{cls} の {prop} が見つからない: {html[:300]}"
    return float(m.group(1))


class BuilderScaleTest(unittest.TestCase):
    def test_bar_many_items_scales_down_below_floor_to_avoid_overflow(self) -> None:
        # 6項目だとnaturalがfloor(0.55)適用のままではbudgetに収まらない(issue #12で判明:
        # フロア死守だと見出し/字幕帯にはみ出す)。フィット優先でfloor未満まで縮み、
        # 最終防波堤(0.15)は下回らない。
        spec = {
            "type": "bar",
            "title": "テスト",
            "data": [{"label": f"項目{i}", "value": i + 1, "display": f"{i}億"} for i in range(6)],
        }
        html = charts._bar(spec, 1080, 1920)
        track_h = _style_value(html, "bar-track", "height")
        self.assertLess(track_h, 6.4 * 0.55 - 1e-6)  # フロアでは収まらずさらに縮む
        self.assertGreaterEqual(track_h, 6.4 * 0.15 - 1e-6)  # 最終防波堤(0.15)以上

    def test_bar_few_items_stays_at_scale_one(self) -> None:
        spec = {
            "type": "bar",
            "title": "テスト",
            "data": [{"label": "A", "value": 1, "display": "1"}, {"label": "B", "value": 2, "display": "2"}],
        }
        html = charts._bar(spec, 1080, 1920)
        track_h = _style_value(html, "bar-track", "height")
        self.assertAlmostEqual(track_h, 6.4, delta=1e-6)

    def test_compare_cards_many_items_scales_down_but_respects_floor(self) -> None:
        spec = {
            "type": "compare",
            "title": "テスト",
            "items": [{"label": f"項目{i}", "value": f"値{i}"} for i in range(6)],
        }
        html = charts._compare(spec, 1080, 1920)
        pad_v = _style_value(html, "cmp-item", "padding")
        self.assertLess(pad_v, 2.4)
        self.assertGreaterEqual(pad_v, 2.4 * 0.45 - 1e-6)

    def test_timeline_many_events_scales_down_but_respects_floor(self) -> None:
        spec = {
            "type": "timeline",
            "title": "テスト",
            "events": [{"year": str(1990 + i), "label": f"出来事{i}"} for i in range(6)],
        }
        html = charts._timeline(spec, 1080, 1920)
        year_fs = _style_value(html, "y", "font-size")
        self.assertLess(year_fs, 3.0)
        self.assertGreaterEqual(year_fs, 3.0 * 0.42 - 1e-6)


class LandscapeOrientationTest(unittest.TestCase):
    """issue #12: 長尺(横16:9)で図表の文字/行がvh基準のまま縮まず、見出しや字幕帯にはみ出す
    バグの回帰テスト。縦9:16は無補正(scale=1)、横16:9はbudget内に収まることを確認する。"""

    def test_scale_is_noop_in_portrait_and_boosts_in_landscape(self) -> None:
        self.assertAlmostEqual(charts._scale(1080, 1920), 1.0)
        self.assertGreater(charts._scale(1920, 1080), 1.5)

    def test_bar_landscape_content_fits_within_budget(self) -> None:
        spec = {
            "type": "bar", "title": "国別 生産量の比較（1930年代）", "unit": "単位：万トン",
            "data": [
                {"label": "アメリカ合衆国", "value": 120, "display": "120万トン"},
                {"label": "ソビエト連邦", "value": 95, "display": "95万トン"},
                {"label": "ドイツ", "value": 60, "display": "60万トン"},
                {"label": "グレートブリテンおよび北アイルランド連合王国", "value": 40, "display": "40万トン"},
            ],
        }
        w, h = 1920, 1080
        html = charts._bar(spec, w, h)
        label_fs = _style_value(html, "bar-label", "font-size")
        label_mb = _style_value(html, "bar-label", "margin-bottom")
        track_h = _style_value(html, "bar-track", "height")
        gap = float(re.search(r'class="bars" style="gap:([\d.]+)vh"', html).group(1))
        n = len(spec["data"])
        rendered = n * (label_fs * 1.3 + label_mb + track_h) + (n - 1) * gap
        budget = charts._avail_vh(spec, w, h)
        self.assertLessEqual(rendered, budget + 0.5)  # 許容誤差0.5vh。超過=見出し/字幕帯へのはみ出し


class NewChartTypesTest(unittest.TestCase):
    def test_donut_chart_html_contains_data_donut(self) -> None:
        spec = {
            "type": "donut",
            "title": "構成比テスト",
            "items": [
                {"label": "労働者", "value": 38.6, "display": "38.6%"},
                {"label": "資本家", "value": 30.0, "display": "30.0%"},
                {"label": "その他", "value": 31.4, "display": "31.4%"},
            ],
            "source": "テスト出典",
        }
        html = charts.chart_html(spec)
        self.assertIsInstance(html, str)
        self.assertIn("data-donut", html)
        self.assertIn("data-dwin", html)

    def test_line_chart_html_contains_data_line(self) -> None:
        spec = {
            "type": "line",
            "title": "推移テスト",
            "unit": "万台",
            "points": [{"x": str(1990 + i), "y": 50 + i * i * 0.7, "display": f"{50 + i * i * 0.7:.1f}万台"}
                       for i in range(8)],
        }
        html = charts.chart_html(spec)
        self.assertIsInstance(html, str)
        self.assertIn("data-line", html)

    def test_line_chart_handles_flat_series_without_div_by_zero(self) -> None:
        spec = {
            "type": "line",
            "title": "横ばいテスト",
            "points": [{"x": str(1990 + i), "y": 10.0, "display": "10"} for i in range(4)],
        }
        html = charts.chart_html(spec)
        self.assertIsInstance(html, str)
        self.assertIn("data-line", html)

    def test_donut_and_line_not_registered_under_old_types_only(self) -> None:
        self.assertIn("donut", charts._BUILDERS)
        self.assertIn("line", charts._BUILDERS)

    def test_donut_row_fits_content_width(self) -> None:
        # リング(vh→vw換算) + gap6vw + 凡例幅 が内容幅82vwに収まる（左端見切れの再発防止）。
        items = [
            {"label": f"とても長い項目ラベルその{i}", "value": 10.0 + i, "display": f"{10 + i}.0%"}
            for i in range(4)
        ]
        spec = {"type": "donut", "title": "テスト", "items": items}
        disps = [it["display"] for it in items]
        budget = charts._avail_vh(spec, 1080, 1920)
        size, leg_fs, chip, legend_w = charts._donut_layout(items, disps, budget, 1080, 1920)
        self.assertLessEqual(size * charts._vh2vw(1080, 1920) + charts._DONUT_GAP_VW + legend_w, 82.01)
        html = charts._donut(spec, 1080, 1920)
        self.assertIn(f"width:{size}vh", html)
        self.assertIn(f"font-size:{leg_fs}vh", html)

    def test_donut_long_legend_shrinks_ring(self) -> None:
        short_items = [{"label": "短い", "value": 50.0, "display": "50%"},
                       {"label": "他", "value": 50.0, "display": "50%"}]
        long_items = [{"label": "非常に長い凡例ラベルで幅を圧迫する項目", "value": 50.0, "display": "50.0%"},
                      {"label": "こちらも長い凡例ラベルの項目です", "value": 50.0, "display": "50.0%"}]
        budget = charts._avail_vh({"type": "donut", "title": "テスト"}, 1080, 1920)
        size_s, *_ = charts._donut_layout(short_items, [i["display"] for i in short_items], budget, 1080, 1920)
        size_l, *_ = charts._donut_layout(long_items, [i["display"] for i in long_items], budget, 1080, 1920)
        self.assertLess(size_l, size_s)

    def test_line_x_range_inset_and_edge_anchors(self) -> None:
        spec = {
            "type": "line",
            "title": "推移テスト",
            "points": [{"x": str(1973 + i * 5), "y": 12.0 - i, "display": f"{12 - i}万人"}
                       for i in range(8)],
        }
        html = charts._line(spec, 1080, 1920)
        # x範囲が viewBox の [10,90] に内側寄せされている。
        self.assertIn("M 10.00,", html)
        self.assertIn('x="90.00"', html)
        self.assertNotIn('x="0.00"', html)
        self.assertNotIn('x="100.00"', html)
        # 最初の点=start / 最後の点=end で外側へのはみ出しを防ぐ。
        self.assertIn('text-anchor="start"', html)
        self.assertIn('text-anchor="end"', html)

    def test_line_value_label_clamped_below_when_near_top(self) -> None:
        # 最大値の点は yc≈6(上端付近)。ラベルを上(y≈2.5)に置くと見切れるため点の下(y=13)へ回す。
        spec = {
            "type": "line",
            "title": "推移テスト",
            "points": [{"x": "1990", "y": 10.0}, {"x": "2000", "y": 50.0}, {"x": "2010", "y": 100.0}],
        }
        html = charts._line(spec, 1080, 1920)
        m = re.search(r'<text [^>]*fill="#d8503a"[^>]*>', html)
        self.assertIsNotNone(m)  # 最後の点(=最大値)の強調ラベルが存在する
        ym = re.search(r'y="([\d.]+)"', m.group(0))
        self.assertGreaterEqual(float(ym.group(1)), 5.6)  # フォント高より下＝上端で切れない


class EyebrowRemovedTest(unittest.TestCase):
    def test_place_field_is_ignored_and_not_rendered(self) -> None:
        spec = {"type": "stat", "title": "テスト", "value": "42%", "caption": "説明", "place": "起"}
        html = charts.chart_html(spec)
        self.assertNotIn("class='eyebrow'", html)
        self.assertNotIn('class="eyebrow"', html)
        self.assertNotIn("class=\"act\"", html)


if __name__ == "__main__":
    unittest.main()
