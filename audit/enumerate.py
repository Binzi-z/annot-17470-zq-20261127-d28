"""独立穷举：小整数分钟 × 所有合法身份代 × 两种 fold 映射。

不依赖真实数据分布，直接对规则表做数学穷举，保证：

1. 春前跳日与秋回拨日窗口内每个本地分钟的合法 fold 集合都被访问
   （春令时缺口 0 个、普通分钟 1 个、回拨重复分钟 2 个）；
2. 回拨重复分钟上 fold=0 / fold=1 给出**两个不同的 UTC 映射**；
3. 给定身份代集合，所有“合法独立身份代对”（不同 root）与
   “同 root 非独立对”都被枚举并分别判定联合覆盖是否成立。

“小整数分钟”指相对窗口起点的 0..span-1 连续整数分钟。
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import ReplayEngine
from .model import LoadedInput, Sig
from .zones import SyntheticZone


@dataclass(frozen=True)
class MinuteCase:
    local_minute: int
    folds: tuple[int, ...]
    utc_by_fold: tuple[int | None, ...]
    ambiguous: bool
    window: str


def enumerate_minutes(
    zone: SyntheticZone, local_start: int, span: int, window: str = ""
) -> list[MinuteCase]:
    """从本地挂钟分钟 local_start 起穷举 span 个连续小整数分钟。"""
    cases: list[MinuteCase] = []
    for offset in range(span):
        local = local_start + offset
        folds = tuple(zone.valid_folds(local))
        utc = (
            zone.local_to_utc(local, 0),
            zone.local_to_utc(local, 1),
        )
        ambiguous = (
            utc[0] is not None and utc[1] is not None and utc[0] != utc[1]
        )
        # 双向回验：每个合法 fold 转回本地必须等于原分钟。
        for f in folds:
            assert zone.utc_to_local(utc[f]) == local
        cases.append(MinuteCase(local, folds, utc, ambiguous, window))
    return cases


def transition_windows(zone: SyntheticZone) -> list[tuple[str, int, int]]:
    """返回 (标签, 本地窗口起点, 跨度)：覆盖规则表每个切换日的 24 本地小时。"""
    return [
        ("spring-forward", zone.parse_local("2026-03-29T00:00"), 24 * 60),
        ("fall-back", zone.parse_local("2026-11-01T00:00"), 24 * 60),
    ]


def enumerate_identity_pairs(loaded: LoadedInput, version: str) -> dict:
    """枚举某版本所有合法身份代的两两组合，按 root 独立性分类。

    每个身份代取为 ``(alias, generation)``；在该代有效区间内的代表时刻
    （anchor 时刻本身）评估 root 归属。
    """
    engine = ReplayEngine(loaded)
    book = engine.book(version)
    members: list[tuple[str, int, str, int]] = []  # alias, gen, root, at
    for alias in book.aliases():
        for at, gen, root in book.alias_chain(alias):
            members.append((alias, gen, root, at))

    independent: list[dict] = []
    same_root: list[dict] = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, ga, ra, at_a = members[i]
            b, gb, rb, at_b = members[j]
            at = max(at_a, at_b)
            pair = {
                "a": {"alias": a, "generation": ga},
                "b": {"alias": b, "generation": gb},
            }
            # 独立性按较晚时刻重新评估（旧代可能已被 re_root 换掉）。
            root_a = book.root_at(a, ga, at)
            root_b = book.root_at(b, gb, at)
            if root_a is not None and root_b is not None and root_a != root_b:
                sigs = (Sig(a, ga), Sig(b, gb))
                pair["joint_ok"] = len(engine.independent_roots(book, sigs, at)) == 2
                independent.append(pair)
            else:
                pair["joint_ok"] = False
                same_root.append(pair)
    return {
        "members": [
            {"alias": a, "generation": g, "root": r} for a, g, r, _ in members
        ],
        "independent_pairs": independent,
        "same_root_pairs": same_root,
    }


def exhaustive_report(loaded: LoadedInput, version: str) -> dict:
    """题面要求的完整穷举报告（春前跳日 + 秋回拨日两个窗口）。"""
    zone = loaded.zone
    all_cases: list[MinuteCase] = []
    window_population: dict[str, dict[int, int]] = {}
    for label, start, span in transition_windows(zone):
        cases = enumerate_minutes(zone, start, span, label)
        all_cases.extend(cases)
        window_population[label] = {}
        for case in cases:
            n = len(case.folds)
            window_population[label][n] = window_population[label].get(n, 0) + 1

    fold_counts: dict[int, int] = {}
    double_mappings: list[dict] = []
    for case in all_cases:
        fold_counts[len(case.folds)] = fold_counts.get(len(case.folds), 0) + 1
        if case.ambiguous:
            double_mappings.append({
                "window": case.window,
                "local_minute": case.local_minute,
                "local_text": zone.fmt_local(case.local_minute),
                "fold0_utc": case.utc_by_fold[0],
                "fold1_utc": case.utc_by_fold[1],
                "utc_minute_gap": case.utc_by_fold[1] - case.utc_by_fold[0],
            })

    # 两类异常分钟都必须真实命中，否则规则表或穷举窗口配置有误。
    if not any(len(c.folds) == 0 for c in all_cases):
        raise AssertionError("未命中春令时前跳缺口分钟")
    if not double_mappings:
        raise AssertionError("未命中回拨重复本地分钟")

    pairs = enumerate_identity_pairs(loaded, version)
    return {
        "minutes_enumerated": len(all_cases),
        "fold_population": {str(k): v for k, v in sorted(fold_counts.items())},
        "window_fold_population": {
            label: {str(k): v for k, v in sorted(pop.items())}
            for label, pop in sorted(window_population.items())
        },
        "ambiguous_minute_count": len(double_mappings),
        "double_utc_mappings": double_mappings,
        "identity_generations": pairs["members"],
        "independent_pairs": pairs["independent_pairs"],
        "same_root_pairs": pairs["same_root_pairs"],
    }
