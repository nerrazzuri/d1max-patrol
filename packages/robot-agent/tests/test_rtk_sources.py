"""W09e 狗上的 RTK 来源:NMEA 解析、自己的串口驱动(pty 当模组)、厂家的辅助进程(假的)。"""

from __future__ import annotations

import os
import sys
import time

import pytest

from d1max_agent.rtk import OwnRtk, VendorRtk, _nmea_ok, parse_gga, parse_gst


def _nmea(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}\r\n"


GGA_FIX = "GNGGA,083015.00,0524.8460,N,10019.7280,E,4,22,0.7,12.3,M,-7.6,M,1.2,0001"
GST = "GNGST,083015.00,0.9,0.010,0.008,45.0,0.012,0.009,0.02"


def _等(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_校验和():
    assert _nmea_ok(_nmea(GGA_FIX)) == GGA_FIX
    bad = _nmea(GGA_FIX).replace("*", "*F")
    assert _nmea_ok(bad) is None
    assert _nmea_ok(GGA_FIX) is None, "没有 $ 与校验"


@pytest.mark.parametrize("q,want", [(0, "none"), (1, "single"), (2, "dgps"), (4, "fixed"),
                                    (5, "float")])
def test_GGA_定位质量(q, want):
    body = GGA_FIX.replace(",4,22,", f",{q},22,")
    g = parse_gga(body)
    assert g["fix"] == want


def test_GGA_经纬高_龄期():
    g = parse_gga(GGA_FIX)
    assert g["lat"] == pytest.approx(5 + 24.846 / 60)
    assert g["lon"] == pytest.approx(100 + 19.728 / 60)
    assert g["alt"] == pytest.approx(12.3 - 7.6), "椭球高 = 海拔 + 大地水准面差距"
    assert g["sats"] == 22 and g["hdop"] == 0.7 and g["age_s"] == 1.2
    s = parse_gga(GGA_FIX.replace(",N,", ",S,").replace(",E,", ",W,"))
    assert s["lat"] < 0 and s["lon"] < 0


@pytest.mark.parametrize("bad", ["GNGGA,1,2,3", "GNGGA,083015.00,,N,,E,4,22,0.7,1,M,0,M,,",
                                 "GNGGA,083015.00,0524.8460,N,10019.7280,E,x,22,0.7,1,M,0,M,,"])
def test_GGA_不像话的丢掉(bad):
    assert parse_gga(bad) is None


def test_GST_水平标准差():
    assert parse_gst(GST) == pytest.approx((0.012 ** 2 + 0.009 ** 2) ** 0.5)
    assert parse_gst("GNGST,1,2") is None


def test_自己的驱动_pty当模组_读GGA与GST_改正原样写进去_初始化命令():
    master, slave = os.openpty()
    r = OwnRtk(os.ttyname(slave), 460800, init=["GPGGA COM1 0.2", "GPGST COM1 1"],
               now_ms=lambda: 77)
    r.start()
    try:
        assert _等(lambda: r._fd is not None)
        got = b""
        t0 = time.monotonic()
        while b"GPGST" not in got and time.monotonic() - t0 < 3:
            got += os.read(master, 1024)
        assert got.startswith(b"GPGGA COM1 0.2\r\nGPGST COM1 1\r\n")
        os.write(master, (_nmea(GST) + _nmea(GGA_FIX)[:20]).encode())
        os.write(master, _nmea(GGA_FIX)[20:].encode())       # 半行拼起来
        assert _等(lambda: r.latest() is not None)
        fix = r.latest()
        assert fix["fix"] == "fixed" and fix["std_h_m"] == pytest.approx(0.015, abs=1e-3)
        assert fix["stamp_ms"] == 77 and fix["stale"] is False
        r.feed(b"\xd3\x00\x13rtcm-bytes")
        got = b""
        t0 = time.monotonic()
        while len(got) < 13 and time.monotonic() - t0 < 3:
            got += os.read(master, 1024)
        assert got == b"\xd3\x00\x13rtcm-bytes" and r.fed_bytes == 13
    finally:
        r.close()
        os.close(master)
        os.close(slave)


def test_自己的驱动_过时标上_串口开不了说原因不炸(monkeypatch):
    import d1max_agent.rtk as R
    monkeypatch.setattr(R, "REOPEN_S", 0.05)
    t = {"now": 0.0}
    r = OwnRtk("/dev/nope-d1max", 115200, now_ms=lambda: 1, mono=lambda: t["now"])
    r._line(_nmea(GGA_FIX))
    assert r.latest()["stale"] is False
    t["now"] = 3.0
    assert r.latest()["stale"] is True
    r.feed(b"x" * 10)
    assert r.dropped_bytes == 10, "串口没开:改正丢掉"
    r.start()
    try:
        assert _等(lambda: "nope" in r.error or "No such" in r.error)
    finally:
        r.close()


def test_自己的驱动_波特率不认拒():
    with pytest.raises(ValueError):
        OwnRtk("/dev/x", 12345, now_ms=lambda: 0)


def test_厂家的_辅助进程一行一条JSON_pos_type换成我们的说法_改正不用(tmp_path):
    script = tmp_path / "fake_vendor.py"
    script.write_text(
        "import json, sys, time\n"
        "print('不是 JSON', flush=True)\n"
        "print(json.dumps({'pos_type': 50, 'svs_num': 25, 'diff_age_s': 0.8, 'lat': 5.41,"
        " 'lon': 100.32, 'alt': 20.0, 'lat_std': 0.01, 'lon_std': 0.01}), flush=True)\n"
        "time.sleep(30)\n")
    r = VendorRtk([sys.executable, str(script)], now_ms=lambda: 9)
    r.start()
    try:
        assert _等(lambda: r.latest() is not None)
        fix = r.latest()
        assert fix["fix"] == "fixed" and fix["sats"] == 25 and fix["age_s"] == 0.8
        assert fix["std_h_m"] == pytest.approx(0.01414, abs=1e-4)
        r.feed(b"ignored")
    finally:
        r.close()
    assert r._proc.poll() is not None, "收尾时辅助进程收掉"


def test_厂家的_辅助进程退了_隔一会儿再起(tmp_path, monkeypatch):
    import d1max_agent.rtk as R
    monkeypatch.setattr(R, "REOPEN_S", 0.05)
    script = tmp_path / "dies.py"
    script.write_text("import sys; sys.exit(3)\n")
    r = VendorRtk([sys.executable, str(script)], now_ms=lambda: 0)
    r.start()
    try:
        assert _等(lambda: "returncode=3" in r.error)
    finally:
        r.close()
