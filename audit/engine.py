"""重放引擎：把一版离线名单重放成可验证片段与空洞。

半开整数分钟区间语义：区间 ``[start, end)`` 覆盖 ``end-start`` 个分钟点。
引擎只做整数分钟运算，跨平台且确定性。

扫描计数
========
引擎对“读取过的记录”逐条计数（``scan_count``）：

* 全量重放读取该版本全部记录；
* 按组索引重放只读取该组记录——六万条里只改一个组时，索引路径的
  扫描条数远小于全量路径，实测数字由 bigdata 模块产出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .model import AliasBook, LoadedInput, Record

SOLO = 1
JOINT = 2


@dataclass(frozen=True)
class Fragment:
    """一段连续可验证覆盖（UTC 半开区间）+ 最少支撑记录。"""

    start: int
    end: int
    support: tuple[str, ...]
    required_signers: int  # SOLO=1 / JOINT=2

    @property
    def is_empty(self) -> bool:
        return self.start >= self.end


@dataclass(frozen=True)
class Gap:
    """请求范围内无法验证覆盖的空洞及原因。"""

    start: int
    end: int
    reason: str  # deny / revoke / reroot / unsigned / no_grant
    detail: str


@dataclass
class GroupReplay:
    version: str
    group: str
    horizon: tuple[int, int] | None
    fragments: list[Fragment] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    scanned: int = 0

    @property
    def empty(self) -> bool:
        """请求范围非空但一个可验证片段都没有。"""
        return bool(self.horizon and self.horizon[0] < self.horizon[1]) and not self.fragments


class ReplayEngine:
    def __init__(self, loaded: LoadedInput):
        self.loaded = loaded
        self.zone = loaded.zone
        self.scan_count = 0
        self._index: dict[tuple[str, str], list[Record]] = {}
        self._books: dict[str, AliasBook] = {}
        for rec in loaded.records:
            self._index.setdefault((rec.version, rec.group), []).append(rec)
            if rec.kind in ("anchor", "re_root"):
                self._book(rec.version).install(rec)

    # ---- 身份代簿记 -----------------------------------------------------

    def _book(self, version: str) -> AliasBook:
        book = self._books.get(version)
        if book is None:
            book = AliasBook()
            self._books[version] = book
        return book

    def book(self, version: str) -> AliasBook:
        return self._books[version]

    def _count(self, records: list[Record]) -> None:
        self.scan_count += len(records)

    # ---- 签名有效性 -----------------------------------------------------

    def sig_valid(self, book: AliasBook, sig, at: int) -> bool:
        """签名在时刻 at 是否有效：别名该身份代仍挂载在某个 root 下。"""
        return book.root_at(sig.alias, sig.generation, at) is not None

    def sig_root(self, book: AliasBook, sig, at: int) -> str | None:
        return book.root_at(sig.alias, sig.generation, at)

    def independent_roots(self, book: AliasBook, signers, at: int) -> tuple[str, ...]:
        """返回时刻 at 各有效签名挂载的去重 root 元组（按首次出现排序）。"""
        roots: list[str] = []
        for sig in signers:
            root = self.sig_root(book, sig, at)
            if root is not None and root not in roots:
                roots.append(root)
        return tuple(roots)

    def grant_strength(self, book: AliasBook, rec: Record, at: int) -> int:
        """grant 在 at 的覆盖强度：0 不覆盖 / 1 单人 / 2 双人独立联合。"""
        roots = self.independent_roots(book, rec.signers, at)
        if len(rec.signers) == 1:
            return SOLO if roots else 0
        # 两个（或更多）签署人必须来自两个实际独立的 root。
        return JOINT if len(roots) >= 2 else 0

    # ---- 撤销表 ---------------------------------------------------------

    def _revoke_map(self, version: str, records: list[Record]) -> dict[str, int]:
        """target grant id -> 撤销生效 UTC 分钟（仅保留签署有效、最早的一条）。"""
        book = self._books[version]
        out: dict[str, int] = {}
        for rec in records:
            if rec.kind != "revoke":
                continue
            if all(self.sig_valid(book, sig, rec.at) for sig in rec.signers):
                old = out.get(rec.target)
                if old is None or rec.at < old:
                    out[rec.target] = rec.at
        return out

    # ---- 单组重放 -------------------------------------------------------

    def replay_group(
        self,
        version: str,
        group: str,
        horizon: tuple[int, int] | None = None,
    ) -> GroupReplay:
        records = self._index.get((version, group), [])
        self._count(records)
        book = self._books[version]
        windows = [r for r in records if r.kind in ("grant", "deny")]
        if horizon is None:
            if not windows:
                return GroupReplay(version, group, None, scanned=len(records))
            horizon = (
                min(r.start for r in windows),
                max(r.end for r in windows),
            )
        h0, h1 = horizon
        result = GroupReplay(version, group, (h0, h1), scanned=len(records))
        if h0 >= h1:
            return result

        grants = [r for r in windows if r.kind == "grant"]
        denies = [r for r in windows if r.kind == "deny"]
        revokes = self._revoke_map(version, records)
        reroot_ats = [
            ev[0]
            for alias in book.aliases()
            for ev in book.alias_chain(alias)[1:]
        ]

        # 所有状态可能改变的分钟点：窗口端点、撤销生效点、换根点。
        breaks = {h0, h1}
        for rec in windows:
            if rec.end > h0 and rec.start < h1:
                breaks.add(max(h0, rec.start))
                breaks.add(min(h1, rec.end))
        for at in list(revokes.values()) + reroot_ats:
            if h0 < at < h1:
                breaks.add(at)
        cuts = sorted(breaks)

        # 每段状态恒定，中点评估即可。
        piece_active: list[tuple[int, int, list[Record]]] = []
        piece_gap: list[tuple[int, int, Gap]] = []
        for s, e in zip(cuts, cuts[1:]):
            if s >= e:
                continue
            m = s + (e - s) // 2
            deniers = [
                d for d in denies
                if d.start <= m < d.end
                and all(self.sig_valid(book, sig, m) for sig in d.signers)
            ]
            active: list[Record] = []
            window_grants = [g for g in grants if g.start <= m < g.end]
            for g in window_grants:
                strength = self.grant_strength(book, g, m)
                if strength == 0:
                    continue
                killed_at = revokes.get(g.record_id)
                if killed_at is not None and m >= killed_at:
                    continue
                active.append(g)
            if active and not deniers:
                piece_active.append((s, e, active))
            else:
                piece_gap.append((s, e, self._gap_reason(
                    s, e, deniers, window_grants, revokes, book, m,
                )))

        # 相邻覆盖段合并为最大可验证片段；记录并集保留，最少支撑在发射时重算。
        if piece_active:
            cur_s, cur_e, cur_active = piece_active[0]
            for s, e, active in piece_active[1:]:
                if s == cur_e:
                    cur_e = e
                    for g in active:
                        if g not in cur_active:
                            cur_active.append(g)
                    continue
                self._emit_fragment(result, cur_s, cur_e, cur_active)
                cur_s, cur_e, cur_active = s, e, active
            self._emit_fragment(result, cur_s, cur_e, cur_active)

        result.gaps = [gap for _, _, gap in sorted(
            piece_gap, key=lambda x: x[0]
        )]
        return result

    def _gap_reason(self, s, e, deniers, window_grants, revokes, book, m) -> Gap:
        if deniers:
            return Gap(s, e, "deny",
                       "显式拒绝: " + ",".join(d.record_id for d in deniers))
        if not window_grants:
            return Gap(s, e, "no_grant", "请求范围内无任何 grant 覆盖该分钟")
        revoked = [g for g in window_grants
                   if revokes.get(g.record_id) is not None
                   and m >= revokes[g.record_id]
                   and self.grant_strength(book, g, m) > 0]
        if revoked and all(
            self.grant_strength(book, g, m) == 0
            or (revokes.get(g.record_id) is not None and m >= revokes[g.record_id])
            for g in window_grants
        ):
            return Gap(s, e, "revoke",
                       "晚到撤销: " + ",".join(g.record_id for g in revoked))
        rerooted = [
            g for g in window_grants
            if any(
                book.root_at(sig.alias, sig.generation, m) is None
                and book.alias_chain(sig.alias)
                for sig in g.signers
            )
        ]
        if rerooted:
            return Gap(s, e, "reroot",
                       "别名换根致旧签名失效: "
                       + ",".join(g.record_id for g in rerooted))
        return Gap(s, e, "unsigned", "签署身份代无法验证")

    def _emit_fragment(self, result: GroupReplay, s: int, e: int,
                       active: list[Record]) -> None:
        # active 已在合并时取交集：这些 grant 贯穿整个 [s, e)。
        support = self._minimal_support(s, e, active)
        required = JOINT if any(len(r.signers) >= 2 for r in active
                                if r.record_id in support) else SOLO
        result.fragments.append(Fragment(s, e, support, required))

    @staticmethod
    def _minimal_support(s: int, e: int, active: list[Record]) -> tuple[str, ...]:
        """最少支撑记录：按记录数升序、id 字典序穷举，首个全覆盖组合即最少。

        组合内区间并集恰好铺满 [s, e) 才入选，因此重叠 grant 中多余的
        记录不会进入支撑集；空候选返回空元组（调用方不会遇到）。
        """
        items = sorted(active, key=lambda r: r.record_id)
        for k in range(1, len(items) + 1):
            for combo in combinations(items, k):
                if _union_covers(combo, s, e):
                    return tuple(r.record_id for r in combo)
        return tuple(r.record_id for r in items)

    # ---- 整版重放 -------------------------------------------------------

    def replay_version(self, version: str) -> dict[str, GroupReplay]:
        groups = sorted(group for (ver, group) in self._index if ver == version)
        return {group: self.replay_group(version, group) for group in groups}


def _union_covers(records, s: int, e: int) -> bool:
    """若干半开区间的并集是否连续铺满 [s, e)。"""
    intervals = sorted((max(r.start, s), min(r.end, e)) for r in records)
    cursor = s
    for a, b in intervals:
        if a > cursor:
            return False
        if b > cursor:
            cursor = b
        if cursor >= e:
            return True
    return cursor >= e
