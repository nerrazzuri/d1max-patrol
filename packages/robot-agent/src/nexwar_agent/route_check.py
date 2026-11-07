"""Route Validator V1.0 的几何检查(导航 V1 第 2 节,``docs/导航V1-设计.md``)。纯 Python,不碰 ROS。

Nav2 规划出路线(``plan_result``,带朝向、不抽稀)之后、出发之前:

1. 把位姿插值到 ≤ 5 cm 一个(朝向走最短角)。
2. 每个位姿放「物理车身 + 基础余量」(半长 :data:`~nexwar_contract.footprint.BASE_HALF_LEN`、
   半宽 :data:`~nexwar_contract.footprint.BASE_HALF_WID`),算四边一起往外扩多少碰到地图墙,
   就是这一处的余量。
3. 最窄处余量 < :data:`~nexwar_contract.footprint.ROUTE_MARGIN` 就拒绝出发:最窄处在终点附近报
   ``goal_too_close_to_wall``,别处报 ``route_too_narrow``,原因里写在哪、剩多少、要多少。

起步那一小段(:data:`SKIP_START_M`)不查:机器人本来就站在那儿(停在墙边、桩上),不该因此走不了。
只看地图墙;人、车、临时障碍归守卫管。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from nexwar_contract.footprint import (
    BASE_HALF_LEN,
    BASE_HALF_WID,
    ROUTE_MARGIN,
    ROUTE_MIN_WIDTH,
)

STEP_M = 0.05
SKIP_START_M = 0.5
#: 最窄处离终点这么近(沿路线,米)算「目标点贴墙」。
GOAL_ZONE_M = 0.3
#: 余量算到这么大就够了(再远的墙不看)。
CAP_M = 0.5


@dataclass(frozen=True, slots=True)
class RouteCheck:
    ok: bool
    #: 查过的位姿里最小的余量(米;负 = 车身压进墙);没有可查的位姿是 CAP_M
    margin: float
    #: 最窄处位姿 ``(x, y, yaw)``
    at: tuple[float, float, float] | None
    code: str = ""
    why: str = ""


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def densify(poses: Sequence[Sequence[float]],
            step: float = STEP_M) -> list[tuple[float, float, float]]:
    """相邻位姿插值到不超过 ``step`` 米一个;朝向按最短角插。"""
    if not poses:
        return []
    out = [(float(poses[0][0]), float(poses[0][1]), float(poses[0][2]))]
    for a, b in zip(poses, poses[1:], strict=False):
        d = math.dist(a[:2], b[:2])
        n = max(1, math.ceil(d / step - 1e-9))
        dyaw = _wrap(float(b[2]) - float(a[2]))
        for i in range(1, n + 1):
            t = i / n
            out.append((float(a[0]) + t * (float(b[0]) - float(a[0])),
                        float(a[1]) + t * (float(b[1]) - float(a[1])),
                        _wrap(float(a[2]) + t * dyaw)))
    return out


def pose_margins(poses: Sequence[tuple[float, float, float]], walls: Any, res: float,
                 origin: tuple[float, float]) -> np.ndarray:
    """每个位姿:车身(基础余量那一层)四边一起往外扩多少米碰到墙(墙格按方格算,偏保守)。"""
    h, w = walls.shape
    reach = math.hypot(BASE_HALF_LEN, BASE_HALF_WID) + CAP_M + res
    span = int(math.ceil(reach / res))
    out = np.full(len(poses), CAP_M)
    ox, oy = origin
    for i, (x, y, yaw) in enumerate(poses):
        r0, k0 = int((y - oy) // res), int((x - ox) // res)
        rs, re_ = max(0, r0 - span), min(h, r0 + span + 1)
        ks, ke = max(0, k0 - span), min(w, k0 + span + 1)
        if rs >= re_ or ks >= ke:
            out[i] = -1.0                                  # 出了地图:当压进墙
            continue
        rr, kk = np.nonzero(walls[rs:re_, ks:ke])
        if r0 < 0 or r0 >= h or k0 < 0 or k0 >= w:
            out[i] = -1.0
            continue
        if not len(rr):
            continue
        cx = ox + (kk + ks + 0.5) * res - x
        cy = oy + (rr + rs + 0.5) * res - y
        c, s = math.cos(yaw), math.sin(yaw)
        lx, ly = np.abs(c * cx + s * cy), np.abs(-s * cx + c * cy)
        need = np.maximum(lx - BASE_HALF_LEN, ly - BASE_HALF_WID) - res / 2
        out[i] = min(CAP_M, float(need.min()))
    return out


def check_route(poses: Sequence[Sequence[float]], walls: Any, res: float,
                origin: tuple[float, float], *, need: float = ROUTE_MARGIN) -> RouteCheck:
    """查一条路线;``poses`` = ``plan_result.poses``(地图系)。"""
    dense = densify(poses)
    if not dense:
        return RouteCheck(True, CAP_M, None)
    along = np.concatenate([[0.0], np.cumsum([math.dist(a[:2], b[:2])
                                              for a, b in zip(dense, dense[1:], strict=False)])])
    keep = along >= min(SKIP_START_M, along[-1])
    keep[-1] = True                                        # 终点总要查
    idx = np.flatnonzero(keep)
    m = pose_margins([dense[i] for i in idx], walls, res, origin)
    j = int(np.argmin(m))
    worst, at = float(m[j]), dense[int(idx[j])]
    if worst >= need:
        return RouteCheck(True, worst, at)
    near_goal = along[-1] - along[int(idx[j])] <= GOAL_ZONE_M
    gx, gy = dense[-1][0], dense[-1][1]
    if near_goal:
        why = (f"目标点 ({gx:.2f}, {gy:.2f}) 离墙太近:机器人站在那儿只剩 "
               f"{max(worst, 0.0):.2f} m 余量(要求每边 ≥ {need:.2f} m),把航点往空处挪一点")
        return RouteCheck(False, worst, at, "goal_too_close_to_wall", why)
    why = (f"路线在 ({at[0]:.2f}, {at[1]:.2f}) 处离墙只剩 {max(worst, 0.0):.2f} m"
           f"(要求每边 ≥ {need:.2f} m,直通道约需 {ROUTE_MIN_WIDTH:.2f} m 净宽),无法安全通过")
    return RouteCheck(False, worst, at, "route_too_narrow", why)
