"""地图桥(``tools/ros2_map_bridge.py``)的线协议 —— 换行分隔的 JSON,单向。

**为什么地图要单独走一条线:** 建图 UI 要看的是 ROS 侧 ``/map`` 上那张
``nav_msgs/OccupancyGrid``,而 app 是普通 Python 进程,不在 ROS 环境里。
和位姿一样,这里靠一条零依赖的 TCP 线把 ROS 侧的东西送出来。

**为什么要压:** 一张 40m x 40m / 5cm 分辨率的图是 64 万格,直接按 JSON
数组发一帧就是几兆,2Hz 推起来现场链路顶不住。占据栅格绝大部分是成片的
未知(-1)和成片的空(0),游程编码能把这种图压到几百字节。

线格式:``(值, 次数)`` 对,值是 ``int8``(-1..100),次数是小端 ``uint32``,
整块 ``struct.pack`` 之后 base64。

**单向。** 桥只出不进,没有命令通道 —— 和定位桥同一条理由。

**分层**: 本模块只做编解码,不碰 socket,也不 import ``backends``。
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass

#: 协议版本。桥在 ``hello`` 里报,对不上就拒绝。与 ``pose_frames`` 同惯例。
PROTO_VERSION = 1

#: 地图的默认推送上限。地图只是画面,不是安全关键路径 —— 1Hz 足够看出
#: "图在长",再快只是白烧带宽。真正的降频在桥那边发送前做。
DEFAULT_MAX_HZ = 1.0

#: 占据栅格的合法取值:-1 未知,0..100 占据概率(ROS 的 int8 约定)。
_MIN_CELL = -1
_MAX_CELL = 100

#: 一对里的次数存成 uint32,再长就拆成多对。真实的图不会有这么长的游程,
#: 但截断会让整张图往后错位,所以宁可多写几对。
_MAX_RUN = 0xFFFFFFFF

#: 一对 = 1 字节的值 + 4 字节的次数。
_PAIR = struct.Struct("<bI")


class MapProtocolError(Exception):
    """收到的行不是合法的地图帧。"""


@dataclass(frozen=True, slots=True)
class MapHello:
    """桥连上后的第一帧,报协议版本和它在盯哪个话题。"""

    proto: int
    topic: str


@dataclass(frozen=True, slots=True)
class MapFrame:
    """一帧占据栅格。``data`` 已解成逐格的 int(-1 未知 / 0..100 占据概率)。

    ``data`` 是行优先的,第 ``(x, y)`` 格在 ``data[y * width + x]`` ——
    和 ROS 的 ``OccupancyGrid.data`` 一致,免得画的时候还要翻一道。
    """

    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    data: tuple[int, ...]
    ts_ms: int


Downstream = MapHello | MapFrame


def encode_rle(cells: Sequence[int]) -> str:
    """把逐格的占据值压成 base64 的游程串。

    值不合法当场抛,不做"就近取整"之类的补救 —— 一个越界的值多半说明
    上游拿错了数组,悄悄修好只会让错误活到画面上。
    """
    out = bytearray()
    run_value: int | None = None
    run_len = 0
    for index, cell in enumerate(cells):
        value = _check_cell(cell, index)
        if value == run_value:
            run_len += 1
            if run_len == _MAX_RUN:
                out += _PAIR.pack(run_value, run_len)
                run_value, run_len = None, 0
            continue
        if run_value is not None:
            out += _PAIR.pack(run_value, run_len)
        run_value, run_len = value, 1
    if run_value is not None:
        out += _PAIR.pack(run_value, run_len)
    return base64.b64encode(bytes(out)).decode("ascii")


def decode_rle(blob: str, *, expect: int) -> tuple[int, ...]:
    """解游程串。``expect`` 是宽乘高 —— 对不上一律抛,不返回半张图。"""
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MapProtocolError(f"rle 不是合法 base64: {exc}") from exc
    if len(raw) % _PAIR.size:
        raise MapProtocolError(
            f"rle 长度 {len(raw)} 不是 {_PAIR.size} 的整数倍,流被截断了")
    cells: list[int] = []
    for offset in range(0, len(raw), _PAIR.size):
        value, count = _PAIR.unpack_from(raw, offset)
        if not _MIN_CELL <= value <= _MAX_CELL:
            raise MapProtocolError(f"rle 里的占据值 {value} 越界")
        if len(cells) + count > expect:
            raise MapProtocolError(f"rle 解出来的格子多于 {expect} 个")
        cells.extend([value] * count)
    if len(cells) != expect:
        raise MapProtocolError(f"rle 解出 {len(cells)} 格,应为 {expect} 格")
    return tuple(cells)


def encode_hello(topic: str) -> bytes:
    return _dump({"t": "hello", "proto": PROTO_VERSION, "topic": topic})


def encode_map(
    width: int,
    height: int,
    resolution: float,
    origin: tuple[float, float, float],
    cells: Sequence[int],
    ts_ms: int,
) -> bytes:
    """编一帧地图。``origin`` 是 ``(x, y, yaw)``,即图左下角在地图系的位姿。"""
    if width * height != len(cells):
        raise MapProtocolError(
            f"{width}x{height} 要 {width * height} 格,给了 {len(cells)} 格")
    ox, oy, oyaw = origin
    return _dump({
        "t": "map", "proto": PROTO_VERSION,
        "w": int(width), "h": int(height), "res": float(resolution),
        "ox": float(ox), "oy": float(oy), "oyaw": float(oyaw),
        "rle": encode_rle(cells), "ts_ms": int(ts_ms),
    })


def decode_frame(line: str | bytes) -> Downstream:
    """解一行。认不出来就抛,不返回 None —— 和定位桥同一条规矩。"""
    obj = _load(line)
    kind = obj.get("t")
    if kind == "hello":
        proto = obj.get("proto")
        if not isinstance(proto, int) or isinstance(proto, bool):
            raise MapProtocolError(f"hello 的 proto 应为整数,实际为 {proto!r}")
        return MapHello(proto=proto, topic=str(obj.get("topic", "")))
    if kind == "map":
        width = _int(obj, "w")
        height = _int(obj, "h")
        rle = obj.get("rle")
        if not isinstance(rle, str):
            raise MapProtocolError(f"map 的 rle 应为字符串,实际为 {rle!r}")
        return MapFrame(
            width=width,
            height=height,
            resolution=_num(obj, "res"),
            origin_x=_num(obj, "ox"),
            origin_y=_num(obj, "oy"),
            origin_yaw=_num(obj, "oyaw"),
            data=decode_rle(rle, expect=width * height),
            ts_ms=int(_num(obj, "ts_ms")),
        )
    raise MapProtocolError(f"未知的帧类型 {kind!r}")


def _check_cell(cell: object, index: int) -> int:
    # bool 是 int 的子类,``True`` 会静悄悄变成 1 —— 单独挡掉。
    if isinstance(cell, bool) or not isinstance(cell, int):
        raise MapProtocolError(f"第 {index} 格不是整数: {cell!r}")
    if not _MIN_CELL <= cell <= _MAX_CELL:
        raise MapProtocolError(
            f"第 {index} 格的值 {cell} 越界,应在 {_MIN_CELL}..{_MAX_CELL}")
    return cell


def _dump(obj: dict[str, object]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _load(line: str | bytes) -> dict[str, object]:
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MapProtocolError(f"不是合法 JSON: {text!r}") from exc
    if not isinstance(obj, dict):
        raise MapProtocolError(f"帧应为 JSON 对象,实际为 {type(obj).__name__}")
    return obj


def _int(obj: dict[str, object], key: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MapProtocolError(f"map 的 {key} 应为整数,实际为 {value!r}")
    if value < 0:
        raise MapProtocolError(f"map 的 {key} 不能是负数: {value}")
    return value


def _num(obj: dict[str, object], key: str, default: float = 0.0) -> float:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


__all__ = [
    "DEFAULT_MAX_HZ",
    "PROTO_VERSION",
    "Downstream",
    "MapFrame",
    "MapHello",
    "MapProtocolError",
    "decode_frame",
    "decode_rle",
    "encode_hello",
    "encode_map",
    "encode_rle",
]
