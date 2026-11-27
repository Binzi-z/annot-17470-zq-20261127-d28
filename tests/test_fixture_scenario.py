"""对 data/synthetic_input.json 手工场景的端到端断言。"""

import unittest

from audit.diffaudit import audit_versions
from audit.engine import JOINT, SOLO, ReplayEngine
from audit.model import load_input
from tests.helper import load_fixture


class FixtureScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_input(load_fixture())
        cls.zone = cls.loaded.zone
        cls.engine = ReplayEngine(cls.loaded)

    def _utc(self, text):
        return self.zone.parse_utc(text)

    def test_shift_a_same_local_text_different_fold_shifts_by_hour(self):
        v1 = self.engine.replay_group("v1", "shift-A")
        v2 = self.engine.replay_group("v2", "shift-A")
        self.assertEqual(v1.fragments[0].start,
                         self._utc("2026-11-01T00:00:00Z"))
        self.assertEqual(v2.fragments[0].start,
                         self._utc("2026-11-01T01:00:00Z"))
        # 两个班次获得不同的 UTC 时段权限。
        self.assertEqual(v2.fragments[0].start - v1.fragments[0].start, 60)

    def test_shift_b_deny_revoke_reroot_leave_verifiable_pieces(self):
        rep = self.engine.replay_group("v2", "shift-B")
        reasons = sorted({g.reason for g in rep.gaps})
        self.assertIn("deny", reasons)
        self.assertIn("revoke", reasons)
        self.assertIn("reroot", reasons)
        # 连续时间被切成两个可验证片段。
        self.assertEqual(len(rep.fragments), 2)
        self.assertTrue(all(f.required_signers == JOINT for f in rep.fragments))
        # 空洞与片段互不重叠且并集铺满整个请求范围。
        pieces = sorted(
            [(f.start, f.end) for f in rep.fragments]
            + [(g.start, g.end) for g in rep.gaps]
        )
        self.assertEqual(pieces[0][0], rep.horizon[0])
        self.assertEqual(pieces[-1][1], rep.horizon[1])
        for (_, e), (s2, _) in zip(pieces, pieces[1:]):
            self.assertEqual(e, s2)

    def test_shift_b_minimal_support_is_the_joint_grant_only(self):
        rep = self.engine.replay_group("v2", "shift-B")
        for f in rep.fragments:
            self.assertEqual(f.support, ("V2-SHIFT-B-G1",))

    def test_shift_c_is_empty_in_v2_but_covered_in_v1(self):
        v1 = self.engine.replay_group("v1", "shift-C")
        v2 = self.engine.replay_group("v2", "shift-C")
        self.assertFalse(v1.empty)
        self.assertTrue(v2.empty)
        self.assertTrue(all(g.reason == "deny" for g in v2.gaps))

    def test_shift_d_reroot_splits_joint_from_solo_coverage(self):
        rep = self.engine.replay_group("v2", "shift-D")
        strengths = sorted(f.required_signers for f in rep.fragments)
        self.assertEqual(strengths, [SOLO, JOINT])
        gap_reasons = {g.reason for g in rep.gaps}
        self.assertEqual(gap_reasons, {"reroot"})
        # 联合片段的支撑是双人 grant，单人片段是 solo grant。
        joint = next(f for f in rep.fragments if f.required_signers == JOINT)
        solo = next(f for f in rep.fragments if f.required_signers == SOLO)
        self.assertEqual(joint.support, ("V2-SHIFT-D-G1",))
        self.assertEqual(solo.support, ("V2-SHIFT-D-G2",))

    def test_shift_e_revoke_alone_removes_tail(self):
        rep = self.engine.replay_group("v2", "shift-E")
        self.assertEqual(len(rep.fragments), 1)
        f = rep.fragments[0]
        self.assertEqual(f.start, self._utc("2026-11-01T03:00:00Z"))
        self.assertEqual(f.end, self._utc("2026-11-01T03:30:00Z"))
        self.assertEqual(len(rep.gaps), 1)
        self.assertEqual(rep.gaps[0].reason, "revoke")
        self.assertEqual(rep.gaps[0].start,
                         self._utc("2026-11-01T03:30:00Z"))

    def test_diff_covers_every_widened_and_narrowed_piece(self):
        diffs = audit_versions(self.engine, "v1", "v2")
        # 每个有变化的组都至少产出一段扩大或收窄。
        changed = [gd for gd in diffs.values() if gd.has_change]
        self.assertTrue(changed)
        for gd in changed:
            for w in gd.widened:
                self.assertTrue(w.support)
                self.assertTrue(w.support_necessary)
                self.assertLess(w.start, w.end)
            for n in gd.narrowed:
                self.assertLess(n.start, n.end)

    def test_widened_and_narrowed_minutes_balance_on_fold_shift(self):
        # shift-A 纯 fold 平移：两个 30 分钟窗口不相交，扩大与收窄各 30 分钟。
        gd = audit_versions(self.engine, "v1", "v2")["shift-A"]
        widened = sum(w.end - w.start for w in gd.widened)
        narrowed = sum(n.end - n.start for n in gd.narrowed)
        self.assertEqual(widened, 30)
        self.assertEqual(narrowed, 30)

    def test_no_overlap_between_fragments_and_gaps(self):
        for ver in ("v1", "v2"):
            for rep in self.engine.replay_version(ver).values():
                if rep.horizon is None:
                    continue
                for f in rep.fragments:
                    for g in rep.gaps:
                        self.assertFalse(
                            f.start < g.end and g.start < f.end,
                            f"片段与空洞重叠: {f} vs {g}",
                        )

    def test_total_minutes_accounted_within_every_horizon(self):
        for ver in ("v1", "v2"):
            for rep in self.engine.replay_version(ver).values():
                if rep.horizon is None:
                    continue
                total = sum(f.end - f.start for f in rep.fragments)
                total += sum(g.end - g.start for g in rep.gaps)
                self.assertEqual(total, rep.horizon[1] - rep.horizon[0])


if __name__ == "__main__":
    unittest.main()
