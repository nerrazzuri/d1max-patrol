"""本机定位桥的报文(W09a,W08 决定 2、4)。

定位器(ROS 那一侧,W09b)跟代理在同一台狗上,经 Unix 套接字说话:**一行一个 JSON**(UTF-8),每行
最多 :data:`MAX_LINE` 字节。代理这边监听,定位器来连,第一行必须是 ``hello``。这一份两边都用,所以放在
契约包、不依赖 ROS。

定位器 → 代理:

- ``hello {proto, name, version}``
- ``pose {seq, stamp_ns, map_id, map_version, x, y, yaw, sigma_xy, sigma_yaw, source, jump,
  reloc_id?}``:地图位姿与不确定度(米、弧度);``source`` 是 ``scan_match``/``rtk``/``fused``;
  ``jump`` 为真 = 这一帧跟上一帧不连续(比如刚重定位完);``reloc_id`` = 是哪一次重定位请求的结果。
  ``stamp_ns`` 是定位器自己的时间戳(只作参考:新鲜不新鲜按代理收到的时刻判)。
- ``status {seq, state, reason}``:``initializing``/``tracking``/``lost``
- ``hb {seq}``:心跳,每秒一条
- ``reply {req, ok, reason}``:对代理请求的回复

代理 → 定位器:``hello {proto}``、``set_prior {req, map_id, map_version, dir}``(换先验:这张图在狗上
的目录;空 = 狗上没有这张图的目录(按启动参数载的图),定位器按自己的配置找)、
``relocalize {req, map_id, map_version, x, y, yaw, sigma_xy}``(带初值重定位)、``hb {seq}``、
``error {reason}``(握手不对,说完就断)。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from d1max_contract.errors import ContractError
from d1max_contract.maps import check_name

PROTO = 1
MAX_LINE = 16 * 1024
SOURCES = ("scan_match", "rtk", "fused")
STATES = ("initializing", "tracking", "lost")


def _pos_int(v: Any, what: str, *, zero: bool = False) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < (0 if zero else 1):
        raise ContractError(f"定位桥:{what} 要是{'不小于 0' if zero else '正'}的整数:{v!r}")


def _num(v: Any, what: str, *, nonneg: bool = False) -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ContractError(f"定位桥:{what} 要是有限数:{v!r}")
    if nonneg and v < 0:
        raise ContractError(f"定位桥:{what} 不许为负:{v!r}")


def _text(v: Any, what: str, limit: int, *, empty: bool = True) -> None:
    if not isinstance(v, str) or len(v) > limit or (not empty and not v):
        raise ContractError(f"定位桥:{what} 要是{'' if empty else '非空'}字符串、最多 {limit} 字")


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
class Pose:
    T: ClassVar[str] = "pose"
    seq: int
    stamp_ns: int
    map_id: str
    map_version: str
    x: float
    y: float
    yaw: float
    sigma_xy: float
    sigma_yaw: float
    source: str
    jump: bool = False
    reloc_id: int | None = None

    def __post_init__(self) -> None:
        _pos_int(self.seq, "seq")
        _pos_int(self.stamp_ns, "stamp_ns", zero=True)
        check_name(self.map_id, "定位桥:map_id")
        check_name(self.map_version, "定位桥:map_version")
        for k in ("x", "y", "yaw"):
            _num(getattr(self, k), k)
        for k in ("sigma_xy", "sigma_yaw"):
            _num(getattr(self, k), k, nonneg=True)
        if self.source not in SOURCES:
            raise ContractError(f"定位桥:source 要是 {'/'.join(SOURCES)}:{self.source!r}")
        if not isinstance(self.jump, bool):
            raise ContractError("定位桥:jump 要是 true/false")
        if self.reloc_id is not None:
            _pos_int(self.reloc_id, "reloc_id")


@dataclass(frozen=True)
class State:
    T: ClassVar[str] = "status"
    seq: int
    state: str
    reason: str = ""

    def __post_init__(self) -> None:
        _pos_int(self.seq, "seq")
        if self.state not in STATES:
            raise ContractError(f"定位桥:state 要是 {'/'.join(STATES)}:{self.state!r}")
        _text(self.reason, "reason", 200)


@dataclass(frozen=True)
class Heartbeat:
    T: ClassVar[str] = "hb"
    seq: int

    def __post_init__(self) -> None:
        _pos_int(self.seq, "seq")


@dataclass(frozen=True)
class Reply:
    T: ClassVar[str] = "reply"
    req: int
    ok: bool
    reason: str = ""

    def __post_init__(self) -> None:
        _pos_int(self.req, "req")
        if not isinstance(self.ok, bool):
            raise ContractError("定位桥:ok 要是 true/false")
        _text(self.reason, "reason", 200)


@dataclass(frozen=True)
class SetPrior:
    T: ClassVar[str] = "set_prior"
    req: int
    map_id: str
    map_version: str
    dir: str

    def __post_init__(self) -> None:
        _pos_int(self.req, "req")
        check_name(self.map_id, "定位桥:map_id")
        check_name(self.map_version, "定位桥:map_version")
        _text(self.dir, "dir", 4096)


@dataclass(frozen=True)
class Relocalize:
    T: ClassVar[str] = "relocalize"
    req: int
    map_id: str
    map_version: str
    x: float
    y: float
    yaw: float
    sigma_xy: float

    def __post_init__(self) -> None:
        _pos_int(self.req, "req")
        check_name(self.map_id, "定位桥:map_id")
        check_name(self.map_version, "定位桥:map_version")
        for k in ("x", "y", "yaw"):
            _num(getattr(self, k), k)
        _num(self.sigma_xy, "sigma_xy", nonneg=True)


@dataclass(frozen=True)
class Error:
    T: ClassVar[str] = "error"
    reason: str

    def __post_init__(self) -> None:
        _text(self.reason, "reason", 200)


Message = Hello | Pose | State | Heartbeat | Reply | SetPrior | Relocalize | Error
_TYPES: dict[str, type] = {c.T: c for c in (Hello, Pose, State, Heartbeat, Reply, SetPrior,
                                             Relocalize, Error)}


def encode(msg: Message) -> bytes:
    return json.dumps({"t": msg.T, **asdict(msg)}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def parse(line: bytes) -> Message:
    """一行 → 报文。不成形的抛 ``ContractError``(说清楚哪里不对)。"""
    if len(line) > MAX_LINE:
        raise ContractError(f"定位桥:一行太长({len(line)} 字节,最多 {MAX_LINE})")
    try:
        d = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError(f"定位桥:不是 UTF-8 的 JSON:{exc}") from None
    if not isinstance(d, dict):
        raise ContractError("定位桥:一行要是一个 JSON 对象")
    t = d.pop("t", None)
    cls = _TYPES.get(t) if isinstance(t, str) else None
    if cls is None:
        raise ContractError(f"定位桥:不认识的报文类型:{t!r}")
    try:
        return cls(**d)
    except TypeError as exc:
        raise ContractError(f"定位桥:{cls.T} 的字段不对:{exc}") from None
