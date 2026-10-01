"""前后两台半球雷达的点云仿真(W09i 设计稿 §6:没有狗,合并、标定在开发机上拿它测)。

- 世界:地面 z = 0 + 若干轴对齐的箱子(墙、柱子、障碍)。
- 狗:机身系 x 朝狗头、y 朝左、z 朝上,站在地面上(只有平面位姿 x、y、yaw)。
- 雷达:跟 C40011 一样的装法 —— RS-Airy 原始系 **X 朝下、Z 朝前**(真机待验证清单 #64),
  前雷达在狗头那头
  (``x = +0.4043``)、后雷达在狗尾那头绕竖直轴转 π;离地 :data:`SENSOR_HEIGHT_M`;**半球视场**(原始系
  z > 0)。
- 射线按天顶角、方位角均匀撒,跟地面、箱子求最近交点,加高斯噪声。纯 numpy。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

SENSOR_HEIGHT_M = 0.53
SENSOR_X_M = 0.4043
RANGE_M = 30.0


def _T(R: Any, t: Any) -> Any:
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def body_to_world(x: float, y: float, yaw: float) -> Any:
    c, s = math.cos(yaw), math.sin(yaw)
    return _T(np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]]), [x, y, 0.0])


#: 前雷达原始系 → 机身系:原始 X → 机身 −z(朝下)、原始 Y → 机身 +y、原始 Z → 机身 +x(朝前)。
R_BODY_FRONT = np.array([[0, 0, 1.0], [0, 1.0, 0], [-1.0, 0, 0]])
T_BODY_FRONT = _T(R_BODY_FRONT, [SENSOR_X_M, 0.0, SENSOR_HEIGHT_M])
#: 后雷达:前雷达的装法绕机身竖直轴转 π、装在狗尾那头。
_RZ_PI = np.array([[-1.0, 0, 0], [0, -1.0, 0], [0, 0, 1.0]])
T_BODY_REAR = _T(_RZ_PI @ R_BODY_FRONT, [-SENSOR_X_M, 0.0, SENSOR_HEIGHT_M])
#: 真值:后雷达系 → 前雷达系。
T_FRONT_REAR = np.linalg.inv(T_BODY_FRONT) @ T_BODY_REAR


@dataclass
class Box:
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]


def room(w: float = 12.0, h: float = 8.0, wall: float = 0.2, height: float = 2.5,
         extra: tuple[Box, ...] = ()) -> list[Box]:
    """四面墙的院子 + 额外的箱子。"""
    return [Box((0, 0, 0), (w, wall, height)), Box((0, h - wall, 0), (w, h, height)),
            Box((0, 0, 0), (wall, h, height)), Box((w - wall, 0, 0), (w, h, height)),
            *extra]


def _rays(n_theta: int, n_phi: int) -> Any:
    """原始系里的射线方向:半球(z > 0),天顶角 2°–88°、方位角一圈。"""
    th = np.radians(np.linspace(2.0, 88.0, n_theta))
    ph = np.linspace(0.0, 2 * math.pi, n_phi, endpoint=False)
    TH, PH = np.meshgrid(th, ph, indexing="ij")
    d = np.stack([np.sin(TH) * np.cos(PH), np.sin(TH) * np.sin(PH), np.cos(TH)], -1)
    return d.reshape(-1, 3)


def scan(T_world_sensor: Any, world: list[Box], *, n_theta: int = 40, n_phi: int = 120,
         noise: float = 0.01, rng: Any = None) -> Any:
    """一帧点云(传感器原始系)。"""
    rng = rng if rng is not None else np.random.default_rng(0)
    d_s = _rays(n_theta, n_phi)
    R, o = T_world_sensor[:3, :3], T_world_sensor[:3, 3]
    d = d_s @ R.T                                         # 世界系方向
    best = np.full(len(d), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        tg = -o[2] / d[:, 2]                              # 地面 z = 0
        best = np.where((tg > 0) & np.isfinite(tg), np.minimum(best, tg), best)
        for b in world:
            lo, hi = np.array(b.lo), np.array(b.hi)
            t1, t2 = (lo - o) / d, (hi - o) / d
            tmin = np.nanmax(np.minimum(t1, t2), axis=1)
            tmax = np.nanmin(np.maximum(t1, t2), axis=1)
            hit = (tmax >= tmin) & (tmax > 0)
            tin = np.where(tmin > 0, tmin, tmax)
            best = np.where(hit & (tin < best), tin, best)
    ok = np.isfinite(best) & (best < RANGE_M)
    r = best[ok] + rng.normal(0.0, noise, ok.sum())
    return d_s[ok] * r[:, None]


def scans_along(poses: list[tuple[float, float, float]], world: list[Box], *,
                noise: float = 0.01, seed: int = 0, **kw: Any) -> list[tuple[Any, Any, Any]]:
    """沿一串机身位姿出 (前雷达在世界系的位姿 4×4, 前雷达一帧, 后雷达一帧)。"""
    rng = np.random.default_rng(seed)
    out = []
    for x, y, yaw in poses:
        Tw = body_to_world(x, y, yaw)
        Twf, Twr = Tw @ T_BODY_FRONT, Tw @ T_BODY_REAR
        out.append((Twf, scan(Twf, world, noise=noise, rng=rng, **kw),
                    scan(Twr, world, noise=noise, rng=rng, **kw)))
    return out
