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


def test_超大的数_当不成形_不炸():
    """W09a 内审:400 位的整数让 ``math.isfinite`` 抛 ``OverflowError``,以前冒出去把读任务弄死、
    连接断开。"""
    with pytest.raises(ContractError):
        parse(_pose(x=10**400))
    with pytest.raises(ContractError):
        parse(_pose(seq=10**400, sigma_xy=10**400))
    with pytest.raises(ContractError):                   # 不经 parse、直接造(定位器节点那边这么用)
        Pose(seq=1, stamp_ns=1, map_id="m", map_version="1", x=10**400, y=0.0, yaw=0.0,
             sigma_xy=0.1, sigma_yaw=0.1, source="scan_match")


def test_位姿可以说这一帧是它自己推了多久的():
    """W09a 内审阻断 1:定位器匹配线程死了、拿自己的里程往前推,帧照发 —— 它要在 ``meas_age_ms`` 里说
    离上一次真匹配过了多久(真匹配的那一帧是 0),代理把它算进新鲜。"""
    assert parse(_pose()).meas_age_ms == 0
    assert parse(_pose(meas_age_ms=1500)).meas_age_ms == 1500
    for bad in (-1, 1.5, True):
        with pytest.raises(ContractError):
            parse(_pose(meas_age_ms=bad))


def test_W41_重定位带是不是人给的_缺省是人给的_不是布尔拒():
    import pytest as _pt

    from d1max_contract.errors import ContractError
    from d1max_contract.locbridge import Relocalize, encode, parse
    r = Relocalize(req=3, map_id="e", map_version="7", x=0.0, y=0.0, yaw=0.0, sigma_xy=0.5,
                   human=False)
    assert parse(encode(r)) == r
    old = b'{"t":"relocalize","req":3,"map_id":"e","map_version":"7","x":0,"y":0,"yaw":0,' \
          b'"sigma_xy":0.5}'
    assert parse(old).human is True, "老代理不带:当人给的(跟原来一样)"
    with _pt.raises(ContractError):
        parse(old.replace(b"}", b',"human":1}'))


def test_工单11_位姿带原始ICP质量_可选_越界拒():
    from d1max_contract.locbridge import Pose, encode, parse
    kw = dict(seq=1, stamp_ns=1, map_id="m", map_version="1", x=0.0, y=0.0, yaw=0.0,
              sigma_xy=0.1, sigma_yaw=0.02, source="scan_match")
    assert Pose(**kw).icp_quality is None
    p = Pose(**kw, icp_quality=0.94)
    assert parse(encode(p)) == p
    for bad in (1.5, -0.1, float("nan")):
        with pytest.raises(ContractError):
            Pose(**kw, icp_quality=bad)


def test_外审R9_后加的字段取默认值时不写进报文_老定位器照样认():
    """W40/W41 外审 R9:新代理的人工重定位总带 ``human: true``,老定位器(6793485)整条拒收。"""
    import json

    from d1max_contract.locbridge import Pose, Relocalize, encode, parse
    r = Relocalize(req=1, map_id="m", map_version="1", x=1.0, y=1.0, yaw=0.0, sigma_xy=0.5)
    d = json.loads(encode(r))
    assert "human" not in d and parse(encode(r)) == r
    auto = Relocalize(req=2, map_id="m", map_version="1", x=1.0, y=1.0, yaw=0.0, sigma_xy=0.5,
                      human=False)
    assert json.loads(encode(auto))["human"] is False, "不是默认值照写(老的拒,不悄悄当成人给的)"
    p = Pose(seq=1, stamp_ns=1, map_id="m", map_version="1", x=0.0, y=0.0, yaw=0.0,
             sigma_xy=0.1, sigma_yaw=0.02, source="scan_match")
    assert "icp_quality" not in json.loads(encode(p))
    # 老定位器的解析器(真代码)认得新代理的人工重定位
    import subprocess
    import sys
    import types
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    try:
        old = subprocess.check_output(["git", "-C", str(root), "show",
                                       "6793485:packages/contract/src/d1max_contract/locbridge.py"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("没有 git 历史")
    mod = types.ModuleType("old_locbridge_6793485")
    sys.modules[mod.__name__] = mod
    exec(compile(old, "<old-locbridge>", "exec"), mod.__dict__)
    got = mod.parse(encode(r))
    assert (got.x, got.y) == (1.0, 1.0)
