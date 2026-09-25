"""``SidecarDeviceBackend`` 对着仿真旁路进程的行为。

这些用例大半是从 2026-09-01 现场那几条教训倒推出来的:控制权是独占的、
释放不得、站起要时间、量给小了会安静地不动。测的是"这些坑还在不在被挡住"。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from d1max_patrol.backends.base import (
    BatteryEvent,
    ControlLostEvent,
    DeviceBackendError,
    DevicePoseEvent,
    FaultEvent,
)
from d1max_patrol.backends.sidecar_device import (
    MAX_WALK_SECONDS,
    MAX_WALK_SPEED,
    SidecarDeviceBackend,
    parse_agent_endpoint,
)
from d1max_patrol.protocol.agent_frames import PROTO_VERSION, MotionStatus
from d1max_sim import agent_server
from d1max_sim.agent_server import SimAgentServer


@contextlib.asynccontextmanager
async def _pair(**sim_kwargs) -> AsyncIterator[tuple[SimAgentServer,
                                                     SidecarDeviceBackend]]:
    sim = SimAgentServer(port=0, **sim_kwargs)
    await sim.start()
    backend = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=5.0)
    try:
        await backend.connect()
        yield sim, backend
    finally:
        await backend.close()
        await sim.stop()


async def _until(predicate, timeout_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等条件成立超时")


async def _drain(queue: asyncio.Queue, kind: type, timeout_s: float = 3.0):
    """从事件队列里取到第一条指定类型的事件。"""
    async def _pump():
        while True:
            event = await queue.get()
            if isinstance(event, kind):
                return event
    return await asyncio.wait_for(_pump(), timeout_s)


# ---------------------------------------------------------------- 连接


async def test_连上后能读到旁路进程的自我介绍():
    async with _pair() as (_sim, backend):
        assert backend.connected
        assert backend.hello is not None
        assert backend.hello.sdk == "0.1.1"
        assert await backend.has_control()


async def test_没人监听时报错说得清是旁路进程没起():
    backend = SidecarDeviceBackend("127.0.0.1", 1, ack_timeout_s=0.5)
    with pytest.raises(DeviceBackendError, match="连不上运控旁路进程"):
        await backend.connect()
    assert not backend.connected


async def test_协议版本对不上就拒绝连接(monkeypatch):
    # 新旁路进程配旧 Python(或反过来)必须当场炸,而不是连上之后字段对不上
    # 慢慢出怪事 —— 现场最不需要的就是半死不活的链路。
    monkeypatch.setattr(agent_server, "PROTO_VERSION", PROTO_VERSION + 1)
    sim = SimAgentServer(port=0)
    await sim.start()
    backend = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0)
    try:
        with pytest.raises(DeviceBackendError, match="协议版本对不上"):
            await backend.connect()
        assert not backend.connected
    finally:
        await backend.close()
        await sim.stop()


async def test_重复connect是空操作():
    async with _pair() as (_sim, backend):
        hello = backend.hello
        await backend.connect()
        assert backend.hello is hello


async def test_close之后再close不炸():
    async with _pair() as (_sim, backend):
        await backend.close()
        await backend.close()
        assert not backend.connected


# ---------------------------------------------------------------- 控制权


async def test_上装占着时申请控制权被拒且错误原文照抄真机():
    # 真机报的就是 "Controlled denial of service"(清单 #40/#47),
    # 照抄它,现场看日志能一眼对上。
    async with _pair(deny_control=True) as (_sim, backend):
        assert not await backend.has_control()
        with pytest.raises(DeviceBackendError,
                           match="Controlled denial of service"):
            await backend.acquire_control()


async def test_没握着控制权时动作指令连发都不发():
    async with _pair(deny_control=True) as (sim, backend):
        with pytest.raises(DeviceBackendError, match="没握着控制权"):
            await backend.stand()
        # 关键:命令根本没发出去,而不是发出去被拒。少往那条会话上扔垃圾。
        assert sim.commands == []


async def test_release_control不会真的释放SDK会话():
    """这是本文件里最要紧的一条。

    上层的 finally / 异常收尾天然会调 release_control。要是它真去
    ReleaseControl,上装立刻收回控制权,整台 RK3588 就得重启(清单 #47)——
    等于把"巡检程序崩了一次"升级成"今天不用干活了"。
    """
    async with _pair() as (sim, backend):
        await backend.release_control()
        assert not await backend.has_control()
        # 一条 SDK 侧的命令都没发出去
        assert not any(cmd == "shutdown" for cmd, _ in sim.commands)
        # 而且再 acquire 就能接着开车 —— 会话根本没断过
        await backend.acquire_control()
        assert await backend.has_control()


async def test_close之后新后端还能接上同一条会话():
    # 巡检程序重启不该影响 SDK 会话,这是整个旁路进程架构的立身之本。
    sim = SimAgentServer(port=0)
    await sim.start()
    try:
        first = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=5.0)
        await first.connect()
        await first.stand()
        await first.close()

        second = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=5.0)
        await second.connect()
        assert await second.has_control()
        await _until(lambda: second.last_state is not None)
        assert await second.motion_status() is MotionStatus.GENERAL
        await second.close()
    finally:
        await sim.stop()


async def test_shutdown_agent才是真的交还控制权():
    async with _pair() as (sim, backend):
        await backend.shutdown_agent()
        assert not await backend.has_control()
        assert ("shutdown", {}) in sim.commands
        assert not backend.connected


async def test_控制权被上装收回时广播事件():
    async with _pair() as (sim, backend):
        with backend.subscription() as queue:
            sim.drop_control("上装收回控制权")
            event = await _drain(queue, ControlLostEvent)
        assert "上装收回" in event.reason
        assert not await backend.has_control()


async def test_控制权丢了之后动作指令立刻被挡住():
    async with _pair() as (sim, backend):
        with backend.subscription() as queue:
            sim.drop_control()
            await _drain(queue, ControlLostEvent)
        with pytest.raises(DeviceBackendError, match="没握着控制权"):
            await backend.walk(seconds=1.0, forward=0.4)


# ---------------------------------------------------------------- 动作


async def test_站起是个过程不是瞬间():
    # 真机实测约 6s(清单 #36)。上层若假设站起是原子的,就该在这里暴露。
    async with _pair() as (_sim, backend):
        await _until(lambda: backend.last_state is not None)
        assert await backend.motion_status() is MotionStatus.LIE_DOWN
        await backend.stand()
        await backend.wait_motion(MotionStatus.GENERAL, timeout_s=3.0)


async def test_趴下():
    async with _pair() as (_sim, backend):
        await backend.stand()
        await backend.wait_motion(MotionStatus.GENERAL, timeout_s=3.0)
        await backend.lie()
        await backend.wait_motion(MotionStatus.LIE_DOWN, timeout_s=3.0)


async def test_等状态超时时报错说清现在是什么状态():
    async with _pair() as (_sim, backend):
        await _until(lambda: backend.last_state is not None)
        with pytest.raises(DeviceBackendError, match="当前是 LieDown"):
            await backend.wait_motion(MotionStatus.GAIT, timeout_s=0.2)


async def test_趴着直接走会被旁路进程拒绝():
    async with _pair() as (_sim, backend):
        with pytest.raises(DeviceBackendError, match="先 stand"):
            await backend.walk(seconds=1.0, forward=0.4)


async def test_量给够了才真的走():
    async with _pair() as (sim, backend):
        await backend.stand()
        await backend.wait_motion(MotionStatus.GENERAL, timeout_s=3.0)
        await backend.walk(seconds=1.0, forward=0.4)
        assert sim.distance > 0.3


async def test_量给太小时安静地不动这正是现场那次站起不走():
    """清单 #37:`fwd=0.11` 几乎不动,当时被误判成"模式不对"。

    真机在这种情况下**不报错**,它就是不动。仿真也不报错 —— 上层要靠
    里程去发现"发了走的命令但没走",而不是指望一个异常。
    """
    async with _pair() as (sim, backend):
        await backend.stand()
        await backend.wait_motion(MotionStatus.GENERAL, timeout_s=3.0)
        await backend.walk(seconds=1.0, forward=0.11)
        assert sim.distance == 0.0


@pytest.mark.parametrize("seconds", [0.0, -1.0, MAX_WALK_SECONDS + 0.1])
async def test_行走时长越界在本地就挡住(seconds):
    async with _pair() as (sim, backend):
        with pytest.raises(DeviceBackendError, match="行走时长"):
            await backend.walk(seconds=seconds, forward=0.4)
        assert not any(cmd == "walk" for cmd, _ in sim.commands)


@pytest.mark.parametrize("field,value", [
    ("forward", MAX_WALK_SPEED + 0.01),
    ("forward", -MAX_WALK_SPEED - 0.01),
    ("lateral", 1.0),
    ("yaw", -2.0),
    ("forward", float("nan")),
    ("forward", float("inf")),
])
async def test_速度越界在本地就挡住(field, value):
    # 安全阀在客户端这一侧,不指望旁路进程或机器去兜底。现场旁边站着人。
    async with _pair() as (sim, backend):
        kwargs = {"seconds": 1.0, "forward": 0.4}
        kwargs[field] = value
        with pytest.raises(DeviceBackendError, match=field):
            await backend.walk(**kwargs)
        assert not any(cmd == "walk" for cmd, _ in sim.commands)


async def test_软急停不需要控制权也能发():
    # 急停是安全动作,任何时候都得能发出去 —— 包括控制权已经丢了的时候。
    async with _pair(deny_control=True) as (sim, backend):
        await backend.emergency_stop(True)
        assert ("estop", {"on": True}) in sim.commands


async def test_急停生效后拒绝动作():
    async with _pair() as (_sim, backend):
        await backend.emergency_stop(True)
        with pytest.raises(DeviceBackendError, match="软急停生效中"):
            await backend.stand()
        await backend.emergency_stop(False)
        await backend.stand()


@pytest.mark.parametrize("call,which", [
    ("set_light", "both"),
    ("set_front_light", "front"),
    ("set_back_light", "back"),
])
async def test_补光灯分位控制(call, which):
    async with _pair() as (sim, backend):
        await getattr(backend, call)(True)
        assert ("light", {"which": which, "on": True}) in sim.commands


async def test_转头映射到ControlHead的两个轴():
    async with _pair() as (sim, backend):
        await backend.set_gimbal(pitch=0.2, yaw=-0.1)
        assert ("head", {"yaw": -0.1, "pitch": 0.2}) in sim.commands


async def test_拍照明确抛错而不是假装能用():
    # SDK 0.1.1 跟相机沾边的只有 UpdateCameraBitrate,不出图。
    async with _pair() as (_sim, backend):
        with pytest.raises(DeviceBackendError, match="RTSP"):
            await backend.take_photo()


# ---------------------------------------------------------------- 遥测


async def test_电量来自遥测且取低的那块():
    async with _pair(battery=71.0) as (sim, backend):
        await _until(lambda: backend.last_state is not None)
        assert await backend.battery() == 71.0
        sim.battery1 = 17.0
        await _until(lambda: backend.last_state is not None
                     and backend.last_state.battery == 17.0)
        assert await backend.battery() == 17.0


async def test_还没收到状态帧时读电量抛错而不是猜一个():
    # 不猜、不返回 0 —— "读不到"和"没电了"是两件事,巡检决策靠它区分。
    backend = SidecarDeviceBackend()
    with pytest.raises(DeviceBackendError, match="读不到电量"):
        await backend.battery()
    with pytest.raises(DeviceBackendError, match="读不到运动状态"):
        await backend.motion_status()


async def test_电量变了才广播不变就闭嘴():
    # 遥测 20Hz,要是每帧都发一条 BatteryEvent,订阅者的无界队列会被灌爆。
    async with _pair(battery=71.0) as (sim, backend):
        await _until(lambda: backend.last_state is not None)
        with backend.subscription() as queue:
            sim.set_battery(17.0)
            event = await _drain(queue, BatteryEvent)
            assert event.percent == 17.0
            with pytest.raises(asyncio.TimeoutError):
                await _drain(queue, BatteryEvent, timeout_s=0.3)


async def test_里程转成位姿事件():
    async with _pair() as (_sim, backend):
        with backend.subscription() as queue:
            event = await _drain(queue, DevicePoseEvent)
        assert event.pose.position.x == 0.0
        await _until(lambda: backend.last_odom is not None)


async def test_走完之后里程动了():
    async with _pair() as (_sim, backend):
        await backend.stand()
        await backend.wait_motion(MotionStatus.GENERAL, timeout_s=3.0)
        await backend.walk(seconds=1.0, forward=0.5)
        await _until(lambda: backend.last_odom is not None
                     and backend.last_odom.x > 0.3)


async def test_故障帧转成故障事件且分级():
    async with _pair() as (sim, backend):
        with backend.subscription() as queue:
            sim.push_fault(2, 17, "腿过热")
            event = await _drain(queue, FaultEvent)
        assert event.fatal is True
        assert "code=17" in event.items[0]
        assert "腿过热" in event.items[0]


async def test_低等级故障不算致命():
    async with _pair() as (sim, backend):
        with backend.subscription() as queue:
            sim.push_fault(1, 3, "补光灯电压偏低")
            event = await _drain(queue, FaultEvent)
        assert event.fatal is False


async def test_看不懂的帧不会拖垮链路():
    # 固件/旁路进程升级完全可能多出我们还不认识的帧类型。丢掉,继续跑。
    async with _pair() as (sim, backend):
        sim.push_raw('{"t":"quantum_flux","v":1}')
        sim.push_raw("this is not json at all")
        sim.push_raw("[1, 2, 3]")
        await backend.stand()
        await backend.wait_motion(MotionStatus.GENERAL, timeout_s=3.0)


async def test_旁路进程死掉时在等的命令立刻失败而不是干等超时():
    sim = SimAgentServer(port=0)
    await sim.start()
    backend = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=30.0)
    await backend.connect()
    try:
        with backend.subscription() as queue:
            task = asyncio.ensure_future(backend.stand())
            await asyncio.sleep(0.05)
            await sim.stop()
            with pytest.raises(DeviceBackendError, match="连接断了"):
                await asyncio.wait_for(task, 5.0)
            event = await _drain(queue, ControlLostEvent)
        assert "连接断开" in event.reason
    finally:
        await backend.close()


async def test_断开之后再发命令报的是没连上():
    async with _pair() as (_sim, backend):
        await backend.close()
        with pytest.raises(DeviceBackendError, match="没连上旁路进程"):
            await backend.emergency_stop(True)


# ---------------------------------------------------------------- 地址解析


@pytest.mark.parametrize("text,expected", [
    ("127.0.0.1:8090", ("127.0.0.1", 8090)),
    ("  10.0.0.5:9000  ", ("10.0.0.5", 9000)),
    ("8090", ("127.0.0.1", 8090)),
    ("192.168.168.168:1", ("192.168.168.168", 1)),
])
def test_旁路进程地址解析(text, expected):
    assert parse_agent_endpoint(text) == expected


@pytest.mark.parametrize("text,match", [
    ("", "不能为空"),
    ("host:abc", "不是整数"),
    ("host:0", "超范围"),
    ("host:65536", "超范围"),
])
def test_旁路进程地址解析拒绝垃圾(text, match):
    with pytest.raises(ValueError, match=match):
        parse_agent_endpoint(text)


async def test_vel在本端就挡住越界_不发出去():
    """W00d:``vel`` 的比例上限与 ttl 范围本端先查一遍,越界的一条都不发给旁路进程。"""
    async with _pair() as (sim, backend):
        await backend.acquire_control()
        await backend.stand()
        for fwd, yaw, ttl in ((MAX_WALK_SPEED + 0.01, 0.0, 300), (0.3, -0.6, 300),
                              (float("nan"), 0.0, 300), (0.3, 0.0, 49), (0.3, 0.0, 1001)):
            with pytest.raises(DeviceBackendError):
                await backend.vel(fwd, 0.0, yaw, ttl)
        assert "vel" not in [c for c, _ in sim.commands]
        await backend.vel(0.3, 0.0, 0.0, 300)
        assert [a for c, a in sim.commands if c == "vel"] == [
            {"fwd": 0.3, "lat": 0.0, "yaw": 0.0, "ttl_ms": 300}]


async def test_vel没控制权本端就拒():
    async with _pair(held=False) as (sim, backend):
        with pytest.raises(DeviceBackendError):
            await backend.vel(0.3, 0.0, 0.0, 300)
        assert "vel" not in [c for c, _ in sim.commands]
