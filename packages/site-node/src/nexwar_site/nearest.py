"""让最近的机器人过去看看(W44,设计 §4.3):地图上点一处,按跟机器人同一份的规划算谁走得最近。

只在同一张图、同一版上的机器人里挑(别的图上的坐标在这张图上没意义);走不到的不选。估到达时间按
:data:`NOMINAL_MPS`(巡逻常用的速度),只是给人看个大概。派出去照旧走 ``goto``
(时间关口、监护规则照旧)。
"""

from __future__ import annotations

from typing import Any

NOMINAL_MPS = 0.5


def nearest(reach: Any, candidates: list[tuple[str, str, str, float, float]], map_id: str,
            version: str, x: float, y: float) -> dict[str, Any] | None:
    """``candidates``:``(robot_id, map_id, map_version, x, y)``。

    回 ``{robot_id, path_m, eta_s}``;没有回 None。"""
    best: dict[str, Any] | None = None
    for rid, mid, ver, rx, ry in candidates:
        if mid != map_id or ver != version:
            continue
        m = reach.path_m(map_id, version, (rx, ry), (x, y))
        if m is None:
            continue
        if best is None or m < best["path_m"]:
            best = {"robot_id": rid, "path_m": m, "eta_s": round(m / NOMINAL_MPS)}
    return best
