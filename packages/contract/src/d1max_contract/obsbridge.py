"""本机障碍桥的报文(W11 设计稿 §3,W08 决定 8):感知节点(ROS 那一侧)→ 代理。

跟定位桥(:mod:`d1max_contract.locbridge`)同一套规矩:Unix 套接字、**一行一个 JSON**
(≤ :data:`MAX_LINE`)、第一行 ``hello``、未知字段整行拒。单开一个套接字(``obs.sock``):
定位器、感知是两个进程。

感知 → 代理:

- ``hello {proto, name, version}``
- ``grid {seq, stamp_ns, res, size, occ, known, rear, rear_cal, check, reason}``:**狗身系**局部栅格
  (狗身中心居中,``size × size`` 格、每格 ``res`` 米)。格 ``(r, c)`` 的中心在狗身系
  ``x = (r − size/2 + 0.5)·res``(朝前)、``y = (c − size/2 + 0.5)·res``(朝左);
  第 ``i = r·size + c`` 格对应位图第 ``i`` 位(第 ``i // 8`` 个字节、从高位数第 ``i % 8`` 位)。
  ``occ``:挡(凸起障碍或落差);``known``:看见过(不在里面 = 未知 = 当挡);都是 base64。
  ``rear``:后雷达用上了没有;``rear_cal``:后雷达外参真标过(``lidars.json``,W09i)—— 标过才用它的
  「空」,狗尾为前才许自己走;``check``:启动自检 ``ok`` / ``extrinsic_bad`` / ``initializing``,
  不是 ``ok`` 时 ``reason`` 写原因。
- ``hb {seq}``:心跳,每秒一条。

代理 → 感知:``hello {proto}``、``error {reason}``(握手不对,说完就断)。
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from d1max_contract.errors import ContractError

PROTO = 1
MAX_LINE = 16 * 1024
CHECKS = ("ok", "extrinsic_bad", "initializing")
MIN_SIZE, MAX_SIZE = 10, 120
MIN_RES, MAX_RES = 0.05, 0.5


def _pos_int(v: Any, what: str, *, zero: bool = False) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < (0 if zero else 1):
        raise ContractError(f"障碍桥:{what} 要是{'不小于 0' if zero else '正'}的整数:{v!r}")


def _text(v: Any, what: str, limit: int) -> None:
    if not isinstance(v, str) or len(v) > limit:
        raise ContractError(f"障碍桥:{what} 要是字符串、最多 {limit} 字")


def nbytes(size: int) -> int:
    return (size * size + 7) // 8


def pack_bits(bits: list[bool] | bytes | bytearray, size: int) -> str:
    """``size × size`` 个真假(行优先)→ base64 位图。"""
    if len(bits) != size * size:
        raise ContractError(f"障碍桥:位图要 {size * size} 位,给了 {len(bits)}")
    out = bytearray(nbytes(size))
    for i, b in enumerate(bits):
        if b:
            out[i >> 3] |= 0x80 >> (i & 7)
    return base64.b64encode(bytes(out)).decode("ascii")


def unpack_bits(s: str, size: int) -> bytes:
    """base64 位图 → ``size × size`` 个字节(0 / 1,行优先)。长度不对抛 ``ContractError``。"""
    try:
        raw = base64.b64decode(s.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise ContractError("障碍桥:位图不是 base64") from None
    if len(raw) != nbytes(size):
        raise ContractError(f"障碍桥:位图要 {nbytes(size)} 字节,给了 {len(raw)}")
    n = size * size
    return bytes((raw[i >> 3] >> (7 - (i & 7))) & 1 for i in range(n))


@dataclass(frozen=True)
class Hello:
    T: ClassVar[str] = "hello"
    proto: int
    name: str = ""
    version: str = ""

    def __post_init__(self) -> None:
        _pos_int(self.proto, "proto")
        _text(self.name, "name", 64)
        _text(self.version, "version", 64)


@dataclass(frozen=True)
class Grid:
    T: ClassVar[str] = "grid"
    seq: int
    stamp_ns: int
    res: float
    size: int
    occ: str
    known: str
    rear: bool = False
    rear_cal: bool = False
    check: str = "ok"
    reason: str = ""

    def __post_init__(self) -> None:
        _pos_int(self.seq, "seq")
        _pos_int(self.stamp_ns, "stamp_ns", zero=True)
        if (isinstance(self.res, bool) or not isinstance(self.res, (int, float))
                or not math.isfinite(self.res) or not MIN_RES <= self.res <= MAX_RES):
            raise ContractError(f"障碍桥:res 要在 {MIN_RES}–{MAX_RES} 米:{self.res!r}")
        _pos_int(self.size, "size")
        if not MIN_SIZE <= self.size <= MAX_SIZE or self.size % 2:
            raise ContractError(f"障碍桥:size 要是 {MIN_SIZE}–{MAX_SIZE} 的偶数:{self.size!r}")
        if not isinstance(self.rear, bool) or not isinstance(self.rear_cal, bool):
            raise ContractError("障碍桥:rear、rear_cal 要是真假")
        if self.check not in CHECKS:
            raise ContractError(f"障碍桥:check 要是 {'/'.join(CHECKS)}:{self.check!r}")
        _text(self.reason, "reason", 200)
        for name in ("occ", "known"):
            v = getattr(self, name)
            if not isinstance(v, str):
                raise ContractError(f"障碍桥:{name} 要是 base64 字符串")
            unpack_bits(v, self.size)                 # 长度、编码都核一遍

    def bits(self) -> tuple[bytes, bytes]:
        """→ (挡, 看见过),各 ``size × size`` 个 0 / 1。"""
        return unpack_bits(self.occ, self.size), unpack_bits(self.known, self.size)


@dataclass(frozen=True)
class Heartbeat:
    T: ClassVar[str] = "hb"
    seq: int

    def __post_init__(self) -> None:
        _pos_int(self.seq, "seq")


@dataclass(frozen=True)
class Error:
    T: ClassVar[str] = "error"
    reason: str

    def __post_init__(self) -> None:
        _text(self.reason, "reason", 200)


Message = Hello | Grid | Heartbeat | Error
_TYPES: dict[str, type] = {c.T: c for c in (Hello, Grid, Heartbeat, Error)}


def encode(msg: Message) -> bytes:
    return json.dumps({"t": msg.T, **asdict(msg)}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def parse(line: bytes) -> Message:
    """一行 → 报文。不成形的抛 ``ContractError``。"""
    if len(line) > MAX_LINE:
        raise ContractError(f"障碍桥:一行太长({len(line)} 字节,最多 {MAX_LINE})")
    try:
        d = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError(f"障碍桥:不是 UTF-8 的 JSON:{exc}") from None
    if not isinstance(d, dict):
        raise ContractError("障碍桥:一行要是一个 JSON 对象")
    t = d.pop("t", None)
    cls = _TYPES.get(t) if isinstance(t, str) else None
    if cls is None:
        raise ContractError(f"障碍桥:不认识的报文类型:{t!r}")
    try:
        return cls(**d)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError(f"障碍桥:{cls.T} 的字段不对:{exc}") from None
