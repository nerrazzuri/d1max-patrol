"""防区的查看点、值守位,防区路线自动生成(W73 第一期,设计 ``docs/W73-布防值守-设计.md`` §4)。

- **查看点**(出事时机器人去哪看)、**值守位**(布防空档站哪儿盯,带朝向):圈好的防区里站点自动算一个 ——
  防区里机器人**站得下**(规划栅格上不在墙、障碍、禁行区和它们的致命带里)、**定位可靠**(离建图时走过的
  地方不远,``maps.reach_problem``)、**从待命点 / 原点走得到**的格子里,离防区形心最近的那一格。值守位
  默认跟查看点同一处,朝着防区形心(离形心不到 1 m 时朝防区最远的那个角,看得最开)。人挪过就按人的
  (``zone_posts`` 一行),可以重置回自动。
- **防区路线生成**(:func:`generate`):每个防区里按 ``spacing_m`` 撒网格点,每个网格点吸到附近能用的
  格子上;防区之间、防区里的点按最近邻排、再两两交换改进(开放路径,从待命点出发);每个防区可以多转
  几圈(每圈换个方向)、每点停几秒、拍几张(朝防区里面)。生成的只是草稿:交给路线编辑器,存的时候照样
  逐点、逐段检查。

「走得到」按每张图每版一份的**连通区域**判(从所有待命点、原点出发 8 连通能走到的格子;站点规划同一份
代价图),不按每个点跑一次规划 —— 大图上规划是纯 Python,一个防区几十个候选点挨个规划太慢。
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any

from d1max_site.areas import inside

#: 生成路线时网格间距的范围(米)和默认值(设计 §4.2:5–8 m,我定的 6 m)。
SPACING_DEFAULT_M = 6.0
SPACING_MIN_M, SPACING_MAX_M = 1.0, 30.0
MAX_LAPS = 10
MAX_DWELL_S = 600
MAX_PHOTOS = 5
#: 两张照片之间停一下(同一处连拍,隔一秒才是两张不同的画面)。
PHOTO_GAP_S = 1.0
#: 吸附:网格点附近多远以内找能用的格子(网格间距的一半,最多这么多米)。
SNAP_MAX_M = 3.0
#: 一个网格点最多试几个候选格子(过定位可靠那一关);自动选查看点时最多试这么多。
SNAP_TRIES = 40
AUTO_TRIES = 3000
#: 值守位离防区形心近于这个,就不朝形心(朝什么都差不多),朝最远的角。
FACE_NEAR_M = 1.0
_CACHE = 4


class GuardPlanError(ValueError):
    """参数不对、防区不存在、点放不下(接口回 400)。"""


def _centroid(poly: list[list[float]]) -> tuple[float, float]:
    a = cx = cy = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i - 1]
        x1, y1 = poly[i]
        k = x0 * y1 - x1 * y0
        a += k
        cx += (x0 + x1) * k
        cy += (y0 + y1) * k
    if abs(a) < 1e-9:
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)
    return (cx / (3 * a), cy / (3 * a))


def _face(x: float, y: float, poly: list[list[float]]) -> float:
    cx, cy = _centroid(poly)
    if math.hypot(cx - x, cy - y) >= FACE_NEAR_M:
        return round(math.atan2(cy - y, cx - x), 4)
    fx, fy = max(poly, key=lambda p: math.hypot(p[0] - x, p[1] - y))
    return round(math.atan2(fy - y, fx - x), 4)


class _Ground:
    """一张图一版上「机器人站得下、走得到」的格子(连通区域)。"""

    def __init__(self, cm: Any, starts: list[tuple[float, float]]) -> None:
        import numpy as np
        self.cm = cm
        free = ~cm.lethal & ~cm.hard
        self.free = free
        if not starts:
            self.ok = free                       # 没有待命点 / 原点:只要站得下
            self.no_starts = True
            return
        self.no_starts = False
        from d1max_site.intercept_reach import snap
        h, w = free.shape
        seen = np.zeros((h, w), dtype=bool)
        q: deque[tuple[int, int]] = deque()
        for x, y in starts:
            s = snap(cm, cm.cell_of(x, y))
            if s is not None and not seen[s]:
                seen[s] = True
                q.append(s)
        flat = free.tobytes()                    # 逐格访问 bytes 比 numpy 下标快得多
        mark = bytearray(seen.tobytes())
        while q:
            r, c = q.popleft()
            for dr in (-1, 0, 1):
                rr = r + dr
                if rr < 0 or rr >= h:
                    continue
                base = rr * w
                for dc in (-1, 0, 1):
                    cc = c + dc
                    if cc < 0 or cc >= w:
                        continue
                    i = base + cc
                    if flat[i] and not mark[i]:
                        mark[i] = 1
                        q.append((rr, cc))
        self.ok = np.frombuffer(bytes(mark), dtype=bool).reshape(h, w)


class ZonePosts:
    def __init__(self, db: Any, maps: Any, reach: Any, areas: Any, *, now_ms: Any) -> None:
        self.db = db
        self.maps = maps
        self.reach = reach
        self.areas = areas
        self._now = now_ms
        self._lock = threading.Lock()
        self._grounds: dict[tuple[str, str, int, str], _Ground | None] = {}

    # ------------------------------------------------------------ 地面

    def ground(self, map_id: str, version: str) -> _Ground | None:
        """这一版的「站得下、走得到」;没有规划栅格(老图、测试台子)是 None。"""
        if self.reach is None:
            return None
        try:
            cm = self.reach.costmap(map_id, version)
        except Exception:                                # noqa: BLE001 - 不认识的图、栅格坏了
            return None
        if cm is None:
            return None
        starts = [(x, y) for _, x, y in self.reach.starts(map_id, version)]
        k = (map_id, version, id(cm), ";".join(f"{x:.2f},{y:.2f}" for x, y in starts))
        with self._lock:
            if k in self._grounds:
                return self._grounds[k]
        g = _Ground(cm, starts)
        with self._lock:
            self._grounds[k] = g
            while len(self._grounds) > _CACHE:
                self._grounds.pop(next(iter(self._grounds)))
        return g

    def _usable(self, map_id: str, version: str, x: float, y: float,
                g: _Ground | None) -> str:
        """这一点能不能当机器人站的位置:能回 ``""``,不能回原因。"""
        why = self.maps.reach_problem(map_id, version, [("这个点", x, y)]) \
            if self.maps is not None else ""
        if why:
            return why
        if g is None:
            return ""
        rc = g.cm.cell_of(x, y)
        if rc is None:
            return "这个点在地图外面"
        if not g.free[rc]:
            return "这个点在墙、障碍、禁行区里,或离它们太近(机器人站不下)"
        if not g.ok[rc]:
            return "这个点从待命点、原点走不到"
        return ""

    def pick(self, map_id: str, version: str, poly: list[list[float]],
             near: tuple[float, float], *, radius_m: float | None = None,
             g: _Ground | None = None) -> tuple[tuple[float, float] | None, str]:
        """防区 ``poly`` 里离 ``near`` 最近的能用的点(``radius_m`` 以内;None = 整个防区)。"""
        import numpy as np

        from d1max_contract.planning import costmap
        g = g if g is not None else self.ground(map_id, version)
        if g is None:
            # 没有规划栅格:按定位可靠那一关挑网格上的点(粗一点)
            if inside(near[0], near[1], poly) and not self._usable(map_id, version, *near, None):
                return near, ""
            return None, "此版本地图没有规划栅格,无法自动选点"
        cm = g.cm
        mask = costmap.polygon_mask(tuple((p[0], p[1]) for p in poly), cm.shape, float(cm.res),
                                    cm.origin) & g.ok
        rr, cc = np.nonzero(mask)
        if len(rr) == 0:
            if not (costmap.polygon_mask(tuple((p[0], p[1]) for p in poly), cm.shape,
                                         float(cm.res), cm.origin) & g.free).any():
                return None, "防区里没有机器人站得下的地方"
            return None, "防区里站得下的地方从待命点、原点都走不到"
        res = float(cm.res)
        xs = cm.origin[0] + (cc + 0.5) * res
        ys = cm.origin[1] + (rr + 0.5) * res
        d2 = (xs - near[0]) ** 2 + (ys - near[1]) ** 2
        if radius_m is not None:
            keep = d2 <= radius_m * radius_m
            xs, ys, d2 = xs[keep], ys[keep], d2[keep]
        order = np.argsort(d2, kind="stable")[:SNAP_TRIES if radius_m is not None else AUTO_TRIES]
        for j in order:
            x, y = round(float(xs[j]), 2), round(float(ys[j]), 2)
            if not inside(x, y, poly):
                continue
            if not self.maps.reach_problem(map_id, version, [("p", x, y)]):
                return (x, y), ""
        return None, "防区里站得下的地方离建图时走过的路都太远(定位不可靠)"

    # ------------------------------------------------------------ 查看点、值守位

    def auto(self, map_id: str, version: str, poly: list[list[float]]
             ) -> tuple[dict[str, float] | None, dict[str, float] | None, str]:
        p, why = self.pick(map_id, version, poly, _centroid(poly))
        if p is None:
            return None, None, why
        return ({"x": p[0], "y": p[1]}, {"x": p[0], "y": p[1], "yaw": _face(p[0], p[1], poly)},
                "")

    def view(self, map_id: str, version: str) -> list[dict[str, Any]]:
        """这一版上每个防区的查看点、值守位(``auto``:站点算的;``False``:人挪过)。"""
        rows = {r["zone"]: r for r in self.db.query(
            "SELECT * FROM zone_posts WHERE map_id=? AND map_version=?", (map_id, version))}
        out = []
        for a in self.areas.list(map_id, version):
            r = rows.get(a["zone"])
            if r is not None:
                out.append({"zone": a["zone"], "auto": False,
                            "view": {"x": r["view_x"], "y": r["view_y"]},
                            "watch": {"x": r["watch_x"], "y": r["watch_y"], "yaw": r["watch_yaw"]},
                            "updated_by": r["updated_by"], "note": ""})
                continue
            v, w, why = self.auto(map_id, version, a["points"])
            out.append({"zone": a["zone"], "auto": True, "view": v, "watch": w,
                        "updated_by": "", "note": why})
        return out

    def _area(self, map_id: str, version: str, zone: str) -> list[list[float]]:
        for a in self.areas.list(map_id, version):
            if a["zone"] == zone:
                return a["points"]
        raise GuardPlanError(f"此地图上没有防区 {zone!r}")

    def set(self, map_id: str, version: str, zone: str, *, view: Any, watch: Any,
            by: str) -> None:
        poly = self._area(map_id, version, zone)
        pts = []
        for label, p, keys in (("查看点", view, ("x", "y")), ("值守位", watch, ("x", "y", "yaw"))):
            if not isinstance(p, dict) or not all(
                    isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)
                    and math.isfinite(p[k]) for k in keys):
                raise GuardPlanError(f"{label}要是 {{{', '.join(keys)}}} 几个数")
            x, y = float(p["x"]), float(p["y"])
            if not inside(x, y, poly):
                raise GuardPlanError(f"{label}在防区外面:要放在 {zone} 里")
            why = self._usable(map_id, version, x, y, self.ground(map_id, version))
            if why:
                raise GuardPlanError(f"{label}放不了:{why.replace('这个点', '')}")
            pts.append((round(x, 3), round(y, 3)))
        yaw = round(math.remainder(float(watch["yaw"]), math.tau), 4)
        with self.db.tx() as c:
            c.execute("INSERT INTO zone_posts(map_id, map_version, zone, view_x, view_y, watch_x, "
                      "watch_y, watch_yaw, updated_by, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(map_id, map_version, zone) DO UPDATE SET "
                      "view_x=excluded.view_x, view_y=excluded.view_y, watch_x=excluded.watch_x, "
                      "watch_y=excluded.watch_y, watch_yaw=excluded.watch_yaw, "
                      "updated_by=excluded.updated_by, updated_ms=excluded.updated_ms",
                      (map_id, version, zone, *pts[0], *pts[1], yaw, by, self._now()))

    def reset(self, map_id: str, version: str, zone: str) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM zone_posts WHERE map_id=? AND map_version=? AND zone=?",
                             (map_id, version, zone)).rowcount > 0


# ------------------------------------------------------------ 防区路线生成


def _num(v: Any, name: str, lo: float, hi: float, default: float, *, integer: bool) -> float:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or (integer and int(v) != v) or not lo <= v <= hi:
        raise GuardPlanError(f"{name} 要是 {lo:g}–{hi:g} 的{'整数' if integer else '数'}:{v!r}")
    return int(v) if integer else float(v)


def _order(points: list[tuple[float, float]], start: tuple[float, float]
           ) -> list[tuple[float, float]]:
    """开放路径:从 ``start`` 最近邻排一遍,再两两交换(2-opt)改到不再变短。"""
    left = list(points)
    out: list[tuple[float, float]] = []
    cur = start
    while left:
        j = min(range(len(left)), key=lambda k: math.hypot(left[k][0] - cur[0],
                                                           left[k][1] - cur[1]))
        cur = left.pop(j)
        out.append(cur)

    def d(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])
    seq = [start, *out]
    better = True
    rounds = 0
    while better and rounds < 50:
        better = False
        rounds += 1
        for i in range(1, len(seq) - 1):
            for k in range(i + 1, len(seq)):
                a, b = seq[i - 1], seq[i]
                c = seq[k]
                nxt = seq[k + 1] if k + 1 < len(seq) else None
                old = d(a, b) + (d(c, nxt) if nxt else 0.0)
                new = d(a, c) + (d(b, nxt) if nxt else 0.0)
                if new < old - 1e-9:
                    seq[i:k + 1] = reversed(seq[i:k + 1])
                    better = True
    return seq[1:]


def _zone_points(posts: ZonePosts, map_id: str, version: str, poly: list[list[float]],
                 spacing: float, g: _Ground | None) -> tuple[list[tuple[float, float]], str]:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    # 按防区外框平均分格(格子边长约等于间距),每格中心一个网格点
    ex, ey = max(xs) - min(xs), max(ys) - min(ys)
    nx, ny = max(1, int(ex / spacing + 0.5)), max(1, int(ey / spacing + 0.5))
    sx, sy = ex / nx, ey / ny
    grid = [(min(xs) + (i + 0.5) * sx, min(ys) + (j + 0.5) * sy)
            for i in range(nx) for j in range(ny)]
    step = min(sx, sy)
    radius = min(SNAP_MAX_M, step / 2)
    got: list[tuple[float, float]] = []
    for gx, gy in grid:
        if not inside(gx, gy, poly):
            continue
        p, _ = posts.pick(map_id, version, poly, (gx, gy), radius_m=radius, g=g)
        if p is not None and all(math.hypot(p[0] - q[0], p[1] - q[1]) > step / 3 for q in got):
            got.append(p)
    if got:
        return got, ""
    p, why = posts.pick(map_id, version, poly, _centroid(poly), g=g)   # 防区小:至少放一个
    return ([p], "") if p is not None else ([], why)


def generate(maps: Any, reach: Any, areas: Any, posts: ZonePosts, map_id: str, version: str,
             zones: Any, *, spacing_m: Any = None, start: Any = None,
             store: Any = None) -> dict[str, Any]:
    """按防区生成一条防区路线的草稿:``{"waypoints": [平的点], "zones": [{zone, points, note}]}``。

    给了 ``store``(:class:`d1max_site.routes.RouteStore`)就按存路线的同一套规矩逐点、逐段查一遍,
    结果在 ``problems``(跟存路线时报的一样)。

    ``zones``:``[{zone, laps?, dwell_s?, photos?}]``;``start``:``{x, y}``(不给用这张图的第一个待命点
    / 原点;都没有用第一个防区的形心)。"""
    from d1max_site.routes import MAX_WAYPOINTS
    if not isinstance(zones, list) or not zones:
        raise GuardPlanError("至少选一个防区")
    spacing = _num(spacing_m, "spacing_m", SPACING_MIN_M, SPACING_MAX_M, SPACING_DEFAULT_M,
                   integer=False)
    polys = {a["zone"]: a["points"] for a in areas.list(map_id, version)}
    specs = []
    for z in zones:
        if not isinstance(z, dict) or not isinstance(z.get("zone"), str):
            raise GuardPlanError("每个防区要写成 {zone, laps?, dwell_s?, photos?}")
        if z["zone"] not in polys:
            raise GuardPlanError(f"此地图上没有防区 {z['zone']!r}:先在「防区」里圈出来")
        specs.append({"zone": z["zone"],
                      "laps": _num(z.get("laps"), "laps", 1, MAX_LAPS, 1, integer=True),
                      "dwell_s": _num(z.get("dwell_s"), "dwell_s", 0, MAX_DWELL_S, 0,
                                      integer=False),
                      "photos": _num(z.get("photos"), "photos", 0, MAX_PHOTOS, 1, integer=True)})
    if len({s["zone"] for s in specs}) != len(specs):
        raise GuardPlanError("同一个防区写了两遍:多转几圈用 laps")
    if isinstance(start, dict) and all(isinstance(start.get(k), (int, float)) for k in "xy"):
        cur = (float(start["x"]), float(start["y"]))
    else:
        st = reach.starts(map_id, version) if reach is not None else []
        cur = (st[0][1], st[0][2]) if st else _centroid(polys[specs[0]["zone"]])
    g = posts.ground(map_id, version)

    found: dict[str, list[tuple[float, float]]] = {}
    report: dict[str, dict[str, Any]] = {}
    for s in specs:
        pts, why = _zone_points(posts, map_id, version, polys[s["zone"]], spacing, g)
        found[s["zone"]] = pts
        report[s["zone"]] = {"zone": s["zone"], "points": len(pts), "note": why}
    # 防区的顺序:从起点按形心最近邻(走不到的防区没有点,不参与)
    todo = [s for s in specs if found[s["zone"]]]
    ordered = []
    here = cur
    while todo:
        k = min(range(len(todo)), key=lambda i: math.hypot(
            _centroid(polys[todo[i]["zone"]])[0] - here[0],
            _centroid(polys[todo[i]["zone"]])[1] - here[1]))
        s = todo.pop(k)
        seq = _order(found[s["zone"]], here)
        ordered.append((s, seq))
        here = seq[-1]

    waypoints: list[dict[str, Any]] = []
    for s, seq in ordered:
        poly = polys[s["zone"]]
        for lap in range(1, s["laps"] + 1):
            pts = seq if lap % 2 == 1 else seq[::-1]
            for i, (x, y) in enumerate(pts):
                n = seq.index((x, y)) + 1
                acts: list[dict[str, Any]] = []
                if s["dwell_s"]:
                    acts.append({"type": "dwell", "seconds": s["dwell_s"]})
                for k in range(s["photos"]):
                    if k:
                        acts.append({"type": "dwell", "seconds": PHOTO_GAP_S})
                    acts.append({"type": "photo", "camera": "front"})
                yaw = _face(x, y, poly)
                if math.hypot(*(a - b for a, b in zip(_centroid(poly), (x, y), strict=True))) \
                        < FACE_NEAR_M and i + 1 < len(pts):
                    yaw = round(math.atan2(pts[i + 1][1] - y, pts[i + 1][0] - x), 4)
                w = {"name": f"{s['zone']}-{n}" + (f"·{lap}" if lap > 1 else ""),
                     "x": x, "y": y, "yaw": yaw}
                if acts:
                    w["actions"] = acts
                waypoints.append(w)
    if len(waypoints) > MAX_WAYPOINTS:
        raise GuardPlanError(f"生成了 {len(waypoints)} 个点,一条路线最多 {MAX_WAYPOINTS} 个:"
                             "加大间距、少转几圈,或者分成几条路线")
    problems = store.check_flat(map_id, version, waypoints) if store is not None and waypoints \
        else []
    return {"waypoints": waypoints, "zones": [report[s["zone"]] for s in specs],
            "problems": problems,
            "spacing_m": spacing, "start": {"x": cur[0], "y": cur[1]}}
