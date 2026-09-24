"""HAL → NavBackend / DeviceBackend 两个桥(W00b 决定 2)。引擎只认识 NavBackend/DeviceBackend;
桥把 RobotHAL 包成它们,并按厂商导航状态机的时序发事件:StandBy → Initializing →(init_delay)→
Active → Succeed/Failed/Cancelled →(terminal_hold)→ StandBy。SimRobot + 假钟,tick 驱动。"""

from __future__ import annotations

import asyncio

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.bridges.hal_device import HalDeviceBackend
from d1max_agent.bridges.hal_nav import HalNavBackend
from d1max_patrol.backends.base import (
    ControlLostEvent,
    DeviceBackendError,
    LocStatusEvent,
    MediaError,
    NavBackend,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose


class 钟:
    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))


@pytest.fixture
async def 台子():
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    nav = HalNavBackend(r, now_ms=c, map_id="m", init_delay_s=0.3, terminal_hold_s=0.5)
    dev = HalDeviceBackend(r, now_ms=c)
    await dev.connect()
    await dev.acquire_control()
    await nav.connect()
    return c, r, nav, dev


async def _步(c, r, nav, n=1, dt=0.1, dev=None):
    for _ in range(n):
        await nav.step(dt)
        if dev is not None:
            await dev.step(dt)
        r.tick(dt)
        c.advance(dt)


def _收(nav):
    q = nav.subscribe()
    got = []

    def drain():
        while not q.empty():
            got.append(q.get_nowait())
        return got
    return drain


async def test_是NavBackend而且能力集为空(台子):
    c, r, nav, dev = 台子
    assert isinstance(nav, NavBackend)
    assert nav.capabilities == frozenset()
    assert nav.connected is True
    assert await nav.nav_status() is NavStatus.STANDBY
    assert await nav.loc_status() is LocStatus.CONTINUOUS_LOC
    assert await nav.list_maps() == ["m"]
    await nav.load_map("m")
    with pytest.raises(NavRequestError):
        await nav.load_map("other")
    for call in (nav.remove_maps(["m"]), nav.rename_map("m", "n"), nav.start_mapping(),
                 nav.stop_mapping(), nav.save_path("m", "p", []), nav.remove_path("m", "p"),
                 nav.reset_localization()):
        with pytest.raises(NavRequestError):
            await call
    assert await nav.mapping_status() is None
    assert await nav.list_paths("m") == {}
    assert await nav.get_map_grid("m") == {}


async def test_goto按厂商状态机时序走到点(台子):
    c, r, nav, dev = 台子
    drain = _收(nav)
    await nav.goto(Pose.from_xy_yaw(2.0, 0.0))
    assert await nav.nav_status() is NavStatus.INITIALIZING
    assert (await r.odometry()).vx == 0.0
    await _步(c, r, nav, 3)                                    # 0.3 s init 还差最后一拍去看表
    assert await nav.nav_status() is NavStatus.INITIALIZING
    await _步(c, r, nav, 1)
    assert await nav.nav_status() is NavStatus.ACTIVE
    await _步(c, r, nav, 2)
    assert (await r.odometry()).vx > 0, "Active 之后才开始发速度"
    await _步(c, r, nav, 60)
    kinds = [e.status for e in drain() if isinstance(e, NavStatusEvent)]
    assert kinds[:3] == [NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.SUCCEED]
    assert kinds[-1] is NavStatus.STANDBY, "终态驻留后回落 StandBy"
    assert await nav.nav_status() is NavStatus.STANDBY
    o = await r.odometry()
    assert abs(o.x - 2.0) <= 0.15 and o.vx == 0.0


