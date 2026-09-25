"""狗的盘况(W00c5d,决策 8)。

决策 8:业务数据都在站点;狗上只有运行必需的工作副本与**断网暂存的发件箱**(有上限、恢复后上传、
站点确认即删)。发件箱满不满、积压多久,站点要看得见 —— 这份事实随遥测每 10 s 带一次,站点据此出
``disk_80``/``upload_backlog`` 告警、填值守汇总。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

#: 盘用到这个比例:站点出 ``disk_80``。
WARN_RATIO = 0.8
#: 盘用到这个比例(或发件箱到上限):代理拒绝新的巡检(``storage_full``)。
STOP_RATIO = 0.9


def _count(d: dict, k: str, *, lo: int = 0) -> int:
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise ContractError(f"StorageFacts: {k} 要是 ≥{lo} 的整数")
    return v


@dataclass(frozen=True)
class StorageFacts:
    #: 发件箱所在的那块盘已用的比例(0–1)。
    disk_used_ratio: float
    outbox_bytes: int
    outbox_cap_bytes: int
    #: 还没被站点确认的文件数、字节数。
    backlog_files: int
    backlog_bytes: int
    #: 最老一条待传的等了多久(秒);没有积压为 None。
    oldest_backlog_s: int | None

    def full(self, *, stop_ratio: float = STOP_RATIO) -> bool:
        """满了:盘到停止水位,或发件箱到上限。满了就不接新的巡检。"""
        return self.disk_used_ratio >= stop_ratio or self.outbox_bytes >= self.outbox_cap_bytes

    def to_wire(self) -> dict[str, Any]:
        return {"disk_used_ratio": self.disk_used_ratio, "outbox_bytes": self.outbox_bytes,
                "outbox_cap_bytes": self.outbox_cap_bytes, "backlog_files": self.backlog_files,
                "backlog_bytes": self.backlog_bytes, "oldest_backlog_s": self.oldest_backlog_s}

    @classmethod
    def from_wire(cls, d: Any) -> StorageFacts:
        if not isinstance(d, dict):
            raise ContractError("StorageFacts: 要是对象")
        r = d.get("disk_used_ratio")
        if isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(r) \
                or not 0.0 <= r <= 1.0:
            raise ContractError("StorageFacts: disk_used_ratio 要是 0–1 的数")
        oldest = d.get("oldest_backlog_s")
        return cls(disk_used_ratio=float(r), outbox_bytes=_count(d, "outbox_bytes"),
                   outbox_cap_bytes=_count(d, "outbox_cap_bytes", lo=1),
                   backlog_files=_count(d, "backlog_files"),
                   backlog_bytes=_count(d, "backlog_bytes"),
                   oldest_backlog_s=None if oldest is None else _count(d, "oldest_backlog_s"))
