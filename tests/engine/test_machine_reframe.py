"""中途重设位置之后引擎怎么办(W00c6e 内审)。

- 丢定位暂停时人重新给了位置:引擎记的出发点、来路都是旧坐标系里的 —— 按修正量挪过去
  (``relocalized(Δ)``);
  修正量算不出来(锚定作废过,比如里程归零)就**来路作废**,沿来路回的时候原地停、说清楚。
- 人给了位置,丢定位的重置次数从头算(以前整趟累计,第 4 次丢定位就中止,理由还是「重置 3 次
  仍未收敛」)。
- 返航途中丢定位也暂停等人(以前直接「返航失败」,低电的狗停在半路,人给位置也救不回来)。
- 等人给位置的时限跟着导航后端走(``RELOCALIZE_WAIT_S``;里程锚定要等人,30 s 不够)。
"""

from __future__ import annotations

import asyncio

from d1max_agent.engine.machine import RunState
from d1max_agent.engine.mission import Mission, MissionWaypoint, Policy
from d1max_patrol.backends.base import (
    BatteryEvent,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

from .conftest import _HOME, ARRIVED, NEVER, until


def _两点() -> Mission:
    wps = (MissionWaypoint(name="A", pose=Pose.from_xy_yaw(3.0, 0.0)),
           MissionWaypoint(name="B", pose=Pose.from_xy_yaw(3.0, 3.0)))
    return Mission(mission="t", map_id="map_test", waypoints=wps,
                   policy=Policy(battery_abort_pct=15.0))


def _拒返航(nav) -> None:
    async def 不支持() -> None:
        raise NavRequestError("return_home", "直线桥不认返航")
    nav.return_home = 不支持
    nav.PATH_KIND = "straight"


def _按脚本(nav, 脚本: list) -> None:
    原来的 = nav.goto

    async def goto(pose):
        i = len(nav.goto_calls)
        nav.on_goto = 脚本[i] if i < len(脚本) else list(ARRIVED)
        await 原来的(pose)
    nav.goto = goto


async def test_重设位置_来路按修正量挪过去_沿来路回走修正后的(make_engine, nav, device):
    _拒返航(nav)
    nav.pose_now = _HOME.pose
    _按脚本(nav, [ARRIVED, NEVER])
    engine = make_engine()
    await engine.start(_两点(), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    engine.relocalized((0.5, 0.0, 0.0))                   # 人说:其实都往东偏了 0.5 m
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE, engine.snapshot.reason
    back = nav.goto_calls[2:]
    assert (round(back[0].position.x, 2), round(back[0].position.y, 2)) == (3.5, 0.0)
    assert back[-1] == _HOME.pose, "出发点挪到 (0.5, 0),还在原点边上:最后一段回原点"
    await engine.aclose()


async def test_修正量算不出来_来路作废_返航原地停说清楚(make_engine, nav, device):
    _拒返航(nav)
    nav.pose_now = _HOME.pose
    _按脚本(nav, [ARRIVED, NEVER])
    engine = make_engine()
    await engine.start(_两点(), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    engine.relocalized(None)
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert "来路" in engine.snapshot.reason and "电量" in engine.snapshot.reason
    assert len(nav.goto_calls) == 2, "一段都不走"
    await engine.aclose()


async def test_人给了位置_丢定位的次数从头算(make_engine, nav, device):
    nav.reset_recovers = False                            # 锚定自己找不回位置,等人给
    nav.pose_now = _HOME.pose
    _按脚本(nav, [NEVER] * 20)                            # 一直在去 A 的路上
    engine = make_engine()
    await engine.start(_两点(), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 1)
    for _ in range(3):
        nav.emit_loc(LocStatus.LOC_LOST)
        await until(lambda: engine.state is RunState.PAUSED)
        engine.relocalized((0.0, 0.0, 0.0))
        nav.emit_loc(LocStatus.CONTINUOUS_LOC)
        await until(lambda: engine.state is RunState.RUNNING)
    assert engine._live.loc_reset_attempts <= 1
    nav.emit_loc(LocStatus.LOC_LOST)
    await until(lambda: engine.state is RunState.PAUSED)
    assert engine.state is not RunState.ABORTED, "人每次都给了位置:第 4 次不该直接中止"
    await engine.abort("收尾")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_返航途中丢定位_暂停等人_给了位置接着回(make_engine, nav, device):
    _拒返航(nav)
    nav.reset_recovers = False
    nav.pose_now = _HOME.pose
    _按脚本(nav, [ARRIVED, NEVER, NEVER])                  # A 到、B 卡;回程去 A 也卡
    engine = make_engine()
    await engine.start(_两点(), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    device.emit(BatteryEvent(percent=20.0))
    await until(lambda: len(nav.goto_calls) == 3)
    nav.emit_loc(LocStatus.LOC_LOST)
    await until(lambda: engine.state is RunState.PAUSED)
    nav.emit_loc(LocStatus.CONTINUOUS_LOC)
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE, engine.snapshot.reason
    assert nav.goto_calls[3:] == [_两点().waypoints[0].pose, _HOME.pose], "接着走当前这一段"
    await engine.aclose()


async def test_等人给位置的时限跟着导航后端走(make_engine, nav, device, clock):
    nav.RELOCALIZE_WAIT_S = 600.0
    nav.reset_recovers = False
    nav.pose_now = _HOME.pose
    _按脚本(nav, [NEVER] * 20)
    engine = make_engine()
    await engine.start(_两点(), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 1)
    nav.emit_loc(LocStatus.LOC_LOST)
    await until(lambda: engine.state is RunState.PAUSED)
    clock.offset += 100.0
    nav.emit(NavStatusEvent(NavStatus.STANDBY))              # 有个事件,引擎才会回头看表
    await asyncio.sleep(0.1)
    assert engine.state is RunState.PAUSED, "100 s 还在 600 s 里:接着等人"
    nav.emit_loc(LocStatus.CONTINUOUS_LOC)
    await until(lambda: engine.state is RunState.RUNNING)
    await engine.abort("收尾")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()