async def test_终态驻留期间goto被拒(台子):
    c, r, nav, dev = 台子
    await nav.goto(Pose.from_xy_yaw(0.5, 0.0))
    await _步(c, r, nav, 4)
    # 走到 SUCCEED 但还没回 STANDBY 的那一段
    for _ in range(40):
        if await nav.nav_status() is NavStatus.SUCCEED:
            break
        await _步(c, r, nav, 1)
    assert await nav.nav_status() is NavStatus.SUCCEED
    with pytest.raises(NavRequestError, match="StandBy"):
        await nav.goto(Pose.from_xy_yaw(1.0, 0.0))
    await _步(c, r, nav, 6)                                    # 0.5 s 驻留过了
    assert await nav.nav_status() is NavStatus.STANDBY
    await nav.goto(Pose.from_xy_yaw(1.0, 0.0))


async def test_stop走Cancelled再回StandBy_并且真停了(台子):
    c, r, nav, dev = 台子
    drain = _收(nav)
    await nav.goto(Pose.from_xy_yaw(5.0, 0.0))
    await _步(c, r, nav, 6)
    assert (await r.odometry()).vx > 0
    await nav.stop()
    assert await nav.nav_status() is NavStatus.CANCELLED
    await _步(c, r, nav, 3)
    assert await r.stopped() is True
    await _步(c, r, nav, 4)
    assert await nav.nav_status() is NavStatus.STANDBY
    kinds = [e.status for e in drain() if isinstance(e, NavStatusEvent)]
    assert NavStatus.CANCELLED in kinds and kinds[-1] is NavStatus.STANDBY
    await nav.stop()                                      # StandBy 下 stop 是空操作


async def test_pause_resume(台子):
    c, r, nav, dev = 台子
    await nav.goto(Pose.from_xy_yaw(5.0, 0.0))
    await _步(c, r, nav, 6)
    await nav.pause()
    assert await nav.nav_status() is NavStatus.PAUSE
    await _步(c, r, nav, 4)
    assert (await r.odometry()).vx == 0.0
    await nav.resume()
    assert await nav.nav_status() is NavStatus.ACTIVE
    await _步(c, r, nav, 3)
    assert (await r.odometry()).vx > 0


async def test_HAL拒速度就Failed(台子):
    c, r, nav, dev = 台子
    drain = _收(nav)
    await nav.goto(Pose.from_xy_yaw(5.0, 0.0))
    await _步(c, r, nav, 6)
    await r.emergency_stop(True)
    await _步(c, r, nav, 2)
    assert await nav.nav_status() is NavStatus.FAILED
    assert NavStatus.FAILED in [e.status for e in drain() if isinstance(e, NavStatusEvent)]


async def test_定位丢了_loc_status变LocLost且发事件且停(台子):
    c, r, nav, dev = 台子
    drain = _收(nav)
    await nav.goto(Pose.from_xy_yaw(5.0, 0.0))
    await _步(c, r, nav, 6)
    r.inject_loc_lost(True)
    await _步(c, r, nav, 2)
    assert await nav.loc_status() is LocStatus.LOC_LOST
    assert any(isinstance(e, LocStatusEvent) and e.status is LocStatus.LOC_LOST for e in drain())
    await _步(c, r, nav, 4)
    assert await r.stopped() is True
    r.inject_loc_lost(False)
    await _步(c, r, nav, 1)
    assert await nav.loc_status() is LocStatus.CONTINUOUS_LOC


async def test_set_speed夹到HAL上限并返回实际值_侧移拒(台子):
    c, r, nav, dev = 台子
    got = await nav.set_speed(3.0, z=9.0)
    assert got == {"x": 1.0, "y": 0.0, "z": 1.5}
    assert await nav.get_speed() == got
    got = await nav.set_speed(0.4)
    assert got["x"] == 0.4
    with pytest.raises(NavRequestError):
        await nav.set_speed(0.4, y=0.2)
    await nav.goto(Pose.from_xy_yaw(9.0, 0.0))
    vmax = 0.0
    for _ in range(30):
        await _步(c, r, nav, 1)
        vmax = max(vmax, (await r.odometry()).vx)
    assert 0 < vmax <= 0.4 + 1e-9


