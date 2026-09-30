"""W09e:RTCM3 切帧(站点收基站、狗转串口都用)、下行主题 ``rtcm``、遥测里的 ``rtk``。"""

from __future__ import annotations

import math
import struct

import pytest

from d1max_contract.messages import Telemetry
from d1max_contract.rtcm import RtcmFramer, crc24q, ecef_to_llh, frame, msg_type, station_ecef
from d1max_contract.topics import TopicAcl, Topics


def _body(mt: int, extra: bytes = b"\x00" * 10) -> bytes:
    """报文体:头 12 位是报文号。"""
    return bytes([mt >> 4, (mt & 0xF) << 4]) + extra


def test_crc24q_已知值():
    # RTCM 标准里的 CRC-24Q:空串 0,「123456789」= 0xCDE703
    assert crc24q(b"") == 0
    assert crc24q(b"123456789") == 0xCDE703


def test_组帧_切帧_报文号():
    f = frame(_body(1077))
    assert f[0] == 0xD3 and msg_type(f) == 1077
    r = RtcmFramer()
    assert r.feed(f) == [f] and r.bad == 0


def test_半帧拼起来_垃圾里找头_坏校验丢掉计数():
    a, b = frame(_body(1005)), frame(_body(1087, b"\x11" * 40))
    bad = bytearray(frame(_body(1097)))
    bad[-1] ^= 0xFF
    r = RtcmFramer()
    stream = b"junk\x00\xd3" + a + bytes(bad) + b
    got = []
    for i in range(0, len(stream), 7):                     # 一次来 7 个字节
        got += r.feed(stream[i:i + 7])
    assert got == [a, b]
    assert r.bad == 1 and r.skipped >= 5


def test_缓冲不无限长():
    r = RtcmFramer()
    r.feed(b"\xd3\x03\xff" + b"\x00" * 100)                # 说自己 1023 字节,一直等不齐
    for _ in range(50):
        r.feed(b"\x00" * 1000)
    assert len(r._buf) <= RtcmFramer.MAX_BUF


def _1005(x: float, y: float, z: float) -> bytes:
    """造一条 1005(基站天线参考点 ECEF,0.0001 m 一个单位,38 位有符号)。"""
    bits = ""
    # DF002 报文号 12、DF003 站号 12、DF021 6、DF022–024 各 1、DF141 1;然后 X 38、DF142 + DF001 2、
    # Y 38、DF364 2、Z 38
    bits += format(1005, "012b") + format(0, "012b") + format(0, "006b") + "000" + "0"
    for v, tail in ((x, "00"), (y, "00"), (z, "")):
        n = round(v * 10000)
        bits += format(n & ((1 << 38) - 1), "038b")
        bits += tail
    bits += "0" * (-len(bits) % 8)
    return frame(int(bits, 2).to_bytes(len(bits) // 8, "big"))


def test_1005_解出基站坐标_换成经纬高():
    # 槟城附近一点:经纬高 → ECEF(WGS84)→ 编进 1005 → 解回来
    lat, lon, h = 5.4141, 100.3288, 20.0
    a, e2 = 6378137.0, 6.69437999014e-3
    n = a / math.sqrt(1 - e2 * math.sin(math.radians(lat)) ** 2)
    x = (n + h) * math.cos(math.radians(lat)) * math.cos(math.radians(lon))
    y = (n + h) * math.cos(math.radians(lat)) * math.sin(math.radians(lon))
    z = (n * (1 - e2) + h) * math.sin(math.radians(lat))
    f = _1005(x, y, z)
    assert msg_type(f) == 1005
    ex, ey, ez = station_ecef(f)
    assert (ex, ey, ez) == pytest.approx((x, y, z), abs=1e-3)
    la, lo, hh = ecef_to_llh(ex, ey, ez)
    assert (la, lo) == pytest.approx((lat, lon), abs=1e-8)
    assert hh == pytest.approx(h, abs=1e-3), "1005 按 0.1 mm 量化"
    assert station_ecef(frame(_body(1077))) is None


def test_下行主题rtcm_狗订得到_发不了():
    t = Topics(site_id="s", robot_id="r")
    assert t.rtcm == "site/s/robot/r/rtcm"
    assert Topics.parse(t.rtcm) == ("s", "r", "rtcm")
    acl = TopicAcl(t)
    assert acl.may_subscribe(t.rtcm) and not acl.may_publish(t.rtcm)


def test_遥测带rtk_往返_老狗不带():
    rtk = {"fix": "fixed", "lat": 5.41, "lon": 100.32, "alt": 20.0, "sats": 22, "hdop": 0.7,
           "std_h_m": 0.012, "age_s": 1.2, "stamp_ms": 5}
    t = Telemetry(stamp=1, pose=None, battery_pct=90, task_state=None, loc_quality=1.0, rtk=rtk)
    assert Telemetry.from_wire(t.to_wire()).rtk == rtk
    old = Telemetry(stamp=1, pose=None, battery_pct=90, task_state=None, loc_quality=1.0)
    assert "rtk" not in old.to_wire() and Telemetry.from_wire(old.to_wire()).rtk is None


def test_rtcm_帧长度上限():
    with pytest.raises(ValueError):
        frame(b"\x00" * 1024)
    assert struct.unpack(">H", frame(b"\x00" * 1023)[1:3])[0] & 0x3FF == 1023
