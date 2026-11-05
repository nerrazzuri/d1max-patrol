"""地图上的出口线、易藏点(W74 三期,设计 ``docs/W74-看见人处置-设计.md`` §六、§七)。

- **出口线**(``exit``):一段线(两个点),比如大门、通道口。被跟的人从线这边走到那边 = 离开值守区。
  不按地图边界判(玻璃墙、门、没扫到的地方都会被当成边界)。
- **易藏点**(``hide``):一个点加朝向 —— 机器人站在这里、朝这个方向看(楼梯间、卫生间门口、货架后面)。
  跟丢以后去附近的看一圈;加密巡查时先看。

按图、按版存(``map_marks``),跟防区一样换了版要重画。
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from typing import Any

KINDS = ("exit", "hide")
#: 每张图每版最多这么多个标注。
MAX_MARKS = 200
NAME_MAX = 40
#: 出口线长度范围(米)。
EXIT_MIN_M, EXIT_MAX_M = 0.5, 60.0
#: 跟丢以后看多远以内的易藏点(米,设计 §六,我定的)、最多看几个。
#: 离出口线这么近(米)算「在线上」:不算哪一边(定位、测距误差以内;外审 r1 #12)。
LINE_BAND_M = 0.3
HIDE_NEAR_M = 30.0
HIDE_MAX = 6
#: 人越过出口线以后,机器人停在线内多远(米,设计 §七)。
INSIDE_M = 3.0


class MarkError(ValueError):
    pass


class MapMarks:
    def __init__(self, db: Any, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def list(self, map_id: str, version: str, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM map_marks WHERE map_id=? AND map_version=? "
                             "ORDER BY kind, name, id", (map_id, version))
        out = [{"id": r["id"], "kind": r["kind"], "name": r["name"],
                "points": json.loads(r["points"]), "yaw": r["yaw"],
                "updated_by": r["updated_by"], "updated_ms": r["updated_ms"]} for r in rows]
        return [m for m in out if kind is None or m["kind"] == kind]

    def set(self, map_id: str, version: str, mark: Any, *, by: str) -> dict[str, Any]:
        """新建(不带 ``id``)或改(带这张图上已有的 ``id``)。回存下的那一条。"""
        if not isinstance(mark, dict):
            raise MarkError("标注要是 {kind, name, points[, yaw, id]}")
        kind = mark.get("kind")
        if kind not in KINDS:
            raise MarkError(f"标注类型只认 exit(出口线)、hide(易藏点):{kind!r}")
        name = mark.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
            raise MarkError(f"名字要 1–{NAME_MAX} 个字")
        pts = _points(mark.get("points"), 2 if kind == "exit" else 1)
        if kind == "exit":
            n = math.dist(pts[0], pts[1])
            if not EXIT_MIN_M <= n <= EXIT_MAX_M:
                raise MarkError(f"出口线长度要在 {EXIT_MIN_M}–{EXIT_MAX_M} m:{n:.2f} m")
        yaw = mark.get("yaw", 0.0)
        if isinstance(yaw, bool) or not isinstance(yaw, (int, float)) or not math.isfinite(yaw):
            raise MarkError("朝向要是数(弧度)")
        mid = mark.get("id")
        with self.db.tx() as c:
            if mid is None:
                n = c.execute("SELECT COUNT(*) FROM map_marks WHERE map_id=? AND map_version=?",
                              (map_id, version)).fetchone()[0]
                if n >= MAX_MARKS:
                    raise MarkError(f"这张图上标注已经 {MAX_MARKS} 个了")
                mid = uuid.uuid4().hex[:12]
                c.execute("INSERT INTO map_marks(id, map_id, map_version, kind, name, points, yaw, "
                          "updated_by, updated_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                          (mid, map_id, version, kind, name.strip(), json.dumps(pts),
                           float(yaw), by, self._now()))
            elif c.execute("UPDATE map_marks SET kind=?, name=?, points=?, yaw=?, updated_by=?, "
                           "updated_ms=? WHERE id=? AND map_id=? AND map_version=?",
                           (kind, name.strip(), json.dumps(pts), float(yaw), by, self._now(),
                            str(mid), map_id, version)).rowcount == 0:
                raise MarkError("这张图上没有这个标注")
        return next(m for m in self.list(map_id, version) if m["id"] == mid)

    def remove(self, map_id: str, version: str, mark_id: str) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM map_marks WHERE id=? AND map_id=? AND map_version=?",
                             (mark_id, map_id, version)).rowcount > 0

    def crossed(self, map_id: str, version: str, a: tuple[float, float],
                b: tuple[float, float]) -> dict[str, Any] | None:
        """人从 ``a`` 走到 ``b`` 越过了哪条出口线(好几条取最先碰上的);没越过 ``None``。

        外审 r1 #12:两头都要离线 :data:`LINE_BAND_M` 以外、分在两边才算 —— 走到线上停住、
        折返,过线一点点(定位、测距误差以内)都不算。"""
        best: tuple[float, dict[str, Any]] | None = None
        for m in self.list(map_id, version, "exit"):
            p, q = m["points"][0], m["points"][1]
            sa, sb = _side(a, p, q), _side(b, p, q)
            if sa == 0 or sb == 0 or sa == sb:
                continue
            t = _cross(a, b, p, q)
            if t is not None and (best is None or t < best[0]):
                best = (t, m)
        return best[1] if best else None

    def track(self, map_id: str, version: str, last: dict[str, Any],
              pt: tuple[float, float]
              ) -> tuple[dict[str, Any], tuple[float, float], tuple[float, float]] | None:
        """被跟的人走到 ``pt``:越过了哪条出口线 → ``(那条线, 实际穿过线的那一段的起点, 终点)``;
        没有 ``None``。

        外审 r2 #7 / r3 #3 / r4 #2:每条线各自记(``last``,按线的 id;调用方存着,这里就地改):最后
        **确定**在哪一边(离线 :data:`LINE_BAND_M` 以外)``d``、上一个点 ``r``、从那以后实际走过的
        每一段穿过线段本身几次 ``n``、最后穿过的那一段 ``c``。换到另一边、而且穿过的次数是单数才算
        越线(从端点外面绕过去、穿过去又穿回来都不算);离线太近的点只记轨迹、不改确定的那一边。
        记的东西大小固定:人在线上待多久都不会截掉轨迹。"""
        best: tuple[dict[str, Any], tuple[float, float], tuple[float, float]] | None = None
        x, y = float(pt[0]), float(pt[1])
        for m in self.list(map_id, version, "exit"):
            p, q = m["points"][0], m["points"][1]
            k = str(m["id"])
            st = last.get(k)
            if not isinstance(st, dict) or st.get("d") not in (1, -1) \
                    or not isinstance(st.get("r"), list):
                st = None
            side = _side(pt, p, q)
            if st is None:
                if side != 0:
                    last[k] = {"d": side, "r": [x, y], "n": 0, "c": None}
                continue
            r = (float(st["r"][0]), float(st["r"][1]))
            n, c = int(st.get("n") or 0), st.get("c")
            if _cross(r, (x, y), p, q) is not None:
                n, c = n + 1, [list(r), [x, y]]
            if side == 0:
                last[k] = {"d": st["d"], "r": [x, y], "n": n, "c": c}
                continue
            last[k] = {"d": side, "r": [x, y], "n": 0, "c": None}
            if side != st["d"] and n % 2 == 1 and c is not None and best is None:
                best = (m, (float(c[0][0]), float(c[0][1])), (float(c[1][0]), float(c[1][1])))
        return best

    def hide_near(self, map_id: str, version: str, x: float, y: float,
                  heading_deg: float | None, *, radius: float = HIDE_NEAR_M,
                  limit: int = HIDE_MAX) -> list[dict[str, Any]]:
        """``(x, y)`` 附近 ``radius`` 以内的易藏点:人去的那个方向(前半边)的先看,各自按远近。
        不知道方向就只按远近。"""
        out = []
        for m in self.list(map_id, version, "hide"):
            px, py = m["points"][0]
            d = math.hypot(px - x, py - y)
            if d > radius:
                continue
            back = False
            if heading_deg is not None and d > 1e-6:
                diff = math.degrees(math.atan2(py - y, px - x)) - heading_deg
                back = abs((diff + 180) % 360 - 180) > 90
            out.append((back, d, m))
        out.sort(key=lambda t: (t[0], t[1]))
        return [m for _, _, m in out[:limit]]


def inside_pose(mark: dict[str, Any], a: tuple[float, float],
                b: tuple[float, float], inside_m: float = INSIDE_M) -> tuple[float, float, float]:
    """人从 ``a`` 越过出口线走到 ``b``:机器人停在线内(``a`` 那一边)离线 ``inside_m`` 的地方,
    对着越线的那一处,朝出口看。"""
    (x1, y1), (x2, y2) = mark["points"]
    t = _cross(a, b, (x1, y1), (x2, y2))
    if t is None:                                   # 没真越线(调的人自己保证);按线的中点
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    else:
        cx, cy = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
    lx, ly = x2 - x1, y2 - y1
    n = math.hypot(lx, ly)
    nx, ny = -ly / n, lx / n
    if (a[0] - cx) * nx + (a[1] - cy) * ny < 0:
        nx, ny = -nx, -ny
    return cx + nx * inside_m, cy + ny * inside_m, math.atan2(-ny, -nx)


def _side(a: Any, p: Any, q: Any) -> int:
    """``a`` 在直线 p–q 的哪一边:1 / -1;离线 :data:`LINE_BAND_M` 以内算 0(线上)。"""
    dx, dy = q[0] - p[0], q[1] - p[1]
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return 0
    d = (dx * (a[1] - p[1]) - dy * (a[0] - p[0])) / n
    return 0 if abs(d) < LINE_BAND_M else (1 if d > 0 else -1)


def _cross(a: Any, b: Any, p: Any, q: Any) -> float | None:
    """线段 a→b 跟 p–q 相交:交点在 a→b 上的比例 t(0–1);不相交 ``None``。"""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = q[0] - p[0], q[1] - p[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    t = ((p[0] - a[0]) * sy - (p[1] - a[1]) * sx) / den
    u = ((p[0] - a[0]) * ry - (p[1] - a[1]) * rx) / den
    return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None


def _points(raw: Any, n: int) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) != n:
        raise MarkError(f"要 {n} 个点")
    out = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in p):
            raise MarkError(f"点要是 [x, y](米):{p!r}")
        out.append([float(p[0]), float(p[1])])
    return out
