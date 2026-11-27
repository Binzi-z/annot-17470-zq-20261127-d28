import unittest

from audit.diffaudit import (
    _canonical,
    audit_versions,
    build_manifest,
    changed_groups,
    sha256_blob,
    subtract,
    subtract_many,
    targeted_audit,
)
from audit.engine import ReplayEngine
from audit.model import load_input
from tests.helper import (
    anchor, deny, grant, load_fixture, payload, sig,
)


class IntervalMathTests(unittest.TestCase):
    def test_subtract_disjoint_and_contained(self):
        self.assertEqual(subtract((0, 10), (10, 20)), [(0, 10)])
        self.assertEqual(subtract((0, 10), (0, 10)), [])
        self.assertEqual(subtract((0, 10), (3, 7)), [(0, 3), (7, 10)])
        self.assertEqual(subtract((0, 10), (-5, 4)), [(4, 10)])

    def test_subtract_many_splits_into_pieces(self):
        pieces = subtract_many((0, 100), [(10, 20), (50, 60)])
        self.assertEqual(pieces, [(0, 10), (20, 50), (60, 100)])

    def test_subtract_many_full_cover_is_empty_list(self):
        self.assertEqual(subtract_many((0, 10), [(0, 5), (5, 10)]), [])
        # 可能为空的区间：空目标直接返回空列表而非报错。
        self.assertEqual(subtract_many((5, 5), [(0, 10)]), [])


def _fold_shift_records():
    return [
        anchor("v1a", "v1", "alpha", "R1"),
        anchor("v2a", "v2", "alpha", "R1"),
        # 窗口完全落在回拨歧义小时内，随 fold 整体平移（两个 30 分钟窗口不相交）。
        grant("v1g", "grp", [sig("alpha")], "2026-11-01T01:00",
              "2026-11-01T01:30", version="v1", fold=0),
        grant("v2g", "grp", [sig("alpha")], "2026-11-01T01:00",
              "2026-11-01T01:30", version="v2", fold=1),
    ]


