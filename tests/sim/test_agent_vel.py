"""仿真旁路的 ``vel``(协议 v3,W00d 设计决定一 A):带有效期的持续速度。立刻回执;有效期内
一直走,到期自停;新的 ``vel`` 覆盖旧的并续期;``halt``/``estop`` 插队作废;趴着、急停、没控制权、
超限都拒。跟 ``motion/patrol_agent.cpp`` 的 ``DoVel`` 同一套语义。"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from d1max_patrol.protocol.agent_frames import PROTO_VERSION, Ack
from d1max_sim.agent_server import SimAgentServer
from tests.sim.test_agent_server import _client

ROOT = Path(__file__).resolve().parents[2]


def test_协议是v3_跟C加加旁路进程的常量一致():
    src = (ROOT / "motion" / "patrol_agent.cpp").read_text(encoding="utf-8")
    m = re.search(r"static const int kProtoVersion = (\d+);", src)
    assert m and int(m.group(1)) == PROTO_VERSION == 3


async def _站起(sim: SimAgentServer, client) -> None:
    await client.recv()                                   # hello
    ack = await client.call("stand", timeout_s=10)
    assert ack.ok


async def test_vel立刻回执_有效期内一直走_到期自停():
    async with _client() as (sim, client):
        await _站起(sim, client)
        t0 = time.monotonic()
        ack = await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)
        assert ack.ok and time.monotonic() - t0 < 0.2, "vel 是立刻回执的,不等走完"
        await asyncio.sleep(0.15)
        assert sim.vx > 0
        await asyncio.sleep(0.4)
        assert sim.vx == 0.0, "有效期到了自己停"
        assert sim.x > 0.05


async def test_续期就一直走_halt插队立刻停():
    async with _client() as (sim, client):
        await _站起(sim, client)
        for _ in range(6):
            assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=300)).ok
            await asyncio.sleep(0.1)
        assert sim.vx > 0, "每拍续一次,中间不该停"
        assert (await client.call("halt")).ok
        await asyncio.sleep(0.08)
        assert sim.vx == 0.0


async def test_趴着_急停_没控制权_超限_ttl不对_都拒():
    async with _client() as (sim, client):
        await client.recv()
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "趴着" in ack.error
        assert (await client.call("stand", timeout_s=10)).ok
        for bad in ({"fwd": 0.9}, {"yaw": -0.6}, {"ttl_ms": 0}, {"ttl_ms": 5000},
                    {"fwd": float("nan")}):
            args = {"fwd": 0.3, "lat": 0.0, "yaw": 0.0, "ttl_ms": 300} | bad
            ack = await client.call("vel", **args)
            assert not ack.ok, bad
        assert (await client.call("estop", on=True)).ok
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "急停" in ack.error
    async with _client(held=False) as (sim, client):
        await client.recv()
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "Controlled denial" in ack.error


async def test_低于死区的比例_回执ok但不动():
    """真机的样子:量太小就只是原地蹭,不报错(清单 #37)。适配器那层自己拦死区。"""
    async with _client() as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.05, lat=0.0, yaw=0.0, ttl_ms=300)).ok
        await asyncio.sleep(0.15)
        assert sim.vx == 0.0 and sim.x == 0.0


async def test_vel不排在walk后面():
    async with _client() as (sim, client):
        await _站起(sim, client)
        await client.send("walk", seconds=5.0, fwd=0.3, lat=0.0, yaw=0.0)
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        ack = await client.call("vel", fwd=0.2, lat=0.0, yaw=0.0, ttl_ms=300)
        assert isinstance(ack, Ack) and time.monotonic() - t0 < 0.3


