"""规划调用(W10 设计稿 §3):代价图 + 起终点 → 地图系路径。**不在事件循环里跑**(W08 决定 1):
默认一个常驻子进程(``spawn``);测试可以换成线程。

子进程只拿字节(代价、硬挡),不引 numpy;A* 自己带截止时刻,超时就收手,子进程不会被一次坏规划一直占着。
"""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

from d1max_agent.planning import astar
from d1max_agent.planning.astar import PlanError
from d1max_agent.planning.costmap import Costmap

PLAN_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class PlannedPath:
    points: tuple[tuple[float, float], ...]

    @property
    def length_m(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.points, self.points[1:], strict=False))


def _job(cost: bytes, hard: bytes, w: int, h: int, s: tuple[int, int], g: tuple[int, int],
         timeout_s: float) -> list[tuple[int, int]]:
    return astar.plan(cost, hard, w, h, s, g, deadline=time.monotonic() + timeout_s)


class Planner:
    def __init__(self, *, timeout_s: float = PLAN_TIMEOUT_S, in_process: bool = False) -> None:
        self.timeout_s = timeout_s
        self._in_process = in_process
        self._pool: ProcessPoolExecutor | None = None

    def _executor(self) -> ProcessPoolExecutor | None:
        if self._in_process:
            return None
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=1,
                                             mp_context=multiprocessing.get_context("spawn"))
        return self._pool

    async def plan(self, cm: Costmap, start: tuple[float, float],
                   goal: tuple[float, float]) -> PlannedPath:
        s, g = cm.cell_of(*start), cm.cell_of(*goal)
        if s is None:
            raise PlanError("outside", "起点在规划栅格外")
        if g is None:
            raise PlanError("outside", "终点在规划栅格外")
        h, w = cm.shape
        args = (cm.cost.tobytes(), cm.hard.astype("uint8").tobytes(), w, h, s, g, self.timeout_s)
        loop = asyncio.get_running_loop()
        ex = self._executor()
        cf = None
        try:
            if ex is None:
                fut = loop.run_in_executor(None, _job, *args)
            else:
                cf = ex.submit(_job, *args)
                fut = asyncio.wrap_future(cf)
            # A* 自己在 timeout_s 收手;这里多等一点,防子进程起不来或卡住
            cells = await asyncio.wait_for(fut, self.timeout_s + 5.0)
        except asyncio.CancelledError:
            if cf is not None and cf.cancelled():
                # 池子重起时排队的被取消了(内审应修 7):不是调用方被取消,别把 CancelledError 往上漏
                raise PlanError("worker", "规划子进程重起了,这一次作废") from None
            raise
        except asyncio.TimeoutError:
            self._reset()
            raise PlanError("timeout", "规划超时") from None
        except BrokenProcessPool:
            self._reset()
            raise PlanError("worker", "规划子进程坏了(下一次重起)") from None
        mid = [cm.center(r, c) for r, c in cells[1:-1]]
        return PlannedPath(((float(start[0]), float(start[1])), *mid,
                            (float(goal[0]), float(goal[1]))))

    def _reset(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        self._reset()
