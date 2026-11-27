import unittest

from audit.engine import JOINT, SOLO, ReplayEngine
from audit.model import load_input
from tests.helper import (
    anchor, deny, grant, payload, reroot, revoke, sig,
)


def engine_of(records):
    return ReplayEngine(load_input(payload(records)))


def anchors():
    return [
        anchor("a0", "v1", "alpha", "ROOT-A"),
        anchor("b0", "v1", "beta", "ROOT-B"),
        anchor("c0", "v1", "gamma", "ROOT-A"),  # 与 alpha 同 root
    ]


class FragmentTests(unittest.TestCase):
    def test_solo_grant_single_fragment_and_support(self):
        eng = engine_of(anchors() + [
            grant("g1", "grp", [sig("alpha")],
                  "2026-11-05T01:00", "2026-11-05T03:00"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(len(rep.fragments), 1)
        f = rep.fragments[0]
        self.assertEqual(rep.scanned, 1)
        self.assertEqual(f.support, ("g1",))
        self.assertEqual(f.required_signers, SOLO)
        self.assertEqual(f.end - f.start, 120)
        self.assertEqual(rep.gaps, [])

    def test_minimal_support_drops_redundant_overlap(self):
        # A [01:00,02:00) B [01:30,02:30) C [02:00,03:00)：
        # A+C 已连续铺满，B 是多余重叠记录，不得进入最少支撑集。
        eng = engine_of(anchors() + [
            grant("A", "grp", [sig("alpha")], "2026-11-05T01:00", "2026-11-05T02:00"),
            grant("B", "grp", [sig("alpha")], "2026-11-05T01:30", "2026-11-05T02:30"),
            grant("C", "grp", [sig("alpha")], "2026-11-05T02:00", "2026-11-05T03:00"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(len(rep.fragments), 1)
        self.assertEqual(rep.fragments[0].support, ("A", "C"))

    def test_adjacent_grants_merge_into_one_fragment_with_intersection_support(self):
        # 两段相邻 grant，合并片段的支撑必须是贯穿整段的交集思路下
        # 仍能铺满的最小组合（这里必须两条都保留）。
        eng = engine_of(anchors() + [
            grant("A", "grp", [sig("alpha")], "2026-11-05T01:00", "2026-11-05T02:00"),
            grant("B", "grp", [sig("alpha")], "2026-11-05T02:00", "2026-11-05T03:00"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(len(rep.fragments), 1)
        f = rep.fragments[0]
        self.assertEqual((f.start, f.end), rep.horizon)
        self.assertEqual(set(f.support), {"A", "B"})


class DenyRevokeRerootTests(unittest.TestCase):
    def test_deny_holes_are_kept(self):
        eng = engine_of(anchors() + [
            grant("g1", "grp", [sig("alpha")],
                  "2026-11-05T01:00", "2026-11-05T03:00"),
            deny("d1", "grp", [sig("alpha")],
                 "2026-11-05T01:30", "2026-11-05T02:00"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual([g.reason for g in rep.gaps], ["deny"])
        self.assertEqual(len(rep.fragments), 2)
        covered = sum(f.end - f.start for f in rep.fragments)
        self.assertEqual(covered, 120 - 30)

    def test_revoke_cuts_grant_from_effective_minute(self):
        eng = engine_of(anchors() + [
            grant("g1", "grp", [sig("alpha")],
                  "2026-11-05T01:00", "2026-11-05T03:00"),
            revoke("x1", "grp", "g1", [sig("alpha")],
                   "2026-11-05T02:00:00Z"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(len(rep.fragments), 1)
        self.assertEqual(rep.fragments[0].end - rep.fragments[0].start, 60)
        self.assertTrue(all(g.reason == "revoke" for g in rep.gaps))

    def test_revoke_signed_by_stale_generation_is_ignored(self):
        # alpha 在 01:30 换根；02:00 的撤销若仍用旧第 0 代签名则无效，
        # grant 保持完整覆盖。
        records = anchors() + [
            reroot("a1", "v1", "alpha", "ROOT-X",
                   "2026-11-05T01:30:00Z", gen=1),
            grant("g1", "grp", [sig("alpha", 0)],
                  "2026-11-05T01:00", "2026-11-05T03:00"),
            revoke("x1", "grp", "g1", [sig("alpha", 0)],
                   "2026-11-05T02:00:00Z"),
        ]
        eng = engine_of(records)
        rep = eng.replay_group("v1", "grp")
        # grant 自身在 01:30 后也失效，但不存在 revoke 空洞——
        # 原因必须全部是 reroot，证明撤销被忽略。
        self.assertTrue(rep.gaps)
        self.assertTrue(all(g.reason == "reroot" for g in rep.gaps))

    def test_reroot_invalidates_old_generation_mid_window(self):
        records = anchors() + [
            reroot("a1", "v1", "alpha", "ROOT-X",
                   "2026-11-05T01:30:00Z", gen=1),
            grant("g1", "grp", [sig("alpha", 0)],
                  "2026-11-05T01:00", "2026-11-05T03:00"),
        ]
        eng = engine_of(records)
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(len(rep.fragments), 1)
        self.assertEqual(rep.fragments[0].end - rep.fragments[0].start, 30)
        self.assertTrue(all(g.reason == "reroot" for g in rep.gaps))


class JointSignerTests(unittest.TestCase):
    def test_two_independent_roots_form_joint_coverage(self):
        eng = engine_of(anchors() + [
            grant("j1", "grp", [sig("alpha"), sig("beta")],
                  "2026-11-05T01:00", "2026-11-05T02:00"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(len(rep.fragments), 1)
        self.assertEqual(rep.fragments[0].required_signers, JOINT)

    def test_same_root_signers_are_not_independent(self):
        # alpha 与 gamma 都挂 ROOT-A：联合不成立，无任何可验证片段。
        eng = engine_of(anchors() + [
            grant("j1", "grp", [sig("alpha"), sig("gamma")],
                  "2026-11-05T01:00", "2026-11-05T02:00"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(rep.fragments, [])
        self.assertTrue(rep.empty)

    def test_same_alias_twice_is_one_root_only(self):
        eng = engine_of(anchors() + [
            grant("j1", "grp", [sig("alpha"), sig("alpha")],
                  "2026-11-05T01:00", "2026-11-05T02:00"),
        ])
        rep = eng.replay_group("v1", "grp")
        self.assertEqual(rep.fragments, [])


class ScanCountTests(unittest.TestCase):
    def _two_group_payload(self):
        return anchors() + [
            grant("g1a", "grp1", [sig("alpha")],
                  "2026-11-05T01:00", "2026-11-05T02:00"),
            grant("g2a", "grp2", [sig("alpha")],
                  "2026-11-05T01:00", "2026-11-05T02:00"),
        ]

    def test_full_replay_scans_every_group(self):
        eng = engine_of(self._two_group_payload())
        eng.replay_version("v1")
        # ID 组 3 条锚点 + grp1/grp2 各 1 条。
        self.assertEqual(eng.scan_count, 5)

    def test_indexed_replay_scans_only_target_group(self):
        eng = engine_of(self._two_group_payload())
        rep = eng.replay_group("v1", "grp2")
        self.assertEqual(eng.scan_count, 1)
        self.assertEqual(rep.scanned, 1)

    def test_unknown_group_scans_zero(self):
        eng = engine_of(self._two_group_payload())
        rep = eng.replay_group("v1", "nope")
        self.assertEqual(eng.scan_count, 0)
        self.assertIsNone(rep.horizon)


if __name__ == "__main__":
    unittest.main()
