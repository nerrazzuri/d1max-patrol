"""停车/急停链路:**真后端**(``SidecarDeviceBackend``)对着仿真旁路进程。

这一组存在的原因是一个"测试说谎"的洞:以前 ``Teleop.stop`` 和守死人开关
发的是 ``walk(0, 0, 0, 0)``,测试桩照单全收、全绿;真后端要求时长为正,
当场拒掉,错误又被 ``suppress`` 吞了 —— 真狗上停车一直是空的,急停也从来
没发过 ``SoftEmergencyStop``。所以这里**不用桩**,只用真后端 + 仿真旁路进程,
断言线上到底发出去了什么。

另一半是插队:旧的旁路进程一条连接上的命令是串行的,急停排在还没走完的
walk 后面。这里量急停回执要多久、排队的 walk 有没有被作废。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

import pytest

from d1max_patrol.app.teleop import EmergencyStopFailed, Teleop
from d1max_patrol.backends.base import DeviceBackendError
from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
from d1max_patrol.protocol.agent_frames import EmergencyStatus, MotionStatus
from d1max_sim import agent_server
from d1max_sim.agent_server import WALK_CANCELLED, SimAgentServer

#: 仿真里一拍 walk 被拉长到这么久,好让急停确实落在"正在走"的时候。
LONG_STRIDE_S = 3.0

#: 插队的急停/停车,回执最多等这么久。旧的串行旁路进程在这个场景下要等
#: 满 ``LONG_STRIDE_S`` 以上,所以这个上限能把"没插队"和"插队了"分开。
URGENT_BUDGET_S = 0.5


class _Engine:
    """Teleop 只看引擎这几样。"""

    def __init__(self) -> None:
        self.running = False
        self.yielding = False
        self.aborts: list[str] = []

    def add_busy_check(self, _check) -> None: ...

    async def abort(self, reason: str) -> None:
        self.aborts.append(reason)
        self.running = False


@contextlib.asynccontextmanager
async def _pair(**sim_kwargs) -> AsyncIterator[tuple[SimAgentServer,
                                                     SidecarDeviceBackend]]:
    sim = SimAgentServer(port=0, **sim_kwargs)
    await sim.start()
    backend = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=10.0)
    try:
        await backend.connect()
        yield sim, backend
    finally:
        await backend.close()
        await sim.stop()


async def _until(predicate, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等条件成立超时")


def _sent(sim: SimAgentServer, cmd: str) -> list[dict]:
    return [args for c, args in sim.commands if c == cmd]


async def _standing(sim, backend, monkeypatch) -> None:
    """站起来,再把一拍拉长 —— 站起用的也是 STAND_SECONDS,所以要站完再改。"""
    await backend.stand()
    monkeypatch.setattr(agent_server, "STAND_SECONDS", LONG_STRIDE_S)
    assert sim.motion is MotionStatus.GENERAL


# ------------------------------------------------------------ Teleop 发了什么


async def test_遥控停车发的是halt不是walk零():
    async with _pair() as (sim, backend):
        t = Teleop(backend, _Engine(), video_gate=lambda: "")
        try:
            await t.stop()
        finally:
            await t.aclose()
        assert _sent(sim, "halt"), "停车命令根本没发出去"
        assert _sent(sim, "walk") == [], "停车不许再借用 walk"


async def test_零输入的一拍也是真停车():
    async with _pair() as (sim, backend):
        t = Teleop(backend, _Engine(), video_gate=lambda: "")
        try:
            await t.pulse(0.0, 0.0, 0.0)
        finally:
            await t.aclose()
        assert _sent(sim, "halt")


async def test_心跳断了守死人开关真的发出停车(monkeypatch):
    async with _pair() as (sim, backend):
        await backend.stand()
        t = Teleop(backend, _Engine(), video_gate=lambda: "",
                   watch_period_s=0.01, heartbeat_timeout_s=0.1)
        try:
            # 这一拍在仿真里要走 STAND_SECONDS(0.3s),比心跳超时(0.1s)长:
            # 守死人开关在这一拍**走到一半**就发停车,旁路进程把这一拍打断。
            with pytest.raises(DeviceBackendError, match=WALK_CANCELLED):
                await t.pulse(0.4, 0.0, 0.0, seconds=0.2)
            await _until(lambda: bool(_sent(sim, "halt")), timeout_s=3.0)
        finally:
            await t.aclose()
        assert not t.active


async def test_画面掉了守死人开关真的发出停车():
    why = [""]
    async with _pair() as (sim, backend):
        await backend.stand()
        t = Teleop(backend, _Engine(), video_gate=lambda: why[0],
                   watch_period_s=0.01)
        try:
            await t.pulse(0.4, 0.0, 0.0, seconds=0.2)
            why[0] = "前相机没画面"
            await _until(lambda: bool(_sent(sim, "halt")), timeout_s=3.0)
        finally:
            await t.aclose()


async def test_急停发出软急停和停车并打断任务():
    async with _pair() as (sim, backend):
        engine = _Engine()
        engine.running = True
        t = Teleop(backend, engine, video_gate=lambda: "")
        try:
            await t.emergency_stop("页面按了急停")
        finally:
            await t.aclose()
        assert _sent(sim, "estop") == [{"on": True}]
        assert _sent(sim, "halt")
        assert sim.estop_software is EmergencyStatus.STOP
        assert engine.aborts == ["页面按了急停"]


async def test_急停在控制权被占时也发得出去():
    """控制权丢了的时候恰恰最可能要急停。软急停和停车都不许要控制权。"""
    async with _pair(deny_control=True) as (sim, backend):
        t = Teleop(backend, _Engine(), video_gate=lambda: "")
        try:
            await t.emergency_stop()
        finally:
            await t.aclose()
        assert _sent(sim, "estop") == [{"on": True}]
        assert _sent(sim, "halt")


async def test_急停链路断了要报错不许装作停了():
    """链路断了:软急停和停车都发不出去。以前这里静默成功,页面写「已急停」。"""
    async with _pair() as (_sim, backend):
        engine = _Engine()
        engine.running = True
        t = Teleop(backend, engine, video_gate=lambda: "")
        await backend.close()
        try:
            with pytest.raises(EmergencyStopFailed) as info:
                await t.emergency_stop()
        finally:
            await t.aclose()
        assert "软急停" in str(info.value)
        assert "停车" in str(info.value)
        # 一步失败不挡下一步:任务照样被打断。
        assert engine.aborts


async def test_解除急停():
    async with _pair() as (sim, backend):
        t = Teleop(backend, _Engine(), video_gate=lambda: "")
        try:
            await t.emergency_stop()
            await t.release_emergency_stop()
        finally:
            await t.aclose()
        assert _sent(sim, "estop") == [{"on": True}, {"on": False}]
        assert sim.estop_software is EmergencyStatus.RECOVER


# ------------------------------------------------------------ 插队


async def test_急停不排在正在走的walk后面(monkeypatch):
    async with _pair() as (sim, backend):
        await _standing(sim, backend, monkeypatch)
        walking = asyncio.ensure_future(backend.walk(5.0, 0.4))
        queued = asyncio.ensure_future(backend.walk(5.0, 0.4))
        await _until(lambda: sim.motion is MotionStatus.GAIT)

        started = time.monotonic()
        await backend.emergency_stop(True)
        took = time.monotonic() - started
        assert took < URGENT_BUDGET_S, f"急停回执等了 {took:.2f}s —— 没插队"

        for task in (walking, queued):
            with pytest.raises(DeviceBackendError, match=WALK_CANCELLED):
                await asyncio.wait_for(task, 2.0)
        assert sim.cancelled_walks == 2
        # 正在走的那一拍只走了一截:整拍是 5s × 0.48m/s = 2.4m。
        assert sim.distance < 1.0
        assert (sim.vx, sim.vy, sim.vyaw) == (0.0, 0.0, 0.0)


async def test_停车也插队而且作废排队的walk(monkeypatch):
    async with _pair() as (sim, backend):
        await _standing(sim, backend, monkeypatch)
        walking = asyncio.ensure_future(backend.walk(5.0, 0.4))
        queued = asyncio.ensure_future(backend.walk(5.0, 0.4))
        await _until(lambda: sim.motion is MotionStatus.GAIT)

        started = time.monotonic()
        await backend.halt()
        took = time.monotonic() - started
        assert took < URGENT_BUDGET_S, f"停车回执等了 {took:.2f}s —— 没插队"
        for task in (walking, queued):
            with pytest.raises(DeviceBackendError, match=WALK_CANCELLED):
                await asyncio.wait_for(task, 2.0)

        # 停车不是急停:停完之后新的一拍照常能走。
        monkeypatch.setattr(agent_server, "STAND_SECONDS", 0.05)
        await backend.walk(0.5, 0.4)
        assert sim.estop_software is EmergencyStatus.RECOVER


async def test_客户端断开正在走的walk收手(monkeypatch):
    """巡检程序崩了/重启了:没人等这一拍的回执,也没人看着狗了。"""
    async with _pair() as (sim, backend):
        await _standing(sim, backend, monkeypatch)
        walking = asyncio.ensure_future(backend.walk(5.0, 0.4))
        await _until(lambda: sim.motion is MotionStatus.GAIT)
        await backend.close()
        with pytest.raises(DeviceBackendError):
            await asyncio.wait_for(walking, 2.0)
        await _until(lambda: sim.motion is MotionStatus.GENERAL, timeout_s=1.0)
        assert (sim.vx, sim.vy, sim.vyaw) == (0.0, 0.0, 0.0)


async def test_旧协议的旁路进程连不上(monkeypatch):
    """协议 1 的旁路进程不认识 halt、急停也不插队 —— 新巡检程序配它,停车会
    **无声地**不灵。必须在握手时就拒绝,逼现场先重编 patrol_agent。"""
    monkeypatch.setattr(agent_server, "PROTO_VERSION", 1)
    sim = SimAgentServer(port=0)
    await sim.start()
    backend = SidecarDeviceBackend("127.0.0.1", sim.port, ack_timeout_s=2.0)
    try:
        with pytest.raises(DeviceBackendError, match="协议版本对不上"):
            await backend.connect()
    finally:
        await backend.close()
        await sim.stop()
