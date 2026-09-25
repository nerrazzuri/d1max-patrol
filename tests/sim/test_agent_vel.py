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
