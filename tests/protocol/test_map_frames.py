"""地图桥线协议的编解码。

和定位桥一样,这条协议也有两份实现:本模块测的是权威定义,
``tools/ros2_map_bridge.py`` 手抄了一份。最后那条"字面量对齐"的测试
就是把两边钉在一起的钉子。
"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.protocol import map_frames
from d1max_patrol.protocol.map_frames import (
    PROTO_VERSION,
    MapFrame,
    MapHello,
    MapProtocolError,
    decode_frame,
    decode_rle,
    encode_hello,
    encode_map,
    encode_rle,
)

# ----------------------------------------------------------------------- RLE


def test_全空的图压得很小():
    cells = [0] * 160_000
    blob = encode_rle(cells)
    assert len(blob) < 100, "16 万个相同的格子应该压成一对"


def test_压了再解还是原样():
    cells = [-1] * 10 + [0] * 5 + [100] * 3 + [-1]
    assert decode_rle(encode_rle(cells), expect=len(cells)) == tuple(cells)


def test_空图也能压能解():
    assert decode_rle(encode_rle([]), expect=0) == ()


def test_每个格子都不一样也压得回来():
    """最坏情况:一格一个游程。压不小没关系,不能压错。"""
    cells = list(range(0, 101)) + [-1]
    assert decode_rle(encode_rle(cells), expect=len(cells)) == tuple(cells)


@pytest.mark.parametrize("bad", [-2, 101, 255])
def test_值超出范围当场拒绝(bad):
    with pytest.raises(MapProtocolError):
        encode_rle([0, bad])


@pytest.mark.parametrize("bad", [True, False, 1.5, "0", None])
def test_不是整数的格子也当场拒绝(bad):
    """YAML/JSON 里一个 ``true`` 就能混进来,而 bool 是 int 的子类。"""
    with pytest.raises(MapProtocolError):
        encode_rle([0, bad])


def test_格子数对不上要报错而不是给半张图():
    """半张图画出来是错的位置,比没有图更危险。"""
    with pytest.raises(MapProtocolError, match="格"):
        decode_rle(encode_rle([0] * 10), expect=11)


def test_不是base64的一坨会被拒():
    with pytest.raises(MapProtocolError):
        decode_rle("这不是base64###", expect=1)


def test_长度不是一对一对的会被拒():
    """五字节一对,凑不齐说明流被截断了 —— 截断的图不能画。"""
    import base64

    with pytest.raises(MapProtocolError):
        decode_rle(base64.b64encode(b"\x00\x01\x02").decode(), expect=1)


def test_超长的游程会被拆成多对(monkeypatch):
    """次数是 uint32,理论上会溢出。压不下就拆,不能悄悄截断。"""
    monkeypatch.setattr(map_frames, "_MAX_RUN", 3)
    cells = [7] * 10
    assert decode_rle(encode_rle(cells), expect=10) == tuple(cells)


# ---------------------------------------------------------------------- 帧


def test_编码出来的每一行都自带换行():
    for line in (encode_hello("/map"),
                 encode_map(1, 1, 0.05, (0.0, 0.0, 0.0), [0], 1)):
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1


def test_一帧地图编解码往返():
    raw = encode_map(4, 2, 0.05, (-1.0, -2.0, 0.0), [0, 0, -1, -1, 100, 100, 0, 0], 123)
    f = decode_frame(raw)
    assert isinstance(f, MapFrame)
    assert (f.width, f.height) == (4, 2)
    assert f.resolution == pytest.approx(0.05)
    assert (f.origin_x, f.origin_y) == pytest.approx((-1.0, -2.0))
    assert f.data == (0, 0, -1, -1, 100, 100, 0, 0)
    assert f.ts_ms == 123


def test_原点朝向也带上():
    """图不是永远正north上的,原点带 yaw 才画得对。"""
    f = decode_frame(encode_map(1, 1, 0.05, (0.0, 0.0, 1.57), [0], 1))
    assert f.origin_yaw == pytest.approx(1.57)


def test_宽高和格子数对不上编码时就拒():
    with pytest.raises(MapProtocolError, match="格"):
        encode_map(4, 2, 0.05, (0.0, 0.0, 0.0), [0, 0, 0], 1)


def test_hello里报协议版本():
    h = decode_frame(encode_hello("/map"))
    assert isinstance(h, MapHello)
    assert h.proto == PROTO_VERSION and h.topic == "/map"


@pytest.mark.parametrize("line", ['{"t":"???"}', "{}", "不是json", '{"t":"map"}'])
def test_认不出来的行一律抛(line):
    with pytest.raises(MapProtocolError):
        decode_frame(line)


def test_乱码字节不会炸在解码上():
    with pytest.raises(MapProtocolError):
        decode_frame(b"\xff\xfe\x00")


def test_帧里宽高对不上格子数也要抛():
    """桥那边算错一次,画出来就是错位的图 —— 收的时候必须挡住。"""
    bad = {"t": "map", "proto": PROTO_VERSION, "w": 3, "h": 1, "res": 0.05,
           "ox": 0.0, "oy": 0.0, "oyaw": 0.0,
           "rle": encode_rle([0, 100]), "ts_ms": 7}
    with pytest.raises(MapProtocolError, match="格"):
        decode_frame(json.dumps(bad))


def test_帧不是json对象也抛():
    with pytest.raises(MapProtocolError):
        decode_frame("[1, 2, 3]")


def test_地图帧是不可变的():
    """UI 那边会攥着 latest 不放,能被改就意味着能被改坏。"""
    f = decode_frame(encode_map(1, 1, 0.05, (0.0, 0.0, 0.0), [0], 1))
    with pytest.raises(AttributeError):
        f.width = 2  # type: ignore[misc]


def test_桥那边手抄的字面量和本模块对得上():
    """tools/ros2_map_bridge.py 不 import 本模块,两边靠这条测试锁死。"""
    literal = {"t": "map", "proto": 1, "w": 2, "h": 1, "res": 0.05,
               "ox": 0.0, "oy": 0.0, "oyaw": 0.0,
               "rle": encode_rle([0, 100]), "ts_ms": 7}
    f = decode_frame(json.dumps(literal))
    assert f.data == (0, 100)


def test_本模块不碰socket():
    """线协议只做编解码 —— 和 pose_frames 一样,分层不能在这里破。"""
    import inspect

    src = inspect.getsource(map_frames)
    for banned in ("import socket", "asyncio", "d1max_patrol.backends"):
        assert banned not in src, f"线协议里不该出现 {banned}"
