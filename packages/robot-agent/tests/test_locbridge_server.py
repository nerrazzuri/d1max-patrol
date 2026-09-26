"""本机定位桥,代理这一头(W09a,W08 决定 2):Unix 套接字、对端账号认人、握手、一次只接一个、行长上限、
序号、心跳、请求等回复。真套接字(路径放在 /tmp 下一个短目录:Unix 套接字路径最多一百来个字节)。"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from d1max_agent.bridge_localizer import LocalizerUnavailable
from d1max_agent.locbridge import HB_DEAD_S, LocBridgeServer
from d1max_contract.errors import ContractError
from d1max_contract.locbridge import (
    MAX_LINE,
    PROTO,
    Error,
    Heartbeat,
    Hello,
    Pose,
    Reply,
    SetPrior,
    State,
    encode,
    parse,
)


class 钟:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


class 记录:
    def __init__(self):
        self.calls = []

    def on_connect(self):
        self.calls.append("connect")

    def on_disconnect(self):
        self.calls.append("disconnect")

    def on_pose(self, p):
        self.calls.append(("pose", p.seq))

    def on_state(self, s):
        self.calls.append(("state", s.state))


@pytest.fixture
async def 桥():
    d = Path(tempfile.mkdtemp(prefix="lb", dir="/tmp"))
    c, sink = 钟(), 记录()
    srv = LocBridgeServer(d / "loc.sock", sink, monotonic=c)
    await srv.start()
    yield srv, c, sink, d / "loc.sock"
    await srv.close()
    shutil.rmtree(d, ignore_errors=True)


def _pose(seq):
    return Pose(seq=seq, stamp_ns=0, map_id="m", map_version="1", x=0.0, y=0.0, yaw=0.0,
                sigma_xy=0.1, sigma_yaw=0.01, source="scan_match")


_HELLO = Hello(proto=PROTO, name="t")


async def _连(path, hello=_HELLO):
    r, w = await asyncio.open_unix_connection(str(path), limit=MAX_LINE * 4)
    w.write(encode(hello))
    await w.drain()
    return r, w


async def _读(r, timeout=2.0):
    """下一行;断开了(对面关了、或者没读我们发的就关、内核回复位)是 ``None``。"""
    try:
        line = await asyncio.wait_for(r.readline(), timeout)
    except ConnectionResetError:
        return None
    return parse(line) if line else None


async def _等(cond, n=200):
    for _ in range(n):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到")


async def test_握手之后位姿状态心跳进来_序号不比上一条大的丢(桥):
    srv, c, sink, path = 桥
    r, w = await _连(path)
    assert await _读(r) == Hello(proto=PROTO)
    await _等(lambda: "connect" in sink.calls)
    for m in (_pose(1), _pose(1), State(seq=2, state="tracking"), Heartbeat(seq=3), _pose(2),
              _pose(4)):
        w.write(encode(m))
    await w.drain()
    await _等(lambda: ("pose", 4) in sink.calls)
    assert sink.calls == ["connect", ("pose", 1), ("state", "tracking"), ("pose", 4)]
    assert srv.connected
    w.close()
    await _等(lambda: "disconnect" in sink.calls)
    assert not srv.connected


@pytest.mark.parametrize("first", [Hello(proto=PROTO + 1), _pose(1)])
async def test_第一行不是这一版的hello_回一句错误就断(桥, first):
    srv, c, sink, path = 桥
    r, w = await _连(path, hello=first)
    got = await _读(r)
    assert isinstance(got, Error) and got.reason
    assert await _读(r) is None, "断开了"
    assert sink.calls == []


async def test_坏的一行丢掉不断开_太长的一行断开(桥):
    srv, c, sink, path = 桥
    r, w = await _连(path)
    await _读(r)
    w.write(b"garbage\n" + encode(_pose(1)))
    await w.drain()
    await _等(lambda: ("pose", 1) in sink.calls)
    w.write(b'{"t":"hb","seq":2,"pad":"' + b"x" * (MAX_LINE + 10) + b'"}\n')
    await w.drain()
    await _等(lambda: "disconnect" in sink.calls)


async def test_别的账号连不上(桥):
    srv, c, sink, path = 桥
    srv.allowed_uid = os.getuid() + 1
    r, w = await _连(path)
    assert await _读(r) is None
    assert sink.calls == []


async def test_一次只接一个_旧的活着拒新的_旧的没声了新的顶替(桥):
    srv, c, sink, path = 桥
    ra, wa = await _连(path)
    await _读(ra)
    rb, wb = await _连(path)
    got = await _读(rb)
    assert isinstance(got, Error) and "已经有" in got.reason
    assert await _读(rb) is None
    c.t += HB_DEAD_S + 0.5                               # 旧的这么久没声
    rc, wc = await _连(path)
    assert await _读(rc) == Hello(proto=PROTO)
    await _等(lambda: sink.calls == ["connect", "disconnect", "connect"])
    assert await _读(ra) is None, "旧的被断了"


async def test_每秒一条心跳_没声超过两秒当它断了(桥):
    srv, c, sink, path = 桥
    r, w = await _连(path)
    await _读(r)
    await srv.tick()
    assert isinstance(await _读(r), Heartbeat)
    c.t += 0.5
    await srv.tick()
    with pytest.raises(asyncio.TimeoutError):
        await _读(r, timeout=0.2)
    c.t += 0.6
    await srv.tick()
    assert isinstance(await _读(r), Heartbeat)
    w.write(encode(Heartbeat(seq=1)))                    # 它回了一声:活着
    await w.drain()
    await asyncio.sleep(0.05)
    c.t += 1.5
    await srv.tick()
    assert srv.connected
    c.t += HB_DEAD_S
    await srv.tick()
    assert not srv.connected and sink.calls[-1] == "disconnect"


async def test_请求等回复_超时_没连上_等的时候断了(桥):
    srv, c, sink, path = 桥
    with pytest.raises(LocalizerUnavailable):
        await srv.request(lambda req: SetPrior(req=req, map_id="m", map_version="1", dir=""), 1.0)
    r, w = await _连(path)
    await _读(r)

    async def 回():
        m = await _读(r)
        assert isinstance(m, SetPrior)
        w.write(encode(Reply(req=m.req, ok=True)))
        await w.drain()
    t = asyncio.get_running_loop().create_task(回())
    got = await srv.request(lambda req: SetPrior(req=req, map_id="m", map_version="1", dir="/x"),
                            2.0)
    await t
    assert got.ok
    with pytest.raises(asyncio.TimeoutError):
        await srv.request(lambda req: SetPrior(req=req, map_id="m", map_version="1", dir=""), 0.1)
    pending = asyncio.get_running_loop().create_task(
        srv.request(lambda req: SetPrior(req=req, map_id="m", map_version="1", dir=""), 5.0))
    await asyncio.sleep(0.05)
    w.close()
    with pytest.raises(LocalizerUnavailable):
        await pending


async def test_定位器不读_心跳不卡住每拍_积多了断开(桥, monkeypatch):
    """内审应修 3:以前每拍的心跳要等发送(drain),定位器只写不读的话,缓冲满了就把整个代理的
    循环卡死。"""
    import d1max_agent.locbridge as lb
    srv, c, sink, path = 桥
    monkeypatch.setattr(lb, "HB_EVERY_S", 0.0)
    monkeypatch.setattr(lb, "MAX_WBUF", 1024)
    r, w = await _连(path)                               # 只连、之后一个字都不读
    await _等(lambda: srv.connected)

    async def 狂发():
        for _ in range(40000):
            await srv.tick()
            if not srv.connected:
                return
            await asyncio.sleep(0)
    await asyncio.wait_for(狂发(), 10.0)
    assert not srv.connected and sink.calls[-1] == "disconnect"
    w.close()


async def test_回hello就发不出去_不当它连上(桥, monkeypatch):
    """内审小问题:以前回 hello 失败(已经 on_disconnect 了)之后还接着 on_connect,代理以为连着。"""
    import d1max_agent.locbridge as lb
    srv, c, sink, path = 桥
    monkeypatch.setattr(lb, "MAX_WBUF", -1)              # 发什么都算积多了
    r, w = await _连(path)
    await _等(lambda: sink.calls)
    await asyncio.sleep(0.05)
    assert not srv.connected and "connect" not in sink.calls, sink.calls
    w.close()


async def test_交给代理的回调出错_不断开连接(桥):
    srv, c, sink, path = 桥
    炸 = [True]
    real = sink.on_pose

    def on_pose(p):
        if 炸[0]:
            炸[0] = False
            raise RuntimeError("引擎那边炸了")
        real(p)
    sink.on_pose = on_pose
    r, w = await _连(path)
    await _读(r)
    w.write(encode(_pose(1)) + encode(_pose(2)))
    await w.drain()
    await _等(lambda: ("pose", 2) in sink.calls)
    assert srv.connected and "disconnect" not in sink.calls


async def test_两个几乎同时连_后握手的不顶掉先握手的活连接(桥):
    """内审小问题:两个连接都过了「旧的活着」那一关,晚握手的以前会把早握手的活连接顶掉。"""
    srv, c, sink, path = 桥
    ra, wa = await asyncio.open_unix_connection(str(path), limit=MAX_LINE * 4)   # 先连、先不握手
    await asyncio.sleep(0.05)
    rb, wb = await _连(path)
    assert await _读(rb) == Hello(proto=PROTO)
    wa.write(encode(_HELLO))
    await wa.drain()
    got = await _读(ra)
    assert isinstance(got, Error) and "已经有" in got.reason
    wb.write(encode(_pose(1)))
    await wb.drain()
    await _等(lambda: ("pose", 1) in sink.calls)
    assert sink.calls == ["connect", ("pose", 1)]


async def test_套接字只给自己_旧的套接字文件换掉_同名的普通文件不动():
    d = Path(tempfile.mkdtemp(prefix="lb", dir="/tmp"))
    try:
        p = d / "loc.sock"
        a = LocBridgeServer(p, 记录())
        await a.start()
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        await a.close()
        import socket
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(p))                                   # 上次没收拾的套接字文件
        s.close()
        b = LocBridgeServer(p, 记录())
        await b.start()
        await b.close()
        p.unlink(missing_ok=True)
        p.write_text("不是套接字")
        with pytest.raises(FileExistsError):
            await LocBridgeServer(p, 记录()).start()
        assert p.read_text() == "不是套接字"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_契约错误类型():
    with pytest.raises(ContractError):
        parse(b"{}")
