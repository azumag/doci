"""run_daily._workdir_name のテスト。

同日同コーナーの後続run（3時間間隔で日に複数回走る運用）が前runの
workdir（script.json/video.mp4等）を上書きして未アップロード動画が
喪失した不具合の再発防止。実行時刻を末尾に付けて run ごとに一意にする。
"""
from __future__ import annotations

import unittest

from doci.run_daily import _workdir_name


class WorkdirNameTest(unittest.TestCase):
    def test_format_includes_day_corner_and_time(self) -> None:
        name = _workdir_name("2026-07-02", "capitalism", "143005")
        self.assertEqual(name, "2026-07-02_capitalism_143005")

    def test_keeps_existing_day_corner_prefix_for_searchability(self) -> None:
        name = _workdir_name("2026-07-02", "communism", "090000")
        self.assertTrue(name.startswith("2026-07-02_communism"))

    def test_different_times_yield_distinct_names_same_day_and_corner(self) -> None:
        name1 = _workdir_name("2026-07-01", "capitalism", "090000")
        name2 = _workdir_name("2026-07-01", "capitalism", "120000")
        self.assertNotEqual(name1, name2)


if __name__ == "__main__":
    unittest.main()
