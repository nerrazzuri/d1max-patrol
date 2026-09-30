"""8 邻接 A*(W10 设计稿 §3)。纯 Python、只吃字节:能在子进程里跑,不引 numpy。

- 代价栅格 ``cost``:每格一个字节,``LETHAL``(255)不许进,0–100 是软代价
  (一步的代价 = 步长 × (1 + 软代价 / 50))。
- 起点在致命区(贴墙停着)但不在硬挡里:先沿非硬挡的格用最短步数走出去
  (最多 ``ESCAPE_CELLS`` 格),再规划。
- 扩展上限、截止时刻兜底:超了抛 :class:`PlanError`(``timeout``),子进程不会一直占着。
- 出路径后视线剪枝:两点之间连直线经过的格都不致命、软代价不高于被替掉的那段,就把中间点去掉。
"""

from __future__ import annotations

import heapq
import math
import time
from collections import deque

LETHAL = 255
SQRT2 = math.sqrt(2.0)
MAX_EXPANSIONS = 3_000_000
ESCAPE_CELLS = 8
_NB = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
       (-1, -1, SQRT2), (-1, 1, SQRT2), (1, -1, SQRT2), (1, 1, SQRT2))


class PlanError(Exception):
    """``reason``:``start_blocked`` / ``goal_blocked`` / ``outside`` / ``no_path`` /
    ``timeout`` / ``worker``。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __reduce__(self):                 # 要能从子进程传回来
        return (PlanError, (self.reason, str(self)))


def _escape(cost: bytes, hard: bytes, w: int, h: int, s: int) -> list[int]:
    """从致命区里的起点,沿非硬挡的格走到最近的不致命格(步数最少);走不出去 → ``start_blocked``。"""
    prev = {s: -1}
    q = deque([(s, 0)])
    while q:
        i, n = q.popleft()
        if cost[i] != LETHAL:
            out = []
            while i != -1:
                out.append(i)
                i = prev[i]
            return out[::-1]
        if n >= ESCAPE_CELLS:
            continue
        y, x = divmod(i, w)
        for dy, dx, _ in _NB:
            yy, xx = y + dy, x + dx
            if 0 <= yy < h and 0 <= xx < w:
                j = yy * w + xx
                if j not in prev and not hard[j]:
                    prev[j] = i
                    q.append((j, n + 1))
    raise PlanError("start_blocked", f"起点贴着障碍,{ESCAPE_CELLS} 格内走不出去")


def line_cells(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """两格心连线经过的格(超覆盖:正好斜穿格角时角两边的格都算)。"""
    (r0, c0), (r1, c1) = a, b
    dr, dc = abs(r1 - r0), abs(c1 - c0)
    sr, sc = (1 if r1 > r0 else -1), (1 if c1 > c0 else -1)
    r, c = r0, c0
    out = [(r, c)]
    ix = iy = 0
    while ix < dc or iy < dr:
        dec = (1 + 2 * ix) * dr - (1 + 2 * iy) * dc     # 先碰到竖边(<0)还是横边(>0)
        if dec == 0:
            out.append((r, c + sc))
            out.append((r + sr, c))
            r, c, ix, iy = r + sr, c + sc, ix + 1, iy + 1
        elif dec < 0:
            c, ix = c + sc, ix + 1
        else:
            r, iy = r + sr, iy + 1
        out.append((r, c))
    return out


def smooth(path: list[int], cost: bytes, w: int) -> list[int]:
    """视线剪枝:从当前点尽量连到最远的点,连线上的格都不致命、软代价不超过原来那段的最大值。"""
    if len(path) <= 2:
        return path
    out = [path[0]]
    i = 0
    while i < len(path) - 1:
        best = i + 1
        seg_max = cost[path[i + 1]]
        for j in range(i + 2, len(path)):
            seg_max = max(seg_max, cost[path[j]])
            a, b = divmod(path[i], w), divmod(path[j], w)
            if all(cost[r * w + c] != LETHAL and cost[r * w + c] <= seg_max
                   for r, c in line_cells(a, b)):
                best = j
        out.append(path[best])
        i = best
    return out


def plan(cost: bytes, hard: bytes, w: int, h: int, start: tuple[int, int],
         goal: tuple[int, int], *, max_expansions: int = MAX_EXPANSIONS,
         deadline: float | None = None) -> list[tuple[int, int]]:
    """→ 格序列(行, 列),含起终点,已剪枝。``deadline``:``time.monotonic()`` 的截止时刻。"""
    for name, (r, c) in (("start", start), ("goal", goal)):
        if not (0 <= r < h and 0 <= c < w):
            raise PlanError("outside", f"{'起点' if name == 'start' else '终点'}在规划栅格外")
    s = start[0] * w + start[1]
    g = goal[0] * w + goal[1]
    if hard[s]:
        raise PlanError("start_blocked", "起点在障碍或禁行区里")
    if cost[g] == LETHAL:
        raise PlanError("goal_blocked", "终点在障碍、禁行区里或贴得太近")
    prefix = _escape(cost, hard, w, h, s) if cost[s] == LETHAL else [s]
    s0 = prefix[-1]
    gy, gx = goal
    gs = {s0: 0.0}
    par = {s0: -1}
    closed = bytearray(w * h)
    op = [(0.0, s0)]
    exp = 0
    found = False
    while op:
        _, i = heapq.heappop(op)
        if closed[i]:
            continue
        if i == g:
            found = True
            break
        closed[i] = 1
        exp += 1
        if exp >= max_expansions:
            raise PlanError("timeout", f"扩展了 {exp} 格还没找到路")
        if deadline is not None and not exp & 0x3FFF and time.monotonic() > deadline:
            raise PlanError("timeout", "规划超时")
        y, x = divmod(i, w)
        gi = gs[i]
        for dy, dx, step in _NB:
            yy, xx = y + dy, x + dx
            if not (0 <= yy < h and 0 <= xx < w):
                continue
            j = yy * w + xx
            cj = cost[j]
            if cj == LETHAL or closed[j]:
                continue
            if dy and dx and (cost[y * w + xx] == LETHAL or cost[yy * w + x] == LETHAL):
                continue                  # 斜着走不许擦过致命格的角
            ng = gi + step * (1.0 + cj / 50.0)
            if ng < gs.get(j, math.inf):
                gs[j] = ng
                par[j] = i
                ady, adx = abs(yy - gy), abs(xx - gx)
                hh = max(ady, adx) + (SQRT2 - 1.0) * min(ady, adx)
                heapq.heappush(op, (ng + hh, j))
    if not found:
        raise PlanError("no_path", "没有路")
    rev = []
    i = g
    while i != -1:
        rev.append(i)
        i = par[i]
    body = smooth(rev[::-1], cost, w)
    full = prefix[:-1] + body
    return [divmod(i, w) for i in full]
