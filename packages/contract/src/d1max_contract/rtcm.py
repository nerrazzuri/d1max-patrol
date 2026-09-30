"""RTCM3 切帧(W09e):站点从自建基站(UM982)收改正数据、按帧校验后发给狗;狗原样写进 RTK 模组。

帧:``0xD3``、6 位保留 + 10 位长度、报文体(≤ 1023 字节)、CRC-24Q(3 字节,算整帧除校验本身)。
只认帧、不解报文内容 —— 除了 1005 / 1006(基站天线参考点的 ECEF 坐标,给人看基站在哪)。
"""

from __future__ import annotations

import math

PREAMBLE = 0xD3
MAX_BODY = 1023


def crc24q(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def frame(body: bytes) -> bytes:
    """报文体 → 一整帧。"""
    if len(body) > MAX_BODY:
        raise ValueError(f"RTCM3 报文体最多 {MAX_BODY} 字节")
    head = bytes([PREAMBLE, (len(body) >> 8) & 0x03, len(body) & 0xFF]) + body
    return head + crc24q(head).to_bytes(3, "big")


def msg_type(f: bytes) -> int:
    """一帧的报文号(报文体头 12 位)。"""
    return (f[3] << 4) | (f[4] >> 4) if len(f) >= 6 else -1


class RtcmFramer:
    """喂字节、吐整帧。找 ``0xD3`` 头、等齐长度、核 CRC:坏的丢掉、计数(``bad``),头之前的垃圾字节计
    ``skipped``。缓冲最多 :data:`MAX_BUF` 字节(一直等不齐的就从下一个头再找)。"""

    MAX_BUF = 4096

    def __init__(self) -> None:
        self._buf = bytearray()
        self.bad = 0
        self.skipped = 0
        self.frames = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while True:
            i = self._buf.find(PREAMBLE)
            if i < 0:
                self.skipped += len(self._buf)
                self._buf.clear()
                break
            if i:
                self.skipped += i
                del self._buf[:i]
            if len(self._buf) < 3:
                break
            n = ((self._buf[1] & 0x03) << 8) | self._buf[2]
            if self._buf[1] & 0xFC:                        # 保留位不是 0:不是帧头
                self.skipped += 1
                del self._buf[:1]
                continue
            total = 3 + n + 3
            if len(self._buf) < total:
                if len(self._buf) > self.MAX_BUF:          # 一直等不齐:这个头是假的
                    self.skipped += 1
                    del self._buf[:1]
                    continue
                break
            f = bytes(self._buf[:total])
            if crc24q(f[:-3]) == int.from_bytes(f[-3:], "big"):
                out.append(f)
                self.frames += 1
                del self._buf[:total]
            else:
                self.bad += 1
                del self._buf[:1]                         # 从下一个字节再找头
        if len(self._buf) > self.MAX_BUF:
            self.skipped += len(self._buf) - self.MAX_BUF
            del self._buf[:-self.MAX_BUF]
        return out


def _bits(body: bytes, start: int, n: int, signed: bool = False) -> int:
    v = int.from_bytes(body, "big") >> (len(body) * 8 - start - n) & ((1 << n) - 1)
    if signed and v & (1 << (n - 1)):
        v -= 1 << n
    return v


def station_ecef(f: bytes) -> tuple[float, float, float] | None:
    """1005 / 1006:基站天线参考点的 ECEF(米);别的报文 None。"""
    if msg_type(f) not in (1005, 1006):
        return None
    body = f[3:-3]
    if len(body) < 19:
        return None
    x = _bits(body, 34, 38, signed=True)
    y = _bits(body, 74, 38, signed=True)
    z = _bits(body, 114, 38, signed=True)
    return x * 1e-4, y * 1e-4, z * 1e-4


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    """WGS84 ECEF → (纬度°, 经度°, 椭球高 m)(Bowring 迭代)。"""
    a, e2 = 6378137.0, 6.69437999014e-3
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    h = 0.0
    for _ in range(8):
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


__all__ = ["MAX_BODY", "PREAMBLE", "RtcmFramer", "crc24q", "ecef_to_llh", "frame", "msg_type",
           "station_ecef"]
