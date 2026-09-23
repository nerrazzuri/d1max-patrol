"""W01c:厂商导航推来的未知报文不许刷日志。

真机(2026-09-22)上厂商 WebSocket 每秒推约 40 条 ``app_sub_topic``,读循环
对每一条都打一行 WARNING —— 4 分钟 9 千行,journal 与 eMMC 一起遭殃,真正
的错误被冲掉。这里钉死:**同一类无法解析的报文只警告一次,之后只计数**。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from websockets.asyncio.server import serve

from d1max_patrol.backends.vendor_nav import (
    UNPARSEABLE_REMIND_EVERY,
    VendorNavBackend,
)
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_frames import build_response, parse_request
from d1max_patrol.protocol.nav_requests import response_func_for

#: 轮询周期大到一次都发不出来 —— 桩服务端不会应答,别让轮询器往日志里掺超时。
_POLLER_OFF = 3600.0

_SUB = json.dumps({"head": {"type": "app_sub_topic", "time_stamp": 1},
                   "data": {"topic": "/odom"}})
_OTHER = json.dumps({"head": {"type": "heartbeat"}, "data": {}})
_GARBAGE = "this is not json"


class _只会推送的桩服务端:
    """每条连接握手之后按脚本推一串帧;请求一律回「ok、没数据」—— 只够让
    重连探针(``get_nav_status``)通过,别的什么都不会。"""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self._server = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = next(iter(self._server.sockets)).getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws) -> None:
        with contextlib.suppress(Exception):
            for raw in self._frames:
                await ws.send(raw)
            async for raw in ws:
                frame_count, req_func, _args = parse_request(raw)
                await ws.send(json.dumps(build_response(
                    response_func_for(req_func), frame_count or 0, data=None)))


async def _wait_until(predicate, timeout_s: float = 10.0) -> None:
    async def loop():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(loop(), timeout=timeout_s)


@contextlib.asynccontextmanager
async def _连上(frames: list[str]):
    server = _只会推送的桩服务端(frames)
    await server.start()
    b = VendorNavBackend(NavConfig(url=server.url, status_poll_interval_s=_POLLER_OFF,
                                   request_timeout_s=1.0, connect_timeout_s=2.0))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()
        await server.stop()


def _警告(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "d1max_patrol.backends.vendor_nav"
            and r.levelno >= logging.WARNING and "无法解析" in r.getMessage()]


async def test_同一类未知报文只警告一次_其余只计数(caplog):
    caplog.set_level(logging.DEBUG, logger="d1max_patrol.backends.vendor_nav")
    n = 200
    async with _连上([_SUB] * n) as b:
        await _wait_until(lambda: b.unparseable_total >= n)
        assert b.unparseable_frames == {"type='app_sub_topic'": n}
    警告 = _警告(caplog)
    assert len(警告) == 1, [r.getMessage() for r in 警告]
    assert "app_sub_topic" in 警告[0].getMessage()
    assert "只计数" in 警告[0].getMessage(), "第一行要告诉看日志的人:后面没有了不是修好了"


async def test_不同类各警告一次_坏JSON也算一类(caplog):
    caplog.set_level(logging.DEBUG, logger="d1max_patrol.backends.vendor_nav")
    frames = [_SUB, _OTHER, _GARBAGE, _SUB, _OTHER, _GARBAGE, _SUB]
    async with _连上(frames) as b:
        await _wait_until(lambda: b.unparseable_total >= len(frames))
        assert b.unparseable_frames["type='app_sub_topic'"] == 3
        assert b.unparseable_frames["type='heartbeat'"] == 2
        assert b.unparseable_frames["报文不是合法 JSON"] == 2, \
            "坏 JSON 按冒号前那半句归类,不带 line/column 位置"
    警告 = _警告(caplog)
    assert len(警告) == 3, [r.getMessage() for r in 警告]


async def test_重连之后同一类也不再重复警告(caplog):
    """真机上链路会断会重连;要是按连接清零,每次重连就又刷一遍。"""
    caplog.set_level(logging.DEBUG, logger="d1max_patrol.backends.vendor_nav")
    async with _连上([_SUB] * 5) as b:
        await _wait_until(lambda: b.unparseable_total >= 5)
        ws = b._ws
        assert ws is not None
        await ws.close()                       # 逼一次断链
        await b.wait_connected(timeout_s=10.0)
        await _wait_until(lambda: b.unparseable_total >= 10)
    assert len(_警告(caplog)) == 1


def test_到了提醒阈值再补一行_让人知道还在发生():
    """只警告一次的另一面:几小时后翻日志的人得看得出「它一直在发生、发生了
    多少」。阈值对应真机 40 条/秒约 40 分钟一行 —— 一天不到 40 行。"""
    assert 60_000 <= UNPARSEABLE_REMIND_EVERY <= 200_000


async def test_提醒阈值那一条以INFO级补一行(caplog, monkeypatch):
    import d1max_patrol.backends.vendor_nav as vn
    monkeypatch.setattr(vn, "UNPARSEABLE_REMIND_EVERY", 50)
    caplog.set_level(logging.DEBUG, logger="d1max_patrol.backends.vendor_nav")
    async with _连上([_SUB] * 120) as b:
        await _wait_until(lambda: b.unparseable_total >= 120)
    提醒 = [r for r in caplog.records
            if r.name == "d1max_patrol.backends.vendor_nav"
            and r.levelno == logging.INFO and "累计" in r.getMessage()]
    文本 = [r.getMessage() for r in 提醒]
    assert len(文本) == 2, 文本
    assert "累计 50 条" in 文本[0] and "累计 100 条" in 文本[1], 文本
    assert len(_警告(caplog)) == 1