async def test_return_home走到登记的原点_没登记就拒(台子):
    c, r, nav, dev = 台子
    with pytest.raises(NavRequestError, match="原点"):
        await nav.return_home()
    r.teleport(3.0, 0.0, 0.0)
    nav.set_home(Pose.from_xy_yaw(0.0, 0.0))
    await nav.return_home()
    await _步(c, r, nav, 80)
    o = await r.odometry()
    assert abs(o.x) <= 0.15 and abs(o.y) <= 0.15


async def test_goto非StandBy拒(台子):
    c, r, nav, dev = 台子
    await nav.goto(Pose.from_xy_yaw(1.0, 0.0))
    with pytest.raises(NavRequestError):
        await nav.goto(Pose.from_xy_yaw(2.0, 0.0))


async def test_wait_nav_terminal能用(台子):
    """引擎靠基类的 wait_nav_terminal/事件流等终态;桥发的事件要能让它返回。"""
    c, r, nav, dev = 台子
    with nav.subscription() as q:
        await nav.goto(Pose.from_xy_yaw(0.5, 0.0))

        async def 推():
            for _ in range(60):
                await _步(c, r, nav, 1)
                await asyncio.sleep(0)
        t = asyncio.create_task(推())
        status = await nav.wait_nav_terminal(5.0, queue=q)
        await t
    assert status is NavStatus.SUCCEED


# ------------------------------------------------------------ DeviceBackend

async def test_设备桥透传与不支持项(台子):
    c, r, nav, dev = 台子
    assert await dev.has_control() is True
    assert await dev.battery() == pytest.approx((await r.battery()).percent)
    assert await dev.emergency() is False
    await dev.emergency_stop(True)
    assert await dev.emergency() is True and await r.estop_status() is True
    await dev.emergency_stop(False)
    await dev.set_light(True)                             # HAL 没有灯:记日志,不抛
    await dev.set_gimbal(0.1, 0.2)                        # HAL 没有云台:记日志,不抛
    with pytest.raises(MediaError):
        await dev.take_photo()
    for call in (dev.stand(), dev.lie(), dev.walk(1.0, 0.1, 0.0, 0.0)):
        with pytest.raises(DeviceBackendError):
            await call
    await r.set_velocity(__import__("d1max_contract.hal", fromlist=["VelocityCommand"])
                         .VelocityCommand(seq=1, ttl_ms=5000, frame="base", vx=0.5, vy=0, wz=0))
    await dev.halt()
    await _步(c, r, nav, 3, dev=dev)
    assert await r.stopped() is True
    await dev.release_control()
    assert await dev.has_control() is False


async def test_设备桥在控制权丢失时发ControlLostEvent(台子):
    c, r, nav, dev = 台子
    q = dev.subscribe()
    r.inject_control_lost()
    await _步(c, r, nav, 1, dev=dev)
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    assert any(isinstance(e, ControlLostEvent) for e in got)


async def test_设备桥有connected给老HTTP面画绿灯(台子):
    c, r, nav, dev = 台子
    assert dev.connected is True
    await dev.close()
    assert dev.connected is False and await dev.has_control() is False


async def test_终态一进就叫HAL停_不靠ttl到期(台子):
    """停止靴子要真踢:``_enter_terminal`` 必须 ``hal.stop()``。拍长到 0.5 s、速度命令的 ttl
    随之到 1.5 s,不主动停的话机器会照着上一条命令再走一秒半。"""
    c, r, nav, dev = 台子
    await nav.goto(Pose.from_xy_yaw(9.0, 0.0))
    await _步(c, r, nav, 2, dt=0.5)                       # init 0.3 s 过了,Active,已发速度
    assert (await r.odometry()).vx > 0
    x0 = (await r.odometry()).x
    await nav.stop()
    await _步(c, r, nav, 1, dt=0.5)                       # 制动 0.2 s < 0.5 s:这一拍末已停
    assert await r.stopped() is True
    assert (await r.odometry()).x - x0 < 0.25, "stop 之后不该再走出去(ttl 还有 1 s 没到期)"
