"""issue #59: caption 無し図表マーカー {"chart_id": N} が narration に残存し、
TTS スキップ＋字幕への生 JSON 露出（「文字化け」報告）を起こした事故の回帰テスト。

対象: ai_text._INLINE_CHART / _strip_chart_markers / _recover_inline_charts。
"""
from __future__ import annotations

import unittest

from doci import ai_text


class StripChartMarkersTest(unittest.TestCase):
    def test_strips_marker_with_caption(self) -> None:
        self.assertEqual(
            ai_text._strip_chart_markers(
                '前文です。{"chart_id": 1, "caption": "推移"}後文です。'
            ),
            "前文です。後文です。",
        )

    def test_strips_captionless_marker_with_inner_spaces(self) -> None:
        # 実事故 (output/ideology/2026-07-30_communism_183609) と同一の形式。
        self.assertEqual(
            ai_text._strip_chart_markers('前文です。{ "chart_id": 0 }後文です。'),
            "前文です。後文です。",
        )

    def test_strips_compact_captionless_marker(self) -> None:
        self.assertEqual(
            ai_text._strip_chart_markers('前文{"chart_id":2}後文'), "前文後文"
        )

    def test_non_marker_json_and_plain_text_are_untouched(self) -> None:
        text = '{"scene": 1} はマーカーではなく、chart_idという語も残る。'
        self.assertEqual(ai_text._strip_chart_markers(text), text)


class RecoverInlineChartsCaptionlessTest(unittest.TestCase):
    def _script(self, narration: str, n_scenes: int = 3) -> dict:
        return {
            "narration": narration,
            "scenes": [{"caption": "", "chart_id": None} for _ in range(n_scenes)],
        }

    def test_captionless_marker_is_removed_and_chart_id_moved_to_scene(self) -> None:
        script = self._script('冒頭の文。{ "chart_id": 0 }続きの文。')
        moved = ai_text._recover_inline_charts(script)
        self.assertEqual(moved, 1)
        self.assertNotIn("chart_id", script["narration"])
        self.assertNotIn("{", script["narration"])
        self.assertIn(0, [s.get("chart_id") for s in script["scenes"]])

    def test_captionless_marker_does_not_touch_scene_caption(self) -> None:
        script = self._script('冒頭の文。{ "chart_id": 0 }続きの文。')
        ai_text._recover_inline_charts(script)
        self.assertTrue(all(s["caption"] == "" for s in script["scenes"]))

    def test_marker_with_caption_still_fills_scene_caption(self) -> None:
        # 従来形式のリグレッションが無いことの確認。
        script = self._script('冒頭。{"chart_id": 1, "caption": "推移"}続き。')
        moved = ai_text._recover_inline_charts(script)
        self.assertEqual(moved, 1)
        target = next(s for s in script["scenes"] if s.get("chart_id") == 1)
        self.assertEqual(target["caption"], "推移")

    def test_three_captionless_markers_all_recovered_like_real_incident(self) -> None:
        # 実事故と同構成: 7 scenes、caption 無しマーカー3箇所。
        narration = (
            '一段目の説明。{ "chart_id": 0 }'
            '二段目の説明。{ "chart_id": 1 }'
            '三段目の説明。{ "chart_id": 2 }結び。'
        )
        script = self._script(narration, n_scenes=7)
        moved = ai_text._recover_inline_charts(script)
        self.assertEqual(moved, 3)
        self.assertNotIn("chart_id", script["narration"])
        ids = {s.get("chart_id") for s in script["scenes"]} - {None}
        self.assertEqual(ids, {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
