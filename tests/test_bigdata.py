import json
import unittest

from audit.bigdata import (
    GROUPS,
    RECORDS_PER_GROUP,
    TARGET_GROUP,
    V1,
    V2,
    build_v1_payload,
    build_v2_payload,
    run_bigdata_experiment,
)
from audit.model import load_input


class BigDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v1 = build_v1_payload()
        cls.v2 = build_v2_payload(cls.v1)

    def test_exactly_sixty_thousand_business_records_per_version(self):
        business = [r for r in self.v1["records"] if r["group"] != "ID"]
        self.assertEqual(len(business), GROUPS * RECORDS_PER_GROUP)
        self.assertEqual(GROUPS * RECORDS_PER_GROUP, 60_000)
        self.assertEqual(len(self.v1["records"]), 60_002)
        self.assertEqual(len(self.v2["records"]), 60_002)

    def test_only_one_group_differs_between_versions(self):
        def substance(records, group):
            out = []
            for r in records:
                if r["group"] != group:
                    continue
                item = {k: v for k, v in r.items() if k not in ("id", "version")}
                out.append(json.dumps(item, sort_keys=True, ensure_ascii=False))
            return sorted(out)

        groups = {r["group"] for r in self.v1["records"]}
        diff_groups = [
            g for g in groups
            if substance(self.v1["records"], g) != substance(self.v2["records"], g)
        ]
        self.assertEqual(diff_groups, [TARGET_GROUP])

    def test_exactly_one_record_modified(self):
        v1_by_id = {r["id"]: r for r in self.v1["records"]}
        changed = 0
        for r2 in self.v2["records"]:
            counterpart_id = V1 + r2["id"][len(V2):]
            r1 = v1_by_id[counterpart_id]
            # id/version 命名空间差异除外，其余字段逐字段比对。
            for key in r1:
                if key in ("id", "version"):
                    continue
                if r1[key] != r2[key]:
                    changed += 1
                    break
        self.assertEqual(changed, 1)

    def test_both_payloads_load_and_merge(self):
        merged = {
            "synthetic": True,
            "zone": self.v1["zone"],
            "records": self.v1["records"] + self.v2["records"],
        }
        loaded = load_input(merged)
        ids = [r.record_id for r in loaded.records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 120_004)

    def test_measured_scan_counts_full_vs_indexed(self):
        result = run_bigdata_experiment()
        self.assertEqual(result.full_scan_v1, 60_002)
        self.assertEqual(result.full_scan_v2, 60_002)
        # 索引路径只触碰变更组：每版该组 60 条业务记录。
        self.assertEqual(result.indexed_scan_per_version,
                         RECORDS_PER_GROUP)
        self.assertEqual(result.indexed_scan_total,
                         2 * RECORDS_PER_GROUP)
        self.assertEqual(result.changed_groups, (TARGET_GROUP,))
        # 索引扫描必须远小于全量扫描。
        self.assertLess(result.indexed_scan_total, result.full_scan_v1 // 100)


if __name__ == "__main__":
    unittest.main()
