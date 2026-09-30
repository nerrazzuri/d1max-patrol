"""W09e:站点从基站收 RTCM3,按帧校验,发给在线的狗。假串口用 pty,另有 TCP 源。"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from d1max_contract.rtcm import frame
from d1max_site.rtk_relay import RtcmRelay, SourceError, parse_source


def _msg(mt: int, n: int = 20) -> bytes:
    return frame(bytes([mt >> 4, (mt & 0xF) << 4]) + bytes(n))


def _等(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("text,want", [
    ("serial:/dev/ttyUSB0:115200", ("serial", "/dev/ttyUSB0", 115200)),
    ("tcp:10.0.0.5:5018", ("tcp", "10.0.0.5", 5018)),
])
def test_源的写法(text, want):
    assert parse_source(text) == want


@pytest.mark.parametrize("bad", ["", "serial:/dev/x", "serial:/dev/x:12345", "udp:a:1",
                                 "tcp:host:0", "tcp::5018", "tcp:h:abc"])
def test_源写错了拒(bad):
    with pytest.raises(SourceError):
        parse_source(bad)


def test_假串口_好帧一批发出去_坏帧丢掉计数(tmp_path):
    master, slave = os.openpty()
    got: list[bytes] = []
    r = RtcmRelay(f"serial:{os.ttyname(slave)}:115200", publish=got.append, now_ms=lambda: 5000)
    r.start()
    try:
        assert _等(lambda: r.connected)
        good1, good2 = _msg(1077), _msg(1087)
        bad = bytearray(_msg(1097))
        bad[-2] ^= 0x55
        os.write(master, b"\x00junk" + good1 + bytes(bad) + good2)
        assert _等(lambda: sum(len(g) for g in got) >= len(good1) + len(good2))
        assert b"".join(got) == good1 + good2, "只发好帧、原样"
        st = r.stats()
        assert st["connected"] and st["frames"] == 2 and st["bad"] == 1
        assert st["types"] == {"1077": 1, "1087": 1} and st["age_s"] == 0.0
    finally:
        r.close()
        os.close(master)
        os.close(slave)


def test_TCP源_断了重连(monkeypatch):
    import d1max_site.rtk_relay as R
    monkeypatch.setattr(R, "RECONNECT_S", 0.1)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    got: list[bytes] = []
    conns = []

    def serve():
        for _ in range(2):
            c, _ = srv.accept()
            conns.append(c)
            c.sendall(_msg(1005 if len(conns) == 1 else 1077))
            if len(conns) == 1:
                c.close()                               # 第一条连接发完就断
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    r = RtcmRelay(f"tcp:127.0.0.1:{port}", publish=got.append, now_ms=lambda: 0)
    r.start()
    try:
        assert _等(lambda: len(got) >= 2), got
        assert r.stats()["types"] == {"1005": 1, "1077": 1}
    finally:
        r.close()
        for c in conns:
            c.close()
        srv.close()


def test_源连不上_说原因_不炸(monkeypatch):
    import d1max_site.rtk_relay as R
    monkeypatch.setattr(R, "RECONNECT_S", 0.05)
    r = RtcmRelay("serial:/dev/nope-d1max:115200", publish=lambda b: None, now_ms=lambda: 0)
    r.start()
    try:
        assert _等(lambda: "nope" in r.error or "No such" in r.error)
        assert r.stats()["connected"] is False
    finally:
        r.close()


def test_发的时候炸了_不影响下一批():
    calls = []

    def publish(b):
        calls.append(b)
        if len(calls) == 1:
            raise RuntimeError("事件循环忙")
    r = RtcmRelay("tcp:h:1", publish=publish, now_ms=lambda: 0)
    r.feed(_msg(1077))
    r.feed(_msg(1087))
    assert len(calls) == 2 and r.stats()["frames"] == 2


async def test_派遣器_改正只发给在线新鲜的狗_QoS0不保留(tmp_path):
    from test_site_dispatcher import 台子
    t = 台子(tmp_path)
    await t.start()
    try:
        await t.run(3)
        heard = []
        real = t.site._t.publish

        async def 听(topic, payload, qos=0, retain=False):
            heard.append((topic, payload, qos, retain))
            return await real(topic, payload, qos=qos, retain=retain)
        t.site._t.publish = 听
        n = await t.site.publish_rtcm(b"\xd3rtcm")
        assert n == 1 and heard == [("site/estate-1/robot/A/rtcm", b"\xd3rtcm", 0, False)]
        t.site.clients["A"].status_live_at = 0              # 状态不新鲜了
        heard.clear()
        assert await t.site.publish_rtcm(b"x") == 0 and heard == []
    finally:
        await t.agent.close()
        await t.site.close()


def test_站点接口_没配说没配_配了给统计(tmp_path):
    from test_site_api import PW, 站

    class 假转发:
        def stats(self):
            return {"source": "tcp:h:1", "connected": True, "frames": 3, "bad": 0, "age_s": 0.4,
                    "types": {"1077": 3}, "base": None, "error": ""}
    s = 站(tmp_path)
    try:
        tok = s.req("POST", "/api/login", {"name": "alice", "password": PW})[1]["token"]
        assert s.req("GET", "/api/rtk", token=tok) == (200, {"configured": False})
        s.api.rtk = 假转发()
        code, d = s.req("GET", "/api/rtk", token=tok)
        assert code == 200 and d["configured"] is True and d["frames"] == 3
        assert s.req("GET", "/api/rtk")[0] == 401
    finally:
        s.close()


def test_命令行_源写错了当场退(tmp_path):
    import json

    from d1max_site.main import cmd_serve
    (tmp_path / "site.json").write_text(json.dumps({"site_id": "s", "broker_port": 1}))
    assert cmd_serve(tmp_path, "127.0.0.1", 0, None, "udp:x:1") == 2