async def test_锁死与硬急停也拒_跟CPP的DoVel一致():
    from d1max_patrol.protocol.agent_frames import EmergencyStatus, MotionStatus

    async with _client() as (sim, client):
        await _站起(sim, client)
        sim.motion = MotionStatus.LOCKED
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "锁死" in ack.error
        sim.motion = MotionStatus.GENERAL
        sim.estop_hardware = EmergencyStatus.STOP
        ack = await client.call("vel", fwd=0.3, lat=0.0, yaw=0.0, ttl_ms=300)
        assert not ack.ok and "急停" in ack.error


def test_CPP运动安全门_行为测试编译并通过(tmp_path):
    """W00d 外审阻断 1:``motion/vel_gate.hpp`` 用假 SDK 跑行为测试(``motion/test_vel_gate.cpp``):
    halt/急停与 vel 登记同一把锁、急停闩锁、状态变坏作废且不复活、拿锁后与 Gait 后现查、
    并发压测。"""
    import shutil
    import subprocess

    cxx = shutil.which("g++")
    if cxx is None:
        import pytest
        pytest.skip("没有 g++")
    exe = tmp_path / "test_vel_gate"
    subprocess.run([cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-o", str(exe), str(ROOT / "motion" / "test_vel_gate.cpp")],
                   check=True, capture_output=True, timeout=180)
    got = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "全部通过" in got.stdout


def test_旁路进程的vel全走运动安全门_常量两边一致():
    from d1max_patrol.backends.sidecar_device import MAX_WALK_SPEED, VEL_TTL_MAX_MS, VEL_TTL_MIN_MS

    hdr = (ROOT / "motion" / "vel_gate.hpp").read_text(encoding="utf-8")
    for name, want in (("kMaxFraction", MAX_WALK_SPEED), ("kTtlMinMs", VEL_TTL_MIN_MS),
                       ("kTtlMaxMs", VEL_TTL_MAX_MS)):
        m = re.search(rf"constexpr \w+ {name} = ([0-9.]+);", hdr)
        assert m and float(m.group(1)) == want, name
    src = (ROOT / "motion" / "patrol_agent.cpp").read_text(encoding="utf-8")

    def body(sig: str) -> str:
        i = src.index(sig)
        return src[i:src.index("\n}\n", i)]
    assert "g_gate.Register(" in body("static Outcome DoVel(")
    assert "g_gate.Cancel()" in body("static Outcome DoHalt(")
    estop = body("static Outcome DoEstop(")
    assert estop.index("g_gate.LatchEstop(true)") < estop.index("SoftEmergencyStop"), \
        "先闩本地急停,再跟 SDK 说"
    assert estop.index("g_gate.LatchEstop(false)") > estop.index("SoftEmergencyStop")
    assert "g_gate.OnState(" in body("void OnRobotStateData(")
    assert "velgate::Driver<" in body("static void VelLoop(")
    assert "g_vel_mtx" not in src and "g_client->Move" not in body("static void VelLoop(")


async def test_走着的时候硬急停或趴下_目标作废_恢复了也不接着走():
    from d1max_patrol.protocol.agent_frames import EmergencyStatus, MotionStatus

    async with _client() as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)).ok
        await asyncio.sleep(0.12)
        assert sim.vx > 0
        sim.estop_hardware = EmergencyStatus.STOP
        await asyncio.sleep(0.12)
        assert sim.vx == 0.0
        sim.estop_hardware = EmergencyStatus.RECOVER
        await asyncio.sleep(0.2)
        assert sim.vx == 0.0, "急停前收下的速度不许在解除后复活"
    async with _client() as (sim, client):
        await _站起(sim, client)
        assert (await client.call("vel", fwd=0.4, lat=0.0, yaw=0.0, ttl_ms=1000)).ok
        await asyncio.sleep(0.12)
        sim.motion = MotionStatus.LIE_DOWN
        await asyncio.sleep(0.12)
        assert sim.vx == 0.0
        sim.motion = MotionStatus.GENERAL
        await asyncio.sleep(0.2)
        assert sim.vx == 0.0, "趴下前收下的速度不许在站起来后复活"
