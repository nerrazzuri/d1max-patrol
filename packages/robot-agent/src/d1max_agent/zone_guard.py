"""机身轮廓对区域的几何:遥控守硬禁区(决策 65)、规划导航每拍的禁行区回退检查(能力对账 S7)共用。

**机身**:矩形,长 0.93 m、宽 0.48 m(跟避障守卫 :class:`~d1max_agent.obstacles.ObstacleGuard`
同一份数,W10 设计稿「机体尺寸 930 × 480 mm」),狗身中心为原点、x 朝狗头;半长 0.465 m、
外接圆 0.523 m。

**刹车**:命令发出去到狗开始减速有延迟 :data:`LATENCY_S`,再按 :data:`DECEL_MPS2` 减到零;原地转按
:data:`ANG_DECEL_RPS2`。延迟、减速度跟避障守卫同一份**占位数**(0.2 s、0.5 m/s²,待真机量:3f.6、3h.4、
3e.12);角减速度 2 rad/s² 也是占位。

**遥控守硬禁区**(:class:`TeleopZoneGuard`,决策 65):

- 只管**硬禁区**(``hard`` 的禁行区:水体、落差);普通禁行区(花坛)人开的照样能进;没有硬禁区一点不管。
- 这条命令照着开、到下一拍再查(:data:`TICK_S`)再刹停,机身扫过的那一片(按定位 σ + :data:`MARGIN_M`
  加宽)碰不碰硬禁区:不碰照发;碰的话先试慢一点(一半、四分之一,不低于死区)、再试只转不走,
  都不行就零速。往外开、平着开(离硬禁区不更近)照发 —— 已经在边上、甚至在里面的狗要能开出来:
  没压上的按机身到区域的距离,压上了的按压进去的面积不变大、狗身中心不往里走。
- **定位不可信**(没设位置、σ 过线、定位器断了……)而这张图上有硬禁区:不知道狗离水多远,几何判不了;
  不拦(遥控是把狗开回能设位置的地方的唯一办法,拦了狗就困住了),**全程限速**
  (:data:`UNTRUSTED_VX`,不低于死区;转 :data:`UNTRUSTED_WZ`)并告诉操作的人「位置不可信,硬禁区守不了,
  看着画面开」。限速 0.2 m/s 时刹停只要 8 cm。没有硬禁区的图上照常。
- 判出来的结果(``ok`` / ``limited`` / ``blocked`` / ``inside`` / ``untrusted``)随续租回执给站点,
  站点转给手机显示(:meth:`TeleopZoneGuard.note`)。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from d1max_agent.obstacles import ObstacleGuard
from d1max_contract.zones import (
    Zone,
    point_in_polygon,
    polygon_area,
    segments_cross,
    signed_distance,
)

BODY_HL = ObstacleGuard.body_len / 2
BODY_HW = ObstacleGuard.body_wid / 2
#: 机身轮廓外再留这么多(米),跟避障守卫一样。
MARGIN_M = ObstacleGuard.margin
LATENCY_S = ObstacleGuard.latency
DECEL_MPS2 = ObstacleGuard.decel
ANG_DECEL_RPS2 = 2.0
#: 这一拍发出去的命令至少要管到下一拍再查(运行时 0.1 s 一拍)。
TICK_S = 0.1
#: σ 最多算这么多(跟规划加宽禁行区同一条线;σ 过 0.5 m 定位本来就不认,W34)。
MAX_SIGMA_M = 0.5
#: 定位不可信、图上有硬禁区时的限速(m/s、rad/s)。
UNTRUSTED_VX = 0.2
UNTRUSTED_WZ = 0.4
#: 扫过区的采样间距:平移(米)、转(弧度)。外接圆 0.523 m,转 0.08 rad 角点挪 4 cm,小于余量。
STEP_M = 0.05
STEP_RAD = 0.08
_EPS_M = 1e-3
_EPS_A = 1e-4

Pt = tuple[float, float]
Pose = tuple[float, float, float]


# ------------------------------------------------------------ 几何

def footprint(x: float, y: float, yaw: float, hl: float = BODY_HL,
              hw: float = BODY_HW) -> tuple[Pt, Pt, Pt, Pt]:
    """机身矩形的四个角(逆时针)。"""
    c, s = math.cos(yaw), math.sin(yaw)
    return tuple((x + c * lx - s * ly, y + s * lx + c * ly)          # type: ignore[return-value]
                 for lx, ly in ((hl, hw), (-hl, hw), (-hl, -hw), (hl, -hw)))


def stop_distance(v: float) -> float:
    """按 ``v`` 走着、这一刻叫停,还要走多远(含延迟)。"""
    v = abs(v)
    return v * LATENCY_S + v * v / (2.0 * DECEL_MPS2)


def stop_angle(w: float) -> float:
    w = abs(w)
    return w * LATENCY_S + w * w / (2.0 * ANG_DECEL_RPS2)


def _seg_dist(p: Pt, a: Pt, b: Pt) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _in_convex(p: Pt, poly: tuple[Pt, ...]) -> bool:
    """点在逆时针凸多边形里(含边上)。"""
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) < 0:
            return False
    return True


def rect_poly_distance(rect: tuple[Pt, ...], poly: tuple[Pt, ...]) -> float:
    """机身矩形(凸、逆时针)到区域多边形的距离:碰上、压上、套住都是 0。"""
    if any(point_in_polygon(x, y, poly) for x, y in rect) or any(_in_convex(p, rect) for p in poly):
        return 0.0
    n, m = len(poly), len(rect)
    for i in range(m):
        a, b = rect[i], rect[(i + 1) % m]
        for k in range(n):
            if segments_cross(a, b, poly[k], poly[(k + 1) % n]):
                return 0.0
    best = math.inf
    for i in range(m):
        a, b = rect[i], rect[(i + 1) % m]
        for q in poly:
            best = min(best, _seg_dist(q, a, b))
    for k in range(n):
        a, b = poly[k], poly[(k + 1) % n]
        for p in rect:
            best = min(best, _seg_dist(p, a, b))
    return best


def overlap_area(rect: tuple[Pt, ...], poly: tuple[Pt, ...]) -> float:
    """机身矩形跟区域多边形重叠的面积(Sutherland–Hodgman:区域多边形按凸的矩形一条边一条边地裁)。"""
    out = list(poly)
    m = len(rect)
    for i in range(m):
        a, b = rect[i], rect[(i + 1) % m]
        if not out:
            break

        def side(p: Pt, a: Pt = a, b: Pt = b) -> float:
            return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        src, out = out, []
        for k in range(len(src)):
            p, q = src[k], src[(k + 1) % len(src)]
            sp, sq = side(p), side(q)
            if sp >= 0:
                out.append(p)
            if (sp >= 0) != (sq >= 0):
                t = sp / (sp - sq)
                out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return polygon_area(tuple(out)) if len(out) >= 3 else 0.0


def sweep(x: float, y: float, yaw: float, dist: float, ang: float,
          heading: float | None = None) -> list[Pose]:
    """从 ``(x, y, yaw)`` 起、沿等曲率的弧走 ``dist`` 米(带符号:负的往后)、转 ``ang`` 弧度的一串位姿
    (含起点、终点)。``heading``:平移的方向(默认机身朝向;狗尾为前时加 π)。"""
    h0 = yaw if heading is None else heading
    n = max(1, int(math.ceil(abs(dist) / STEP_M)), int(math.ceil(abs(ang) / STEP_RAD)))
    n = min(n, 80)
    out = []
    for i in range(n + 1):
        f = i / n
        s, th = dist * f, ang * f
        if abs(ang) < 1e-9:
            px, py = x + s * math.cos(h0), y + s * math.sin(h0)
        else:
            r = dist / ang
            px = x + r * (math.sin(h0 + th) - math.sin(h0))
            py = y - r * (math.cos(h0 + th) - math.cos(h0))
        out.append((px, py, yaw + th))
    return out


def _bbox(pts: Iterable[Pt]) -> tuple[float, float, float, float]:
    xs, ys = zip(*pts, strict=False)
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True)
class _Boxed:
    zone: Zone
    box: tuple[float, float, float, float]


def boxed(zones: Iterable[Zone]) -> list[_Boxed]:
    return [_Boxed(z, _bbox(z.polygon)) for z in zones]


def _box_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def clearance(pose: Pose, z: _Boxed, cap: float) -> float:
    """机身在 ``pose`` 时离区域多远;包围盒都隔着 ``cap`` 以上就直接回 ``cap``(只关心近的)。"""
    rect = footprint(*pose)
    if _box_gap(_bbox(rect), z.box) >= cap:
        return cap
    return rect_poly_distance(rect, z.zone.polygon)


def sigma_of(est: Any) -> float:
    s = getattr(est, "sigma_xy_m", None)
    if not isinstance(s, (int, float)) or not math.isfinite(s):
        return MAX_SIGMA_M
    return min(max(float(s), 0.0), MAX_SIGMA_M)


# ------------------------------------------------------------ 遥控守硬禁区(决策 65)

@dataclass(frozen=True)
class Verdict:
    vx: float
    wz: float
    #: ``ok``(照发)、``limited``(慢下来了)、``blocked``(往前截了、只许转或者零速)、
    #: ``inside``(已经在硬禁区边上或里面,只许往外开)、``untrusted``(位置不可信,全程限速)。
    state: str
    zone: Zone | None = None
    reason: str = ""


class TeleopZoneGuard:
    """``zones()``:此刻要守的硬禁区;``locate(odom)``:
    ``(位置估计或 None, 可不可信, 为什么不可信)``。"""

    def __init__(self, *, zones: Callable[[], Iterable[Zone]],
                 locate: Callable[[Pose], tuple[Any, bool, str]], deadband_vx: float = 0.0) -> None:
        self._zones = zones
        self._locate = locate
        self._deadband = deadband_vx
        self.last = Verdict(0.0, 0.0, "ok")

    def note(self) -> dict[str, Any]:
        """给站点、手机看的那一份(续租回执里带)。"""
        v = self.last
        out: dict[str, Any] = {"state": v.state}
        if v.zone is not None:
            out["zone"] = v.zone.id
            out["label"] = v.zone.label
        if v.reason:
            out["reason"] = v.reason
        if v.state == "untrusted":
            out["max_vx"] = max(UNTRUSTED_VX, self._deadband)
        return out

    def check(self, vx: float, wz: float, odom: Pose, v_meas: float = 0.0) -> Verdict:
        self.last = v = self._check(vx, wz, odom, v_meas)
        return v

    def _check(self, vx: float, wz: float, odom: Pose, v_meas: float) -> Verdict:
        # 抹掉区(地图台账 44)只有也标了硬的才守,跟普通禁行区一样
        hard = boxed(z for z in self._zones() if z.keep_out and z.hard)
        if not hard:
            return Verdict(vx, wz, "ok")
        est, trusted, why = self._locate(odom)
        if est is None or not trusted:
            cap = max(UNTRUSTED_VX, self._deadband)
            return Verdict(max(-cap, min(cap, vx)), max(-UNTRUSTED_WZ, min(UNTRUSTED_WZ, wz)),
                           "untrusted", None,
                           f"位置不可信({why or '没有位置'}):硬禁区守不了,限速 {cap:.1f} m/s,"
                           "看着画面开")
        pose = (est.x, est.y, est.yaw)
        sigma = sigma_of(est)
        need = sigma + MARGIN_M
        here_hit = None
        for z in hard:
            if clearance(pose, z, need) < need:
                here_hit = z.zone
                break
        if vx == 0.0 and wz == 0.0:
            return Verdict(0.0, 0.0, "inside" if here_hit else "ok", here_hit,
                           _inside_text(here_hit) if here_hit else "")
        cands = [(vx, wz)]
        for k in (0.5, 0.25):
            if abs(vx * k) >= max(self._deadband, 1e-6):
                cands.append((vx * k, wz))
        if vx != 0.0 and wz != 0.0:
            cands.append((0.0, wz))
        first: Zone | None = None
        for i, (cvx, cwz) in enumerate(cands):
            hit = self._hits(pose, need, hard, cvx, cwz, v_meas)
            if hit is None:
                if i == 0:
                    return Verdict(cvx, cwz, "inside" if here_hit else "ok", here_hit,
                                   _inside_text(here_hit) if here_hit else "")
                return Verdict(cvx, cwz, "limited" if cvx != 0.0 else "blocked", first,
                               _block_text(first, cvx != 0.0))
            first = first or hit
        return Verdict(0.0, 0.0, "blocked", first, _block_text(first, False))

    def _hits(self, pose: Pose, need: float, hard: list[_Boxed], vx: float, wz: float,
              v_meas: float) -> Zone | None:
        """这条命令照着开、下一拍再刹停,扫过的那一片会不会更靠近哪个硬禁区(到 ``need`` 以内)。"""
        v = abs(vx)
        if vx != 0.0 and v_meas * vx > 0:
            v = max(v, abs(v_meas))                          # 还在往同一个方向冲:按快的那个算刹车
        dist = math.copysign(v * TICK_S + stop_distance(v), vx) if vx != 0.0 else 0.0
        ang = math.copysign(abs(wz) * TICK_S + stop_angle(wz), wz) if wz != 0.0 else 0.0
        poses = sweep(*pose, dist, ang)
        reach = abs(dist) + 2 * math.hypot(BODY_HL, BODY_HW) + need
        for z in hard:
            if _box_gap((pose[0] - reach, pose[1] - reach, pose[0] + reach, pose[1] + reach),
                        z.box) > 0:
                continue
            c_now = clearance(pose, z, need)
            cs = [clearance(p, z, need) for p in poses[1:]]
            if min(cs, default=need) >= need:
                continue                                     # 扫过的那一片都够远
            if c_now >= need:
                return z.zone                                # 本来够远、这一下就近了
            if c_now > 0.0:
                if min(cs) < c_now - _EPS_M:
                    return z.zone                            # 在边上:不许更近
                continue
            # 压上了(在里面):压进去的面积不许变大;平移时狗身中心不许往里走
            rect = footprint(*pose)
            a_now = overlap_area(rect, z.zone.polygon)
            if any(overlap_area(footprint(*p), z.zone.polygon) > a_now + _EPS_A for p in poses):
                return z.zone
            if dist != 0.0 and signed_distance(poses[-1][0], poses[-1][1], z.zone.polygon) \
                    < signed_distance(pose[0], pose[1], z.zone.polygon) - _EPS_M:
                return z.zone
        return None


def _name(z: Zone | None) -> str:
    return f"「{z.label or z.id}」" if z is not None else ""


def _block_text(z: Zone | None, slowed: bool) -> str:
    if slowed:
        return f"离硬禁区{_name(z)}近了:减速(刹车距离 + 定位偏差)"
    return f"再往前会进硬禁区{_name(z)}:不许往那边开,往外开、转开都行"


def _inside_text(z: Zone | None) -> str:
    return f"狗在硬禁区{_name(z)}边上或里面:只许往外开"
