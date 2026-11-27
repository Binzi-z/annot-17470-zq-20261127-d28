import unittest

from audit.model import AliasBook, Record, Sig, load_input
from tests.helper import (
    anchor, deny, grant, load_fixture, payload, reroot, revoke, sig,
)


def _anchor_record(rid, alias, root, at_min=100, gen=0):
    return Record(
        record_id=rid, version="v1", group="ID", kind="anchor",
        at=at_min, root=root, signers=(Sig(alias, gen),),
    )


def _reroot_record(rid, alias, root, at_min, gen):
    return Record(
        record_id=rid, version="v1", group="ID", kind="re_root",
        at=at_min, root=root, signers=(Sig(alias, gen),),
    )


class AliasBookTests(unittest.TestCase):
    def test_generation_chain_and_reroot(self):
        book = AliasBook()
        book.install(_anchor_record("A0", "alpha", "ROOT-X", 0))
        book.install(_reroot_record("A1", "alpha", "ROOT-Y", 100, 1))
        self.assertEqual(book.root_at("alpha", 0, 50), "ROOT-X")
        # 换根时刻起旧一代立即失效。
        self.assertIsNone(book.root_at("alpha", 0, 100))
        self.assertIsNone(book.root_at("alpha", 0, 200))
        # 新一代挂载在新 root。
        self.assertEqual(book.root_at("alpha", 1, 100), "ROOT-Y")
        self.assertEqual(book.root_at("alpha", 1, 500), "ROOT-Y")

    def test_unknown_alias_and_generation(self):
        book = AliasBook()
        book.install(_anchor_record("A0", "alpha", "ROOT-X", 0))
        self.assertIsNone(book.root_at("ghost", 0, 10))
        self.assertIsNone(book.root_at("alpha", 9, 10))
        self.assertIsNone(book.root_at("alpha", 0, -1))

    def test_generation_must_be_contiguous(self):
        book = AliasBook()
        book.install(_anchor_record("A0", "alpha", "ROOT-X", 0))
        with self.assertRaises(ValueError):
            book.install(_reroot_record("A2", "alpha", "ROOT-Y", 100, 2))

    def test_reroot_must_be_later(self):
        book = AliasBook()
        book.install(_anchor_record("A0", "alpha", "ROOT-X", 100))
        with self.assertRaises(ValueError):
            book.install(_reroot_record("A1", "alpha", "ROOT-Y", 100, 1))


class LoadInputTests(unittest.TestCase):
    def test_same_local_text_different_fold_different_utc(self):
        records = [
            anchor("v1a", "v1", "alpha", "R1"),
            anchor("v2a", "v2", "alpha", "R1"),
            # 窗口两端都落在回拨歧义小时内，整段随 fold 平移 30 分钟。
            grant("v1g", "g1", [sig("alpha")], "2026-11-01T01:00",
                  "2026-11-01T01:30", version="v1", fold=0),
            grant("v2g", "g1", [sig("alpha")], "2026-11-01T01:00",
                  "2026-11-01T01:30", version="v2", fold=1),
        ]
        loaded = load_input(payload(records))
        by_id = {r.record_id: r for r in loaded.records}
        v1w, v2w = by_id["v1g"].window, by_id["v2g"].window
        # 挂钟文本相同，但 fold=1 整体晚 60 分钟（两端都是）。
        self.assertEqual(v2w[0] - v1w[0], 60)
        self.assertEqual(v2w[1] - v1w[1], 60)
        # 两个 30 分钟窗口互不相交。
        self.assertEqual(v1w[1] - v1w[0], 30)
        self.assertEqual(v2w[1] - v2w[0], 30)

    def test_spring_forward_window_rejected(self):
        records = [
            anchor("a", "v1", "alpha", "R1"),
            grant("g", "g1", [sig("alpha")], "2026-03-29T01:00",
                  "2026-03-29T02:00"),
        ]
        with self.assertRaises(ValueError):
            load_input(payload(records))

    def test_duplicate_id_rejected(self):
        records = [
            anchor("a", "v1", "alpha", "R1"),
            grant("x", "g1", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:00"),
            grant("x", "g1", [sig("alpha")], "2026-11-05T02:00",
                  "2026-11-05T03:00"),
        ]
        with self.assertRaises(ValueError):
            load_input(payload(records))

    def test_unsigned_grant_rejected(self):
        records = [
            anchor("a", "v1", "alpha", "R1"),
            grant("g", "g1", [], "2026-11-05T01:00", "2026-11-05T02:00"),
        ]
        with self.assertRaises(ValueError):
            load_input(payload(records))

    def test_revoke_requires_target_and_signers(self):
        records = [
            anchor("a", "v1", "alpha", "R1"),
            grant("g", "g1", [sig("alpha")], "2026-11-05T01:00",
                  "2026-11-05T02:00"),
        ]
        bad = records + [
            {"id": "x", "version": "v1", "group": "g1", "kind": "revoke",
             "at": "2026-11-05T01:30:00Z", "target": "g", "signers": []},
        ]
        with self.assertRaises(ValueError):
            load_input(payload(bad))

    def test_fixture_loads_and_counts(self):
        loaded = load_input(load_fixture())
        kinds = {}
        for r in loaded.records:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
        self.assertIn("grant", kinds)
        self.assertIn("deny", kinds)
        self.assertIn("revoke", kinds)
        self.assertIn("re_root", kinds)


if __name__ == "__main__":
    unittest.main()
