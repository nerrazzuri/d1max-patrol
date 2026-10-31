"""地图清噪点(W41):建图时有人走动,图上空地里留下零星的占用格;规划器按机身外接圆膨胀,一小块就把整片
路封了(2026-10-26 C40221 现场:v2 狗前方的空地 ``goal_blocked``,现场手工清了 14 个像素做成 v3)。

- **找**(:func:`find_specks`):占用格的连通块(八邻)里,面积不超过 :data:`SPECK_MAX_M2`、外面一圈
  :data:`RING_M` 全是空地(不挨着别的占用格、也不挨着没建图的地方)的,算「零星噪点」的候选。柱子、
  家具一般比它大或者贴着墙;判不准的留给人看 —— 网页上每一处都画出来,人可以去掉几处再清。
- **清**(:func:`clean`):把选中的那几块改成空地的灰度,另存成新的一版(``maps_admin.denoise``)。
  ``prior.mm``(定位器用的点云)不动:只改规划、套图用的栅格。

编号按扫描顺序(从上往下、从左往右),同一份栅格每次算出来一样;清的时候带上找的时候那份栅格的
摘要,对不上(这期间图换了)就拒。
"""

from __future__ import annotations

import hashlib
from collections import deque
from typing import Any

from d1max_site import map_preview as mp

SPECK_MAX_M2 = 0.05
RING_M = 0.2
#: 外面那一圈里没建图的格子最多占这么多。
RING_UNKNOWN = 0.1
#: 一张图最多报这么多处(再多多半不是噪点,是图本身有问题:让人去重建)。
MAX_SPECKS = 500


def digest(pgm: bytes) -> str:
    return hashlib.sha256(pgm).hexdigest()[:16]


def find_specks(pgm: bytes, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """→ 每一处:``{"id", "x", "y", "px", "cells"}``(``x``、``y`` 是地图坐标,米;``cells`` 是
    (行, 列) 列表,行从上往下)。按编号排。

    外面那一圈只看**大块**的占用(墙、家具)和没建图的地方:人走过留下的噪点一小撮一小撮挨着(现场 v2
    那 14 个像素彼此离不到 0.2 m),别的小块不算挨着东西;没建图的格子容许圈里有 :data:`RING_UNKNOWN`
    这么一小部分(噪点旁边常有一两格没扫到)。"""
    import numpy as np
    ranks = mp.grid_ranks(pgm, meta)                  # 0 占用、1 空闲、2 未知,从上往下
    h, w = ranks.shape
    res = float(meta["resolution"])
    ox, oy = float(meta["origin"][0]), float(meta["origin"][1])
    # 外审 R7:按实际面积算;一格就超过上限(粗分辨率的图)就不出候选,原来至少放一格
    max_px = int(SPECK_MAX_M2 / (res * res) + 1e-9)
    if max_px < 1:
        return []
    ring = max(1, int(round(RING_M / res)))
    occ = ranks == 0
    label = np.zeros((h, w), dtype=np.int32)
    comps: list[list[tuple[int, int]] | None] = [None]
    for r0, c0 in zip(*np.nonzero(occ), strict=True):
        if label[r0, c0]:
            continue
        k = len(comps)
        comp: list[tuple[int, int]] = []
        q = deque([(int(r0), int(c0))])
        label[r0, c0] = k
        while q:
            r, c = q.popleft()
            if len(comp) <= max_px:
                comp.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < h and 0 <= cc < w and occ[rr, cc] and not label[rr, cc]:
                        label[rr, cc] = k
                        q.append((rr, cc))
        comps.append(comp if len(comp) <= max_px else None)    # None = 大块
    big = np.array([c is None for c in comps], dtype=bool)
    big[0] = False
    out: list[dict[str, Any]] = []
    for comp in comps:
        if not comp:
            continue
        rs = [p[0] for p in comp]
        cs = [p[1] for p in comp]
        r_lo, r_hi = min(rs) - ring, max(rs) + ring
        c_lo, c_hi = min(cs) - ring, max(cs) + ring
        if r_lo < 0 or c_lo < 0 or r_hi >= h or c_hi >= w:
            continue                                  # 贴着图边:不算
        lab = label[r_lo:r_hi + 1, c_lo:c_hi + 1]
        if big[lab].any():
            continue                                  # 挨着墙、家具:多半是它的一部分
        rk = ranks[r_lo:r_hi + 1, c_lo:c_hi + 1]
        if (rk == 2).sum() > RING_UNKNOWN * (rk.size - len(comp)):
            continue                                  # 一圈里没建图的地方太多:说不准
        out.append({"id": len(out) + 1, "px": len(comp), "cells": comp,
                    "x": round(ox + (sum(cs) / len(cs) + 0.5) * res, 2),
                    "y": round(oy + (h - sum(rs) / len(rs) - 0.5) * res, 2)})
        if len(out) >= MAX_SPECKS:
            break
    return out


def clean(pgm: bytes, meta: dict[str, Any], specks: list[dict[str, Any]]) -> bytes:
    """把这几处改成空地,回新的 PGM(P5、最大值 255)。空地用图里空闲格最常见的那个灰度。"""
    import numpy as np
    w, h, body = mp.parse_pgm(pgm)
    img = np.frombuffer(body, dtype=np.uint8).reshape(h, w).copy()
    ranks = mp.grid_ranks(pgm, meta)
    free_vals = img[ranks == 1]
    if not free_vals.size:
        raise ValueError("图里没有空闲格,无法确定空地灰度")
    free = int(np.bincount(free_vals).argmax())
    for s in specks:
        for r, c in s["cells"]:
            img[r, c] = free
    return b"P5\n%d %d\n255\n" % (w, h) + img.tobytes()
