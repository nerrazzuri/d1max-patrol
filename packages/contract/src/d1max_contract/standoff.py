"""保持距离(W25,决策 40):驱离时狗守在拦截点,人逼近就退,退不了原地站定、报警。

站点开场派一趟 ``standoff`` 任务(优先级 :data:`STANDOFF_PRIORITY`,**比谁都低**:回待命点、
人手动派的、事件派遣、遥控都能直接抢它,抢占照常先停稳)。判定全在狗上(人的方向距离、局部障碍、禁行区都在狗上,
延迟要短):

- 人在 :data:`TOO_CLOSE_M`(3 m)以内 → 往**远离人**的方向退(直退或带一点弧,前后都行),
  退到 :data:`CLEAR_M`(4 m)以外停;
- **只退不进**(决策 40):人在远处不追、不往人那边走;离开场的位置最多 ``leash_m`` 米;
- 每个退法都要过避障守卫(扫过区没挡、看得见)、不进禁行区、真的让距离变大;一个都不行 → **无路可退**:
  原地站定(不转身、不往前),能力里报 ``cornered``,站点据此报 P1 告警(决策 40);
- 人不知道在哪(检测中断、没测到距离)→ 原地站着,不动。

``payload``:``{max_s, leash_m}``。``max_s`` 到了狗自己收(站点断了也不会一直守着);
``leash_m``:离开场的位置最远退多远。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

#: 比回待命点(-10)还低:任何派单都能抢。
STANDOFF_PRIORITY = -20
TOO_CLOSE_M = 3.0
CLEAR_M = 4.0
DEFAULT_LEASH_M = 6.0
MAX_LEASH_M = 15.0
MAX_S = 900
TASK_PREFIX = "standoff-"
#: 能力里 ``tasks.standoff.state``:
#: ``idle`` 没在守;``hold`` 守着、站着;``retreat`` 在退;``cornered`` 无路可退、原地站定。
STATES = ("idle", "hold", "retreat", "cornered")


@dataclass(frozen=True)
class StandoffRequest:
    max_s: int
    leash_m: float = DEFAULT_LEASH_M

    def to_payload(self) -> dict[str, Any]:
        return {"max_s": self.max_s, "leash_m": self.leash_m}


def parse_standoff(payload: Any) -> StandoffRequest:
    if not isinstance(payload, dict) or set(payload) - {"max_s", "leash_m"}:
        raise ContractError("standoff:payload 只认 max_s、leash_m")
    m = payload.get("max_s")
    if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= MAX_S:
        raise ContractError(f"standoff:max_s 要是 1–{MAX_S} 的整数:{m!r}")
    lm = payload.get("leash_m", DEFAULT_LEASH_M)
    if isinstance(lm, bool) or not isinstance(lm, (int, float)) or not math.isfinite(lm) \
            or not 0.5 <= lm <= MAX_LEASH_M:
        raise ContractError(f"standoff:leash_m 要在 0.5–{MAX_LEASH_M} 米:{lm!r}")
    return StandoffRequest(max_s=m, leash_m=float(lm))
