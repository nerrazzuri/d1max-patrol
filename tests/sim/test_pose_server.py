"""假定位桥本身的行为。

``LocalNavBackend`` 的测试把这个当地基用,所以地基本身得先立住:发的是不是
agent 的真位姿、注入的三种故障是不是真的各自不同。
"""

from __future__ import annotations

import asyncio
import contextlib
import math

import pytest

from d1max_patrol.protocol.pose_frames import (
    PROTO_VERSION,
    PoseFrame,
    PoseHello,
    decode_frame,
)
from d1max_sim.agent_server import SimAgentServer
from d1max_sim.pose_server import SimPoseServer


@contextlib.asynccontextmanager
async def _bridge(hz: float = 100.0):
    agent = SimAgentServer(port=0)
    await agent.start()
    pose = SimPoseServer(agent, port=0, hz=hz)
    await pose.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", pose.port)
    try:
        yield agent, pose, reader
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await pose.stop()
        await agent.stop()


async def _next(reader: asyncio.StreamReader, timeout_s: float = 2.0):
    line = await asyncio.wait_for(reader.readline(), timeout_s)
    assert line, "对端关了"
    return decode_frame(line)


async def _until_frame(reader: asyncio.StreamReader, predicate,
                       timeout_s: float = 2.0):
    """一直读到某一帧满足条件为止。

    不能靠"跳过固定条数"来等注入生效:桥按 100 Hz 一直写,而我们不读的
    时候这些帧就堆在 socket 缓冲里,堆多少取决于调度,数不准。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        frame = await _next(reader, max(0.05, deadline - loop.time()))
        if predicate(frame):
            return frame
    raise AssertionError("等不到满足条件的帧")


async def _drain(reader: asyncio.StreamReader) -> None:
    """把已经堆在缓冲里的帧读干净。"""
    while True:
        try:
            await asyncio.wait_for(reader.readline(), 0.05)
        except asyncio.TimeoutError:
            return


async def test_连上先收到hello():
    async with _bridge() as (_agent, _pose, reader):
        hello = await _next(reader)
    assert isinstance(hello, PoseHello)
    assert hello.proto == PROTO_VERSION
    assert (hello.map_frame, hello.base_frame) == ("loc_map", "base_link")


async def test_发的是agent的位姿而不是自己另存一份():
    """两边各存一份位姿就会各自自洽地跑偏,而且测试照样全绿。"""
    async with _bridge() as (agent, _pose, reader):
        await _next(reader)                     # hello
        agent.x, agent.y, agent.yaw = 3.0, -4.0, 1.25
        frame = await _until_frame(reader, lambda f: f.x == 3.0)
        assert isinstance(frame, PoseFrame)
        assert frame.ok
        assert (frame.x, frame.y) == (3.0, -4.0)
        assert frame.yaw == pytest.approx(1.25)


async def test_丢定位改发ok为假但仍在发():
    """"定位器活着但查不到 TF" —— 流不能断,断了就跟进程挂掉分不开了。"""
    async with _bridge() as (_agent, pose, reader):
        await _next(reader)
        pose.drop_localization("sim: no tf")
        frame = await _until_frame(reader, lambda f: not f.ok)
        assert isinstance(frame, PoseFrame)
        assert frame.reason == "sim: no tf"

        pose.restore_localization()
        assert isinstance(await _until_frame(reader, lambda f: f.ok), PoseFrame)


async def test_冻住就一个字节都不发():
    """跟 drop_localization 的区别就在这里:上层只能靠超时发现。"""
    async with _bridge() as (_agent, pose, reader):
        await _next(reader)
        pose.freeze()
        await _drain(reader)                    # 在途的帧先流干净
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), 0.3)

        pose.thaw()
        assert isinstance(await _next(reader), PoseFrame)


async def test_跳变只动位姿不动agent():
    """jump 模拟的是**定位器**重定位到错的地方,机器本身没挪窝。

    两者必须能拉开 —— 恒等的话交叉校验就永远测不出来。
    """
    async with _bridge() as (agent, pose, reader):
        await _next(reader)
        pose.jump(2.0, -1.0)
        frame = await _until_frame(reader, lambda f: f.x == 2.0)
        assert isinstance(frame, PoseFrame)
        assert (frame.x, frame.y) == (2.0, -1.0)
        assert (agent.x, agent.y) == (0.0, 0.0)


async def test_跳变是累加的():
    async with _bridge() as (_agent, pose, reader):
        await _next(reader)
        pose.jump(1.0, 0.0)
        pose.jump(0.5, 0.0)
        frame = await _until_frame(reader, lambda f: f.x == pytest.approx(1.5))
        assert isinstance(frame, PoseFrame)


async def test_时间戳是毫秒且在递增():
    async with _bridge(hz=50.0) as (_agent, _pose, reader):
        await _next(reader)
        first = await _next(reader)
        await asyncio.sleep(0.1)
        later = await _next(reader)
    assert isinstance(first, PoseFrame) and isinstance(later, PoseFrame)
    assert later.ts_ms >= first.ts_ms
    assert first.ts_ms > 1_600_000_000_000, "看着不像毫秒级 Unix 时间"


async def test_多个客户端各收各的():
    """看板和导航后端会同时连上来,一个读得慢不能影响另一个。"""
    agent = SimAgentServer(port=0)
    await agent.start()
    pose = SimPoseServer(agent, port=0, hz=100.0)
    await pose.start()
    conns = [await asyncio.open_connection("127.0.0.1", pose.port)
             for _ in range(2)]
    try:
        for reader, _writer in conns:
            assert isinstance(await _next(reader), PoseHello)
        agent.x = 7.0
        for reader, _writer in conns:
            frame = await _until_frame(reader, lambda f: f.x == 7.0)
            assert isinstance(frame, PoseFrame)
    finally:
        for _reader, writer in conns:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await pose.stop()
        await agent.stop()


async def test_客户端走了服务端不塌():
    """真机上后端会重连,桥不能因为一次断开就死掉。"""
    agent = SimAgentServer(port=0)
    await agent.start()
    pose = SimPoseServer(agent, port=0, hz=100.0)
    await pose.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pose.port)
        assert isinstance(await _next(reader), PoseHello)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await asyncio.sleep(0.05)

        reader2, writer2 = await asyncio.open_connection("127.0.0.1", pose.port)
        assert isinstance(await _next(reader2), PoseHello)
        assert isinstance(await _next(reader2), PoseFrame)
        writer2.close()
        with contextlib.suppress(Exception):
            await writer2.wait_closed()
    finally:
        await pose.stop()
        await agent.stop()


async def test_yaw跟着agent走():
    async with _bridge() as (agent, _pose, reader):
        await _next(reader)
        agent.yaw = -math.pi / 2
        frame = await _until_frame(
            reader, lambda f: f.yaw == pytest.approx(-math.pi / 2))
        assert isinstance(frame, PoseFrame)
