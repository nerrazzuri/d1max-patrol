"""W10 规划基准(W08 决定 1「先在 Orin 上量最坏情况的规划耗时」,真机项 3e.1)。

用法(狗上,代理的虚拟环境里)::

    python tools/w10_plan_bench.py <地图版本目录> [--pairs 20] [--radius 0.52]

读 ``floor.pgm/.yaml`` → 量代价图生成耗时;在可走的格里挑离得最远的几对点(再加随机的)各规划一次,
报每次的耗时、扩展了多少格、路长;最后报最坏的一次。不起子进程(量的就是 A* 本身)。
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import numpy as np

from d1max_agent.planning import astar, costmap


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("map_dir", type=Path)
    p.add_argument("--pairs", type=int, default=20)
    p.add_argument("--radius", type=float, default=costmap.ROBOT_RADIUS_M)
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args(argv)
    t0 = time.perf_counter()
    free, res, origin = costmap.load_floor(a.map_dir)
    blocked, pres = costmap.downsample(free, res)
    t1 = time.perf_counter()
    cm = costmap.build(blocked, pres, origin, robot_radius_m=a.radius)
    t2 = time.perf_counter()
    h, w = cm.shape
    print(f"栅格 {w}×{h} 格({pres:.2f} m),读图 {t1 - t0:.2f} s,代价图 {t2 - t1:.2f} s")
    ok = np.argwhere(cm.cost != costmap.LETHAL)
    if len(ok) < 2:
        print("没有能走的格")
        return 1
    rng = random.Random(a.seed)
    # 最远的几对:沿对角线两端挑,再加随机的
    order = ok[np.argsort(ok[:, 0] + ok[:, 1])]
    pairs = [(tuple(map(int, order[i])), tuple(map(int, order[-1 - i])))
             for i in range(min(5, len(order) // 2))]
    pairs += [(tuple(map(int, ok[rng.randrange(len(ok))])),
               tuple(map(int, ok[rng.randrange(len(ok))])))
              for _ in range(max(0, a.pairs - len(pairs)))]
    cost, hard = cm.cost.tobytes(), cm.hard.astype(np.uint8).tobytes()
    worst = 0.0
    for s, g in pairs:
        t = time.perf_counter()
        try:
            cells = astar.plan(cost, hard, w, h, (int(s[0]), int(s[1])), (int(g[0]), int(g[1])))
            what = f"{len(cells)} 个点"
        except astar.PlanError as exc:
            what = f"没规划出来({exc.reason})"
        dt = time.perf_counter() - t
        worst = max(worst, dt)
        print(f"{s} → {g}:{dt:.2f} s,{what}")
    print(f"最坏 {worst:.2f} s(超时兜底 20 s;超过 5 s 要定分辨率或换实现)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
