"""录包时的轨迹(W00c6h):录包期间狗按里程记一条轨迹,站点经 ``mapping_trail`` 取新增的点给手机画 ——
录包时看得见哪儿走过了(老服务是在线建图推栅格;W08 决定 4 不再在线跑 2D 建图,见设计稿)。

- 坐标以**录包起点**为原点、起点朝向为 +x(新地方还没有地图、也没锚,只能是里程系)。
- 每挪 ``STEP_M`` 或转 ``TURN_RAD`` 记一点;最多 ``MAX_POINTS`` 点,满了不再记、报 ``full``
  (不抽稀:抽稀会让手机手上的 ``since`` 对不上)。5000 点 × 0.3 m 是 1.5 km,一趟建图够了。
- 录包开始时清空;停了留着(看最后录的那一趟),不再记。
"""

from __future__ import annotations

import math
from typing import Any

STEP_M = 0.3
TURN_RAD = math.radians(15.0)
MAX_POINTS = 5000


def _wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


class MappingTrail:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._origin: tuple[float, float, float] | None = None
        self._points: list[tuple[float, float, float]] = []
        self.full = False

    def feed(self, x: float, y: float, yaw: float) -> None:
        """一帧里程。"""
        if self._origin is None:
            self._origin = (x, y, yaw)
        ox, oy, oyaw = self._origin
        c, s = math.cos(-oyaw), math.sin(-oyaw)
        dx, dy = x - ox, y - oy
        p = (c * dx - s * dy, s * dx + c * dy, _wrap(yaw - oyaw))
        if self._points:
            lx, ly, lyaw = self._points[-1]
            if math.hypot(p[0] - lx, p[1] - ly) < STEP_M and abs(_wrap(p[2] - lyaw)) < TURN_RAD:
                return
        if len(self._points) >= MAX_POINTS:
            self.full = True
            return
        self._points.append(p)

    def since(self, n: int) -> dict[str, Any]:
        """第 ``n`` 个点起的(手机上次拿到了 ``n`` 个)。坐标取到厘米。"""
        return {"points": [[round(x, 2), round(y, 2)] for x, y, _ in self._points[n:]],
                "since": n, "total": len(self._points), "full": self.full}
