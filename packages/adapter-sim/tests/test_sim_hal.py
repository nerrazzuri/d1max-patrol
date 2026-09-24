"""adapter-sim 的 HAL 契约测试(设计 §4):能力声明与入口一致、速度夹与拒、ttl 自停、
停止请求与确认分开、急停与控制权互锁、位姿按速度积分。全部用注入的钟,不睡。"""

from __future__ import annotations

import pytest

from d1max_adapter_sim.robot import SimRobot
from d1max_contract.hal import HalUnsupported, MotionStatus, VelocityCommand


class 钟:
    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        return self.ms

    def advance(self, dt_s: float) -> None:
        self.ms += int(round(dt_s * 1000))


@pytest.fixture
def 台子():
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, deadband_vx=0.05, stop_latency_s=0.2)
    return c, r


async def _就绪(r):
    await r.connect()
    await r.acquire_control()


def _步(c, r, n=1, dt=0.1):
    for _ in range(n):
        r.tick(dt)
        c.advance(dt)


def _v(vx=0.0, wz=0.0, vy=0.0, seq=1, ttl_ms=500):
    return VelocityCommand(seq=seq, ttl_ms=ttl_ms, frame="base", vx=vx, vy=vy, wz=wz)


async def test_能力声明与入口一致(台子):
    """声明 false 的方法要抛 HalUnsupported;声明 true 的不抛。"""
    _, r = 台子
    await _就绪(r)
    caps = r.hal_capabilities()
    assert caps.lateral is False and caps.recharge_mode == "none"
    assert caps.sensing == {"lidar": False, "depth": False, "thermal": False, "imu": False,
                            "joint_effort": False, "foot_force": False}
    assert caps.actuators == {"light": False, "siren": False, "speaker": False,
                              "spotlight": False, "head": False}
    for call in (r.imu(), r.lidar(), r.ultrasonic(), r.joints(), r.contacts(), r.depth(),
                 r.thermal(), r.audio_session(), r.recharge_start(), r.recharge_stop(), r.undock(),
                 r.recharge_status(), r.light("front", True), r.strobe("front", "x", 1.0),
                 r.sound("x", 1.0), r.spotlight(True, 1.0), r.head(0.0, 0.0),
                 r.snapshot("front"), r.stream_url("front")):
        with pytest.raises(HalUnsupported):
            await call
    assert await r.camera_sources() == ()
    await r.odometry()
    await r.battery()
    await r.faults()
    await r.health()


async def test_没控制权先拒(台子):
    _, r = 台子
    await r.connect()
    got = await r.set_velocity(_v(vx=0.5))
    assert got.rejected and got.reason == "no_control" and got.applied_vx == 0.0
    assert (await r.control_status()).held is False
    await r.acquire_control()
    assert (await r.control_status()).held is True
    assert await r.motion_status() is MotionStatus.READY


async def test_侧移拒_超上限夹_死区拒(台子):
    _, r = 台子
    await _就绪(r)
    got = await r.set_velocity(_v(vx=0.2, vy=0.1))
    assert got.rejected and got.reason == "no_lateral"
    got = await r.set_velocity(_v(vx=3.0, wz=-4.0))
    assert got.clamped and not got.rejected
    assert got.applied_vx == 1.0 and got.applied_wz == -1.5
    got = await r.set_velocity(_v(vx=0.02))
    assert got.rejected and got.reason == "deadband"
    got = await r.set_velocity(_v(vx=0.0, wz=0.3))     # 纯转向不算死区
    assert not got.rejected and not got.clamped and got.applied_wz == 0.3


async def test_ttl到期自停(台子):
    c, r = 台子
    await _就绪(r)
    await r.set_velocity(_v(vx=0.5, ttl_ms=300))
    _步(c, r)
    assert (await r.odometry()).vx == 0.5
    c.advance(0.3)
    r.tick(0.1)
    assert (await r.odometry()).vx == 0.0, "ttl 过了没新命令,适配器自己停"
    assert await r.stopped() is True


async def test_位姿按速度积分(台子):
    c, r = 台子
    await _就绪(r)
    await r.set_velocity(_v(vx=1.0, ttl_ms=10_000))
    _步(c, r, 10)
    o = await r.odometry()
    assert o.x == pytest.approx(1.0, abs=1e-6) and o.y == pytest.approx(0.0) and o.valid
    await r.set_velocity(_v(vx=0.0, wz=1.0, seq=2, ttl_ms=10_000))   # 在 max_wz 之内
    _步(c, r, 10)
    assert (await r.odometry()).yaw == pytest.approx(1.0, abs=1e-6)


async def test_stop请求与stopped确认分开(台子):
    c, r = 台子
    await _就绪(r)
    await r.set_velocity(_v(vx=0.8, ttl_ms=10_000))
    _步(c, r)
    await r.stop()
    assert await r.stopped() is False, "刚发请求,还在制动"
    _步(c, r)
    assert await r.stopped() is False
    _步(c, r)                 # 累计 0.2 s = stop_latency
    assert await r.stopped() is True
    assert (await r.odometry()).vx == 0.0
    got = await r.set_velocity(_v(vx=0.3, seq=3))
    assert not got.rejected, "stop 之后新的速度命令可以再走"


async def test_急停拒速度_复位不恢复(台子):
    c, r = 台子
    await _就绪(r)
    await r.set_velocity(_v(vx=0.8, ttl_ms=10_000))
    await r.emergency_stop(True)
    assert await r.estop_status() is True
    _步(c, r)
    assert await r.stopped() is True and (await r.odometry()).vx == 0.0
    got = await r.set_velocity(_v(vx=0.3, seq=2))
    assert got.rejected and got.reason == "estop"
    await r.estop_reset()
    assert await r.estop_status() is False
    _步(c, r)
    assert (await r.odometry()).vx == 0.0, "复位不自动恢复任务"
    h = await r.health()
    assert h.estop is False and h.control is True


async def test_控制权被夺(台子):
    c, r = 台子
    await _就绪(r)
    await r.set_velocity(_v(vx=0.5, ttl_ms=10_000))
    r.inject_control_lost()
    assert (await r.control_status()).held is False
    _步(c, r)
    assert (await r.odometry()).vx == 0.0
    got = await r.set_velocity(_v(vx=0.3, seq=2))
    assert got.rejected and got.reason == "no_control"


async def test_定位丢失反映在health(台子):
    _, r = 台子
    await _就绪(r)
    assert (await r.health()).loc_quality == 1.0
    r.inject_loc_lost(True)
    assert (await r.health()).loc_quality == 0.0
    assert (await r.odometry()).valid is False


async def test_电量随时间下降且可注入(台子):
    c, r = 台子
    await _就绪(r)
    b0 = (await r.battery()).percent
    c.advance(3600)
    assert (await r.battery()).percent < b0
    r.inject_battery(12.0)
    assert (await r.battery()).percent == 12.0


async def test_release_control后拒速度(台子):
    _, r = 台子
    await _就绪(r)
    await r.release_control()
    got = await r.set_velocity(_v(vx=0.3))
    assert got.rejected and got.reason == "no_control"


async def test_close撤控制权_断连即停止输出(台子):
    """总设计 §2.1:SDK 断连即停止输出。"""
    c, r = 台子
    await _就绪(r)
    await r.set_velocity(_v(vx=0.5, ttl_ms=10_000))
    await r.close()
    assert (await r.control_status()).held is False
    got = await r.set_velocity(_v(vx=0.3, seq=2))
    assert got.rejected and got.reason == "no_control"
