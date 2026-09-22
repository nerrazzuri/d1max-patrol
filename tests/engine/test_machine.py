"""任务引擎状态机。

**这些测试不接任何真后端。** 引擎的输入全都进同一条队列,所以往假后端上
``emit`` 一串事件就能把整趟跑完 —— 这正是单队列单消费者换来的东西。
"""

from __future__ import annotations

import asyncio

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BatteryEvent,
    ControlLostEvent,
    FaultEvent,
    LocStatusEvent,
    NavStatusEvent,
)
from d1max_patrol.engine.archive import read_events, read_manifest, read_state
from d1max_patrol.engine.homing import HomePoint, ReturnParams
from d1max_patrol.engine.machine import MissionEngine, RunState
from d1max_patrol.engine.mission import Action, MissionWaypoint, Policy
from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import (
    ALG_LIDAR_DISCONNECTED,
    ALG_NAV_BLOCKED,
    LocStatus,
    NavStatus,
    Pose,
)

from ..conftest import BadDisks
from .conftest import _HOME, ARRIVED, NEVER, make_mission, until

#: ``nav``/``device``/``media``/``clock``/``make_engine`` 这几个夹具,连同
#: ``NavStub``/``DeviceStub``/``MediaStub``/``Clock`` 那套假后端,都长在
#: ``conftest.py`` 里 —— pytest 会把它们自动喂给这个目录下的每个测试模块,
#: 不用在这儿 import。


def _alg(code: int) -> AlgErrorEvent:
    return AlgErrorEvent((AlgErrorItem(code, f"code {code}", 1),))


BLOCKED = _alg(ALG_NAV_BLOCKED)
LIDAR_GONE = _alg(ALG_LIDAR_DISCONNECTED)


async def run_to_end(engine: MissionEngine, mission, timeout: float = 5.0):
    await engine.start(mission, home=_HOME)
    state = await engine.wait_done(timeout_s=timeout)
    await engine.aclose()
    return state


def policy(**kw) -> Policy:
    return Policy(**kw)


# --------------------------------------------------------------------- 顺风局


async def test_一路顺风两个点都跑完(make_engine, sample_mission, media):
    engine = make_engine()
    assert await run_to_end(engine, sample_mission) is RunState.DONE
    results = engine.snapshot.results
    assert [r.name for r in results] == ["P1_transformer", "P2_panel"]
    assert all(r.ok for r in results)
    assert media["front"].grabs == 1 and media["back"].grabs == 1


async def test_照片进归档目录而且记在结果里(make_engine, sample_mission):
    engine = make_engine()
    await run_to_end(engine, sample_mission)
    photos = sorted(p.name for p in (engine.archive.path / "photos").iterdir())
    assert len(photos) == 2
    assert engine.snapshot.results[0].photos == (photos[0],)


async def test_每次迁移都同时写事件流和状态文件(make_engine, sample_mission):
    """两件事捆在一起 —— 只写一件,页面和崩溃恢复就会各说各话。"""
    engine = make_engine()
    await run_to_end(engine, sample_mission)
    states = [e for e in read_events(engine.archive.path) if e["kind"] == "state"]
    assert [e["to"] for e in states][:3] == ["PREFLIGHT", "LOCALIZING", "RUNNING"]
    assert read_state(engine.archive.path)["state"] == "DONE"


async def test_收尾会把汇总写进manifest(make_engine, sample_mission):
    engine = make_engine()
    await run_to_end(engine, sample_mission)
    summary = read_manifest(engine.archive.path)["summary"]
    assert summary == {"state": "DONE", "reason": "", "succeeded": 2,
                       "failed": 0, "total": 2,
                       "results": summary["results"]}


async def test_订阅者能收到快照(make_engine, sample_mission):
    engine = make_engine()
    with engine.subscription() as queue:
        await run_to_end(engine, sample_mission)
        seen = []
        while not queue.empty():
            seen.append(queue.get_nowait().state)
    assert RunState.RUNNING in seen and RunState.DONE in seen


