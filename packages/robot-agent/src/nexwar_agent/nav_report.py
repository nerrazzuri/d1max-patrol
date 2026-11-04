"""规划路径上报(运营可视化):狗把正在走的规划路径抽稀后放进遥测,站点画在地图上。

- **只在导航时报**:没路径(没规划、收尾了)就不带 ``nav`` 块 —— 站点看到没有就清掉。
- **有界**:抽稀(Ramer-Douglas-Peucker,0.1 m)后最多 :data:`MAX_POINTS` 个点,坐标取两位小数;
  一份路径不过 1.5 KB。
- **限频**:路径变了才发新的,而且两份路径之间至少隔 :data:`MIN_GAP_MS`;没变的话每
  :data:`REFRESH_MS` 重发一次(遥测是 QoS 0,会丢;站点重启后也靠它补上)。其余的遥测只带
  ``{"seq": n}``(告诉站点「还在导航、路径没换」),不带点。

线格式(``Telemetry.nav``)::

    {"seq": 3}                                  # 还在导航,路径没变(或变了还没到发的时候)
    {"seq": 4, "path": [[x, y], ...]}           # 新的(或定时重发的)路径,地图坐标,米
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

#: 一份路径最多这么多个点(抽稀之后超了再均匀抽)。
MAX_POINTS = 64
#: 两份路径之间最少隔多久(毫秒)。
MIN_GAP_MS = 2000
#: 路径没变也重发的间隔(毫秒)。
REFRESH_MS = 10_000
#: 抽稀的容差(米)。
SIMPLIFY_M = 0.1

Pt = tuple[float, float]


def _rdp(pts: Sequence[Pt], eps: float) -> list[Pt]:
    """Ramer-Douglas-Peucker(迭代,不递归:路径可能有几千个点)。"""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        far, far_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = (math.hypot(px - ax, py - ay) if norm < 1e-9
                 else abs(dx * (ay - py) - (ax - px) * dy) / norm)
            if d > far:
                far, far_i = d, i
        if far > eps and far_i > 0:
            keep[far_i] = True
            stack.append((a, far_i))
            stack.append((far_i, b))
    return [p for p, k in zip(pts, keep, strict=True) if k]


def decimate(points: Sequence[Pt], *, max_points: int = MAX_POINTS) -> list[list[float]]:
    """抽稀、限点数、取两位小数;不是有限数的点丢掉。头尾一定保留。"""
    good = [(float(x), float(y)) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    if len(good) < 2:
        return []
    pts = _rdp(good, SIMPLIFY_M)
    if len(pts) > max_points:
        step = (len(pts) - 1) / (max_points - 1)
        pts = [pts[round(i * step)] for i in range(max_points)]
    return [[round(x, 2), round(y, 2)] for x, y in pts]


class NavPathReporter:
    """每拍遥测调一次 :meth:`wire`;状态都在这里(限频、变没变)。"""

    def __init__(self) -> None:
        self._sent_key: tuple | None = None       # 最近一次发出去的路径
        self._seq = 0                             # 最近一次**发出去**的那份路径的序号
        self._sent_ms: int | None = None

    def reset(self) -> None:
        self._sent_key = self._sent_ms = None

    def wire(self, points: Sequence[Pt] | None, now_ms: int) -> dict[str, Any] | None:
        if not points:
            self.reset()
            return None
        pts = decimate(points)
        if not pts:
            self.reset()
            return None
        key = tuple(map(tuple, pts))
        changed = key != self._sent_key
        due = (self._sent_ms is None
               or now_ms - self._sent_ms >= (MIN_GAP_MS if changed else REFRESH_MS))
        if not due:
            return {"seq": self._seq}
        self._sent_key, self._sent_ms = key, now_ms
        if changed:
            self._seq += 1
        return {"seq": self._seq, "path": pts}
