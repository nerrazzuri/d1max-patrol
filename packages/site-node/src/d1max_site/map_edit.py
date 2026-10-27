"""地图编辑第一版的栅格那一半(W44 第四期,设计 §6 第 1、2、4 条):橡皮擦、画障碍。

改的是机器人「哪里能走」的平面图(``floor.pgm``),**不改**认位置用的立体地图(``prior.mm``)。跟清噪点
(:mod:`d1max_site.map_denoise`)一样另存成新的一版(``maps_admin.edit``),原来那一版不动。

一个操作:``{"op": "erase" | "obstacle", "shape": "stroke" | "line" | "rect",
"points": [[x, y], ...](米), "width_m": 0.05–2.0}``。``stroke`` 是一笔(点连成线、按粗细画),
``line`` 两个点的直线,``rect`` 两个对角的
方框(实心,不看粗细)。``erase`` 改成空地(图里空闲格最常见的灰度),``obstacle`` 改成占用(0)。
"""

from __future__ import annotations

import math
from typing import Any

from d1max_site import map_preview as mp

OPS = ("erase", "obstacle")
SHAPES = ("stroke", "line", "rect")
MAX_OPS = 500
MAX_POINTS = 2000
WIDTH_M = (0.05, 2.0)


class EditError(ValueError):
    """操作写得不对(接口回 400)。"""


def check_ops(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise EditError("ops 要是非空的列表")
    if len(raw) > MAX_OPS:
        raise EditError(f"一次最多 {MAX_OPS} 个操作")
    out = []
    for i, o in enumerate(raw):
        if not isinstance(o, dict) or o.get("op") not in OPS or o.get("shape") not in SHAPES:
            raise EditError(f"第 {i + 1} 个操作要有 op(erase / obstacle)和 "
                            "shape(stroke / line / rect)")
        pts = o.get("points")
        if not isinstance(pts, list) or not 1 <= len(pts) <= MAX_POINTS:
            raise EditError(f"第 {i + 1} 个操作的点要 1–{MAX_POINTS} 个")
        clean = []
        for p in pts:
            if not (isinstance(p, list) and len(p) == 2 and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                    for v in p)):
                raise EditError(f"第 {i + 1} 个操作的点要是 [x, y](米)")
            clean.append((float(p[0]), float(p[1])))
        if o["shape"] in ("line", "rect") and len(clean) != 2:
            raise EditError(f"第 {i + 1} 个操作:直线、方框要两个点")
        w = o.get("width_m", 0.2)
        if isinstance(w, bool) or not isinstance(w, (int, float)) or \
                not WIDTH_M[0] <= w <= WIDTH_M[1]:
            raise EditError(f"第 {i + 1} 个操作的粗细要在 {WIDTH_M[0]}–{WIDTH_M[1]} 米")
        out.append({"op": o["op"], "shape": o["shape"], "points": clean, "width_m": float(w)})
    return out


def apply_ops(pgm: bytes, meta: dict[str, Any], ops: list[dict[str, Any]]) -> tuple[bytes, dict]:
    """→ (新的 PGM(P5、最大值 255), ``{"erased_m2", "obstacle_m2"}``)。
    ``ops`` 先过 :func:`check_ops`。"""
    import numpy as np
    w, h, body = mp.parse_pgm(pgm)
    img = np.frombuffer(body, dtype=np.uint8).reshape(h, w).copy()
    ranks = mp.grid_ranks(pgm, meta)
    free_vals = img[ranks == 1]
    free = int(np.bincount(free_vals).argmax()) if free_vals.size else 254
    res = float(meta["resolution"])
    ox, oy = float(meta["origin"][0]), float(meta["origin"][1])
    rows, cols = np.mgrid[0:h, 0:w]
    xs = ox + (cols + 0.5) * res                       # 每一格中心的地图坐标
    ys = oy + (h - rows - 0.5) * res
    erased = np.zeros((h, w), dtype=bool)
    added = np.zeros((h, w), dtype=bool)
    for o in ops:
        pts = o["points"]
        if o["shape"] == "rect":
            (x0, y0), (x1, y1) = pts
            mask = ((xs >= min(x0, x1)) & (xs <= max(x0, x1))
                    & (ys >= min(y0, y1)) & (ys <= max(y0, y1)))
        else:
            r = max(o["width_m"] / 2, res * 0.71)          # 再细也盖住线经过的那一格
            mask = np.zeros((h, w), dtype=bool)
            segs = list(zip(pts, pts[1:], strict=False)) or [(pts[0], pts[0])]
            for (ax, ay), (bx, by) in segs:
                dx, dy = bx - ax, by - ay
                ll = dx * dx + dy * dy
                t = np.clip(((xs - ax) * dx + (ys - ay) * dy) / ll, 0, 1) if ll > 0 else 0.0
                mask |= (xs - (ax + t * dx)) ** 2 + (ys - (ay + t * dy)) ** 2 <= r * r
        if o["op"] == "erase":
            img[mask] = free
            erased |= mask
            added &= ~mask
        else:
            img[mask] = 0
            added |= mask
            erased &= ~mask
    cell = res * res
    stats = {"erased_m2": round(float(erased.sum()) * cell, 2),
             "obstacle_m2": round(float(added.sum()) * cell, 2)}
    return b"P5\n%d %d\n255\n" % (w, h) + img.tobytes(), stats
