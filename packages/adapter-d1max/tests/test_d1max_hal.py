"""adapter-d1max 的 HAL 测试(W00d 设计 §6):全部跑在仿真旁路(``SimAgentServer``)上。

仿真旁路里比例值 1.0 折 1.2 m/s、转向 1.0 折 1.5 rad/s,低于比例 0.2 原地蹭不动。所以这里
``mps_per_unit=1.2``、``radps_per_unit=1.5``、``deadband_mps=0.25`` —— 跟真狗的默认值(待测)
无关,只是让换算在仿真里对得上。"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time

import pytest

from d1max_adapter_d1max.hal import D1MaxHal
from d1max_contract.hal import HalUnsupported, MotionStatus, VelocityCommand
from d1max_sim.agent_server import SimAgentServer


@contextlib.asynccontextmanager
async def _台子(*, stand: bool = True, held: bool = True, **kw):
    sim = SimAgentServer(port=0, held=held)
    await sim.start()
    hal = D1MaxHal("127.0.0.1", sim.port, mps_per_unit=1.2, radps_per_unit=1.5,
                   deadband_mps=0.25, **kw)
    try:
        await hal.connect()
        if held:
            await hal.acquire_control()
        if stand:
            await hal.set_motion_mode("stand")
        yield sim, hal
    finally:
        await hal.close()
        await sim.stop()


def _v(vx=0.0, wz=0.0, vy=0.0, ttl_ms=300):
    return VelocityCommand(seq=1, ttl_ms=ttl_ms, frame="base", vx=vx, vy=vy, wz=wz)


def _最后一条(sim, cmd):
    got = [a for c, a in sim.commands if c == cmd]
    assert got, f"旁路没收到 {cmd}"
    return got[-1]


async def _等(pred, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await pred():
            return True
        await asyncio.sleep(0.02)
    return False


def test_能力_控制权不可释放_没有横移_只有灯():
    hal = D1MaxHal(mps_per_unit=0.4, radps_per_unit=1.0, deadband_mps=0.05, max_fraction=0.5)
    caps = hal.hal_capabilities()
    assert caps.control_releasable is False and caps.lateral is False
    assert caps.recharge_mode == "none"
    assert math.isclose(caps.max_vx, 0.2) and math.isclose(caps.max_wz, 0.5)
    assert caps.deadband_vx == 0.05
    assert caps.actuators == {"light": True, "strobe": False, "siren": False, "speaker": False,
                              "spotlight": False, "head": False}
    assert [k for k, v in caps.sensing.items() if v] == ["imu"], "W26:姿态与受力走旁路进程"


def test_参数不合理当场拒():
    for bad in ({"mps_per_unit": 0.0}, {"radps_per_unit": -1.0}, {"deadband_mps": -0.1},
                {"max_fraction": 0.0}, {"max_fraction": 0.6}, {"mps_per_unit": float("nan")},
                {"stopped_eps": 0.0}, {"fwd_offset": -0.01}, {"fwd_offset": 0.5},
                {"fwd_offset": float("nan")}):
        with pytest.raises(ValueError):
            D1MaxHal(**bad)


async def test_连上就有状态_电量急停运动状态都读得到():
    async with _台子(stand=False) as (sim, hal):
        assert (await hal.battery()).percent == 71.0
        assert await hal.estop_status() is False
        assert await hal.motion_status() is MotionStatus.LYING
        await hal.set_motion_mode("stand")
        assert await hal.motion_status() is MotionStatus.READY
        sim.set_battery(40.0)
        assert await _等(lambda: _eq(hal.battery(), 40.0))
        h = await hal.health()
        assert h.link_ok and h.control and not h.estop and h.loc_quality == 1.0


async def _eq(coro, want):
    return (await coro).percent == want


async def test_没连上时运动状态未知_里程无效():
    hal = D1MaxHal()
    assert await hal.motion_status() is MotionStatus.UNKNOWN
    assert (await hal.odometry()).valid is False
    h = await hal.health()
    assert not h.link_ok and not h.control and h.estop and h.loc_quality == 0.0


async def test_release_control抛不支持_控制权还在_关链路也不放():
    async with _台子() as (sim, hal):
        with pytest.raises(HalUnsupported):
            await hal.release_control()
        st = await hal.control_status()
        assert st.held and st.releasable is False
        assert not (await hal.set_velocity(_v(vx=0.36))).rejected, "放不掉就还能开"
    assert "shutdown" not in [c for c, _ in sim.commands], "close 不许让旁路进程放控制权"


async def test_速度换成比例值发vel_ttl夹到旁路的范围():
    async with _台子() as (sim, hal):
        got = await hal.set_velocity(_v(vx=0.36, wz=0.6, ttl_ms=300))
        assert not got.rejected and not got.clamped
        a = _最后一条(sim, "vel")
        assert math.isclose(a["fwd"], 0.3) and math.isclose(a["yaw"], 0.4)
        assert a["lat"] == 0.0 and a["ttl_ms"] == 300
        await hal.set_velocity(_v(vx=0.36, ttl_ms=5000))
        assert _最后一条(sim, "vel")["ttl_ms"] == 1000
        await hal.set_velocity(_v(vx=0.36, ttl_ms=10))
        assert _最后一条(sim, "vel")["ttl_ms"] == 50


async def test_前进带死区_比例是死区加直线_零照发零_上限按直线算():
    """2026-10-08 C40221 实测前进 v ≈ 1.09 ×(比例 − 0.10):纯比例换算低速走得比要的慢一半。"""
    async with _台子(fwd_offset=0.1) as (sim, hal):
        assert math.isclose(hal.hal_capabilities().max_vx, (0.5 - 0.1) * 1.2)
        await hal.set_velocity(_v(vx=0.36))
        assert math.isclose(_最后一条(sim, "vel")["fwd"], 0.1 + 0.36 / 1.2)
        await hal.set_velocity(_v(vx=-0.36))
        assert math.isclose(_最后一条(sim, "vel")["fwd"], -(0.1 + 0.36 / 1.2)), "倒着走对称"
        await hal.set_velocity(_v(vx=0.0, wz=0.6))
        a = _最后一条(sim, "vel")
        assert a["fwd"] == 0.0 and math.isclose(a["yaw"], 0.4), "原地转不许带上死区往前蹭"
        got = await hal.set_velocity(_v(vx=5.0))
        assert got.clamped and math.isclose(got.applied_vx, 0.48)
        assert math.isclose(_最后一条(sim, "vel")["fwd"], 0.5), "夹到上限后比例正好是上限"


async def test_超上限夹住并如实报():
    async with _台子() as (sim, hal):
        got = await hal.set_velocity(_v(vx=5.0, wz=-9.0))
        assert got.clamped and not got.rejected
        assert math.isclose(got.applied_vx, 0.6) and math.isclose(got.applied_wz, -0.75)
        a = _最后一条(sim, "vel")
        assert math.isclose(a["fwd"], 0.5) and math.isclose(a["yaw"], -0.5)
        assert abs(a["fwd"]) <= 0.5 and abs(a["yaw"]) <= 0.5, "浮点误差也不许越过旁路的上限"



async def test_换算回比例后浮点越界也夹回来():
    """0.45 × 0.3 ÷ 0.3 = 0.45000000000000007:不再夹一次就越过比例上限(真狗旁路越 0.5 就拒)。"""
    sim = SimAgentServer(port=0)
    await sim.start()
    hal = D1MaxHal("127.0.0.1", sim.port, mps_per_unit=0.3, radps_per_unit=0.3,
                   deadband_mps=0.0, max_fraction=0.45)
    try:
        await hal.connect()
        await hal.acquire_control()
        await hal.set_motion_mode("stand")
        got = await hal.set_velocity(_v(vx=9.0, wz=9.0))
        assert got.clamped and not got.rejected
        a = _最后一条(sim, "vel")
        assert a["fwd"] <= 0.45 and a["yaw"] <= 0.45, a
    finally:
        await hal.close()
        await sim.stop()

async def test_掉头方向可以翻():
    async with _台子(invert_yaw=True) as (sim, hal):
        await hal.set_velocity(_v(wz=0.6))
        assert math.isclose(_最后一条(sim, "vel")["yaw"], -0.4)


async def test_死区_横移_非数_都拒且不发():
    async with _台子() as (sim, hal):
        for cmd, reason in ((_v(vx=0.1), "deadband"), (_v(vx=0.36, vy=0.2), "no_lateral"),
                            (_v(vx=float("nan")), "not_finite"),
                            (_v(wz=float("inf")), "not_finite")):
            got = await hal.set_velocity(cmd)
            assert got.rejected and got.reason == reason, (cmd, got)
        assert "vel" not in [c for c, _ in sim.commands]


async def test_急停时拒_复位后能走():
    async with _台子() as (sim, hal):
        await hal.emergency_stop(True)
        assert await _等(hal.estop_status)
        got = await hal.set_velocity(_v(vx=0.36))
        assert got.rejected and got.reason == "estop"
        assert "vel" not in [c for c, _ in sim.commands]
        await hal.estop_reset()
        assert await _等(lambda: _not(hal.estop_status()))
        assert not (await hal.set_velocity(_v(vx=0.36))).rejected


async def _not(coro):
    return not await coro


async def test_没控制权拒_趴着拒_旁路拒了也如实报(monkeypatch):
    async with _台子(held=False, stand=False) as (sim, hal):
        got = await hal.set_velocity(_v(vx=0.36))
        assert got.rejected and got.reason == "no_control"
    async with _台子(stand=False) as (sim, hal):
        got = await hal.set_velocity(_v(vx=0.36))
        assert got.rejected and got.reason == "not_ready", "趴着的真狗收到 vel 会先 Gait:不下发"
        assert "vel" not in [c for c, _ in sim.commands]

        async def 假装待命():
            return MotionStatus.READY
        monkeypatch.setattr(hal, "motion_status", 假装待命)
        got = await hal.set_velocity(_v(vx=0.36))
        assert got.rejected and got.reason.startswith("sidecar:") and "趴着" in got.reason


async def test_急停有一路不知道_按急停算():
    """旁路进程在客户端刚连上时先补一帧全零(Unknown)的占位状态:不知道就不许走。"""
    from d1max_patrol.protocol.agent_frames import EmergencyStatus

    async with _台子() as (sim, hal):
        sim.estop_hardware = EmergencyStatus.UNKNOWN
        assert await _等(hal.estop_status)
        got = await hal.set_velocity(_v(vx=0.36))
        assert got.rejected and got.reason == "estop"
        assert (await hal.health()).estop
        sim.estop_hardware = EmergencyStatus.RECOVER
        assert await _等(lambda: _not(hal.estop_status()))


async def test_vel回执等不过半秒_不冻住速度环(monkeypatch):
    async with _台子() as (sim, hal):
        seen = {}
        real = hal._b.vel

        async def 记着(*a, **k):
            seen.update(k)
            return await real(*a, **k)
        monkeypatch.setattr(hal._b, "vel", 记着)
        await hal.set_velocity(_v(vx=0.36))
        assert seen["timeout_s"] == 0.5


async def test_ttl到期自己停_里程记下走过的路():
    async with _台子() as (sim, hal):
        await hal.set_velocity(_v(vx=0.48, ttl_ms=300))
        await asyncio.sleep(0.15)
        assert not await hal.stopped()
        assert await _等(hal.stopped, 1.5), "有效期到了狗自己停,HAL 也要看得出来"
        odom = await hal.odometry()
        assert odom.valid and odom.x > 0.05 and odom.frame_id == "odom"


async def test_stop插队_很快确认停了():
    async with _台子() as (sim, hal):
        for _ in range(5):
            await hal.set_velocity(_v(vx=0.48, ttl_ms=300))
            await asyncio.sleep(0.05)
        assert sim.vx > 0
        await hal.stop()
        assert await _等(hal.stopped, 0.5)
        assert sim.vx == 0.0


async def test_刚发了速度_里程还没动也不算停():
    """真狗从静止起步要先 ``Gait``(约 2 s),里程还是零;这时说「停了」会骗上层。"""
    async with _台子() as (sim, hal):
        await hal.set_velocity(_v(vx=0.1 + 0.2, ttl_ms=800))
        assert not await hal.stopped()


async def test_里程不新鲜就无效_定位质量归零():
    async with _台子() as (sim, hal):
        assert (await hal.odometry()).valid
        hal._monotonic = lambda: time.monotonic() + 5.0
        assert not (await hal.odometry()).valid
        assert (await hal.health()).loc_quality == 0.0
        assert not await hal.stopped(), "里程不新鲜就不知道停没停"


async def test_控制权丢了_状态如实反映():
    async with _台子() as (sim, hal):
        sim.drop_control()
        assert await _等(lambda: _not_held(hal))
        assert await hal.motion_status() is MotionStatus.STANDING
        assert not (await hal.health()).control
        got = await hal.set_velocity(_v(vx=0.36))
        assert got.rejected and got.reason == "no_control"


async def _not_held(hal):
    return not (await hal.control_status()).held


async def test_故障帧原样进health与faults():
    async with _台子() as (sim, hal):
        sim.push_fault(2, 7, "左前腿过流")
        sim.push_fault(1, 9, "提示")
        assert await _等(lambda: _n_faults(hal, 2))
        fs = await hal.faults()
        assert [(f.code, f.fatal, f.text) for f in fs] == [("7", True, "左前腿过流"),
                                                           ("9", False, "提示")]
        assert (await hal.health()).faults == ("7", "9")


async def test_姿态受力帧_撞击取峰值读走就清_老旁路进程不发就是无效(monkeypatch):
    async with _台子() as (sim, hal):
        s = await hal.imu()
        assert not s.valid and s.load is None, "没收到过:无效(代理不判)"
        sim.push_imu(0.1, -0.2, 2.5, load=80.0)
        sim.push_imu(0.1, -0.2, 0.3, load=79.0)
        assert await _等(lambda: _imu_load(hal, 79.0))
        s = await hal.imu()
        assert s.valid and (s.roll, s.pitch) == (0.1, -0.2) and s.shock_g == 2.5, "峰值不漏"
        assert (await hal.imu()).shock_g == 0.0, "读走就清"
        monkeypatch.setattr(hal, "_monotonic", lambda: time.monotonic() + 5.0)
        assert not (await hal.imu()).valid, "过期了:无效"


async def _imu_load(hal, want):
    f = hal._b.last_imu
    return f is not None and f.load == want


async def _n_faults(hal, n):
    return len(await hal.faults()) == n


async def test_灯按通道开关_其余执行器与感知抛不支持():
    async with _台子() as (sim, hal):
        await hal.light("front", True)
        assert _最后一条(sim, "light") == {"which": "front", "on": True}
        await hal.light("both", False)
        assert _最后一条(sim, "light") == {"which": "both", "on": False}
        with pytest.raises(ValueError):
            await hal.light("top", True)
        with pytest.raises(HalUnsupported):
            await hal.set_motion_mode("stair")
        for call in (hal.lidar(), hal.ultrasonic(), hal.joints(), hal.contacts(),
                     hal.depth(), hal.thermal(), hal.audio_session(), hal.recharge_start(),
                     hal.recharge_stop(), hal.undock(), hal.recharge_status(),
                     hal.strobe("front", "x", 1.0), hal.siren(True, 1.0), hal.sound("x", 1.0),
                     hal.spotlight(True, 1.0), hal.head(0.0, 0.0), hal.snapshot("front"),
                     hal.stream_url("front")):
            with pytest.raises(HalUnsupported):
                await call
        assert await hal.camera_sources() == ()


async def test_趴下():
    async with _台子() as (sim, hal):
        await hal.set_motion_mode("lie")
        assert await hal.motion_status() is MotionStatus.LYING


async def test_其余站姿不算待命_锁死算趴着():
    """保守:只有 ``General``/``Gait`` 算 READY;``InPlace`` 等站姿报 STANDING,``Locked`` 算趴着。"""
    from d1max_patrol.protocol.agent_frames import MotionStatus as SdkMotion

    async with _台子() as (sim, hal):
        for sdk, want in ((SdkMotion.IN_PLACE, MotionStatus.STANDING),
                          (SdkMotion.STAIR, MotionStatus.STANDING),
                          (SdkMotion.LOCKED, MotionStatus.LYING),
                          (SdkMotion.GAIT, MotionStatus.READY)):
            sim.motion = sdk
            assert await _等(lambda w=want: _motion_is(hal, w)), (sdk, want)


async def _motion_is(hal, want):
    return await hal.motion_status() is want


async def test_停了的阈值按配置():
    """站着的里程噪声比默认阈值大时,配大一点就能确认停了(``--stopped-eps``)。"""
    async with _台子(stopped_eps=0.2) as (sim, hal):
        sim.vx = 0.1                                  # 仿真站着不动,但里程报 0.1 的噪声
        await asyncio.sleep(0.15)
        assert await hal.stopped()
    async with _台子() as (sim, hal):
        sim.vx = 0.1
        await asyncio.sleep(0.15)
        assert not await hal.stopped()


async def test_故障是当前的不是历史_不再报就老化掉(monkeypatch):
    """W00c5a 内部评审阻断:历史里跌倒过一次就永远挂着,站点认不出下一次跌倒。"""
    import d1max_patrol.backends.sidecar_device as sd
    from d1max_adapter_d1max import hal as hal_mod

    async with _台子() as (sim, hal):
        sim.push_fault(2, 3, "机身跌倒")
        assert await _等(lambda: _n_faults(hal, 1))
        real = sd.time.monotonic
        monkeypatch.setattr(sd.time, "monotonic",
                            lambda: real() + hal_mod.FAULT_FRESH_S + 1)
        assert await hal.faults() == ()
        assert (await hal.health()).faults == ()


async def test_头尾方向从旁路进程的状态来():
    """W11a:旁路进程报的 head 进 Health;调过来、不知道都如实报。"""
    async with _台子() as (sim, hal):
        assert (await hal.health()).head == "head"
        sim.head = 2
        assert await _等(lambda: _head(hal, "tail"))
        sim.head = 0
        assert await _等(lambda: _head(hal, "unknown"))


async def _head(hal, want):
    return (await hal.health()).head == want


async def test_机身系_狗尾为前且SDK跟着调头_里程朝向不跳_往狗头走还是正速度():
    """W09i §1:HAL 一律机身系。仿真旁路进程照「SDK 跟着调头」报里程(调头那一刻朝向跳 π)。"""
    async with _台子() as (sim, hal):
        before = await hal.odometry()
        sim.head = 2
        assert await _等(lambda: _head(hal, "tail"))
        o = await hal.odometry()
        assert abs(math.remainder(o.yaw - before.yaw, 2 * math.pi)) < 1e-9, "机身朝向不跳"
        assert sim.yaw != before.yaw, "前提:SDK 报的跳了"
        r = await hal.set_velocity(_v(vx=0.6))           # 机身系往狗头走
        assert not r.rejected
        await asyncio.sleep(0.2)
        assert sim.vx < 0, "SDK 那头是往后(它的「往前」是狗尾)"
        o = await hal.odometry()
        assert o.vx > 0, "机身系的前进速度是正的"
        await hal.stop()


async def test_机身系_SDK不跟着调头就不换算():
    sim = SimAgentServer(port=0, follows_head=False)
    await sim.start()
    hal = D1MaxHal("127.0.0.1", sim.port, mps_per_unit=1.2, radps_per_unit=1.5,
                   deadband_mps=0.25)
    try:
        await hal.connect()
        await hal.acquire_control()
        await hal.set_motion_mode("stand")
        y0 = (await hal.odometry()).yaw
        sim.head = 2
        assert await _等(lambda: _head(hal, "tail"))
        assert (await hal.odometry()).yaw == y0
        assert not (await hal.set_velocity(_v(vx=0.6))).rejected
        await asyncio.sleep(0.2)
        assert sim.vx > 0
        await hal.stop()
    finally:
        await hal.close()
        await sim.stop()


async def test_速度档位不对就拒速度命令_旁路进程设回去之后照常():
    """W12:换算只对旁路进程要的那一档成立。"""
    async with _台子() as (sim, hal):
        assert not (await hal.set_velocity(_v(vx=0.5))).rejected
        await hal.stop()
        sim.speed_level = 2
        assert await _等(lambda: _拒(hal, "speed_level"))
        assert await _等(lambda: _收(hal), timeout_s=3.0), "旁路进程 1 s 后设回低速"
        await hal.stop()


async def test_旁路进程不管档位就不核():
    sim = SimAgentServer(port=0, speed_level_want=0)
    await sim.start()
    hal = D1MaxHal("127.0.0.1", sim.port)
    try:
        await hal.connect()
        await hal.acquire_control()
        await hal.set_motion_mode("stand")
        sim.speed_level = 3
        await asyncio.sleep(0.3)
        assert not hal.speed_level_wrong()
        assert not (await hal.set_velocity(_v(vx=0.4))).rejected
        await hal.stop()
    finally:
        await hal.close()
        await sim.stop()


def test_默认换算按SDK文档的低速档():
    """W12:比例 1.0 = 1.0 m/s、1.5 rad/s,上限 0.5:最快 0.5 m/s(跟原来的实际最快一样)。"""
    hal = D1MaxHal("127.0.0.1", 1)
    caps = hal.hal_capabilities()
    assert caps.max_vx == pytest.approx(0.5) and caps.max_wz == pytest.approx(0.75)
    assert caps.deadband_vx == pytest.approx(0.2)


async def _拒(hal, why):
    r = await hal.set_velocity(_v(vx=0.5))
    return r.rejected and r.reason == why


async def _收(hal):
    return not (await hal.set_velocity(_v(vx=0.5))).rejected


async def test_W26复查3_软急停确认_要新鲜状态明确说开着_不知道不算(monkeypatch):
    hal = D1MaxHal(mps_per_unit=0.4, radps_per_unit=1.0, deadband_mps=0.05, max_fraction=0.5)
    assert await hal.estop_status() and not await hal.soft_estop_confirmed(), \
        "没状态帧:放行按急停算,确认按没确认算"
    async with _台子() as (sim, hal):
        assert await _等(lambda: _有状态(hal))
        assert not await hal.soft_estop_confirmed()
        await hal.emergency_stop(True)
        assert await _等(hal.soft_estop_confirmed)
        monkeypatch.setattr(hal, "_monotonic", lambda: time.monotonic() + 5.0)
        assert not await hal.soft_estop_confirmed(), "状态过期了:不算确认"


async def _有状态(hal):
    return hal._b.last_state is not None