async def test_多圈会把点位跑够遍数(make_engine, nav):
    engine = make_engine()
    mission = make_mission(policy=policy(loops=3))
    assert await run_to_end(engine, mission) is RunState.DONE
    assert len(nav.goto_calls) == 6
    assert len(engine.snapshot.results) == 6


# ----------------------------------------------------------------- 起飞前检查


async def test_起飞检查没过就不跑(make_engine, sample_mission, device, nav):
    device.estop = True
    engine = make_engine()
    assert await run_to_end(engine, sample_mission) is RunState.ABORTED
    assert nav.goto_calls == [], "检查没过还下发导航,等于检查白做了"
    assert "急停" in engine.snapshot.reason


async def test_检查结果整份进事件流(make_engine, sample_mission, device):
    """现场要的是"还差哪几项",所以每一项的结论都得留下来。"""
    device.control = False
    engine = make_engine()
    await run_to_end(engine, sample_mission)
    pre = [e for e in read_events(engine.archive.path) if e["kind"] == "preflight"]
    assert len(pre) == 1
    assert [c["name"] for c in pre[0]["checks"]] == [
        "nav_ready", "device_ready", "localized", "home", "battery", "storage",
        "removable"]


async def test_扫盘炸了不掀翻整份报告(make_engine, sample_mission):
    """``scan`` 在 ``run_preflight`` 的 ``_guard`` 保护圈外面。

    ``root.iterdir()`` 权限不对是 ``PermissionError``,``entry.is_dir()``
    撞上 stale mount 是 ``OSError`` —— 真炸了就走 ``_run`` 的兜底,整趟
    ABORTED +"引擎内部异常",而 preflight 那条事件压根没写:归档里一项
    检查结论都没有,人不知道该修哪儿。
    """
    engine = make_engine(removable=BadDisks())
    assert await run_to_end(engine, sample_mission) is RunState.ABORTED
    pre = [e for e in read_events(engine.archive.path) if e["kind"] == "preflight"]
    assert len(pre) == 1, "扫盘炸了也要把这份报告写出来"
    checks = {c["name"]: c for c in pre[0]["checks"]}
    assert [c["name"] for c in pre[0]["checks"]] == [
        "nav_ready", "device_ready", "localized", "home", "battery", "storage",
        "removable"]
    assert not checks["removable"]["ok"]
    assert all(checks[n]["ok"] for n in
               ("nav_ready", "device_ready", "localized", "home", "battery",
                "storage")), "最该拦住的那块盘不能反过来把别的六项也带下水"
    assert "起飞前检查未通过" in engine.snapshot.reason
    assert "引擎内部异常" not in engine.snapshot.reason


async def test_定位没收敛就等等不到就中止(make_engine, nav, monkeypatch):
    import d1max_patrol.engine.machine as machine

    monkeypatch.setattr(machine, "LOCALIZE_TIMEOUT_S", 0.2)
    # 起飞检查那一刻定位是好的,进了 LOCALIZING 就掉了,而且再没收敛回来。
    calls = {"n": 0}

    async def loc_status():
        calls["n"] += 1
        return LocStatus.CONTINUOUS_LOC if calls["n"] == 1 else LocStatus.LOC_LOST

    nav.loc_status = loc_status
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert "定位" in engine.snapshot.reason
    assert nav.goto_calls == []
    await engine.aclose()


# --------------------------------------------------------------------- 点位


async def test_到点超时判这个点失败(make_engine, nav):
    nav.on_goto = NEVER
    engine = make_engine()
    mission = make_mission(policy=policy(waypoint_timeout_s=0.2,
                                         on_waypoint_failed="skip"))
    assert await run_to_end(engine, mission) is RunState.DONE
    results = engine.snapshot.results
    assert [r.ok for r in results] == [False, False]
    assert "超时" in results[0].note
    assert nav.stop_calls >= 2, "超时了要把导航停掉,不能让狗自己接着走"


async def test_导航回失败也算这个点失败(make_engine, nav):
    nav.on_goto = [NavStatusEvent(NavStatus.FAILED)]
    engine = make_engine()
    mission = make_mission(policy=policy(on_waypoint_failed="skip"))
    await run_to_end(engine, mission)
    assert "Failed" in engine.snapshot.results[0].note


