"""本机定位桥的报文(W09a,W08 决定 2、4):定位器 ↔ 代理,一行一个 JSON。定位器(ROS 那一侧)与代理都用
这一份,所以放在契约包、不依赖 ROS。"""

from __future__ import annotations

import json
import math

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.locbridge import (
    MAX_LINE,
    PROTO,
    Error,
    Heartbeat,
    Hello,
    Pose,
    Relocalize,
    Reply,
    SetPrior,
    State,
    encode,
    parse,
)


def _pose(**kw):
    d = {"t": "pose", "seq": 3, "stamp_ns": 1_000, "map_id": "estate-1", "map_version": "7",
         "x": 1.5, "y": -2.0, "yaw": 0.3, "sigma_xy": 0.1, "sigma_yaw": 0.02,
         "source": "scan_match", "jump": False}
    d.update(kw)
    return json.dumps(d).encode()


def test_每种报文来回一样():
    msgs = [
        Hello(proto=PROTO, name="mola-loc", version="0.1"),
        Pose(seq=3, stamp_ns=1000, map_id="estate-1", map_version="7", x=1.5, y=-2.0, yaw=0.3,
             sigma_xy=0.1, sigma_yaw=0.02, source="scan_match", jump=True, reloc_id=4),
        State(seq=4, state="lost", reason="匹配不上"),
        Heartbeat(seq=5),
        Reply(req=2, ok=False, reason="初值离地图太远"),
        SetPrior(req=1, map_id="estate-1", map_version="7", dir="/var/lib/d1max/agent/maps/e/7"),
        Relocalize(req=2, map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=1.0, sigma_xy=0.5),
        Error(reason="协议版本对不上"),
    ]
    for m in msgs:
        raw = encode(m)
        assert raw.endswith(b"\n") and raw.count(b"\n") == 1
        assert parse(raw) == m


def test_位姿_缺省值_不成形的都拒():
    p = parse(_pose())
    assert p.jump is False and p.reloc_id is None
    for bad in (dict(x=math.inf), dict(y="1"), dict(sigma_xy=-0.1), dict(sigma_yaw=float("nan")),
                dict(source="gps"), dict(seq=0), dict(seq=True), dict(stamp_ns=-1),
                dict(map_id="../x"), dict(map_version=""), dict(jump="yes"), dict(reloc_id=0)):
        with pytest.raises(ContractError):
            parse(_pose(**bad))
    d = json.loads(_pose())
    del d["sigma_xy"]
    with pytest.raises(ContractError, match="sigma_xy"):
        parse(json.dumps(d).encode())


def test_行太长_不是JSON对象_不认识的类型_都拒():
    with pytest.raises(ContractError, match="太长"):
        parse(b'{"t":"hb","seq":1,"pad":"' + b"x" * MAX_LINE + b'"}')
    for raw in (b"[1,2]", b"not json", b"\xff\xfe", b'{"t":"teleport","seq":1}', b'{"seq":1}'):
        with pytest.raises(ContractError):
            parse(raw)


def test_状态_回复_请求的边界():
    with pytest.raises(ContractError):
        State(seq=1, state="sleeping")
    with pytest.raises(ContractError):
        State(seq=1, state="lost", reason="x" * 300)
    with pytest.raises(ContractError):
        Reply(req=0, ok=True)
    with pytest.raises(ContractError):
        Reply(req=1, ok="yes")
    assert SetPrior(req=1, map_id="e", map_version="7", dir="").dir == "", \
        "空 = 定位器按自己的配置找"
    with pytest.raises(ContractError):
        SetPrior(req=1, map_id="e", map_version="7", dir="/" * 5000)
    with pytest.raises(ContractError):
        Relocalize(req=1, map_id="e", map_version="7", x=0.0, y=0.0, yaw=0.0, sigma_xy=-1.0)
    with pytest.raises(ContractError):
        Hello(proto=0)
    with pytest.raises(ContractError):
        Hello(proto=1, name="x" * 65)
