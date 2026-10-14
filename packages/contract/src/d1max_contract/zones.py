"""禁行区与限速区(W10,W08 决定 5):挂在几何版本(``map_id + map_version``)上的一份区域集。

- 区域是地图系里的多边形:``nogo`` 当障碍(规划不许进、狗在里面就停),``slow`` 限速
  (``max_speed_mps``),``erase`` 抹掉区(对狗跟 ``nogo`` 一样,见最后一节)。
- 区域集有自己的**修订号**(从 1 起,改一次 +1);站点是唯一权威,经命令 ``zones_set`` 整份下发。
- **只收紧**(:func:`tightens`)的改动可以热更;放宽的等狗空闲再换(设计稿「与 W08 字面不同」)。
- 不进地图产物:建图不管它,站点单独存。

决策 65、66(2026-10-14)加的两个**可选**字段(线上只在用到时才出现,老的区域集一字不差):

- ``hard``(只给禁行区、抹掉区):**硬禁区** —— 水体、落差那一类,遥控也不许开进去(狗上按机身轮廓 +
  定位 σ + 刹车距离挡,见 ``d1max_agent.zone_guard``)。没有 = 普通禁行区(花坛之类,人遥控可以进)。
  老狗不认这个键,照普通禁行区守(规划照样绕);能力里 ``zones_set.teleop_hard`` 为真的狗才在遥控时守。
- ``until_ms``:**临时区**的到期时刻(站点的钟,毫秒)。到期由**站点**删(新修订、照常下发);狗不按自己的
  钟删(狗的钟可能是错的;过期了还守着是往安全那边错)。

**收紧**按几何判(:func:`tightens`):旧的每个区域都要被新的**某一个**区域整个盖住、而且不比它松
(禁行盖禁行或限速,硬的要硬的盖,限速不升,到期不提前)。按单个区域盖,不算并集:把一个禁行区拆成两半
保守地算放宽(要人确认)。

**抹掉区** ``erase``(地图台账 44,用户 2026-10-14「在图上框一块抹掉」):

- 对狗跟禁行区**一模一样**:规划当障碍、每拍回退检查、狗在里面就停(:attr:`Zone.keep_out`、
  :meth:`ZoneSet.nogo` 都把它算进去);站点画图时把那一块抹成「没建图」(墙也不画)—— 那是建图时的
  杂物、玻璃反射、不该去的地方。
- 遥控(决策 65):跟普通禁行区一样**不守**,除非也标了 ``hard``(那就跟硬禁区一样守)。
- 收紧 / 放宽(决策 66):禁行、抹掉都是「狗不去」,互相盖得住就算不比它松;加抹掉区是收紧,删、缩是
  放宽。
- **老代理不认这个种类**(会把整份区域集拒掉,规划照旧用老的、站点补发不停、派单一直拒):站点发给能力里
  没有 ``zones_set.erase`` 的狗时换成同一块禁行区(:meth:`ZoneSet.erase_as_nogo`)—— 对狗一样严,不加
  兼容级别(级别只在「两头都要会」时加,这里站点一个人就兜住了)。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.maps import check_name

KINDS = ("nogo", "slow", "erase")
#: 狗不许进的种类:禁行、抹掉(规划、每拍回退、狗在里面就停,两样一模一样)。
KEEP_OUT = ("nogo", "erase")
MAX_ZONES = 200
MAX_VERTICES = 64
MIN_AREA_M2 = 0.01
#: 坐标绝对值上限(米):地图系里的点,庄园一公里以内;挡住离谱值。
MAX_COORD_M = 100_000.0
MAX_LABEL = 64
#: 到期时刻(毫秒)的上限:挡住离谱值(秒当毫秒发会落在 1970 年,站点按「已经过了」拒)。
MAX_UNTIL_MS = 2 ** 53
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
    """多边形的边不自交:不相邻的边没有公共点(相邻的边共用一个顶点,不查)。顶点数上限 64,
    两两查就够。共线折回、重复顶点不用单独查:折回去的那一端必然落在不相邻的边上,三个点的
    退化形状面积为 0、另有面积那一关挡。"""
    n = len(pts)
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue                          # 首尾两条边相邻
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


def boundary_distance(x: float, y: float, pts: tuple[tuple[float, float], ...]) -> float:
    """点到多边形**边**的距离(在里面、外面都按边量)。"""
    best = math.inf
    n = len(pts)
    for i in range(n):
        (ax, ay), (bx, by) = pts[i], pts[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


def distance_to_polygon(x: float, y: float, pts: tuple[tuple[float, float], ...]) -> float:
    """点到多边形的距离:在里面是 0。"""
    if point_in_polygon(x, y, pts):
        return 0.0
    return boundary_distance(x, y, pts)


def signed_distance(x: float, y: float, pts: tuple[tuple[float, float], ...]) -> float:
    """点到多边形边的有向距离:外面为正、里面为负(压在边上是 0)。"""
    d = boundary_distance(x, y, pts)
    return -d if point_in_polygon(x, y, pts) else d


#: 判「严格在里面」时离边至少这么远(米):压在边上、浮点差一点点的都不算在里面(往「没盖住」那边判)。
_EPS_M = 1e-9


def _strictly_inside(x: float, y: float, pts: tuple[tuple[float, float], ...]) -> bool:
    return point_in_polygon(x, y, pts) and boundary_distance(x, y, pts) > _EPS_M


def _interior_point(pts: tuple[tuple[float, float], ...]) -> tuple[float, float] | None:
    """简单多边形里面的一个点(O'Rourke:最下最左那个顶点是凸的;它跟两个邻点的三角形里没有别的顶点
    就取三角形重心,有就取离那条底边最远的那个顶点跟它的中点)。浮点上不放心的回 ``None``。"""
    n = len(pts)
    i = min(range(n), key=lambda k: (pts[k][1], pts[k][0]))
    v, a, b = pts[i], pts[i - 1], pts[(i + 1) % n]
    tri = (a, v, b)
    inside = [p for k, p in enumerate(pts) if k not in (i, (i - 1) % n, (i + 1) % n)
              and point_in_polygon(p[0], p[1], tri)]
    if inside:
        def far(p: tuple[float, float]) -> float:
            return abs(_orient(a, b, p))
        q = max(inside, key=far)
        c = ((v[0] + q[0]) / 2.0, (v[1] + q[1]) / 2.0)
    else:
        c = ((a[0] + v[0] + b[0]) / 3.0, (a[1] + v[1] + b[1]) / 3.0)
    return c if _strictly_inside(c[0], c[1], pts) else None


def _cut_params(a, b, poly: tuple[tuple[float, float], ...]) -> list[float]:
    """线段 ab 跟多边形各条边的交点在 ab 上的参数 t(含共线重叠的两端),加上 0、1,排好。"""
    ts = {0.0, 1.0}
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    n = len(poly)
    for k in range(n):
        c, d = poly[k], poly[(k + 1) % n]
        ex, ey = d[0] - c[0], d[1] - c[1]
        den = dx * ey - dy * ex
        if den != 0:
            t = ((c[0] - a[0]) * ey - (c[1] - a[1]) * ex) / den
            u = ((c[0] - a[0]) * dy - (c[1] - a[1]) * dx) / den
            if -1e-12 <= t <= 1 + 1e-12 and -1e-12 <= u <= 1 + 1e-12:
                ts.add(min(1.0, max(0.0, t)))
        elif L2 > 0 and _orient(a, b, c) == 0:
            for p in (c, d):                              # 共线:两端投影到 ab 上
                t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2
                if 0.0 < t < 1.0:
                    ts.add(t)
    return sorted(ts)


def polygon_contains(outer: tuple[tuple[float, float], ...],
                     inner: tuple[tuple[float, float], ...]) -> bool:
    """``inner`` 整个在 ``outer`` 里面(边上算里面)。**宁可错说「没盖住」**(那样只是要人多确认一次):

    1. ``inner`` 的顶点都在 ``outer`` 里(含边上);
    2. ``outer`` 的边一段都不穿过 ``inner`` 的**内部**(按跟 ``inner`` 各边的交点切段、每段中点
       严格在 ``inner`` 里面就是穿过了)—— 只看顶点、只看真交叉都判不出凹口、夹缝从边上插进来;
    3. ``inner`` 里面取一个点,在 ``outer`` 里。

    有了 2,``inner`` 的内部跟 ``outer`` 的边不相交,要么整个在里面、要么整个在外面,3 定是哪个。
    (1 是 2 + 3 推得出来的,只是先便宜地筛掉大多数「没盖住」—— 去掉它结果不变。)"""
    if outer == inner:
        return True
    if not all(point_in_polygon(x, y, outer) for x, y in inner):
        return False
    n = len(outer)
    for k in range(n):
        a, b = outer[k], outer[(k + 1) % n]
        ts = _cut_params(a, b, inner)
        for t0, t1 in zip(ts, ts[1:], strict=False):
            if t1 - t0 <= 1e-12:
                continue
            tm = (t0 + t1) / 2.0
            if _strictly_inside(a[0] + tm * (b[0] - a[0]), a[1] + tm * (b[1] - a[1]), inner):
                return False
    c = _interior_point(inner)
    return c is not None and point_in_polygon(c[0], c[1], outer)


def _bbox(pts: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True)
class Zone:
    id: str
    kind: str
    polygon: tuple[tuple[float, float], ...]
    label: str = ""
    max_speed_mps: float | None = None
    #: 硬禁区(决策 65,只给禁行区、抹掉区):遥控也守。
    hard: bool = False
    #: 临时区的到期时刻(站点的钟,毫秒;决策 66);``None`` = 一直有效。
    until_ms: int | None = None

    def to_wire(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "kind": self.kind, "label": self.label,
                             "polygon": [[x, y] for x, y in self.polygon]}
        if self.kind == "slow":
            d["max_speed_mps"] = self.max_speed_mps
        if self.hard:
            d["hard"] = True                  # 不是硬的不写:老的区域集线上一字不差
        if self.until_ms is not None:
            d["until_ms"] = self.until_ms
        return d

    def expired(self, now_ms: int) -> bool:
        return self.until_ms is not None and self.until_ms <= now_ms

    @property
    def keep_out(self) -> bool:
        """狗不许进(禁行、抹掉)。"""
        return self.kind in KEEP_OUT

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
            raise ContractError(f"zone {zid}: 禁行区、抹掉区不带限速")
        hard = d.get("hard", False)
        if not isinstance(hard, bool):
            raise ContractError(f"zone {zid}: hard 要是 true/false")
        if hard and kind not in KEEP_OUT:
            raise ContractError(f"zone {zid}: 只有禁行区、抹掉区能是硬禁区")
        until = d.get("until_ms")
        if until is not None and (isinstance(until, bool) or not isinstance(until, int)
                                  or not 0 < until <= MAX_UNTIL_MS):
            raise ContractError(f"zone {zid}: until_ms 要是正整数(毫秒)")
        return cls(id=zid, kind=kind, polygon=poly, label=label, max_speed_mps=speed,
                   hard=hard, until_ms=until)


@dataclass(frozen=True)
class ZoneSet:
    map_id: str
    map_version: str
    revision: int
    zones: tuple[Zone, ...] = ()

    def nogo(self) -> tuple[Zone, ...]:
        """狗不许进的:禁行区 + 抹掉区。"""
        return tuple(z for z in self.zones if z.keep_out)

    def slow(self) -> tuple[Zone, ...]:
        return tuple(z for z in self.zones if z.kind == "slow")

    def erase(self) -> tuple[Zone, ...]:
        """抹掉区:站点画图时抹成没建图的那几块。"""
        return tuple(z for z in self.zones if z.kind == "erase")

    def hard(self) -> tuple[Zone, ...]:
        """硬禁区(决策 65):遥控也守的那几个(禁行、抹掉都可以标)。"""
        return tuple(z for z in self.zones if z.keep_out and z.hard)

    def erase_as_nogo(self) -> ZoneSet:
        """发给不认 ``erase`` 的老代理的那一份:抹掉区换成同一块禁行区(id、名字、几何、硬、到期不变),
        修订号不变。没有抹掉区就是自己(线上一字不差)。"""
        if not any(z.kind == "erase" for z in self.zones):
            return self
        return ZoneSet(self.map_id, self.map_version, self.revision, tuple(
            replace(z, kind="nogo") if z.kind == "erase" else z for z in self.zones))

    def without_expired(self, now_ms: int) -> ZoneSet:
        """去掉到期了的(修订号不动;站点删的时候另给新修订)。"""
        keep = tuple(z for z in self.zones if not z.expired(now_ms))
        return self if len(keep) == len(self.zones) else ZoneSet(
            self.map_id, self.map_version, self.revision, keep)

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


def _as_strict(new: Zone, old: Zone) -> bool:
    """``new`` 这个区域的规矩不比 ``old`` 松(不看几何)。"""
    if not (new.until_ms is None or (old.until_ms is not None and new.until_ms >= old.until_ms)):
        return False                                        # 到期提前了(含永久改临时)
    if old.keep_out:                                        # 禁行、抹掉:对狗一样严
        return new.keep_out and (new.hard or not old.hard)
    if new.keep_out:
        return True                                         # 禁行比任何限速都严
    return new.max_speed_mps <= old.max_speed_mps           # type: ignore[operator]


def covered(old: Zone, zones: tuple[Zone, ...]) -> bool:
    """``old`` 被 ``zones`` 里**某一个**不比它松的区域整个盖住(决策 66;不算并集)。"""
    ob = _bbox(old.polygon)
    for n in zones:
        if not _as_strict(n, old):
            continue
        if n.polygon == old.polygon:
            return True
        nb = _bbox(n.polygon)
        if nb[0] > ob[0] or nb[1] > ob[1] or nb[2] < ob[2] or nb[3] < ob[3]:
            continue                                        # 包围盒都盖不住
        if polygon_contains(n.polygon, old.polygon):
            return True
    return False


def tightens(old: ZoneSet, new: ZoneSet, *, now_ms: int | None = None) -> bool:
    """``new`` 相对 ``old`` 是不是**只收紧**(决策 66):同一个几何版本;``old`` 的每个区域都被 ``new``
    里某一个不比它松的区域整个盖住(:func:`covered`;放大、换 id、加硬禁区标记、限速调低、到期推后都算
    收紧)。其余(删、缩、挪、升限速、去掉硬禁区标记、到期提前、换几何版本)都算放宽;又紧又松的算放宽。
    ``now_ms`` 给了的话,``old`` 里到期了的不用再盖(站点会删它)。"""
    if (old.map_id, old.map_version) != (new.map_id, new.map_version):
        return False
    return all(covered(z, new.zones) for z in old.zones
               if now_ms is None or not z.expired(now_ms))