async def test_失败策略是中止时整趟就停(make_engine, nav):
    nav.on_goto = [NavStatusEvent(NavStatus.FAILED)]
    engine = make_engine()
    mission = make_mission(policy=policy(on_waypoint_failed="abort"))
    assert await run_to_end(engine, mission) is RunState.ABORTED
    assert len(nav.goto_calls) == 1, "第一个点就中止了,不该再下发第二个"


async def test_重试次数用完才算这个点失败(make_engine, nav):
    nav.on_goto = [NavStatusEvent(NavStatus.FAILED)]
    engine = make_engine()
    mission = make_mission(policy=policy(on_waypoint_failed="retry_then_skip",
                                         waypoint_retry=2))
    await run_to_end(engine, mission)
    assert len(nav.goto_calls) == 6, "两个点,每个点 1 次加 2 次重试"


async def test_不重试的策略只下发一次(make_engine, nav):
    nav.on_goto = [NavStatusEvent(NavStatus.FAILED)]
    engine = make_engine()
    mission = make_mission(policy=policy(on_waypoint_failed="skip",
                                         waypoint_retry=5))
    await run_to_end(engine, mission)
    assert len(nav.goto_calls) == 2, "skip 就是一次定生死,retry 次数不该偷偷生效"


async def test_上一个点的终态没散掉就先等着不硬下发(make_engine, sample_mission, nav):
    """两个点之间必须等状态机回落 StandBy,不等就是整趟报废。

    真设备上 ``start_nav`` 只在 StandBy 下受理:上一个点走完是 Succeed,紧接着
    下发第二个点会被当场拒绝,而那是个 ``NavBackendError`` —— 掉进兜底那层,
    整趟按"引擎内部异常"中止。**只跑一个点的测试永远盖不住这条路径。**
    """
    nav.hold_after_goto = 3
    engine = make_engine()
    assert await run_to_end(engine, sample_mission) is RunState.DONE
    assert len(nav.goto_calls) == 2
    assert all(r.ok for r in engine.snapshot.results)


async def test_导航一直回不到standby就判这个点失败(make_engine, nav):
    """等也是有上限的。等不到就是这个点失败,不是整趟卡在那里不动。"""
    nav.hold_after_goto = 10 ** 6
    engine = make_engine()
    mission = make_mission(policy=policy(waypoint_timeout_s=0.3,
                                         on_waypoint_failed="skip"))
    assert await run_to_end(engine, mission) is RunState.DONE
    results = engine.snapshot.results
    assert [r.ok for r in results] == [True, False]
    assert "StandBy" in results[1].note


async def test_点位失败也会留在结果里而不是被抹掉(make_engine, nav):
    nav.on_goto = [NavStatusEvent(NavStatus.CANCELLED)]
    engine = make_engine()
    await run_to_end(engine, make_mission(policy=policy(on_waypoint_failed="skip")))
    assert len(engine.snapshot.results) == 2
    assert all(not r.ok and r.note for r in engine.snapshot.results)


# --------------------------------------------------------------------- 动作


async def test_缺相机的点位算失败不静默跳过(make_engine, media):
    """静默跳过等于报告里少一张照片却没人知道 —— 那比失败更糟。"""
    del media["front"]
    engine = make_engine()
    await run_to_end(engine, make_mission(policy=policy(on_waypoint_failed="skip")))
    assert engine.snapshot.results[0].ok is False
    assert "front" in engine.snapshot.results[0].note


async def test_取图失败算这个点失败(make_engine, media):
    media["front"].fail = True
    engine = make_engine()
    await run_to_end(engine, make_mission(policy=policy(on_waypoint_failed="skip")))
    assert engine.snapshot.results[0].ok is False
    assert engine.snapshot.results[1].ok is True, "一个点挂了不该连累下一个"


