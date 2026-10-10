"""防区画在地图上(商业化 B1c):每张图每个版本上,一个防区一个多边形(地图坐标,米)。

以前「防区」只是摄像头、拦截点上的一个名字,站点不知道地图上哪块地属于哪个防区,狗看见人的时候也说不出
是在哪个防区。画上以后 :func:`zone_at` 能按狗的位置算出防区 —— 名单(时段 / 人员授权)靠它判断「这个人
在不在授权的那块地上」。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from d1max_site.db import SiteDB

#: 防区名跟摄像头、事件派遣里的一样:1–128 个可见字符。
_ZONE = re.compile(r"[^\x00-\x1f]{1,128}")
MAX_POINTS = 64


class AreaError(ValueError):
    """多边形或防区名不对(接口回 400)。"""


def _check(zone: Any, points: Any) -> list[list[float]]:
    if not isinstance(zone, str) or not _ZONE.fullmatch(zone):
        raise AreaError("防区名要是 1–128 个字符")
    if not isinstance(points, list) or not 3 <= len(points) <= MAX_POINTS:
        raise AreaError(f"多边形要 3–{MAX_POINTS} 个点")
    out = []
    for p in points:
        if not (isinstance(p, list) and len(p) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in p)):
            raise AreaError("每个点要是 [x, y] 两个数(地图坐标,米)")
        out.append([round(float(p[0]), 3), round(float(p[1]), 3)])
    if abs(_area(out)) < 0.25:
        raise AreaError("多边形太小了(不到 0.25 平方米),或者点都在一条线上")
    return out


def _area(pts: list[list[float]]) -> float:
    return sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts))) / 2


def inside(x: float, y: float, pts: list[list[float]]) -> bool:
    """点在不在多边形里(射线法;边上的点算不准,不要紧)。"""
    hit = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


class AreaBook:
    def __init__(self, db: SiteDB, *, now_ms: Any) -> None:
        self.db = db
        self._now = now_ms

    def list(self, map_id: str, version: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT zone, points, updated_by, updated_ms FROM zone_areas "
                             "WHERE map_id=? AND map_version=? ORDER BY zone", (map_id, version))
        return [{"zone": r["zone"], "points": json.loads(r["points"]),
                 "updated_by": r["updated_by"], "updated_ms": r["updated_ms"]} for r in rows]

    def set(self, map_id: str, version: str, zone: Any, points: Any, *, by: str) -> None:
        pts = _check(zone, points)
        with self.db.tx() as c:
            c.execute("INSERT INTO zone_areas(map_id, map_version, zone, points, updated_by, "
                      "updated_ms) VALUES (?,?,?,?,?,?) ON CONFLICT(map_id, map_version, zone) "
                      "DO UPDATE SET points=excluded.points, updated_by=excluded.updated_by, "
                      "updated_ms=excluded.updated_ms",
                      (map_id, version, zone, json.dumps(pts), by, self._now()))

    def remove(self, map_id: str, version: str, zone: str) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM zone_areas WHERE map_id=? AND map_version=? AND zone=?",
                             (map_id, version, zone)).rowcount > 0

    def zone_at(self, map_id: str, version: str, x: float, y: float) -> str | None:
        """这张图这一版上,点 (x, y) 落在哪个防区(落在好几个里取名字排第一的)。"""
        for a in self.list(map_id, version):
            if inside(x, y, a["points"]):
                return a["zone"]
        return None

    def zones(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT zone FROM zone_areas ORDER BY zone")
        return [r["zone"] for r in rows]
