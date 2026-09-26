"""录包时的轨迹(W00c6h):录包期间狗按里程记一条轨迹,站点经 ``mapping_trail`` 取新增的点给手机画 ——
录包时看得见哪儿走过了(老服务是在线建图推栅格;W08 决定 4 不再在线跑 2D 建图,见设计稿)。

- 坐标以**录包起点**为原点、起点朝向为 +x(新地方还没有地图、也没锚,只能是里程系)。
- 每挪 ``STEP_M`` 或转 ``TURN_RAD`` 记一点;最多 ``MAX_POINTS`` 点,满了不再记、报 ``full``
  (不抽稀:抽稀会让手机手上的 ``since`` 对不上)。5000 点 × 0.3 m 是 1.5 km,一趟建图够了。
- 录包开始时清空、换一个 ``epoch``(一趟一个号;手机据此知道换了一趟,W00c6h 内审);停了留着(看最后
  录的那一趟),不再记。
- 坏里程(NaN、无穷)不记;一拍挪 ``JUMP_M`` 以上或转 ``JUMP_RAD`` 以上当里程跳了(运控重置、归零),
  轨迹在跳的地方**接上**(这一帧当作狗没动),记 ``jumps``。
"""

from __future__ import annotations

import math
from typing import Any

STEP_M = 0.3
TURN_RAD = math.radians(15.0)
MAX_POINTS = 5000
JUMP_M = 1.0
JUMP_RAD = 1.0


def _wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


def _compose(a: tuple[float, float, float], b: tuple[float, float, float]
             ) -> tuple[float, float, float]:
    c, s = math.cos(a[2]), math.sin(a[2])
    return (a[0] + c * b[0] - s * b[1], a[1] + s * b[0] + c * b[1], _wrap(a[2] + b[2]))


def _inverse(p: tuple[float, float, float]) -> tuple[float, float, float]:
    c, s = math.cos(p[2]), math.sin(p[2])
    return (-c * p[0] - s * p[1], s * p[0] - c * p[1], _wrap(-p[2]))


class MappingTrail:
    def __init__(self) -> None:
        self.epoch = 0
        self._clear()

    def reset(self) -> None:
        """新的一趟:清空、换号。"""
        self.epoch += 1
        self._clear()

    def _clear(self) -> None:
        self._origin: tuple[float, float, float] | None = None
        self._last_raw: tuple[float, float, float] | None = None
        self._points: list[tuple[float, float, float]] = []
        self.full = False
        self.jumps = 0

    def feed(self, x: float, y: float, yaw: float) -> None:
        """一帧里程。"""
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(yaw)):
            return
        raw = (x, y, yaw)
        if self._origin is None:
            self._origin = raw
        elif self._last_raw is not None:
            lx, ly, lyaw = self._last_raw
            if math.hypot(x - lx, y - ly) > JUMP_M or abs(_wrap(yaw - lyaw)) > JUMP_RAD:
                # 里程跳了:换一个起点,让这一帧落在上一帧的位置上(轨迹接上,不画长直线)。
                before = _compose(_inverse(self._origin), self._last_raw)
                self._origin = _compose(raw, _inverse(before))
                self.jumps += 1
        self._last_raw = raw
        p = _compose(_inverse(self._origin), raw)
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
                "since": n, "total": len(self._points), "full": self.full, "epoch": self.epoch,
                "jumps": self.jumps}
