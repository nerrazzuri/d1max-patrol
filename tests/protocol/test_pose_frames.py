"""定位桥线协议的编解码。

这条协议有两份实现:本模块是权威定义,``tools/ros2_pose_bridge.py`` 里
手抄了一份(它要在 ROS2 的系统 Python 下跑,不能 import 本仓库)。所以
这里额外钉一条测试,拿桥那边写死的字面量来对 —— 两边漂了要当场看出来。
"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.protocol.pose_frames import (
    PROTO_VERSION,
    PoseFrame,
    PoseHello,
    PoseProtocolError,
    decode_frame,
    encode_hello,
    encode_pose,
    encode_pose_lost,
)


def test_编码出来的每一行都自带换行():
    """桥用 readline 切帧,少一个换行就是两帧粘在一起。"""
    for line in (encode_hello("loc_map", "base_link"),
                 encode_pose(1.0, 2.0, 0.5, 123),
                 encode_pose_lost("no tf", 123)):
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1


def test_hello_一个来回():
    frame = decode_frame(encode_hello("loc_map", "base_link"))
    assert frame == PoseHello(proto=PROTO_VERSION, map_frame="loc_map",
                              base_frame="base_link")


def test_位姿一个来回():
    frame = decode_frame(encode_pose(1.5, -2.5, 0.25, 1700000000123))
    assert frame == PoseFrame(ok=True, x=1.5, y=-2.5, yaw=0.25,
                              ts_ms=1700000000123)


def test_丢失帧带原因且坐标归零():
    """``ok=False`` 时坐标没有意义,必须是确定的 0 而不是上一帧的残值。"""
    frame = decode_frame(encode_pose_lost("lookup failed", 42))
    assert isinstance(frame, PoseFrame)
    assert not frame.ok
    assert frame.reason == "lookup failed"
    assert (frame.x, frame.y, frame.yaw) == (0.0, 0.0, 0.0)


def test_桥那边手抄的字面量和本模块对得上():
    """``ros2_pose_bridge.py`` 拼 JSON 是手写的 dict,字段名靠人保持一致。

    这里把它那几行原样重放一遍,解出来必须和本模块编出来的等价。
    """
    bridge_hello = json.dumps(
        {"t": "hello", "proto": 1, "map_frame": "loc_map",
         "base_frame": "base_link"}, ensure_ascii=False) + "\n"
    bridge_pose = json.dumps(
        {"t": "pose", "ok": True, "x": 1.0, "y": 2.0, "yaw": 0.5,
         "ts_ms": 42}, ensure_ascii=False) + "\n"
    bridge_lost = json.dumps(
        {"t": "pose", "ok": False, "reason": "boom", "ts_ms": 42},
        ensure_ascii=False) + "\n"

    assert decode_frame(bridge_hello) == decode_frame(
        encode_hello("loc_map", "base_link"))
    assert decode_frame(bridge_pose) == decode_frame(
        encode_pose(1.0, 2.0, 0.5, 42))
    assert decode_frame(bridge_lost) == decode_frame(
        encode_pose_lost("boom", 42))


def test_整数坐标也当浮点收():
    """JSON 里 0 和 0.0 是同一个字面量,解出来必须都是 float。

    漏了会在下游变成整数除法之类的隐性错误。
    """
    frame = decode_frame(b'{"t":"pose","ok":true,"x":1,"y":0,"yaw":0,"ts_ms":7}')
    assert isinstance(frame, PoseFrame)
    assert isinstance(frame.x, float) and frame.x == 1.0


def test_缺字段按零补而不是抛():
    """位姿缺字段是桥的 bug,但为此断开整条链路更糟 —— 补零后由新鲜度兜底。"""
    frame = decode_frame(b'{"t":"pose","ok":true,"x":1.0}')
    assert isinstance(frame, PoseFrame)
    assert (frame.y, frame.yaw, frame.ts_ms) == (0.0, 0.0, 0)


def test_布尔不会被当成数字():
    """``True`` 在 Python 里是 int 的子类,不挡的话 x=true 会变成 x=1.0。"""
    frame = decode_frame(b'{"t":"pose","ok":true,"x":true,"y":0,"yaw":0,"ts_ms":0}')
    assert isinstance(frame, PoseFrame)
    assert frame.x == 0.0


@pytest.mark.parametrize("line", [
    b"",
    b"not json",
    b"[1,2,3]",
    b'{"t":"whatever"}',
    b'{"t":"hello","proto":"1"}',
], ids=["空行", "不是JSON", "不是对象", "未知类型", "proto不是整数"])
def test_认不出来的行一律抛(line):
    """静默丢帧会把问题藏到现场 —— 宁可当场炸。"""
    with pytest.raises(PoseProtocolError):
        decode_frame(line)


def test_乱码字节不会炸在解码上():
    """日志混进流里是现场最常见的脏帧,必须落在 PoseProtocolError 上,
    而不是 UnicodeDecodeError —— 后者调用方接不住。
    """
    with pytest.raises(PoseProtocolError):
        decode_frame(b"\xff\xfe some log line\n")
