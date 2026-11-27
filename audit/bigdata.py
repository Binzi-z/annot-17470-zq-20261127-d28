"""六万条记录的确定性合成与单组变更实测。

场景：每版 1000 个组 × 60 条 = 60000 条记录，全部锚定在回拨日之后
（无歧义分钟，fold 固定 0），v2 相对 v1 **只改一个组**的一条 grant
（把 60 分钟窗口换成相邻的 60 分钟窗口，本地挂钟文本不变）。

实测两种扫描路径的读取条数：

* 全量重放：读取该版全部 60000 条；
* 组索引重放：只读取变更组的 60 条——索引让六万条里只改一个组时
  的审计成本与总量解耦。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from .diffaudit import changed_groups, targeted_audit
from .engine import ReplayEngine
from .model import load_input

GROUPS = 1000
RECORDS_PER_GROUP = 60
TARGET_GROUP = "G0500"
V1, V2 = "v1", "v2"


def _base_table() -> dict:
    return {
        "name": "合成夏令时区",
        "origin": "2026-01-01T00:00:00Z",
        "initial_offset_minutes": 0,
        "rules": [
            {"at_utc": "2026-03-29T01:00:00Z", "offset_minutes": 60},
            {"at_utc": "2026-11-01T01:00:00Z", "offset_minutes": 0},
        ],
    }


def _anchor(rid: str, version: str, alias: str, root: str) -> dict:
    return {
        "id": rid, "version": version, "group": "ID",
        "kind": "anchor", "at": "2026-01-02T00:00:00Z",
        "root": root,
        "signers": [{"alias": alias, "generation": 0}],
    }


def _grant(rid, version, group, day, start_hm, signers, fold=0) -> dict:
    h, m = start_hm
    end_h, end_m = h, m + 60
    if end_m >= 60:
        end_h += 1
        end_m -= 60
    return {
        "id": rid, "version": version, "group": group, "kind": "grant",
        "local_start": f"2026-11-{day:02d}T{h:02d}:{m:02d}",
        "local_end": f"2026-11-{day:02d}T{end_h:02d}:{end_m:02d}",
        "fold": fold, "signers": list(signers),
    }


def _deny(rid, version, group, day, start_hm, signers) -> dict:
    h, m = start_hm
    end_h, end_m = h, m + 30
    if end_m >= 60:
        end_h += 1
        end_m -= 60
    return {
        "id": rid, "version": version, "group": group, "kind": "deny",
        "local_start": f"2026-11-{day:02d}T{h:02d}:{m:02d}",
        "local_end": f"2026-11-{day:02d}T{end_h:02d}:{end_m:02d}",
        "fold": 0, "signers": list(signers),
    }


def build_v1_payload() -> dict:
    """确定性生成每版 60000 条记录的 v1 输入。"""
    records = [
        _anchor(f"{V1}-ID-A0", V1, "alpha", "ROOT-A"),
        _anchor(f"{V1}-ID-B0", V1, "beta", "ROOT-B"),
    ]
    solo = [{"alias": "alpha", "generation": 0}]
    joint = [
        {"alias": "alpha", "generation": 0},
        {"alias": "beta", "generation": 0},
    ]
    for gi in range(GROUPS):
        group = f"G{gi:04d}"
        for k in range(RECORDS_PER_GROUP):
            rid = f"{V1}-{group}-R{k:02d}"
            day = 10 + (k % 18)           # 11 月 10..27 日，回拨日之后
            hour = 6 + (k % 14)           # 06:00..19:xx
            minute = (k * 7) % 60
            if k % 6 == 5:                # 每 6 条一条显式拒绝
                records.append(_deny(rid, V1, group, day, (hour, minute), solo))
            elif k % 11 == 7:             # 少量双人联合 grant
                records.append(_grant(rid, V1, group, day, (hour, minute), joint))
            else:
                records.append(_grant(rid, V1, group, day, (hour, minute), solo))
    expected = GROUPS * RECORDS_PER_GROUP + 2
    if len(records) != expected:
        raise AssertionError((len(records), expected))
    return {"synthetic": True, "zone": _base_table(), "records": records}


def build_v2_payload(v1_payload: dict) -> dict:
    """v2 = v1 深拷贝后只改 TARGET_GROUP 的一条 grant（窗口平移 60 分钟）。"""
    payload = deepcopy(v1_payload)
    for rec in payload["records"]:
        # 版本与记录 id 全部切到 v2 命名空间，保证合并输入 id 唯一。
        rec["version"] = V2
        rec["id"] = V2 + rec["id"][len(V1):]
    changed = 0
    for rec in payload["records"]:
        if rec["group"] != TARGET_GROUP or rec["kind"] != "grant":
            continue
        # 本地文本从 06:00-07:00 改成 07:00-08:00，挂钟日期不变，
        # 只移动 UTC 窗口；这是两版之间唯一的记录差异。
        if rec["local_start"].endswith("T06:00"):
            rec["local_start"] = rec["local_start"].replace("T06:00", "T07:00")
            rec["local_end"] = rec["local_end"].replace("T07:00", "T08:00")
            changed += 1
            break
    if changed != 1:
        raise AssertionError(f"预期恰好修改 1 条，实际 {changed} 条")
    return payload


@dataclass
class BigDataResult:
    total_per_version: int
    business_records_per_version: int
    full_scan_v1: int
    full_scan_v2: int
    indexed_scan_total: int
    indexed_scan_per_version: int
    target_group_size: int
    changed_groups: tuple[str, ...]
    elapsed_seconds: float


def run_bigdata_experiment() -> BigDataResult:
    import time

    t0 = time.perf_counter()
    v1_payload = build_v1_payload()
    v2_payload = build_v2_payload(v1_payload)

    # 合并装入一个引擎：两版各 60002 条，索引按 (version, group) 建立。
    merged = {
        "synthetic": True,
        "zone": v1_payload["zone"],
        "records": v1_payload["records"] + v2_payload["records"],
    }

    # 全量路径：重放整版，读取该版全部记录。
    full_engine = ReplayEngine(load_input(merged))
    full_engine.replay_version(V1)
    full_scan_v1 = full_engine.scan_count
    full_engine.replay_version(V2)
    full_scan_v2 = full_engine.scan_count - full_scan_v1

    # 索引路径：组级摘要预筛（不触发重放扫描），再只重放变更组。
    indexed_engine = ReplayEngine(load_input(merged))
    changed = changed_groups(indexed_engine, V1, V2)
    if changed != (TARGET_GROUP,):
        raise AssertionError(f"预筛应只命中 {TARGET_GROUP}，实际 {changed}")
    diffs = targeted_audit(indexed_engine, V1, V2, changed)
    if any(not d.has_change for d in diffs.values()):
        raise AssertionError("变更组必须审计出实际差异")
    indexed_total = indexed_engine.scan_count

    elapsed = time.perf_counter() - t0
    return BigDataResult(
        total_per_version=GROUPS * RECORDS_PER_GROUP + 2,
        business_records_per_version=GROUPS * RECORDS_PER_GROUP,
        full_scan_v1=full_scan_v1,
        full_scan_v2=full_scan_v2,
        indexed_scan_total=indexed_total,
        indexed_scan_per_version=indexed_total // 2,
        target_group_size=RECORDS_PER_GROUP,
        changed_groups=changed,
        elapsed_seconds=elapsed,
    )
