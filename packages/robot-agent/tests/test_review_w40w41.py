"""W40/W41 外审(2026-10-28 Codex,``reviews/2026-10-28-W40-W41-c30fdb9-review.md``)代理这一侧的修复:
R3 移动过的「静止点云」不许拿来自动找位置;R4 后台点云核对算完时,人已经设过新位置 → 旧结果作废。
复现思路照外审报告的独立复现(真定位桥、真匹配器,运行时只搭用到的几样)。"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace as NS

import pytest

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


# ------------------------------------------------------------ 复查 N1(2026-10-29)

_TRUE = (3.0, 3.0, 0.4)


def _看墙(tmp_path):
    from test_bridge_localizer import M
    from test_globalloc import RES, _房间, _扫, 不对称
    now = [98.0]
    occ, free = _房间(extra=不对称)
    view = ObstacleView(monotonic=lambda: now[0])
    for i in range(21):
        now[0] = 98 + 0.1 * i
        view.note_odom(0.0, 0.0, 0.0)
    view.on_wall(NS(points=lambda: _扫(occ, *_TRUE, noise=0.02)))
    glob = GlobalLoc(view)
    glob.set_map(M, tmp_path)
    glob._m = (M, Matcher(occ, free, RES, (0.0, 0.0)))
    return now, occ, view, glob


def _走了又停(now, view, occ, *, new_scan):
    from test_globalloc import _扫
    for i in range(1, 7):
        now[0] = 100 + 0.1 * i
        view.note_odom(0.1 * i, 0.0, 0.0)
    here = (_TRUE[0] + 0.6 * math.cos(_TRUE[2]), _TRUE[1] + 0.6 * math.sin(_TRUE[2]), _TRUE[2])
    if new_scan:
        for i in range(1, 17):
            now[0] = 100.6 + 0.1 * i
            view.note_odom(0.6, 0.0, 0.0)
        view.on_wall(NS(points=lambda: _扫(occ, *here, noise=0.02)))
    return here



@pytest.mark.parametrize("窗口", ["匹配期间来了新点云", "最后读里程时动了"])
async def test_复查N1_匹配结果绑定参与匹配的那份点云_提交前再核(tmp_path, 窗口):
    from test_bridge_localizer import M, _就绪
    now, occ, view, glob = _看墙(tmp_path)
    c, link, loc = await _就绪()
    rt = _rt(loc, M, lambda: now[0])
    rt.globalloc = glob
    entered, release = asyncio.Event(), asyncio.Event()

    async def odom_now():
        return NS(valid=True, x=0.6, y=0.0, yaw=0.0)
    rt.hal.odometry = odom_now
    if 窗口 == "匹配期间来了新点云":
        real = glob.locate

        async def slow(pts):
            entered.set()
            await release.wait()
            return await real(pts)
        glob.locate = slow
    else:
        async def slow_odom():
            entered.set()
            await release.wait()
            return NS(valid=True, x=0.6, y=0.0, yaw=0.0)
        rt.hal.odometry = slow_odom
    job = asyncio.create_task(rt._relocalize(NS(payload={"global": True}, task_id="n1")))
    await asyncio.wait_for(entered.wait(), 5)
    _走了又停(now, view, occ, new_scan=(窗口 == "匹配期间来了新点云"))
    release.set()
    got = await asyncio.wait_for(job, 5)
    assert isinstance(got, str) and "not_found" in got, got
    assert not [r for r in link.sent if isinstance(r, Relocalize)], "旧点云的结果不许提交"


async def test_复查N1_走了以后收到新的静止点云_照样找得到(tmp_path):
    from test_bridge_localizer import M, _就绪
    now, occ, view, glob = _看墙(tmp_path)
    here = _走了又停(now, view, occ, new_scan=True)
    c, link, loc = await _就绪()
    rt = _rt(loc, M, lambda: now[0])
    rt.globalloc = glob

    async def odom_now():
        return NS(valid=True, x=0.6, y=0.0, yaw=0.0)
    rt.hal.odometry = odom_now
    got = await rt._relocalize(NS(payload={"global": True}, task_id="fresh"))
    assert isinstance(got, tuple) and got[0] == "", got
    r = [r for r in link.sent if isinstance(r, Relocalize)][-1]
    assert math.hypot(r.x - here[0], r.y - here[1]) < 0.15


# ------------------------------------------------------------ 第二轮复查 N4、N5(2026-10-29)


async def test_复查N4_匹配期间来了新点云但狗没动_结果照样提交(tmp_path):
    """N4:感知每秒一份点云,大图匹配要 1 s 多;原来要求「还是同一份点云」,狗站着也永远找不到。"""
    from test_bridge_localizer import M, _就绪
    from test_globalloc import _扫
    now, occ, view, glob = _看墙(tmp_path)
    c, link, loc = await _就绪()
    rt = _rt(loc, M, lambda: now[0])
    rt.globalloc = glob
    real = glob.locate

    async def slow(pts):
        for i in range(1, 13):                           # 匹配的这 1.2 s:狗站着,来了一份新点云
            now[0] = 100.0 + 0.1 * i
            view.note_odom(0.0, 0.0, 0.0)
        view.on_wall(NS(points=lambda: _扫(occ, *_TRUE, noise=0.02)))
        return await real(pts)
    glob.locate = slow
    got = await rt._relocalize(NS(payload={"global": True}, task_id="n4"))
    assert isinstance(got, tuple) and got[0] == "", got
    r = [r for r in link.sent if isinstance(r, Relocalize)][-1]
    assert math.hypot(r.x - _TRUE[0], r.y - _TRUE[1]) < 0.15


@pytest.mark.parametrize("挪了", [0.06, 0.6])
async def test_复查N5_最后读到的里程已经挪了_导航那边还没更新_照样拒(tmp_path, 挪了):
    from test_bridge_localizer import M, _就绪
    now, occ, view, glob = _看墙(tmp_path)
    c, link, loc = await _就绪()
    rt = _rt(loc, M, lambda: now[0])
    rt.globalloc = glob

    async def odom_moved():                              # 狗那边的里程挪了,ObstacleView 还没跟上
        return NS(valid=True, x=挪了, y=0.0, yaw=0.0)
    rt.hal.odometry = odom_moved
    got = await rt._relocalize(NS(payload={"global": True}, task_id="n5"))
    assert isinstance(got, str) and "not_found" in got, got
    assert not [r for r in link.sent if isinstance(r, Relocalize)]
