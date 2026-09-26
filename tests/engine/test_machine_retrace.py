"""沿来路回的来路是真走过的路(W00c6b 内审)。

内审在仿真里复现过的几种「沿来路回、其实走了一条没走过的直线」:

- 回路默认这一趟从原点出发(``waypoints[:index]`` 倒序 + 原点)—— 站点的回程巡检、上一趟中止后停在
  原地的狗,都不是从原点出发的;
- 去程失败、跳过的点也在回路里(狗根本没到过);
- ``loops ≥ 2`` 时回路拼成跨圈的直线;
- 返航途中暂停再继续,从第一段重新走(狗掉头奔已经回过的点)。

现在引擎开跑时记下出发点、每到一个点记一笔、点位失败时记下停在哪,回路按这份记录倒着走(同一个点
出现两次就把中间那一圈抹掉);走完一段划掉一段。回到出发点后,出发点就在原点边上才走最后那一小段,
否则原地停、按返航失败收尾。
"""

from __future__ import annotations

import asyncio

import pytest

from d1max_agent.engine.homing import retrace_route_m
from d1max_agent.engine.machine import RunState
from d1max_agent.engine.mission import Mission, MissionWaypoint, Policy
from d1max_patrol.backends.base import BatteryEvent, NavRequestError, NavStatusEvent
from d1max_patrol.protocol.nav_types import NavStatus, Pose

from .conftest import _HOME, ARRIVED, NEVER, until

FAILED = [NavStatusEvent(NavStatus.FAILED)]


def _三点(**pol) -> Mission:
    wps = tuple(MissionWaypoint(name=n, pose=Pose.from_xy_yaw(x, y))
                for n, x, y in (("A", 3.0, 0.0), ("B", 3.0, 3.0), ("C", 0.0, 3.0)))
    pol.setdefault("battery_abort_pct", 15.0)
    return Mission(mission="L", map_id="map_test", waypoints=wps, policy=Policy(**pol))


def _拒返航(nav) -> None:
    async def 不支持() -> None:
        raise NavRequestError("return_home", "直线桥不认返航")
    nav.return_home = 不支持
    nav.PATH_KIND = "straight"


def _按脚本(nav, 脚本: list, 到哪: list | None = None) -> None:
    """第 n 次 ``goto`` 推 ``脚本[n]``,并把 ``current_pose`` 改成 ``到哪[n]``(给了的话)。"""
    原来的 = nav.goto

    async def goto(pose):
        i = len(nav.goto_calls)
        if 到哪 is not None and i < len(到哪) and 到哪[i] is not None:
            nav.pose_now = 到哪[i]
        nav.on_goto = 脚本[i] if i < len(脚本) else list(ARRIVED)
        await 原来的(pose)
    nav.goto = goto


def _p(x, y) -> Pose:
    return Pose.from_xy_yaw(x, y)


