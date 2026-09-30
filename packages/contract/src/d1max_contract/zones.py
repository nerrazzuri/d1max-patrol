"""禁行区与限速区(W10,W08 决定 5):挂在几何版本(``map_id + map_version``)上的一份区域集。

- 区域是地图系里的多边形:``nogo`` 当障碍(规划不许进、狗在里面就停),``slow`` 限速
  (``max_speed_mps``)。
- 区域集有自己的**修订号**(从 1 起,改一次 +1);站点是唯一权威,经命令 ``zones_set`` 整份下发。
- **只收紧**(:func:`tightens`)的改动可以热更;放宽的等狗空闲再换(设计稿「与 W08 字面不同」)。
- 不进地图产物:建图不管它,站点单独存。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.maps import check_name

KINDS = ("nogo", "slow")
MAX_ZONES = 200
MAX_VERTICES = 64
MIN_AREA_M2 = 0.01
#: 坐标绝对值上限(米):地图系里的点,庄园一公里以内;挡住离谱值。
MAX_COORD_M = 100_000.0
MAX_LABEL = 64
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def polygon_area(pts: tuple[tuple[float, float], ...]) -> float:
    """有向面积的绝对值(鞋带公式)。"""
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_seg(a, b, p) -> bool:
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def segments_cross(a, b, c, d) -> bool:
    """线段 ab 与 cd 有没有公共点(含端点、共线重叠)。"""
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and o1 and o2 and o3 and o4:
        return True
    return ((o1 == 0 and _on_seg(a, b, c)) or (o2 == 0 and _on_seg(a, b, d))
            or (o3 == 0 and _on_seg(c, d, a)) or (o4 == 0 and _on_seg(c, d, b)))


def is_simple(pts: tuple[tuple[float, float], ...]) -> bool:
    """多边形的边不自交:不相邻的边没有公共点;相邻的边只在公共顶点相接(共线折回也算自交);
    没有重复的相邻顶点。顶点数上限 64,两两查就够。"""
    n = len(pts)
    if any(pts[i] == pts[(i + 1) % n] for i in range(n)):
        return False
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                s_ = pts[j] if j == i + 1 else pts[i]                 # 公共顶点
                p = pts[i] if j == i + 1 else pts[i + 1]              # 边 i 另一端
                q = pts[(j + 1) % n] if j == i + 1 else pts[j]        # 边 j 另一端
                dot = (p[0] - s_[0]) * (q[0] - s_[0]) + (p[1] - s_[1]) * (q[1] - s_[1])
                if _orient(s_, p, q) == 0 and dot > 0:
                    return False
                continue
            if segments_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                return False
    return True


def point_in_polygon(x: float, y: float, pts: tuple[tuple[float, float], ...]) -> bool:
    """射线法;正好压在边上的点算在里面(禁行区往保守方向)。"""
    inside = False
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        if _orient(a, b, (x, y)) == 0 and _on_seg(a, b, (x, y)):
            return True
        if (a[1] > y) != (b[1] > y):
            xc = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < xc:
                inside = not inside
    return inside


def distance_to_polygon(x: float, y: float, pts: tuple[tuple[float, float], ...]) -> float:
    """点到多边形的距离:在里面是 0。"""
    if point_in_polygon(x, y, pts):
        return 0.0
    best = math.inf
    n = len(pts)
    for i in range(n):
        (ax, ay), (bx, by) = pts[i], pts[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


@dataclass(frozen=True)
class Zone:
    id: str
    kind: str
    polygon: tuple[tuple[float, float], ...]
    label: str = ""
    max_speed_mps: float | None = None

    def to_wire(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "kind": self.kind, "label": self.label,
                             "polygon": [[x, y] for x, y in self.polygon]}
        if self.kind == "slow":
            d["max_speed_mps"] = self.max_speed_mps
        return d

    @classmethod
    def from_wire(cls, d: Any) -> Zone:
        if not isinstance(d, dict):
            raise ContractError("zone: 要是对象")
        zid = d.get("id")
        if not isinstance(zid, str) or not _ID_RE.fullmatch(zid):
            raise ContractError(f"zone: id 只许字母、数字、. _ -,1–64 位:{zid!r}")
        kind = d.get("kind")
        if kind not in KINDS:
            raise ContractError(f"zone {zid}: kind 要是 {'/'.join(KINDS)}:{kind!r}")
        label = d.get("label", "")
        if not isinstance(label, str) or len(label) > MAX_LABEL:
            raise ContractError(f"zone {zid}: label 要是 ≤{MAX_LABEL} 字的字符串")
        raw = d.get("polygon")
        if not isinstance(raw, list) or not 3 <= len(raw) <= MAX_VERTICES:
            raise ContractError(f"zone {zid}: polygon 要 3–{MAX_VERTICES} 个顶点")
        pts = []
        for p in raw:
            if (not isinstance(p, list) or len(p) != 2 or not all(_num(v) for v in p)
                    or not all(abs(v) <= MAX_COORD_M for v in p)):
                raise ContractError(f"zone {zid}: 顶点要是 [x, y] 两个有限数")
            pts.append((float(p[0]), float(p[1])))
        poly = tuple(pts)
        if polygon_area(poly) < MIN_AREA_M2:
            raise ContractError(f"zone {zid}: 面积小于 {MIN_AREA_M2} m²")
        if not is_simple(poly):
            raise ContractError(f"zone {zid}: 边自交")
        speed = None
        if kind == "slow":
            speed = d.get("max_speed_mps")
            if not _num(speed) or not 0 < speed <= 5:
                raise ContractError(f"zone {zid}: 限速区要 max_speed_mps 在 (0, 5]")
            speed = float(speed)
        elif "max_speed_mps" in d and d["max_speed_mps"] is not None:
            raise ContractError(f"zone {zid}: 禁行区不带限速")
        return cls(id=zid, kind=kind, polygon=poly, label=label, max_speed_mps=speed)


@dataclass(frozen=True)
class ZoneSet:
    map_id: str
    map_version: str
    revision: int
    zones: tuple[Zone, ...] = ()

    def nogo(self) -> tuple[Zone, ...]:
        return tuple(z for z in self.zones if z.kind == "nogo")

    def slow(self) -> tuple[Zone, ...]:
        return tuple(z for z in self.zones if z.kind == "slow")

    def to_wire(self) -> dict[str, Any]:
        return {"map_id": self.map_id, "map_version": self.map_version,
                "revision": self.revision, "zones": [z.to_wire() for z in self.zones]}

    @classmethod
    def from_wire(cls, d: Any) -> ZoneSet:
        if not isinstance(d, dict):
            raise ContractError("zones: 要是对象")
        map_id = check_name(d.get("map_id"), "地图号")
        ver = check_name(d.get("map_version"), "版本")
        rev = d.get("revision")
        if isinstance(rev, bool) or not isinstance(rev, int) or not 0 <= rev <= 2 ** 31:
            raise ContractError("zones: revision 要是非负整数")
        raw = d.get("zones")
        if not isinstance(raw, list) or len(raw) > MAX_ZONES:
            raise ContractError(f"zones: zones 要是 ≤{MAX_ZONES} 个的数组")
        zones = tuple(Zone.from_wire(z) for z in raw)
        ids = [z.id for z in zones]
        if len(set(ids)) != len(ids):
            raise ContractError("zones: id 重复")
        return cls(map_id=map_id, map_version=ver, revision=rev, zones=zones)


def tightens(old: ZoneSet, new: ZoneSet) -> bool:
    """``new`` 相对 ``old`` 是不是**只收紧**:同一个几何版本;旧的禁行区原样都在;旧的限速区同 id、
    同顶点、限速不升;新加的随意。其余(删、挪、升限速、换几何版本)都算放宽。"""
    if (old.map_id, old.map_version) != (new.map_id, new.map_version):
        return False
    by_id = {z.id: z for z in new.zones}
    for z in old.zones:
        n = by_id.get(z.id)
        if n is None or n.kind != z.kind or n.polygon != z.polygon:
            return False
        if z.kind == "slow" and n.max_speed_mps > z.max_speed_mps:  # type: ignore[operator]
            return False
    return True