async def test_灯和云台动作会打到设备上(make_engine, device):
    mission = make_mission(waypoints=(
        MissionWaypoint(name="P1", pose=Pose.from_xy_yaw(0.0, 0.0, 0.0),
                        actions=(Action(type="light", on=True),
                                 Action(type="head", pitch=0.3, yaw=-0.2))),
    ))
    engine = make_engine()
    assert await run_to_end(engine, mission) is RunState.DONE
    assert device.lights == [True]
    assert device.gimbals == [(0.3, -0.2)]


async def test_dwell期间照样处理事件(make_engine, nav, device):
    """驱动协程不许裸 sleep —— 等的那几秒里事件不能没人管。"""
    mission = make_mission(waypoints=(
        MissionWaypoint(name="P1", pose=Pose.from_xy_yaw(0.0, 0.0, 0.0),
                        actions=(Action(type="dwell", seconds=30.0),)),
    ))
    engine = make_engine()
    await engine.start(mission, home=_HOME)
    await until(lambda: nav.goto_calls)
    await asyncio.sleep(0.05)
    device.emit(BatteryEvent(percent=1.0))       # 远低于中止线
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert "中止线" in engine.snapshot.reason
    await engine.aclose()


# --------------------------------------------------------------------- 电量


async def test_电量到返航线就返航(make_engine, nav, device):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(policy=policy(battery_abort_pct=15.0)), home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(BatteryEvent(percent=20.0))      # 返航线 25,中止线 15
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.home_calls == 1
    assert RunState.RETURNING in engine._seen
    await engine.aclose()


