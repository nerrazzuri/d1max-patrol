"""全狗限速(W29,决策 41):下雨、雷暴时站点给每台狗发一条限速,狗上所有自己走的(巡检、goto、
回待命点)都不超过它。**不是任务**(不占资源、不进幂等记录,同上装 ``deter``)。

``payload``:``{max_speed_mps, ttl_s}``。``max_speed_mps`` 是 ``null`` = 取消限速;``ttl_s`` 到了狗上
自己取消(站点断了不会一直慢着;站点每隔一阵续一次)。狗按自己的死区往上夹(限得比死区还低就走不动了)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

MIN_MPS = 0.1
MAX_MPS = 3.0
MAX_TTL_S = 3600
#: 下雨、雷暴时的限速(决策 41:湿地防打滑)。
RAIN_MPS = 0.3


@dataclass(frozen=True)
class SpeedCap:
    max_speed_mps: float | None
    ttl_s: int

    def to_payload(self) -> dict[str, Any]:
        return {"max_speed_mps": self.max_speed_mps, "ttl_s": self.ttl_s}


def parse_speed_cap(payload: Any) -> SpeedCap:
    if not isinstance(payload, dict) or set(payload) != {"max_speed_mps", "ttl_s"}:
        raise ContractError("speed_cap:payload 要正好是 max_speed_mps、ttl_s")
    v = payload["max_speed_mps"]
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                          or not math.isfinite(v) or not MIN_MPS <= v <= MAX_MPS):
        raise ContractError(f"speed_cap:max_speed_mps 要是 null 或 {MIN_MPS}–{MAX_MPS}:{v!r}")
    t = payload["ttl_s"]
    if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= MAX_TTL_S:
        raise ContractError(f"speed_cap:ttl_s 要是 1–{MAX_TTL_S} 的整数:{t!r}")
    return SpeedCap(max_speed_mps=None if v is None else float(v), ttl_s=t)
