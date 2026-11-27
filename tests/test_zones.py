import unittest

from audit.zones import SyntheticZone
from tests.helper import ZONE


class SyntheticZoneTests(unittest.TestCase):
    def setUp(self):
        self.zone = SyntheticZone(ZONE)

    def test_offset_segments(self):
        # 规则表前默认 +0；夏令时段 +60；回拨后回到 +0。
        self.assertEqual(self.zone.offset_at(self.zone.parse_utc("2026-02-01T00:00:00Z")), 0)
        self.assertEqual(self.zone.offset_at(self.zone.parse_utc("2026-06-01T00:00:00Z")), 60)
        self.assertEqual(self.zone.offset_at(self.zone.parse_utc("2026-12-01T00:00:00Z")), 0)
        # 切换点当刻即采用新偏移。
        self.assertEqual(self.zone.offset_at(self.zone.parse_utc("2026-03-29T01:00:00Z")), 60)
        self.assertEqual(self.zone.offset_at(self.zone.parse_utc("2026-11-01T01:00:00Z")), 0)

    def test_fall_back_two_distinct_utc_mappings(self):
        local = self.zone.parse_local("2026-11-01T01:30")
        self.assertTrue(self.zone.is_ambiguous(local))
        fold0 = self.zone.local_to_utc(local, 0)
        fold1 = self.zone.local_to_utc(local, 1)
        # 同一种本地时间的两种 UTC 映射，恰好相差 60 分钟。
        self.assertIsNotNone(fold0)
        self.assertIsNotNone(fold1)
        self.assertNotEqual(fold0, fold1)
        self.assertEqual(fold1 - fold0, 60)
        self.assertEqual(self.zone.fmt_utc(fold0), "2026-11-01T00:30:00Z")
        self.assertEqual(self.zone.fmt_utc(fold1), "2026-11-01T01:30:00Z")
        # 两种映射转回本地都等于同一挂钟分钟。
        self.assertEqual(self.zone.utc_to_local(fold0), local)
        self.assertEqual(self.zone.utc_to_local(fold1), local)

    def test_non_ambiguous_minute_has_single_mapping(self):
        local = self.zone.parse_local("2026-11-01T03:30")
        self.assertFalse(self.zone.is_ambiguous(local))
        self.assertEqual(self.zone.valid_folds(local), [0])
        utc = self.zone.local_to_utc(local, 0)
        self.assertEqual(utc, self.zone.local_to_utc(local, 1))

    def test_spring_forward_gap_is_invalid(self):
        # 前跳跳过的是本地 01:00..01:59 这一小时（时钟从 01:00 跳到 02:00），
        # 该小时内任何分钟两个 fold 都非法；02:00 之后重新存在。
        for hm in ("01:00", "01:30", "01:59"):
            local = self.zone.parse_local(f"2026-03-29T{hm}")
            self.assertEqual(self.zone.valid_folds(local), [])
            self.assertIsNone(self.zone.local_to_utc(local, 0))
            self.assertIsNone(self.zone.local_to_utc(local, 1))
        back = self.zone.parse_local("2026-03-29T02:30")
        self.assertEqual(len(self.zone.valid_folds(back)), 1)

    def test_forward_gap_interval_is_the_skipped_local_hour(self):
        gaps = self.zone.forward_gap_local_intervals()
        self.assertEqual(len(gaps), 1)
        g0, g1 = gaps[0]
        self.assertEqual(self.zone.fmt_local(g0), "2026-03-29T01:00")
        self.assertEqual(self.zone.fmt_local(g1), "2026-03-29T02:00")
        self.assertEqual(g1 - g0, 60)

    def test_roundtrip_small_integer_minutes(self):
        # 对连续小整数 UTC 分钟做双向往返，必须逐分钟闭合。
        start = self.zone.parse_utc("2026-10-31T00:00:00Z")
        for utc in range(start, start + 3 * 24 * 60):
            local = self.zone.utc_to_local(utc)
            back = self.zone.local_to_utc(local, 0)
            if back is None:  # 歧义分钟 fold0 可能指向另一瞬间，改用回验
                folds = self.zone.valid_folds(local)
                self.assertTrue(
                    any(self.zone.local_to_utc(local, f) == utc for f in folds)
                )
            else:
                self.assertTrue(
                    back == utc
                    or self.zone.local_to_utc(local, 1) == utc
                )

    def test_duplicate_transition_rejected(self):
        bad = {
            "name": "bad", "origin": ZONE["origin"],
            "rules": [
                {"at_utc": "2026-11-01T01:00:00Z", "offset_minutes": 60},
                {"at_utc": "2026-11-01T01:00:00Z", "offset_minutes": 0},
            ],
        }
        with self.assertRaises(ValueError):
            SyntheticZone(bad)


if __name__ == "__main__":
    unittest.main()
