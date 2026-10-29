"""W46(决策 74):站点按 NTRIP 从商业 CORS(或自建基站的 caster)拉改正数据,接到原来的转发上。
假 caster 在本机起一个 TCP 口。"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time

import pytest

from d1max_contract.rtcm import frame
from d1max_site.rtk_relay import (
    NtripAccount,
    RtcmRelay,
    SourceError,
    gga_sentence,
    load_ntrip,
    parse_source,
)


def _msg(mt: int, n: int = 20) -> bytes:
    return frame(bytes([mt >> 4, (mt & 0xF) << 4]) + bytes(n))


def _等(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class 假caster:
    def __init__(self, head: bytes, body: bytes = b""):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.head, self.body = head, body
        self.request = b""
        self.after = b""
        self._stop = False
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self):
        try:
            c, _ = self.srv.accept()
        except OSError:
            return
        c.settimeout(0.2)
        while b"\r\n\r\n" not in self.request:
            try:
                self.request += c.recv(1024)
            except TimeoutError:
                continue
        c.sendall(self.head + self.body)
        while not self._stop:
            try:
                d = c.recv(1024)
            except TimeoutError:
                continue
            except OSError:
                break
            if not d:
                break
            self.after += d
        c.close()

    def close(self):
        self._stop = True
        self.srv.close()
        self.t.join(2)


ACC = NtripAccount(user="u1", password="s3cret-pw", lat=22.5432, lon=113.9381, alt=30.0)


@pytest.mark.parametrize("text,want", [
    ("ntrip:caster.example.com:2101/RTCM32", ("ntrip", "caster.example.com/RTCM32", 2101)),
    ("ntrip:10.0.0.9:8002/AUTO", ("ntrip", "10.0.0.9/AUTO", 8002)),
])
def test_源的写法(text, want):
    assert parse_source(text) == want


@pytest.mark.parametrize("bad", ["ntrip:h:2101", "ntrip:h:2101/", "ntrip::2101/M",
                                 "ntrip:h:0/M", "ntrip:h:2101/a b", "ntrip:h:x/M"])
def test_源写错了拒(bad):
    with pytest.raises(SourceError):
        parse_source(bad)


def test_GGA句子_度分格式_校验和():
    s = gga_sentence(22.5432, 113.9381, 30.0, 1_793_000_000.0)
    body, _, cs = s.strip().partition("*")
    assert s.endswith("\r\n") and body.startswith("$GPGGA,")
    x = 0
    for ch in body[1:]:
        x ^= ord(ch)
    assert cs == f"{x:02X}"
    f = body.split(",")
    assert f[2] == "2232.59200" and f[3] == "N" and f[4] == "11356.28600" and f[5] == "E"
    assert f[6] == "1" and f[9] == "30.0"
    s = gga_sentence(-33.5, -70.25, 0.0, 0.0)
    assert ",3330.00000,S,07015.00000,W," in s


def test_账号文件_要0600_缺了说清楚(tmp_path):
    p = tmp_path / "ntrip.json"
    with pytest.raises(SourceError, match="ntrip.json"):
        load_ntrip(p)
    p.write_text(json.dumps({"user": "u", "password": "p", "lat": 22.5, "lon": 113.9}))
    os.chmod(p, 0o644)
    with pytest.raises(SourceError, match="0600"):
        load_ntrip(p)
    os.chmod(p, 0o600)
    a = load_ntrip(p)
    assert (a.user, a.password, a.lat, a.lon, a.alt) == ("u", "p", 22.5, 113.9, 0.0)
    p.write_text(json.dumps({"user": "u"}))
    with pytest.raises(SourceError, match="password"):
        load_ntrip(p)
    assert "zzz" not in repr(NtripAccount(user="u", password="zzz")), "日志里打出来也不带密码"


def test_v1_caster_拉到改正数据_带账号_回报位置(monkeypatch):
    import d1max_site.rtk_relay as R
    monkeypatch.setattr(R, "GGA_EVERY_S", 0.2)
    good = _msg(1077) + _msg(1005, 19)
    cs = 假caster(b"ICY 200 OK\r\n\r\n", good)
    got: list[bytes] = []
    r = RtcmRelay(f"ntrip:127.0.0.1:{cs.port}/RTCM32", publish=got.append,
                  now_ms=lambda: 5000, ntrip=ACC)
    r.start()
    try:
        assert _等(lambda: b"".join(got) == good)
        req = cs.request.decode()
        assert req.startswith("GET /RTCM32 HTTP/1.0\r\n")
        auth = base64.b64encode(b"u1:s3cret-pw").decode()
        assert f"Authorization: Basic {auth}\r\n" in req
        assert _等(lambda: cs.after.count(b"$GPGGA") >= 2), "定时回报大概位置(虚拟基站要)"
        st = r.stats()
        assert st["connected"] and "s3cret" not in json.dumps(st)
    finally:
        r.close()
        cs.close()


def test_v2_caster_分块传输也认():
    good = _msg(1077) + _msg(1087)
    body = b"".join(b"%x\r\n" % len(c) + c + b"\r\n" for c in (good[:7], good[7:]))
    cs = 假caster(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                 b"Content-Type: gnss/data\r\n\r\n", body)
    got: list[bytes] = []
    r = RtcmRelay(f"ntrip:127.0.0.1:{cs.port}/M", publish=got.append, now_ms=lambda: 1,
                  ntrip=NtripAccount(user="u", password="p"))
    r.start()
    try:
        assert _等(lambda: b"".join(got) == good)
        assert r.stats()["bad"] == 0, "分块的长度行不许当成数据"
    finally:
        r.close()
        cs.close()


@pytest.mark.parametrize("head, 说", [
    (b"HTTP/1.1 401 Unauthorized\r\n\r\n", "账号密码不对"),
    (b"SOURCETABLE 200 OK\r\nContent-Type: text/plain\r\n\r\nSTR;X;\r\nENDSOURCETABLE\r\n",
     "挂载点不对"),
    (b"HTTP/1.1 503 Busy\r\n\r\n", "503"),
])
def test_caster_拒了_说人话_密码不进错误(monkeypatch, head, 说):
    import d1max_site.rtk_relay as R
    monkeypatch.setattr(R, "RECONNECT_S", 30.0)
    cs = 假caster(head)
    r = RtcmRelay(f"ntrip:127.0.0.1:{cs.port}/M", publish=lambda b: None, now_ms=lambda: 1,
                  ntrip=ACC)
    r.start()
    try:
        assert _等(lambda: 说 in r.error), r.error
        assert "s3cret" not in r.error and not r.connected
    finally:
        r.close()
        cs.close()


def test_NTRIP源没给账号_起不来():
    with pytest.raises(SourceError, match="账号"):
        RtcmRelay("ntrip:h:2101/M", publish=lambda b: None, now_ms=lambda: 1)


def test_站点起的时候_NTRIP没账号文件就不起(tmp_path, capsys):
    from d1max_site.main import cmd_serve
    (tmp_path / "site.json").write_text(json.dumps({"site_id": "s", "broker_port": 1}))
    assert cmd_serve(tmp_path, "127.0.0.1", 0, None, "ntrip:caster:2101/M") == 2
    assert "ntrip.json" in capsys.readouterr().err
