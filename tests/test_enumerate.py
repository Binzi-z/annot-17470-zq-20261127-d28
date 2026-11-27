import unittest

from audit.engine import ReplayEngine
from audit.enumerate import (
    enumerate_identity_pairs,
    enumerate_minutes,
    exhaustive_report,
    transition_windows,
)
from audit.model import load_input
from tests.helper import load_fixture


class MinuteEnumerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_input(load_fixture())
        cls.zone = cls.loaded.zone
        cls.cases = []
        for label, start, span in transition_windows(cls.zone):
            cls.cases.extend(enumerate_minutes(cls.zone, start, span, label))

    def test_two_windows_of_small_integer_minutes(self):
        # 春前跳日与秋回拨日各 24 本地小时，共 2880 个连续小整数分钟。
        self.assertEqual(len(self.cases), 2 * 24 * 60)

    def test_every_minute_has_one_or_two_valid_folds_or_zero_in_gap(self):
        populations = {len(c.folds) for c in self.cases}
        # 0=春令时缺口，1=普通分钟，2=回拨重复，三类都应出现。
        self.assertEqual(populations, {0, 1, 2})

    def test_ambiguous_minutes_have_two_distinct_utc_mappings(self):
        ambiguous = [c for c in self.cases if c.ambiguous]
        # 回拨重复本地小时恰 60 个分钟。
        self.assertEqual(len(ambiguous), 60)
        for case in ambiguous:
            self.assertEqual(len(case.folds), 2)
            self.assertEqual(case.utc_by_fold[1] - case.utc_by_fold[0], 60)
            # 同一本地分钟的两种 UTC 映射都能回验。
            for fold in (0, 1):
                self.assertEqual(
                    self.zone.utc_to_local(case.utc_by_fold[fold]),
                    case.local_minute,
                )

    def test_spring_gap_minutes_have_no_fold(self):
        gap = [c for c in self.cases if len(c.folds) == 0]
        # 前跳丢失的本地小时也是 60 个分钟，且全部落在春前跳窗口。
        self.assertEqual(len(gap), 60)
        self.assertTrue(all(c.window == "spring-forward" for c in gap))
        self.assertTrue(all(c.utc_by_fold == (None, None) for c in gap))

    def test_exhaustive_report_double_mappings(self):
        report = exhaustive_report(self.loaded, "v2")
        self.assertEqual(report["ambiguous_minute_count"], 60)
        first = report["double_utc_mappings"][0]
        self.assertEqual(first["utc_minute_gap"], 60)
        # 两窗口人口：春窗口有缺口无歧义，秋窗口有歧义无缺口。
        self.assertIn("0", report["window_fold_population"]["spring-forward"])
        self.assertNotIn("0", report["window_fold_population"]["fall-back"])
        self.assertIn("2", report["window_fold_population"]["fall-back"])


class IdentityPairEnumerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_input(load_fixture())

    def test_all_generations_are_enumerated(self):
        pairs = enumerate_identity_pairs(self.loaded, "v2")
        aliases = {m["alias"] for m in pairs["members"]}
        self.assertEqual(aliases, {"alpha", "beta", "gamma"})
        # beta 与 gamma 各有两代，alpha 一代 → 共 5 个身份代。
        self.assertEqual(len(pairs["members"]), 5)

    def test_independent_pairs_allow_joint_coverage(self):
        pairs = enumerate_identity_pairs(self.loaded, "v2")
        self.assertTrue(pairs["independent_pairs"])
        self.assertTrue(all(p["joint_ok"] for p in pairs["independent_pairs"]))
        # 同 root/失效代对一律不允许联合覆盖。
        self.assertTrue(all(not p["joint_ok"]
                            for p in pairs["same_root_pairs"]))

    def test_pair_count_is_choose_two_of_members(self):
        pairs = enumerate_identity_pairs(self.loaded, "v2")
        total = len(pairs["independent_pairs"]) + len(pairs["same_root_pairs"])
        self.assertEqual(total, 5 * 4 // 2)

    def test_rerooted_old_generation_cannot_pair_after_reroot(self):
        # beta gen0 在 01:20Z 换根；换根之后它不能再作为独立签署方。
        book = ReplayEngine(self.loaded).book("v2")
        self.assertIsNone(book.root_at("beta", 0,
                                       book.alias_chain("beta")[1][0]))
        self.assertEqual(book.root_at("beta", 1,
                                      book.alias_chain("beta")[1][0]),
                         "ROOT-DELTA")


if __name__ == "__main__":
    unittest.main()
