"""两版名单的差异审计与规范化证据哈希。

差异以 UTC 半开整数分钟区间表达：

* ``widened``  —— v2 可验证但 v1 不可验证的扩大时段，逐段附 v2 的
  最少支撑记录（支撑集在该段上做必要性验证，删掉任一条都会漏分钟）；
* ``narrowed`` —— v1 可验证但 v2 不可验证的收窄时段；
* 重叠与“可能为空的区间”一律保留：没有扩大也输出空列表，
  组在一版完全无覆盖时单独标注 ``empty_in``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from itertools import combinations

from .engine import ReplayEngine, _union_covers
from .model import LoadedInput


def subtract(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """半开区间 a - b，最多返回两段；不相交时原样返回。"""
    a0, a1 = a
    b0, b1 = b
    if b1 <= a0 or b0 >= a1:
        return [(a0, a1)] if a0 < a1 else []
    left = (a0, min(a1, b0))
    right = (max(a0, b1), a1)
    return [seg for seg in (left, right) if seg[0] < seg[1]]


def subtract_many(
    target: tuple[int, int], covers: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """target 减去 covers 中所有区间，返回残余分段（可能为空列表）。"""
    pieces = [target] if target[0] < target[1] else []
    for cov in sorted(covers):
        nxt: list[tuple[int, int]] = []
        for piece in pieces:
            nxt.extend(subtract(piece, cov))
        pieces = nxt
    return pieces


@dataclass
class WidenedPiece:
    group: str
    start: int
    end: int
    support: tuple[str, ...]
    support_necessary: bool


@dataclass
class NarrowedPiece:
    group: str
    start: int
    end: int


@dataclass
class GroupDiff:
    group: str
    horizon: tuple[int, int] | None
    widened: list[WidenedPiece] = field(default_factory=list)
    narrowed: list[NarrowedPiece] = field(default_factory=list)
    empty_in: list[str] = field(default_factory=list)

    @property
    def has_change(self) -> bool:
        return bool(self.widened or self.narrowed or self.empty_in)


def _covered_intervals(replay) -> list[tuple[int, int]]:
    return [(f.start, f.end) for f in replay.fragments if f.start < f.end]


def audit_versions(
    engine: ReplayEngine, v1: str, v2: str
) -> dict[str, GroupDiff]:
    """对两版名单逐组做差异审计（全量路径，扫描所有组）。"""
    groups = sorted({g for (ver, g) in engine._index if ver in (v1, v2)})
    return {group: _audit_one_group(engine, v1, v2, group) for group in groups}


def group_record_digest(engine: ReplayEngine, version: str, group: str) -> str:
    """组内记录实质字段的规范化摘要（剔除版本命名空间），用于预筛变更组。

    id 与 version 只用于命名空间，不参与实质比较——两版同一条记录
    即便 id 带 v1-/v2- 前缀，其实质字段相同即视为未变。
    """
    norm = []
    for r in engine._index.get((version, group), []):
        item = {k: v for k, v in r.raw.items() if k not in ("id", "version")}
        norm.append(item)
    return sha256_blob(_canonical(norm))


def changed_groups(engine: ReplayEngine, v1: str, v2: str) -> tuple[str, ...]:
    """只比对组级记录摘要，返回两版间有记录差异的组（不触发重放扫描）。"""
    groups = sorted({g for (ver, g) in engine._index if ver in (v1, v2)})
    return tuple(
        g for g in groups
        if group_record_digest(engine, v1, g)
        != group_record_digest(engine, v2, g)
    )


def targeted_audit(
    engine: ReplayEngine, v1: str, v2: str, groups: tuple[str, ...]
) -> dict[str, GroupDiff]:
    """索引路径：只重放指定的变更组，扫描条数与总组数解耦。"""
    return {g: _audit_one_group(engine, v1, v2, g) for g in groups}


def _audit_one_group(
    engine: ReplayEngine, v1: str, v2: str, group: str
) -> GroupDiff:
    records_by_id = {r.record_id: r for r in engine.loaded.records}
    r1 = engine.replay_group(v1, group)
    r2 = engine.replay_group(v2, group)
    horizons = [h for h in (r1.horizon, r2.horizon) if h is not None]
    horizon = (
        (min(h[0] for h in horizons), max(h[1] for h in horizons))
        if horizons
        else None
    )
    gd = GroupDiff(group, horizon)
    if horizon is None:
        return gd
    cov1 = _covered_intervals(r1)
    cov2 = _covered_intervals(r2)

    # 扩大时段：v2 每个可验证片段减去 v1 覆盖，保留重叠切分边界。
    for frag in r2.fragments:
        for s, e in subtract_many((frag.start, frag.end), cov1):
            support = _minimal_support_for(
                records_by_id, (s, e),
                [f for f in r2.fragments if f.start < e and f.end > s],
            )
            necessary = all(
                not _support_covers(
                    records_by_id,
                    tuple(sid for sid in support if sid != drop),
                    (s, e),
                )
                for drop in support
            )
            gd.widened.append(
                WidenedPiece(group, s, e, support, necessary)
            )

    # 收窄时段：v1 每个可验证片段减去 v2 覆盖。
    for frag in r1.fragments:
        for s, e in subtract_many((frag.start, frag.end), cov2):
            gd.narrowed.append(NarrowedPiece(group, s, e))

    if r1.empty:
        gd.empty_in.append(v1)
    if r2.empty:
        gd.empty_in.append(v2)
    return gd


def _support_covers(records_by_id, support_ids, piece: tuple[int, int]) -> bool:
    recs = [records_by_id[sid] for sid in support_ids if sid in records_by_id]
    return bool(recs) and _union_covers(recs, piece[0], piece[1])


def _minimal_support_for(
    records_by_id,
    piece: tuple[int, int],
    hit_fragments,
) -> tuple[str, ...]:
    """扩大段的最少支撑：候选 = 与该段相交的 v2 grant，按规模升序穷举。"""
    s, e = piece
    candidate_ids = sorted({
        sid for frag in hit_fragments for sid in frag.support
    })
    candidates = [records_by_id[sid] for sid in candidate_ids]
    for k in range(1, len(candidates) + 1):
        for combo in combinations(candidates, k):
            if _union_covers(combo, s, e):
                return tuple(r.record_id for r in combo)
    return tuple(candidate_ids)


# ---- 规范化证据与完整重放 --------------------------------------------------

def _canonical(obj) -> bytes:
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def sha256_blob(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def input_fingerprint(loaded: LoadedInput) -> str:
    return sha256_blob(_canonical(loaded.raw))


def replay_snapshot(engine: ReplayEngine, version: str) -> dict:
    """一版名单完整重放的确定性快照（证据载荷）。"""
    replays = engine.replay_version(version)
    return {
        "version": version,
        "groups": {
            group: {
                "horizon": rep.horizon,
                "fragments": [
                    {
                        "start": f.start, "end": f.end,
                        "support": list(f.support),
                        "required_signers": f.required_signers,
                    }
                    for f in rep.fragments
                ],
                "gaps": [
                    {"start": g.start, "end": g.end,
                     "reason": g.reason, "detail": g.detail}
                    for g in rep.gaps
                ],
                "empty": rep.empty,
                "scanned": rep.scanned,
            }
            for group, rep in sorted(replays.items())
        },
    }


def build_manifest(loaded: LoadedInput, versions: tuple[str, ...]) -> dict:
    """对输入做完整重放并产出带 sha256 的证据清单。"""
    engine = ReplayEngine(loaded)
    snapshots = {v: replay_snapshot(engine, v) for v in versions}
    payload = {
        "input_fingerprint": input_fingerprint(loaded),
        "snapshots": snapshots,
    }
    payload["manifest_sha256"] = sha256_blob(_canonical(payload))
    payload["scan_count"] = engine.scan_count
    return payload
