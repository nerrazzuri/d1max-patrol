"""假地图桥本身的行为。

建图 UI 的所有测试都要拿这个当地基,所以地基先立住:发的图是不是当前
这张、``reveal`` 是不是真的让图长了、两个客户端看到的是不是同一张。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from d1max_patrol.protocol.map_frames import (
    PROTO_VERSION,
    MapFrame,
    MapHello,
    decode_frame,
)
from d1max_sim.map_server import FREE, OCCUPIED, UNKNOWN, SimMapServer


@contextlib.asynccontextmanager
async def _bridge(**kwargs):
    server = SimMapServer(port=0, hz=kwargs.pop("hz", 50.0), **kwargs)
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        yield server, reader
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await server.stop()


async def _next(reader: asyncio.StreamReader, timeout_s: float = 2.0):
    line = await asyncio.wait_for(reader.readline(), timeout_s)
    assert line, "对端关了"
    return decode_frame(line)


async def _until_frame(reader: asyncio.StreamReader, predicate,
                       timeout_s: float = 2.0):
    """一直读到某一帧满足条件为止。

    不能靠"跳过固定条数"来等改动生效:桥一直在写,不读的时候帧堆在
    socket 缓冲里,堆多少取决于调度,数不准。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        frame = await _next(reader, max(0.05, deadline - loop.time()))
        if isinstance(frame, MapFrame) and predicate(frame):
            return frame
    raise AssertionError("等到超时也没有满足条件的帧")


# ------------------------------------------------------------------ 连接


async def test_客户端连上就有hello和图():
    async with _bridge() as (server, reader):
        hello = await _next(reader)
        assert isinstance(hello, MapHello)
        assert hello.proto == PROTO_VERSION and hello.topic == "/map"
        frame = await _next(reader)
        assert isinstance(frame, MapFrame)
        assert (frame.width, frame.height) == (server.width, server.height)


async def test_一开始整张图都是未知的():
    """建图刚开头就是这个样子 —— UI 得能画出"什么都还没有"。"""
    async with _bridge(width=8, height=8) as (server, reader):
        await _next(reader)
        frame = await _until_frame(reader, lambda f: True)
        assert set(frame.data) == {UNKNOWN}
        assert server.unknown_count == 64


async def test_分辨率和原点照实发():
    async with _bridge(width=4, height=4, resolution=0.1,
                       origin=(-2.0, -3.0)) as (_server, reader):
        await _next(reader)
        frame = await _until_frame(reader, lambda f: True)
        assert frame.resolution == pytest.approx(0.1)
        assert (frame.origin_x, frame.origin_y) == pytest.approx((-2.0, -3.0))


# ------------------------------------------------------------------ reveal


async def test_reveal之后未知格变少():
    async with _bridge(width=10, height=10) as (server, reader):
        await _next(reader)
        before = (await _until_frame(reader, lambda f: True)).data.count(UNKNOWN)
        server.reveal(0.5)
        after = (await _until_frame(
            reader, lambda f: f.data.count(UNKNOWN) < before)).data.count(UNKNOWN)
        assert after < before


async def test_reveal露出来的既有空地也有墙():
    """全露成一块纯色的话,UI 画得对不对根本看不出来。"""
    async with _bridge(width=10, height=10) as (server, _reader):
        server.reveal(1.0)
        assert FREE in server.cells and OCCUPIED in server.cells
        assert UNKNOWN not in server.cells


async def test_reveal是只增不减的():
    """真建图不会倒着走,UI 上图突然变少只会让人以为程序坏了。"""
    async with _bridge(width=10, height=10) as (server, _reader):
        server.reveal(0.6)
        few = server.unknown_count
        server.reveal(0.1)
        assert server.unknown_count == few


async def test_reveal的比例会被夹到0和1之间():
    async with _bridge(width=6, height=6) as (server, _reader):
        server.reveal(-5.0)
        assert server.unknown_count == 36
        server.reveal(99.0)
        assert server.unknown_count == 0


# ---------------------------------------------------------------- set_cell


async def test_set_cell改的格子会出现在下一帧():
    async with _bridge(width=4, height=4) as (server, reader):
        await _next(reader)
        server.set_cell(1, 2, OCCUPIED)
        frame = await _until_frame(reader, lambda f: f.data[2 * 4 + 1] == OCCUPIED)
        assert frame.data[2 * 4 + 1] == OCCUPIED


async def test_set_cell越界当场抛():
    server = SimMapServer(port=0, width=4, height=4)
    for x, y in ((-1, 0), (0, -1), (4, 0), (0, 4)):
        with pytest.raises(IndexError):
            server.set_cell(x, y, FREE)


async def test_set_cell的值越界也抛():
    """越界的值发出去会被线协议拒掉,不如在源头就拦。"""
    server = SimMapServer(port=0, width=4, height=4)
    for bad in (-2, 101):
        with pytest.raises(ValueError):
            server.set_cell(0, 0, bad)


# ------------------------------------------------------------------ 多客户端


async def test_两个客户端都收得到同一张图():
    server = SimMapServer(port=0, width=6, height=6, hz=50.0)
    await server.start()
    server.reveal(0.5)
    conns = []
    try:
        for _ in range(2):
            conns.append(await asyncio.open_connection("127.0.0.1", server.port))
        seen = []
        for reader, _writer in conns:
            await _next(reader)
            seen.append((await _until_frame(reader, lambda f: True)).data)
        assert seen[0] == seen[1]
    finally:
        for _reader, writer in conns:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await server.stop()


async def test_停了之后客户端读到流结束():
    server = SimMapServer(port=0, width=4, height=4, hz=50.0)
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    await _next(reader)
    await server.stop()
    try:
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if not await asyncio.wait_for(reader.readline(), 1.0):
                break
        else:
            raise AssertionError("停了之后还在发")
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def test_没启动就问端口会说清楚():
    with pytest.raises(RuntimeError, match="还没启动"):
        _ = SimMapServer(port=0).port


async def test_停两次不会炸():
    server = SimMapServer(port=0)
    await server.start()
    await server.stop()
    await server.stop()
