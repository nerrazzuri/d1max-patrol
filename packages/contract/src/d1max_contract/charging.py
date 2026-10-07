"""自动回充(W13,决策 25、45):两条对桩的路(厂家回充 / 自己认二维码对桩)都要的框架。

流程(站点编排,每一步都是一趟普通任务,优先级 :data:`CHARGE_PRIORITY`):
1. **走到桩前对准点**(``goto``,点是站点给每台狗登记的:桩前约 1.5 m、正对桩);
2. **对桩、充电、充满出桩**(``dock``,这一单新加的任务;狗上调 HAL 的 ``recharge_start``,
   **按电池确认充上了**(厂家回充从不报成功),充到 ``resume_pct`` 调 ``undock``,出了桩才算完)。

电量线(决策 45):空闲时电量 ≤ :data:`LOW_PCT` 就去充,充到 :data:`RESUME_PCT` 出桩;充电中来了入侵,
电量 ≥ :data:`INTRUSION_MIN_PCT` 就出桩去(入侵 80 抢得走回充 50,``dock`` 被抢时**先出桩再交**),
不够就不派。被抢断的回充,狗空了接着充。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

LOW_PCT = 30.0
RESUME_PCT = 90.0
INTRUSION_MIN_PCT = 50.0
#: 回充的优先级:比排程巡检(10–49)高,比人手动派的(60)、事件派遣(80)低 —— 人和入侵抢得走。
CHARGE_PRIORITY = 50
TASK_PREFIX = "charge-"
DOCK_TIMEOUT_S = 180
UNDOCK_TIMEOUT_S = 120
MAX_CHARGE_S = 6 * 3600


@dataclass(frozen=True)
class DockRequest:
    resume_pct: float = RESUME_PCT
    dock_timeout_s: int = DOCK_TIMEOUT_S
    max_s: int = MAX_CHARGE_S

    def to_payload(self) -> dict[str, Any]:
        return {"resume_pct": self.resume_pct, "dock_timeout_s": self.dock_timeout_s,
                "max_s": self.max_s}


def parse_dock(payload: Any) -> DockRequest:
    if not isinstance(payload, dict) or set(payload) - {"resume_pct", "dock_timeout_s", "max_s"}:
        raise ContractError("dock:payload 只认 resume_pct、dock_timeout_s、max_s")
    r = payload.get("resume_pct", RESUME_PCT)
    if isinstance(r, bool) or not isinstance(r, (int, float)) or not 10 <= r <= 100:
        raise ContractError(f"dock:resume_pct 要在 10–100:{r!r}")
    out = {}
    for k, lo, hi, dflt in (("dock_timeout_s", 10, 1800, DOCK_TIMEOUT_S),
                            ("max_s", 60, 24 * 3600, MAX_CHARGE_S)):
        v = payload.get(k, dflt)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ContractError(f"dock:{k} 要是 {lo}–{hi} 的整数:{v!r}")
        out[k] = v
    return DockRequest(resume_pct=float(r), **out)
