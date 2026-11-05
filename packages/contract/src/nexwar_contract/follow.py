"""跟人(W74 二期,设计 docs/W74-看见人处置-设计.md §五、§六)。

布防时看见人、人在动,站点派一趟 ``follow`` 任务(优先级 :data:`FOLLOW_PRIORITY`,跟报警出动一样)。
**跟随在机器人上做**:人在哪(人员检测的方向 + 距离 + 定位)、往哪走、什么时候算跟丢都在机器人上判,
断网照样跟、照样留证,站点只开、收、看报告。

- 目标点:人后方 ``keep_m``(机器人 → 人那条线上,离人 ``keep_m``),朝着人;每秒按人的新位置更新
  (导航中途改目标,不停车)。不出地图、不进禁行区、照常避障,守卫不放宽。
- 跟丢:连续 ``lost_s`` 秒没看到被跟的人,或者人在到不了的地方(规划不出路)→ 回最后看见人的地方,
  朝人离开的方向停下,任务结束(``result: lost``)。
- 上限:跟了 ``max_s`` 秒或走了 ``max_m`` 米 → 按跟丢办(``result: limit``)。半路电量到线、引擎返航 →
  任务失败(不再跟)。

路上每 :data:`TRACK_EVERY_S` 秒发一条 ``follow_track`` 事件(人在地图上的位置、朝向),断网时进发件箱,
连回来补传;终态 ``detail`` 带 ``result``、最后位置 ``last``、方向 ``heading``、轨迹 ``track``。

``payload``:``{keep_m, max_s, max_m, lost_s, max_speed_mps}``,都可省(按默认)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from nexwar_contract.charging import ALARM_PRIORITY
from nexwar_contract.errors import ContractError

#: 跟报警出动一样(:data:`~nexwar_contract.charging.ALARM_PRIORITY`):值守的这一圈先停。
FOLLOW_PRIORITY = ALARM_PRIORITY
TASK_PREFIX = "follow-"
KEEP_M = 3.0
MAX_S = 180
MAX_M = 150.0
LOST_S = 10.0
MAX_SPEED_MPS = 1.0
#: 路上发轨迹的间隔(秒)。
TRACK_EVERY_S = 2.0
#: 终态里最多带这么多个轨迹点(最新的)。
TRACK_KEEP = 120
#: 结局:跟丢、到上限。
RESULTS = ("lost", "limit")

_LIMITS: dict[str, tuple[float, float]] = {
    "keep_m": (1.5, 10.0), "max_s": (10, 900), "max_m": (5.0, 500.0), "lost_s": (2.0, 60.0),
    "max_speed_mps": (0.2, 1.5),
}


@dataclass(frozen=True)
class FollowRequest:
    keep_m: float = KEEP_M
    max_s: int = MAX_S
    max_m: float = MAX_M
    lost_s: float = LOST_S
    max_speed_mps: float = MAX_SPEED_MPS

    def to_payload(self) -> dict[str, Any]:
        return {"keep_m": self.keep_m, "max_s": self.max_s, "max_m": self.max_m,
                "lost_s": self.lost_s, "max_speed_mps": self.max_speed_mps}


def parse_follow(payload: Any) -> FollowRequest:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or set(payload) - set(_LIMITS):
        raise ContractError(f"follow:payload 只认 {'、'.join(_LIMITS)}")
    out: dict[str, Any] = {}
    for k, (lo, hi) in _LIMITS.items():
        if k not in payload:
            continue
        v = payload[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                or not lo <= v <= hi:
            raise ContractError(f"follow:{k} 要在 {lo}–{hi}:{v!r}")
        if k == "max_s":
            if not isinstance(v, int):
                raise ContractError(f"follow:max_s 要是整数:{v!r}")
            out[k] = v
        else:
            out[k] = float(v)
    return FollowRequest(**out)