class DiffAuditTests(unittest.TestCase):
    def test_fold_shift_is_reported_as_narrowed_and_widened(self):
        eng = ReplayEngine(load_input(payload(_fold_shift_records())))
        diffs = audit_versions(eng, "v1", "v2")
        gd = diffs["grp"]
        # v1 早 60 分钟 → 一段收窄；v2 晚 60 分钟 → 一段扩大。
        self.assertEqual(len(gd.narrowed), 1)
        self.assertEqual(len(gd.widened), 1)
        narrowed = gd.narrowed[0]
        widened = gd.widened[0]
        self.assertEqual(narrowed.end - narrowed.start, 30)
        self.assertEqual(widened.end - widened.start, 30)
        self.assertEqual(widened.support, ("v2g",))
        self.assertTrue(widened.support_necessary)

    def test_identical_versions_have_no_diff_but_empty_lists_kept(self):
        records = [
            anchor("v1a", "v1", "alpha", "R1"),
            anchor("v2a", "v2", "alpha", "R1"),
            grant("v1g", "grp", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:00", version="v1"),
            grant("v2g", "grp", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:00", version="v2"),
        ]
        eng = ReplayEngine(load_input(payload(records)))
        gd = audit_versions(eng, "v1", "v2")["grp"]
        self.assertFalse(gd.has_change)
        self.assertEqual(gd.widened, [])
        self.assertEqual(gd.narrowed, [])

    def test_empty_group_marked_when_deny_covers_everything(self):
        records = [
            anchor("v1a", "v1", "alpha", "R1"),
            anchor("v2a", "v2", "alpha", "R1"),
            grant("v1g", "grp", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:00", version="v1"),
            grant("v2g", "grp", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:00", version="v2"),
            deny("v2d", "grp", [sig("alpha")], "2026-11-05T01:00",
                 "2026-11-05T02:00", version="v2"),
        ]
        eng = ReplayEngine(load_input(payload(records)))
        gd = audit_versions(eng, "v1", "v2")["grp"]
        self.assertIn("v2", gd.empty_in)
        self.assertNotIn("v1", gd.empty_in)

    def test_widening_support_is_minimal(self):
        # v2 用两条重叠 grant 扩大，扩大段只需其中能铺满的最少记录。
        records = [
            anchor("v1a", "v1", "alpha", "R1"),
            anchor("v2a", "v2", "alpha", "R1"),
            grant("v1g", "grp", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:00", version="v1"),
            grant("v2a1", "grp", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:30", version="v2"),
            grant("v2a2", "grp", [sig("alpha")], "2026-11-05T02:00",
                  "2026-11-05T03:00", version="v2"),
        ]
        eng = ReplayEngine(load_input(payload(records)))
        gd = audit_versions(eng, "v1", "v2")["grp"]
        # 扩大段是 [02:00,03:00)，v2a1 已覆盖到 02:30、v2a2 覆盖整段，
        # 单条 v2a2 即可铺满 → 最少支撑只有一条。
        widened = gd.widened
        self.assertTrue(widened)
        for piece in widened:
            self.assertEqual(len(piece.support), 1)
            self.assertTrue(piece.support_necessary)


class TargetedAuditTests(unittest.TestCase):
    def test_changed_groups_prefilter_and_targeted_scan(self):
        records = [
            anchor("v1a", "v1", "alpha", "R1"),
            anchor("v2a", "v2", "alpha", "R1"),
        ]
        for gi in range(5):
            g = f"g{gi}"
            records.append(grant(f"v1-{g}", g, [sig("alpha")],
                                 "2026-11-05T01:00", "2026-11-05T02:00",
                                 version="v1"))
            records.append(grant(f"v2-{g}", g, [sig("alpha")],
                                 "2026-11-05T01:00", "2026-11-05T02:00",
                                 version="v2"))
        # 只让 g3 在 v2 多出一条 grant。
        records.append(grant("v2-g3-extra", "g3", [sig("alpha")],
                             "2026-11-05T02:00", "2026-11-05T03:00",
                             version="v2"))
        eng = ReplayEngine(load_input(payload(records)))
        changed = changed_groups(eng, "v1", "v2")
        self.assertEqual(changed, ("g3",))
        # 预筛不计入重放扫描。
        self.assertEqual(eng.scan_count, 0)
        diffs = targeted_audit(eng, "v1", "v2", changed)
        self.assertEqual(set(diffs), {"g3"})
        # 只重放了 g3 的两个版本（各 1/2 条），其余组零扫描。
        self.assertEqual(eng.scan_count, 3)


class ManifestTests(unittest.TestCase):
    def test_manifest_hashes_stable_across_full_replays(self):
        loaded = load_input(load_fixture())
        first = build_manifest(loaded, ("v1", "v2"))
        second = build_manifest(loaded, ("v1", "v2"))
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        for ver in ("v1", "v2"):
            h1 = sha256_blob(_canonical(first["snapshots"][ver]))
            h2 = sha256_blob(_canonical(second["snapshots"][ver]))
            self.assertEqual(h1, h2)

    def test_tampering_changes_snapshot_hash(self):
        loaded = load_input(load_fixture())
        first = build_manifest(loaded, ("v1", "v2"))
        # 必须在装载前篡改原始输入：重放使用的是转换后的整数分钟记录。
        tampered_raw = load_fixture()
        for rec in tampered_raw["records"]:
            if rec["id"] == "V2-SHIFT-A-G1":
                rec["local_end"] = "2026-11-01T02:30"
        second = build_manifest(load_input(tampered_raw), ("v1", "v2"))
        v1_key = lambda m: sha256_blob(_canonical(m["snapshots"]["v1"]))
        v2_key = lambda m: sha256_blob(_canonical(m["snapshots"]["v2"]))
        self.assertEqual(v1_key(first), v1_key(second))
        self.assertNotEqual(v2_key(first), v2_key(second))


if __name__ == "__main__":
    unittest.main()