async def test_出发点不是原点_回到出发点就停_不走出发点到原点那段直线(make_engine, nav, device):
    """内审阻断 2 的根:回路默认从原点出发。狗从 (5,5) 出发(上一趟停在那儿、或者站点的回程巡检),
    回到出发点之后再「直线回原点」就是一条没走过的线。"""
    _拒返航(nav)
    start = _p(5.0, 5.0)
    nav.pose_now = start
    _按脚本(nav, [ARRIVED, NEVER])                        # A 到, B 卡着
    engine = make_engine()
    await engine.start(_三点(), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    device.emit(BatteryEvent(percent=20.0))              # 返航线 25,中止线 15
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    a = _三点().waypoints[0].pose
    assert nav.goto_calls[2:] == [a, start], "沿来路回到出发点为止,不再往原点走"
    reason = engine.snapshot.reason
    assert "出发点" in reason and "电量" in reason, reason     # 站点按电量告警
    assert engine.returned and "电量" in engine.returned
    await engine.aclose()


async def test_出发点就在原点边上_最后一段回原点(make_engine, nav, device):
    _拒返航(nav)
    nav.pose_now = _p(0.3, -0.2)                         # 离原点 0.36 m
    _按脚本(nav, [ARRIVED, NEVER])
    engine = make_engine()
    await engine.start(_三点(), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.goto_calls[2:] == [_三点().waypoints[0].pose, _HOME.pose]
    await engine.aclose()


async def test_去程没到的点不进回路_失败时停在哪记下来(make_engine, nav, device):
    """A 走不到(导航 Failed,按 skip 跳过),狗停在 X;接着 X → B 到了;往 C 的路上返航。
    真走过的是 原点 → X → B,回路是 B → X → 原点。以前是 B → A → 原点:A 从没到过,A 那段也没走过。"""
    _拒返航(nav)
    x = _p(1.2, 0.4)
    nav.pose_now = _HOME.pose
    m = _三点(on_waypoint_failed="skip")
    b = m.waypoints[1].pose
    _按脚本(nav, [FAILED, ARRIVED, NEVER], 到哪=[x, b, None])
    engine = make_engine()
    await engine.start(m, home=_HOME)
    await until(lambda: len(nav.goto_calls) == 3)
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.goto_calls[3:] == [b, x, _HOME.pose], nav.goto_calls
    await engine.aclose()


async def test_跑两圈_回路抹掉中间那一圈(make_engine, nav, device):
    """两圈:原点 → A → B → C → A → (往 B 的路上返航)。回路是 A → 原点,不是 A → C → B → A → 原点,
    更不是以前按 ``index`` 拼出来的「直接回原点」那条跨圈的线。"""
    _拒返航(nav)
    nav.pose_now = _HOME.pose
    _按脚本(nav, [ARRIVED, ARRIVED, ARRIVED, ARRIVED, NEVER])
    engine = make_engine()
    m = _三点(loops=2)
    await engine.start(m, home=_HOME)
    await until(lambda: len(nav.goto_calls) == 5)
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.goto_calls[5:] == [m.waypoints[0].pose, _HOME.pose], nav.goto_calls
    await engine.aclose()


async def test_返航途中暂停再继续_接着走当前这一段_不从头来(make_engine, nav, device):
    """内审应修 2:以前继续之后按 ``live.index`` 重算整条回路,狗掉头奔已经回过的 B。"""
    _拒返航(nav)
    nav.pose_now = _HOME.pose
    m = _三点()
    a, b = m.waypoints[0].pose, m.waypoints[1].pose
    _按脚本(nav, [ARRIVED, ARRIVED, NEVER, ARRIVED, NEVER])   # A、B 到,C 卡;回程 B 到、A 卡
    engine = make_engine()
    await engine.start(m, home=_HOME)
    await until(lambda: len(nav.goto_calls) == 3)
    device.emit(BatteryEvent(percent=20.0))
    await until(lambda: len(nav.goto_calls) == 5)       # 回到 B,正往 A 走
    await engine.pause()
    await until(lambda: engine.state is RunState.PAUSED)
    await engine.resume()
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.goto_calls[3:] == [b, a, a, _HOME.pose], nav.goto_calls
    await engine.aclose()


async def test_回路每段的超时不短于任务自己的点位超时(make_engine, nav, device, clock):
    """内审小问题:每段固定 300 s,任务给了 600 s 的长腿在半路就判「返航失败: 到点超时」。"""
    _拒返航(nav)
    nav.pose_now = _HOME.pose
    _按脚本(nav, [ARRIVED, NEVER, NEVER])               # A 到,B 卡;回程去 A 也卡
    engine = make_engine()
    await engine.start(_三点(waypoint_timeout_s=600.0), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    device.emit(BatteryEvent(percent=20.0))
    await until(lambda: len(nav.goto_calls) == 3)
    clock.offset += 400.0
    device.emit(BatteryEvent(percent=20.0))              # 有个事件,引擎才会回头看表
    await asyncio.sleep(0.1)
    assert engine.state is RunState.RETURNING, "400 s 还在 600 s 的预算里"
    clock.offset += 250.0
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert "超时" in engine.snapshot.reason
    await engine.aclose()


async def test_没返航的一趟_returned是空的(make_engine, nav, device):
    engine = make_engine()
    await engine.start(_三点(), home=_HOME)
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert engine.returned == ""
    await engine.aclose()


async def test_回家这一趟本身_电量到返航线接着往前走_到中止线才停(make_engine, nav, device):
    """站点的回程巡检(``on_battery_low: continue``):剩下的路就是回家的路。以前电量到线,引擎
    按「从原点出发」倒着走 —— 往远端走、离家更远(内审阻断 2)。"""
    _拒返航(nav)
    _按脚本(nav, [ARRIVED, NEVER])
    engine = make_engine()
    await engine.start(_三点(on_battery_low="continue"), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    device.emit(BatteryEvent(percent=20.0))              # 返航线与中止线之间
    nav.emit(NavStatusEvent(NavStatus.SUCCEED))          # B 到了
    await until(lambda: len(nav.goto_calls) == 3)
    assert RunState.RETURNING not in engine._seen
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert engine.returned == ""
    await engine.aclose()


async def test_回家这一趟本身_低于中止线照样原地停(make_engine, nav, device):
    _按脚本(nav, [ARRIVED, NEVER])
    engine = make_engine()
    await engine.start(_三点(on_battery_low="continue"), home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)
    device.emit(BatteryEvent(percent=10.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert "中止线" in engine.snapshot.reason
    await engine.aclose()


@pytest.mark.parametrize("kind,该返航", [("planned", False), ("straight", True)])
async def test_直线后端_回家的电按沿来路的长度估(make_engine, nav, device, kind, 该返航):
    """直线后端回家是沿来路倒着走,比直线远得多;返航线按直线估的话掉头太晚,回程走到一半碰中止线。
    场景:200 m 出去、再折回到原点边上的长 U 形,正往最后一个点走 —— 直线回家 1.4 m(成本取地板
    3%),沿来路 400 m(约 8.7%)。电量 30%:按直线估返航线 28%,不回;按来路估 33.7%,该回了。"""
    wps = tuple(MissionWaypoint(name=n, pose=_p(x, y))
                for n, x, y in (("A", 200.0, 0.0), ("B", 200.0, 1.0), ("C", 1.0, 1.0)))
    m = Mission(mission="U", map_id="map_test", waypoints=wps,
                policy=Policy(battery_return_pct=25.0, battery_abort_pct=25.0))
    nav.PATH_KIND = kind
    _按脚本(nav, [ARRIVED, ARRIVED, NEVER])
    engine = make_engine()
    await engine.start(m, home=_HOME)
    await until(lambda: len(nav.goto_calls) == 3)
    device.emit(BatteryEvent(percent=30.0))
    await asyncio.sleep(0.1)
    assert (RunState.RETURNING in engine._seen) is 该返航
    await engine.abort("收尾")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


def test_沿来路的长度_抹掉回环_不知道出发点按原点():
    home = _p(0.0, 0.0)
    a, b, c = _p(3.0, 0.0), _p(3.0, 3.0), _p(0.0, 3.0)
    # 正往 C 走,到过 A、B:C → B → A → 原点
    assert retrace_route_m(c, [(0, a), (1, b)], start=home, home=home) == pytest.approx(9.0)
    # 两圈,回到过 A:回环抹掉,A → 原点
    assert retrace_route_m(b, [(0, a), (1, b), (2, c), (0, a)], start=home, home=home) \
        == pytest.approx(3.0 + 3.0)
    # 出发点不知道:按原点
    assert retrace_route_m(b, [(0, a)], start=None, home=home) == pytest.approx(6.0)
