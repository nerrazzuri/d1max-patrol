"""本机人员桥的报文(W24,决策 39):人员检测节点(ROS 那一侧)→ 代理。

跟障碍桥(:mod:`d1max_contract.obsbridge`)同一套规矩:Unix 套接字、**一行一个 JSON**
(≤ :data:`MAX_LINE`)、第一行 ``hello``、未知字段整行拒。单开一个套接字(``persons.sock``)。

**狗只报事实**(这一帧看到几个人、在哪个方向、多远),判定(算不算有人、人走了没有、告警、驱离升级)
在代理和站点(解耦总设计:「狗只报事实,判定在站点」)。

检测节点 → 代理:

- ``hello {proto, name, version}``
- ``persons {seq, stamp_ns, camera, check, reason, people, snapshot}``:一帧的结果。
  ``camera``:``front`` / ``back``;``check``:``ok``、``no_model``(检测模型没装上)、``no_camera``
  (取不到画面)、``initializing``,不是 ``ok`` 时 ``reason`` 写原因,``people`` 为空。
  ``people``:每个人 ``{bearing_deg, range_m, score}``:方向(狗身系,朝前 0°、朝左为正,−180–180)、
  雷达量的距离(米;那个方向雷达没打到人就是 ``null``)、检测分数(0–1)。最多 :data:`MAX_PEOPLE` 个。
  ``snapshot``:这一帧存下来的图(检测节点的截图目录里的文件名,空 = 没存)。
- ``hb {seq}``:心跳,每秒一条。

代理 → 检测节点:``hello {proto}``、``error {reason}``(握手不对,说完就断)。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from d1max_contract.errors import ContractError

PROTO = 1
MAX_LINE = 16 * 1024
CAMERAS = ("front", "back")
CHECKS = ("ok", "no_model", "no_camera", "initializing")
MAX_PEOPLE = 16
MAX_RANGE_M = 100.0
_SNAP = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,95}\.jpg")


def _pos_int(v: Any, what: str, *, zero: bool = False) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < (0 if zero else 1):
        raise ContractError(f"人员桥:{what} 要是{'不小于 0' if zero else '正'}的整数:{v!r}")


def _text(v: Any, what: str, limit: int) -> None:
    if not isinstance(v, str) or len(v) > limit:
        raise ContractError(f"人员桥:{what} 要是字符串、最多 {limit} 字")


def _num(v: Any, what: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not lo <= v <= hi:
        raise ContractError(f"人员桥:{what} 要在 {lo}–{hi}:{v!r}")
    return float(v)


@dataclass(frozen=True)
class Person:
    bearing_deg: float
    range_m: float | None
    score: float

    def __post_init__(self) -> None:
        _num(self.bearing_deg, "bearing_deg", -180.0, 180.0)
        if self.range_m is not None:
            _num(self.range_m, "range_m", 0.0, MAX_RANGE_M)
        _num(self.score, "score", 0.0, 1.0)


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
class Persons:
    T: ClassVar[str] = "persons"
    seq: int
    stamp_ns: int
    camera: str
    check: str = "ok"
    reason: str = ""
    people: tuple[Person, ...] = field(default_factory=tuple)
    snapshot: str = ""

    def __post_init__(self) -> None:
        _pos_int(self.seq, "seq")
        _pos_int(self.stamp_ns, "stamp_ns", zero=True)
        if self.camera not in CAMERAS:
            raise ContractError(f"人员桥:camera 要是 {'/'.join(CAMERAS)}:{self.camera!r}")
        if self.check not in CHECKS:
            raise ContractError(f"人员桥:check 要是 {'/'.join(CHECKS)}:{self.check!r}")
        _text(self.reason, "reason", 200)
        raw = self.people
        if not isinstance(raw, (list, tuple)) or len(raw) > MAX_PEOPLE:
            raise ContractError(f"人员桥:people 要是最多 {MAX_PEOPLE} 个的列表")
        people = []
        for p in raw:
            if isinstance(p, Person):
                people.append(p)
            elif isinstance(p, dict) and set(p) == {"bearing_deg", "range_m", "score"}:
                people.append(Person(**p))
            else:
                raise ContractError("人员桥:people 每一项要是 {bearing_deg, range_m, score}")
        object.__setattr__(self, "people", tuple(people))
        if self.check != "ok" and people:
            raise ContractError("人员桥:check 不是 ok 时 people 要空")
        if self.snapshot and (not isinstance(self.snapshot, str)
                              or not _SNAP.fullmatch(self.snapshot)):
            raise ContractError(f"人员桥:snapshot 要是截图目录里的 .jpg 文件名:{self.snapshot!r}")

    def nearest(self) -> Person | None:
        """有距离的里面最近的;都没距离就是分数最高的。"""
        ranged = [p for p in self.people if p.range_m is not None]
        if ranged:
            return min(ranged, key=lambda p: p.range_m)
        return max(self.people, key=lambda p: p.score) if self.people else None


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


Message = Hello | Persons | Heartbeat | Error
_TYPES: dict[str, type] = {c.T: c for c in (Hello, Persons, Heartbeat, Error)}


def encode(msg: Message) -> bytes:
    return json.dumps({"t": msg.T, **asdict(msg)}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def parse(line: bytes) -> Message:
    """一行 → 报文。不成形的抛 ``ContractError``。"""
    if len(line) > MAX_LINE:
        raise ContractError(f"人员桥:一行太长({len(line)} 字节,最多 {MAX_LINE})")
    try:
        d = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError(f"人员桥:不是 UTF-8 的 JSON:{exc}") from None
    if not isinstance(d, dict):
        raise ContractError("人员桥:一行要是一个 JSON 对象")
    t = d.pop("t", None)
    cls = _TYPES.get(t) if isinstance(t, str) else None
    if cls is None:
        raise ContractError(f"人员桥:不认识的报文类型:{t!r}")
    try:
        return cls(**d)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError(f"人员桥:{cls.T} 的字段不对:{exc}") from None
