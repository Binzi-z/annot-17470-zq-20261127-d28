"""固定的合成时间规则表与 fold 双映射。

题面要求“时区转换使用输入中固定的合成时间规则表”，因此本模块**不依赖**
zoneinfo/tzdata（Windows 上没有系统 tzdata），只按输入 JSON 里给出的
transition 表做纯整数分钟计算。

规则表形状（见 data/synthetic_input.json 的 "zone" 字段）::

    {
      "name": "合成夏令时区",
      "origin": "2026-01-01T00:00:00Z",
      "rules": [
        {"at_utc": "2026-03-29T01:00:00Z", "offset_minutes": 60},
        {"at_utc": "2026-11-01T01:00:00Z", "offset_minutes": 0}
      ]
    }

回拨日 01:00Z 处偏移从 +60 变回 +0，本地时钟 02:00 回拨到 01:00，
本地小时 [01:00, 02:00) 出现两次：fold=0 落在旧偏移 +60，fold=1 落在
新偏移 +0，即“同一种本地时间的两种 UTC 映射”。

所有时间戳统一用“相对 origin 的整数分钟”表示，避免浮点与平台差异。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def parse_iso_minutes(text: str, origin: datetime) -> int:
    """把 ISO-8601 文本解析为相对 origin 的整数分钟。

    接受 ``...Z``（UTC）或带偏移的文本；naive 文本按 UTC 处理。
    """
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - origin
    return int(delta.total_seconds() // 60)


def format_iso_minutes(minutes: int, origin: datetime) -> str:
    """整数分钟还原成 ``YYYY-MM-DDTHH:MM:SSZ`` 文本。"""
    dt = origin + _minutes_timedelta(minutes)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_timedelta(minutes: int):
    from datetime import timedelta

    return timedelta(minutes=minutes)


@dataclass(frozen=True)
class Transition:
    """一条偏移切换规则：at_utc（相对 origin 分钟）起偏移 offset_minutes。"""

    at_utc: int
    offset_minutes: int


class SyntheticZone:
    """由输入规则表构造的合成时区，全部运算基于整数分钟。"""

    def __init__(self, table: dict):
        self.name: str = table["name"]
        origin_text = table["origin"]
        origin = datetime.fromisoformat(
            origin_text.replace("Z", "+00:00")
        )
        if origin.tzinfo is None:
            origin = origin.replace(tzinfo=timezone.utc)
        self.origin = origin.astimezone(timezone.utc)
        rules = sorted(
            (
                Transition(
                    at_utc=parse_iso_minutes(rule["at_utc"], self.origin),
                    offset_minutes=int(rule["offset_minutes"]),
                )
                for rule in table["rules"]
            ),
            key=lambda tr: tr.at_utc,
        )
        if not rules:
            raise ValueError("合成时间规则表至少需要一条 rules")
        if any(rules[i].at_utc == rules[i - 1].at_utc for i in range(1, len(rules))):
            raise ValueError("rules 中存在重复的 at_utc 切换点")
        # 首条规则之前的“默认偏移”：显式给出，否则取首条规则的偏移。
        self.initial_offset = int(
            table.get("initial_offset_minutes", rules[0].offset_minutes)
        )
        self._rules = rules
        self._starts = [tr.at_utc for tr in rules]
        self._offsets = [tr.offset_minutes for tr in rules]

    # ---- 基础查询 -------------------------------------------------------

    def offset_at(self, utc_minutes: int) -> int:
        """给定 UTC 整数分钟，返回当时生效的 UTC 偏移（分钟）。"""
        # bisect_right：切换点当刻即采用新偏移。
        from bisect import bisect_right

        idx = bisect_right(self._starts, utc_minutes) - 1
        if idx < 0:
            return self.initial_offset
        return self._offsets[idx]

    def utc_to_local(self, utc_minutes: int) -> int:
        """UTC 分钟 -> 本地“挂钟分钟”（相对 origin 的分钟数）。"""
        return utc_minutes + self.offset_at(utc_minutes)

    def _inverse_mappings(self, local_minutes: int) -> list[int]:
        """本地挂钟分钟的全部 UTC 映射（0 个=缺口，1 个=普通，2 个=回拨）。

        逐偏移段反解候选 UTC，再要求该 UTC 落在对应偏移段的生效区间内，
        春令时前跳缺口因此不会被相邻段错误“兜底”。
        """
        out: list[int] = []
        # 首条切换点之前的前缀段（负无穷 .. rules[0].at_utc）。
        first = self._rules[0]
        prefix_candidate = local_minutes - self.initial_offset
        if prefix_candidate < first.at_utc:
            out.append(prefix_candidate)
        # 各规则段 [rules[i].at_utc, rules[i+1].at_utc)，末段延伸到正无穷。
        for i, tr in enumerate(self._rules):
            seg_start = tr.at_utc
            seg_end = self._rules[i + 1].at_utc if i + 1 < len(self._rules) else None
            candidate = local_minutes - tr.offset_minutes
            if candidate < seg_start:
                continue
            if seg_end is not None and candidate >= seg_end:
                continue
            out.append(candidate)
        return sorted(set(out))

    def local_to_utc(self, local_minutes: int, fold: int) -> int | None:
        """本地挂钟分钟 + fold -> UTC 分钟；该本地时间不合法时返回 None。

        * 普通分钟只有一个映射，fold 不影响结果（PEP 495 语义）；
        * fold=0：回拨重复分钟取第一次（旧偏移）；
        * fold=1：取第二次（新偏移）；
        * 春令时前跳缺口（任何 fold 都不存在）返回 None。
        """
        if fold not in (0, 1):
            raise ValueError("fold 只能是 0 或 1")
        mappings = self._inverse_mappings(local_minutes)
        if not mappings:
            return None
        if len(mappings) == 1:
            return mappings[0]
        return mappings[fold]

    def valid_folds(self, local_minutes: int) -> list[int]:
        """返回该本地时间合法的 fold 列表（0 个、1 个或 2 个）。"""
        n = len(self._inverse_mappings(local_minutes))
        return list(range(n))

    def forward_gap_local_intervals(self) -> list[tuple[int, int]]:
        """春令时前跳造成的本地缺口区间（本地挂钟半开分钟区间）。

        偏移由 old 跳到 new（new>old）时，本地小时
        ``[at_utc+old, at_utc+new)`` 不存在；回拨只产生重复，不产生缺口。
        """
        gaps: list[tuple[int, int]] = []
        prev = self.initial_offset
        for tr in self._rules:
            if tr.offset_minutes > prev:
                gaps.append((tr.at_utc + prev, tr.at_utc + tr.offset_minutes))
            prev = tr.offset_minutes
        return gaps

    def is_ambiguous(self, local_minutes: int) -> bool:
        """该本地挂钟时间是否对应两个不同的 UTC 瞬间（回拨重复）。"""
        first = self.local_to_utc(local_minutes, 0)
        second = self.local_to_utc(local_minutes, 1)
        return first is not None and second is not None and first != second

    # ---- 文本辅助 -------------------------------------------------------

    def parse_utc(self, text: str) -> int:
        return parse_iso_minutes(text, self.origin)

    def parse_local(self, text: str) -> int:
        """解析 naive 本地时间文本（不带 Z/偏移）为本地挂钟分钟。"""
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            raise ValueError(f"本地时间不得带时区偏移: {text}")
        delta = dt.replace(tzinfo=timezone.utc) - self.origin
        return int(delta.total_seconds() // 60)

    def fmt_utc(self, minutes: int) -> str:
        return format_iso_minutes(minutes, self.origin)

    def fmt_local(self, local_minutes: int) -> str:
        dt = self.origin + _minutes_timedelta(local_minutes)
        return dt.strftime("%Y-%m-%dT%H:%M")
