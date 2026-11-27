"""记录模型、别名身份代簿记与输入装载。

身份模型
========
每个签署人是一个 *别名(alias)*，别名在任一时刻挂在一个 *根身份(root)* 下。
``anchor`` 建立身份代（generation），``re_root`` 让别名换根并开启新一代：

* 同一别名的 anchor/re_root 按时间排序，第 g 代（g 从 0 开始）在
  ``[events[g].at, events[g+1].at)`` 内有效；
* 换根之后，旧一代别名签署的所有记录立即失效（“管理别名换根”）；
* 两个签署人只有在签署时刻挂在**不同 root** 下才算“实际独立”，
  同 root 的两个别名联合签署不构成双人覆盖。

记录类型
========
* ``grant``  —— 授予时段，signers 1 人即可单人覆盖，恰 2 个独立签署人
  可联合覆盖；少于要求或不独立则该记录不产生任何覆盖；
* ``deny``   —— 显式拒绝，在其窗口内挖出空洞（签署本身仍须有效）；
* ``revoke`` —— 晚到撤销，按 record_id 点名撤销一条 grant，自其生效时刻
  起切断该 grant 的覆盖（撤销记录自己的签署也要有效）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .zones import SyntheticZone

WINDOW_KINDS = ("grant", "deny")
EVENT_KINDS = ("anchor", "re_root", "revoke")
ALL_KINDS = WINDOW_KINDS + EVENT_KINDS


@dataclass(frozen=True)
class Sig:
    """一条签名：别名 + 签署时自称的身份代。"""

    alias: str
    generation: int

    @classmethod
    def from_dict(cls, raw: dict) -> "Sig":
        return cls(alias=str(raw["alias"]), generation=int(raw["generation"]))

    def to_dict(self) -> dict:
        return {"alias": self.alias, "generation": self.generation}


@dataclass(frozen=True)
class Record:
    """一条离线名单记录，时间一律存为相对 origin 的 UTC 整数分钟。"""

    record_id: str
    version: str
    group: str
    kind: str
    start: int | None = None        # grant/deny 的 UTC 半开区间起
    end: int | None = None          # grant/deny 的 UTC 半开区间止
    signers: tuple[Sig, ...] = ()
    at: int | None = None           # anchor/re_root/revoke 的生效 UTC 分钟
    root: str | None = None         # anchor/re_root 的根身份
    target: str | None = None       # revoke 点名的 grant record_id
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def window(self) -> tuple[int, int] | None:
        if self.start is None or self.end is None:
            return None
        return self.start, self.end


class AliasBook:
    """一版名单的别名身份代簿记（anchor / re_root）。"""

    def __init__(self) -> None:
        # alias -> 按生效时间排序的 (at, generation, root) 列表
        self._gens: dict[str, list[tuple[int, int, str]]] = {}

    def install(self, record: Record) -> None:
        if record.kind not in ("anchor", "re_root"):
            raise ValueError(f"install 只接受 anchor/re_root，收到 {record.kind}")
        chain = self._gens.setdefault(record.alias_chain_key(), [])
        expected_gen = len(chain)
        sig_gen = record.signers[0].generation if record.signers else expected_gen
        if sig_gen != expected_gen:
            raise ValueError(
                f"{record.record_id}: 别名 {record.alias_chain_key()} 身份代必须连续，"
                f"期望第 {expected_gen} 代，记录自称第 {sig_gen} 代"
            )
        if chain and record.at <= chain[-1][0]:
            raise ValueError(f"{record.record_id}: re_root 必须严格晚于上一事件")
        chain.append((record.at, expected_gen, record.root))

    def alias_chain(self, alias: str) -> tuple[tuple[int, int, str], ...]:
        return tuple(self._gens.get(alias, ()))

    def root_at(self, alias: str, generation: int, at: int) -> str | None:
        """别名在第 generation 代、时刻 at 挂载的 root；无效代/已换根返回 None。"""
        chain = self._gens.get(alias)
        if not chain or generation < 0 or generation >= len(chain):
            return None
        gen_at, _, root = chain[generation]
        next_at = chain[generation + 1][0] if generation + 1 < len(chain) else None
        if at < gen_at:
            return None  # 该代尚未建立
        if next_at is not None and at >= next_at:
            return None  # 已被 re_root 换根，旧一代签名失效
        return root

    def aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self._gens))


# Record 上取链键的小工具（anchor/re_root 的签名别名即被管理的别名）。
def _alias_chain_key(self: Record) -> str:
    if self.kind in ("anchor", "re_root"):
        if not self.signers:
            raise ValueError(f"{self.record_id}: anchor/re_root 必须带签名别名")
        return self.signers[0].alias
    raise ValueError("只有 anchor/re_root 记录有别名链键")


Record.alias_chain_key = _alias_chain_key  # type: ignore[attr-defined]


@dataclass
class LoadedInput:
    zone: SyntheticZone
    records: tuple[Record, ...]
    raw: dict


def load_input(payload: dict) -> LoadedInput:
    """装载并校验一份合成输入，时间文本全部转换为 UTC 整数分钟。

    grant/deny 的窗口用本地挂钟时间 + fold 表达（回拨日两版名单
    “看起来一样”即本地文本相同、fold 不同）；anchor/re_root/revoke
    的 ``at`` 直接给 UTC 文本。
    """
    zone = SyntheticZone(payload["zone"])
    records: list[Record] = []
    seen_ids: set[str] = set()

    for raw in payload["records"]:
        rid = str(raw["id"])
        if rid in seen_ids:
            raise ValueError(f"记录 id 重复: {rid}")
        seen_ids.add(rid)
        kind = str(raw["kind"])
        if kind not in ALL_KINDS:
            raise ValueError(f"{rid}: 未知 kind={kind}")
        version = str(raw["version"])
        group = str(raw["group"])
        signers = tuple(Sig.from_dict(s) for s in raw.get("signers", ()))

        rec_kwargs = dict(
            record_id=rid, version=version, group=group, kind=kind,
            signers=signers, raw=raw,
        )

        if kind in WINDOW_KINDS:
            local_start = zone.parse_local(str(raw["local_start"]))
            local_end = zone.parse_local(str(raw["local_end"]))
            fold = int(raw.get("fold", 0))
            # fold 只作用于回拨重复（歧义）端点；无歧义端点只有一种映射。
            start = zone.local_to_utc(
                local_start, fold if zone.is_ambiguous(local_start) else 0
            )
            end = zone.local_to_utc(
                local_end, fold if zone.is_ambiguous(local_end) else 0
            )
            if start is None or end is None or not start < end:
                raise ValueError(
                    f"{rid}: 本地窗口 {raw['local_start']}..{raw['local_end']} "
                    f"在 fold={fold} 下不合法（端点落在春令时前跳缺口或非法）"
                )
            # 整段必须连续存在：与任一前跳缺口本地区间相交即非法（O(切换点数)）。
            for gap_s, gap_e in zone.forward_gap_local_intervals():
                if local_start < gap_e and gap_s < local_end:
                    raise ValueError(
                        f"{rid}: 本地窗口扫过春令时前跳缺口 "
                        f"{zone.fmt_local(gap_s)}..{zone.fmt_local(gap_e)}"
                    )
            rec_kwargs["start"], rec_kwargs["end"] = start, end
            if not signers:
                raise ValueError(f"{rid}: {kind} 至少需要一个签署人")
        else:
            rec_kwargs["at"] = zone.parse_utc(str(raw["at"]))
            if kind in ("anchor", "re_root"):
                rec_kwargs["root"] = str(raw["root"])
                if len(signers) != 1:
                    raise ValueError(f"{rid}: anchor/re_root 恰需 1 个签名别名")
            else:  # revoke
                rec_kwargs["target"] = str(raw["target"])
                if not signers:
                    raise ValueError(f"{rid}: revoke 必须带有效签署人")

        records.append(Record(**rec_kwargs))

    return LoadedInput(zone=zone, records=tuple(records), raw=payload)
