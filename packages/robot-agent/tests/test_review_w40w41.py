"""W40/W41 外审(2026-10-28 Codex,``reviews/2026-10-28-W40-W41-c30fdb9-review.md``)代理这一侧的修复:
R3 移动过的「静止点云」不许拿来自动找位置;R4 后台点云核对算完时,人已经设过新位置 → 旧结果作废。
复现思路照外审报告的独立复现(真定位桥、真匹配器,运行时只搭用到的几样)。"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace as NS

from d1max_agent.globalloc import Candidate, GlobalLoc, Matcher, Verdict
from d1max_agent.obstacles import ObstacleView
from d1max_agent.runtime import AgentRuntime
from d1max_contract.locbridge import Relocalize


def _rt(loc, m, mono):
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.parts = NS(nav=NS(anchor=loc), engine=NS(relocalized=lambda *a, **k: None))
    rt.loaded_map = m
    rt._mono = mono
    rt._scan_relocs = 0
    rt._map_job, rt._switching = None, False
    rt.processor = NS(current=None)
    rt._rtk_check = NS(human_override=lambda now: None)
    rt.events = NS(emit=lambda *a, **k: None)

    async def odom():
        return NS(valid=True, x=0.0, y=0.0, yaw=0.0)

    async def stopped():
        return True
    rt.hal = NS(odometry=odom, stopped=stopped)
    return rt


async def test_外审R4_后台核对算完时人已经设了新位置_旧结果作废():
    from test_bridge_localizer import M, _就绪, _稳
    c, link, loc = await _就绪()
    _稳(loc, 5.0, 5.0)
    entered, release = asyncio.Event(), asyncio.Event()

    async def check(points, pose):
        entered.set()
        await release.wait()
        return Verdict(False, 0.2, Candidate(3.0, 3.0, 0.0, 0.8), "旧点云的结论")
    rt = _rt(loc, M, c)
    rt.globalloc = NS(view=NS(wall_scan=lambda **kw: [(1.0, 0.0)]), check=check)
    job = asyncio.create_task(rt._scan_check(-math.inf))
    await entered.wait()
    assert await rt._relocalize(NS(payload={"x": 6.0, "y": 6.0, "yaw": 0.0},
                                    task_id="manual-new")) == ""
    release.set()
    await job
    assert loc._anchor.pose == (6.0, 6.0, 0.0), "人刚设的位置不许被旧结论改回去"
    reqs = [(r.human, r.x, r.y) for r in link.sent if isinstance(r, Relocalize)]
    assert reqs == [(True, 6.0, 6.0)], reqs
    assert loc.scan_disagree == ""


async def test_外审R3_收到静止点云以后又走了_不拿它自动找位置(tmp_path):
    from test_bridge_localizer import M, _就绪
    from test_globalloc import RES, _房间, _扫, 不对称
    now = [98.0]
    true = (3.0, 3.0, 0.4)
    occ, free = _房间(extra=不对称)
    points = _扫(occ, *true, noise=0.02)
    view = ObstacleView(monotonic=lambda: now[0])
    for i in range(21):
        now[0] = 98 + i * 0.1
        view.note_odom(0.0, 0.0, 0.0)
    view.on_wall(NS(points=lambda: points))
    assert view.wall_still
    for i in range(1, 7):                                  # 1.2 s 里往前走了 0.6 m、停下
        now[0] = 100 + i * 0.2
        view.note_odom(i * 0.1, 0.0, 0.0)
    glob = GlobalLoc(view)
    glob.set_map(("estate-1", "7"), tmp_path)
    glob._m = (("estate-1", "7"), Matcher(occ, free, RES, (0.0, 0.0)))
    c, link, loc = await _就绪()
    rt = _rt(loc, M, lambda: now[0])
    rt.globalloc = glob

    async def odom():
        return NS(valid=True, x=0.6, y=0.0, yaw=0.0)
    rt.hal.odometry = odom
    got = await rt._relocalize(NS(payload={"global": True}, task_id="global-stale"))
    assert isinstance(got, str) and "点云" in got, got
    assert not [r for r in link.sent if isinstance(r, Relocalize)], "旧点云不许发重定位"
