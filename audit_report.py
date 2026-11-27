"""命令行入口：对 data/synthetic_input.json 输出完整差异审计报告。

用法::

    python audit_report.py                 # 场景审计 + 穷举 + 证据哈希
    python audit_report.py --big           # 追加六万条单组变更实测
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from audit.diffaudit import (
    audit_versions,
    build_manifest,
    sha256_blob,
    _canonical,
)
from audit.engine import ReplayEngine
from audit.enumerate import exhaustive_report
from audit.model import load_input

DATA = Path(__file__).parent / "data" / "synthetic_input.json"
VERSIONS = ("v1", "v2")


def fmt(zone, minutes: int) -> str:
    return zone.fmt_utc(minutes)


def main(with_big: bool = False) -> int:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    loaded = load_input(payload)
    zone = loaded.zone
    engine = ReplayEngine(loaded)
    print("=" * 72)
    print("一、合成时间规则表与回拨双映射")
    print("=" * 72)
    for tr in zone._rules:
        print(f"  {fmt(zone, tr.at_utc)} 起偏移 {tr.offset_minutes:+d} 分钟")
    report = exhaustive_report(loaded, "v2")
    print(f"  穷举本地分钟数: {report['minutes_enumerated']}，"
          f"fold 人口分布: {report['fold_population']}")
    print(f"  回拨歧义分钟数: {report['ambiguous_minute_count']}（示例前 3 条）")
    for item in report["double_utc_mappings"][:3]:
        lm = item["local_minute"]
        print(f"    本地 {zone.fmt_local(lm)}  fold0->UTC "
              f"{fmt(zone, item['fold0_utc'])}  fold1->UTC "
              f"{fmt(zone, item['fold1_utc'])}  相差 "
              f"{item['utc_minute_gap']} 分钟")

    print("=" * 72)
    print("二、两版名单完整重放：可验证片段与空洞")
    print("=" * 72)
    for ver in VERSIONS:
        print(f"[{ver}]")
        for group, rep in sorted(engine.replay_version(ver).items()):
            if rep.horizon is None:
                continue
            print(f"  组 {group}  范围 {fmt(zone, rep.horizon[0])} "
                  f".. {fmt(zone, rep.horizon[1])}  空={rep.empty}")
            for f in rep.fragments:
                kind = "双人联合" if f.required_signers == 2 else "单人"
                print(f"    可验证 {fmt(zone, f.start)} .. {fmt(zone, f.end)}"
                      f"  [{kind}]  最少支撑={list(f.support)}")
            for g in rep.gaps:
                print(f"    空洞   {fmt(zone, g.start)} .. {fmt(zone, g.end)}"
                      f"  原因={g.reason}  {g.detail}")

    print("=" * 72)
    print("三、v2 相对 v1 的差异审计（扩大时段附最少支撑记录）")
    print("=" * 72)
    diffs = audit_versions(engine, *VERSIONS)
    any_change = False
    for group, gd in sorted(diffs.items()):
        if gd.horizon is None or not gd.has_change:
            continue
        any_change = True
        print(f"  组 {group}（空区间版本: {gd.empty_in or '无'}）")
        for w in gd.widened:
            print(f"    扩大 {fmt(zone, w.start)} .. {fmt(zone, w.end)}"
                  f"  支撑={list(w.support)}  必要性校验={w.support_necessary}")
        for n in gd.narrowed:
            print(f"    收窄 {fmt(zone, n.start)} .. {fmt(zone, n.end)}")
    if not any_change:
        print("  两版完全一致，无差异。")

    print("=" * 72)
    print("四、独立身份代穷举（联合覆盖合法性）")
    print("=" * 72)
    print(f"  身份代数: {len(report['identity_generations'])}")
    print(f"  独立 root 对（联合覆盖成立）: "
          f"{sum(1 for p in report['independent_pairs'] if p['joint_ok'])}")
    print(f"  同 root/失效对（联合覆盖不成立）: {len(report['same_root_pairs'])}")

    print("=" * 72)
    print("五、完整重放证据哈希（旧版证据一致性）")
    print("=" * 72)
    manifest = build_manifest(loaded, VERSIONS)
    print(f"  输入指纹 sha256: {manifest['input_fingerprint']}")
    for ver in VERSIONS:
        snap = manifest["snapshots"][ver]
        digest = sha256_blob(_canonical(snap))
        print(f"  {ver} 快照 sha256: {digest}  扫描条数={snap['groups'] and sum(g['scanned'] for g in snap['groups'].values())}")
    print(f"  清单总 sha256: {manifest['manifest_sha256']}")
    # 证据清单落盘（artifacts/ 已被 .gitignore 忽略），供离线复核。
    artifacts = Path(__file__).parent / "artifacts"
    artifacts.mkdir(exist_ok=True)
    manifest_path = artifacts / "evidence_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  证据清单已写入: {manifest_path.relative_to(Path(__file__).parent)}")
    # 再跑一次完整重放，旧版证据必须逐字节一致。
    again = build_manifest(load_input(json.loads(DATA.read_text(encoding="utf-8"))), VERSIONS)
    for ver in VERSIONS:
        h1 = sha256_blob(_canonical(manifest["snapshots"][ver]))
        h2 = sha256_blob(_canonical(again["snapshots"][ver]))
        assert h1 == h2, f"{ver} 重放证据不一致"
    print("  二次完整重放：两版快照哈希全部一致 ✔")

    if with_big:
        print("=" * 72)
        print("六、六万条记录只改一个组：实测扫描条数")
        print("=" * 72)
        from audit.bigdata import run_bigdata_experiment

        r = run_bigdata_experiment()
        print(f"  每版记录总数: {r.total_per_version}（业务记录 "
              f"{r.business_records_per_version} + 身份锚点 2）")
        print(f"  全量重放扫描: v1={r.full_scan_v1}  v2={r.full_scan_v2}")
        print(f"  摘要预筛命中变更组: {r.changed_groups}")
        print(f"  组索引重放扫描: 合计 {r.indexed_scan_total}，"
              f"每版 {r.indexed_scan_per_version}（仅变更组，"
              f"组内 {r.target_group_size} 条）")
        print(f"  实测耗时: {r.elapsed_seconds:.2f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(with_big="--big" in sys.argv[1:]))