async def test_电量到中止线就原地停不返航(make_engine, nav, device):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(BatteryEvent(percent=5.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert nav.home_calls == 0, "撑着走回去可能半路趴在外面,原地停下更好找"
    await engine.aclose()


async def test_飞行中的返航成本用的是引擎自己那份标定系数(make_engine, nav, device):
    """出发线和飞行中的返航线必须用**同一份**系数。

    这四个数是这一卷唯一待真机标定的。写死 ``DEFAULT_RETURN_PARAMS`` 的话,
    标定落地那天出发线换成了实测值,飞行中的返航线还留在默认值上,而今天
    两边都是默认值 —— 不一样的那天没有任何测试会红。

    ``floor_pct=20`` 把回家成本顶到 20:返航线 15 + 20 = 35,35% 的电就该
    掉头;默认的 3.0 算出来是 18,同样的电只会 CONTINUE。
    """
    nav.on_goto = NEVER
    engine = make_engine(return_params=ReturnParams(floor_pct=20.0))
    assert engine.return_params.floor_pct == 20.0
    await engine.start(make_mission(policy=policy(battery_abort_pct=15.0,
                                                  battery_return_pct=15.0)),
                       home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(BatteryEvent(percent=30.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.home_calls == 1, "标定系数没传下去的话,30% 还够继续跑"
    await engine.aclose()


async def test_默认系数下同样的电还够继续跑(make_engine, nav, device):
    """上一条的对照组 —— 不然它测的可能只是"30% 本来就该返航"。"""
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(policy=policy(battery_abort_pct=15.0,
                                                  battery_return_pct=15.0)),
                       home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(BatteryEvent(percent=30.0))
    await asyncio.sleep(0.05)
    assert nav.home_calls == 0
    await engine.abort("测完了")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_中止不含任何位移(make_engine, nav, device):
    """主规范 §6.4:停止导航 + 记录 + 告警,不自动返航、不自动趴下。"""
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    before = len(nav.goto_calls)
    nav.emit(LIDAR_GONE)
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert nav.home_calls == 0
    assert device.lie_calls == 0
    assert len(nav.goto_calls) == before, "中止之后不许再下发任何导航"
    assert nav.stop_calls >= 1
    await engine.aclose()


async def test_返航路上电量再掉也不会打断返航(make_engine, nav, device):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(policy=policy(battery_abort_pct=15.0)), home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(BatteryEvent(percent=20.0))
    await engine.wait_state(RunState.RETURNING)
    device.emit(BatteryEvent(percent=18.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.home_calls == 1, "返航被自己重新触发一遍就永远回不去"
    await engine.aclose()


# ----------------------------------------------------------------- 故障与降级


async def test_雷达掉线直接中止(make_engine, nav):
    nav.on_goto = [LIDAR_GONE]
    engine = make_engine()
    assert await run_to_end(engine, make_mission()) is RunState.ABORTED
    assert "雷达" in engine.snapshot.reason


async def test_致命故障直接中止(make_engine, nav, device):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(FaultEvent(("电机过温",), fatal=True))
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert "电机过温" in engine.snapshot.reason
    await engine.aclose()


async def test_非致命故障不打断任务(make_engine, nav, device):
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    device.emit(FaultEvent(("电池温度偏高",), fatal=False))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    await engine.aclose()


async def test_被挡住先等不立刻判这个点失败(make_engine, nav, clock):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    nav.emit(BLOCKED)
    await asyncio.sleep(0.05)
    assert engine.state is RunState.RUNNING
    assert len(nav.goto_calls) == 1, "人从狗前面走过去要几秒,等一下比判失败划算"
    # 等超了才升级
    clock.offset = 30.0
    nav.emit(BLOCKED)
    await until(lambda: len(nav.goto_calls) == 2)
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_控制权被拿走会暂停(make_engine, nav, device):
    """上装抢走控制权是可以人工夺回的(清单 #46/#47),不必直接判整趟失败。"""
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(ControlLostEvent("上装接管"))
    await asyncio.sleep(0.05)
    assert engine.state is RunState.RUNNING, "PAUSE 裁决不由规则表自己动手"
    ruled = [e for e in read_events(engine.archive.path)
             if e["kind"] == "ruling" and e["decision"] == "pause"]
    assert ruled and "控制权" in ruled[0]["reason"]
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


# --------------------------------------------------------------------- 定位


async def test_定位丢了会重置并重发当前点(make_engine, nav):
    nav.on_goto = [LocStatusEvent(LocStatus.LOC_LOST)]
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.reset_calls >= 1)
    assert nav.stop_calls >= 1, "重置定位之前要先停下来"
    await until(lambda: len(nav.goto_calls) >= 2)
    assert RunState.PAUSED in engine._seen
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_定位重置到上限就中止(make_engine, nav):
    from d1max_patrol.engine.safety import MAX_LOC_RESET

    nav.on_goto = [LocStatusEvent(LocStatus.LOC_LOST)]
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    assert await engine.wait_done(timeout_s=10.0) is RunState.ABORTED
    assert nav.reset_calls == MAX_LOC_RESET
    assert "定位丢失" in engine.snapshot.reason
    await engine.aclose()


async def test_重置之后收不敛就中止而不是干等(make_engine, nav, monkeypatch):
    import d1max_patrol.engine.machine as machine

    monkeypatch.setattr(machine, "LOCALIZE_TIMEOUT_S", 0.2)
    nav.reset_recovers = False
    nav.on_goto = [LocStatusEvent(LocStatus.LOC_LOST)]
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert "未收敛" in engine.snapshot.reason
    await engine.aclose()


async def test_策略要求直接中止时不重置(make_engine, nav):
    nav.on_goto = [LocStatusEvent(LocStatus.LOC_LOST)]
    engine = make_engine()
    mission = make_mission(policy=policy(on_loc_lost="abort"))
    await engine.start(mission, home=_HOME)
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert nav.reset_calls == 0
    await engine.aclose()


# ----------------------------------------------------------------- 人工命令


async def test_人工暂停会真的把狗停下来(make_engine, nav):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.pause()
    await engine.wait_state(RunState.PAUSED)
    assert nav.stop_calls == 1, "暂停要真停,不能只改个状态字"
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_继续之后重发当前点而且不算一次重试(make_engine, nav):
    nav.on_goto = NEVER
    engine = make_engine()
    # skip 策略只给一次机会 —— 还能重发就说明暂停没吃掉这次机会
    mission = make_mission(policy=policy(on_waypoint_failed="skip"))
    await engine.start(mission, home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.pause()
    await engine.wait_state(RunState.PAUSED)
    await engine.resume()
    await until(lambda: len(nav.goto_calls) == 2)
    assert engine.state is RunState.RUNNING
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_暂停期间的事件不会判当前点失败(make_engine, nav, device):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.pause()
    await engine.wait_state(RunState.PAUSED)
    nav.emit(BLOCKED)                    # 停着的时候"被挡住"不是失败
    await asyncio.sleep(0.05)
    assert engine.state is RunState.PAUSED
    await engine.resume()
    await until(lambda: len(nav.goto_calls) == 2)
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_暂停期间也能中止(make_engine, nav):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.pause()
    await engine.wait_state(RunState.PAUSED)
    await engine.abort("人工收工")
    assert await engine.wait_done(timeout_s=5.0) is RunState.ABORTED
    assert engine.snapshot.reason == "人工收工"
    await engine.aclose()


async def test_没暂停的时候继续是空操作不报错(make_engine, nav):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.resume()
    await asyncio.sleep(0.05)
    assert engine.state is RunState.RUNNING, "现场手快点两下很常见,不该炸"
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_命令是入队的不是直接改状态(make_engine, nav):
    """直接改状态就把并发修改放回来了 —— 所以 pause 返回时状态可以还没变。"""
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(make_mission(), home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.pause()
    assert engine.state is RunState.RUNNING
    await engine.wait_state(RunState.PAUSED)
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


# --------------------------------------------------------------------- 其它


async def test_已经在跑的时候再开一趟被拒(make_engine, nav, sample_mission):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)
    from d1max_patrol.engine.machine import EngineBusy

    with pytest.raises(EngineBusy):
        await engine.start(sample_mission, home=_HOME)
    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_已经在跑的时候再开一趟被拒也不动正在飞的原点(make_engine, nav,
                                                             sample_mission):
    """并发的第二个 ``/run`` 会带着它自己查到的原点——但它一开始就该被拒,
    绝不能顺手把正在飞的那一趟手里的原点换掉:``_return_cost_pct`` 那份动态
    返航成本此刻正读着它。"""
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)

    other_home = HomePoint(map_id="map_test", pose=Pose.from_xy_yaw(9.0, 9.0),
                           marked_at_ms=2)
    from d1max_patrol.engine.machine import EngineBusy

    with pytest.raises(EngineBusy):
        await engine.start(sample_mission, home=other_home)
    assert engine._home is _HOME, "被拒的请求不许改动正在跑的那趟的原点"

    await engine.abort("收工")
    await engine.wait_done(timeout_s=5.0)
    await engine.aclose()


async def test_引擎内部异常也会落成说得清原因的中止(make_engine, nav,
                                                     sample_mission):
    """带着异常静默死掉最糟:页面还显示 RUNNING,人在外面等着。"""
    async def boom(pose):
        raise RuntimeError("底层炸了")

    nav.goto = boom
    engine = make_engine()
    assert await run_to_end(engine, sample_mission) is RunState.ABORTED
    assert "底层炸了" in engine.snapshot.reason


async def test_关闭引擎会把转发任务收干净(make_engine, nav, sample_mission):
    nav.on_goto = NEVER
    engine = make_engine()
    await engine.start(sample_mission, home=_HOME)
    await until(lambda: nav.goto_calls)
    await engine.aclose()
    assert engine._forwarders == []
    assert nav._subscribers == [], "订阅没退干净,下一趟就会收到上一趟的事件"


async def test_引擎不认识HTTP(make_engine):
    """它必须能脱离 app 单独用 —— 装了 web 框架的依赖就说明耦合进去了。"""
    import inspect

    import d1max_patrol.engine.machine as machine

    src = inspect.getsource(machine)
    for banned in ("aiohttp", "fastapi", "starlette", "uvicorn", "flask",
                   "http.server", "websockets"):
        assert banned not in src, f"引擎里不该出现 {banned}"


async def test_后端不支持返航_引擎沿来路倒着回原点(make_engine, nav, device):
    """W04。自建导航(和仿真)的 ``return_home`` 是明确拒绝的,以前引擎收到这个
    拒绝就把整趟按「返航失败」中止 —— 低电时狗原地趴下。现在引擎自己回:已到过的
    点位倒序走一遍,最后到原点。倒着走来路是为了不穿墙:直线回家会撞上中间的东西。

    时序:P1 到了(index→1),P2 走不到(卡住),电量掉到返航线 → 引擎该发
    ``goto(P1)`` 再 ``goto(原点)``,而不是 ``return_home()``。
    """
    from d1max_patrol.backends.base import NavRequestError

    async def 不支持() -> None:
        raise NavRequestError("return_home", "该后端不支持返航")

    nav.return_home = 不支持
    脚本 = [list(ARRIVED), list(NEVER), list(ARRIVED), list(ARRIVED)]   # P1 到, P2 卡, 回程两段都到
    原来的 = nav.goto

    async def 按脚本(pose):
        nav.on_goto = 脚本.pop(0) if 脚本 else list(ARRIVED)
        await 原来的(pose)

    nav.goto = 按脚本
    engine = make_engine()
    mission = make_mission(policy=policy(battery_abort_pct=15.0))
    await engine.start(mission, home=_HOME)
    await until(lambda: len(nav.goto_calls) == 2)          # P1 到了,正往 P2 走
    device.emit(BatteryEvent(percent=20.0))                # 返航线 25,中止线 15
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.home_calls == 0
    p1 = mission.waypoints[0].pose
    assert nav.goto_calls[2:] == [p1, _HOME.pose], nav.goto_calls
    assert RunState.RETURNING in engine._seen
    await engine.aclose()


async def test_后端不支持返航_一个点都没到过就直接回原点(make_engine, nav, device):
    from d1max_patrol.backends.base import NavRequestError

    async def 不支持() -> None:
        raise NavRequestError("return_home", "该后端不支持返航")

    nav.return_home = 不支持
    脚本 = [list(NEVER), list(ARRIVED)]                     # P1 卡住, 回原点到
    原来的 = nav.goto

    async def 按脚本(pose):
        nav.on_goto = 脚本.pop(0) if 脚本 else list(ARRIVED)
        await 原来的(pose)

    nav.goto = 按脚本
    engine = make_engine()
    await engine.start(make_mission(policy=policy(battery_abort_pct=15.0)), home=_HOME)
    await until(lambda: nav.goto_calls)
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.goto_calls[1:] == [_HOME.pose], nav.goto_calls
    await engine.aclose()


async def test_后端不支持返航_到过两个点就倒着走两段再回原点(make_engine, nav, device):
    """真正钉住「倒序」的那一条:到过 P1、P2,正往 P3 走时返航,回程必须是
    P2 → P1 → 原点。上面那条只到过一个点,倒不倒序看不出来(评审抓的)。"""
    from d1max_patrol.backends.base import NavRequestError

    async def 不支持() -> None:
        raise NavRequestError("return_home", "该后端不支持返航")

    nav.return_home = 不支持
    脚本 = [list(ARRIVED), list(ARRIVED), list(NEVER)]      # P1 到, P2 到, P3 卡; 回程默认都到
    原来的 = nav.goto

    async def 按脚本(pose):
        nav.on_goto = 脚本.pop(0) if 脚本 else list(ARRIVED)
        await 原来的(pose)

    nav.goto = 按脚本
    p1, p2, p3 = (Pose.from_xy_yaw(1.0, 0.0, 0.0), Pose.from_xy_yaw(2.0, 1.0, 1.57),
                  Pose.from_xy_yaw(3.0, 2.0, 0.0))
    mission = make_mission(
        policy=policy(battery_abort_pct=15.0),
        waypoints=(MissionWaypoint(name="P1", pose=p1, actions=()),
                   MissionWaypoint(name="P2", pose=p2, actions=()),
                   MissionWaypoint(name="P3", pose=p3, actions=())))
    engine = make_engine()
    await engine.start(mission, home=_HOME)
    await until(lambda: len(nav.goto_calls) == 3)          # P1、P2 到了,正往 P3 走
    device.emit(BatteryEvent(percent=20.0))
    assert await engine.wait_done(timeout_s=5.0) is RunState.DONE
    assert nav.goto_calls[3:] == [p2, p1, _HOME.pose], nav.goto_calls
    await engine.aclose()
