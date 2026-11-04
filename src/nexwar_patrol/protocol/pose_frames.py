"""定位桥(``tools/ros2_pose_bridge.py``)的线协议 —— 换行分隔的 JSON,单向。

**为什么位姿要单独走一条线,而不是复用 8090 那条:**

8090 上的 ``patrol_agent`` 是 C++,不认识 ROS。它吐的 ``odom`` 是 SDK 的
**腿式里程** —— 四足走路时脚会打滑、上下台阶会掉高,这份里程注定要漂。
而地图系导航需要的是**地图系真值**,来源是 ROS 侧 slam_toolbox 定位器发的
``loc_map -> base_link`` TF。两个源,两条线。

设计 spec §3.4 的真理源规则在这里落地为:**地图系导航一律以定位桥为准,
``odom`` 只用于交叉校验** —— 两边差得离谱说明定位跳了,该停车而不是继续走。

**单向。** 桥只出不进,没有命令通道。它没有任何可被指挥的余地,也就没有
任何一条控制指令能经由它误伤定位。

**分层**: 本模块只做编解码,不碰 socket,也不 import ``backends``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: 协议版本。桥在 ``hello`` 里报自己的版本,对不上就拒绝 —— 现场最不需要的
#: 就是"连上了但字段对不上"这种半死不活的状态。与 ``agent_frames`` 同惯例。
PROTO_VERSION = 1

#: 桥的发送周期。即使查不到 TF 也照发(``ok:false``),所以这同时是心跳。
DEFAULT_HZ = 10.0


class PoseProtocolError(Exception):
    """收到的行不是合法的位姿帧。"""


@dataclass(frozen=True, slots=True)
class PoseHello:
    """桥连上后的第一帧,报协议版本和它在盯哪两个坐标系。"""

    proto: int
    map_frame: str
    base_frame: str


@dataclass(frozen=True, slots=True)
class PoseFrame:
    """一拍位姿。

    ``ok=False`` 表示这一刻查不到 TF —— 注意这**不代表定位丢了**,现场 TF
    抖一下很常见。判 LocLost 是上层按"连续多久没拿到 ok=True"来做的,
    不是看单帧。单帧的职责只是如实汇报这一刻的情况。
    """

    ok: bool
    x: float
    y: float
    yaw: float
    ts_ms: int
    reason: str = ""


Downstream = PoseHello | PoseFrame


def encode_hello(map_frame: str, base_frame: str) -> bytes:
    return _dump({"t": "hello", "proto": PROTO_VERSION,
                  "map_frame": map_frame, "base_frame": base_frame})


def encode_pose(x: float, y: float, yaw: float, ts_ms: int) -> bytes:
    return _dump({"t": "pose", "ok": True, "x": x, "y": y, "yaw": yaw,
                  "ts_ms": ts_ms})


def encode_pose_lost(reason: str, ts_ms: int) -> bytes:
    return _dump({"t": "pose", "ok": False, "reason": reason, "ts_ms": ts_ms})


def decode_frame(line: str | bytes) -> Downstream:
    """解一行。认不出来就抛,不返回 None —— 静默丢帧会把问题藏到现场。"""
    obj = _load(line)
    kind = obj.get("t")
    if kind == "hello":
        proto = obj.get("proto")
        if not isinstance(proto, int):
            raise PoseProtocolError(f"hello 的 proto 应为整数,实际为 {proto!r}")
        return PoseHello(
            proto=proto,
            map_frame=str(obj.get("map_frame", "")),
            base_frame=str(obj.get("base_frame", "")),
        )
    if kind == "pose":
        ok = bool(obj.get("ok", False))
        return PoseFrame(
            ok=ok,
            x=_num(obj, "x"),
            y=_num(obj, "y"),
            yaw=_num(obj, "yaw"),
            ts_ms=int(_num(obj, "ts_ms")),
            reason=str(obj.get("reason", "")),
        )
    raise PoseProtocolError(f"未知的帧类型 {kind!r}")


def _dump(obj: dict[str, object]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _load(line: str | bytes) -> dict[str, object]:
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PoseProtocolError(f"不是合法 JSON: {text!r}") from exc
    if not isinstance(obj, dict):
        raise PoseProtocolError(f"帧应为 JSON 对象,实际为 {type(obj).__name__}")
    return obj


def _num(obj: dict[str, object], key: str, default: float = 0.0) -> float:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


__all__ = [
    "DEFAULT_HZ",
    "PROTO_VERSION",
    "Downstream",
    "PoseFrame",
    "PoseHello",
    "PoseProtocolError",
    "decode_frame",
    "encode_hello",
    "encode_pose",
    "encode_pose_lost",
]
