"""本机人员桥的报文(W24):往返、各字段的边界、未知字段整行拒、check 不是 ok 时不许带人。"""

from __future__ import annotations

import json

import pytest

from d1max_contract import persbridge as pb
from d1max_contract.errors import ContractError
from d1max_contract.persbridge import (
    MAX_LINE,
    MAX_PEOPLE,
    Heartbeat,
    Hello,
    Person,
    Persons,
    encode,
    parse,
)


def _p(**kw):
    d = {"seq": 1, "stamp_ns": 5, "camera": "front", "check": "ok", "reason": "",
         "people": [{"bearing_deg": 10.0, "range_m": 6.5, "score": 0.9},
                    {"bearing_deg": -30.0, "range_m": None, "score": 0.7}],
         "snapshot": "20261007T010203Z-front.jpg"} | kw
    return d


def test_往返():
    for m in (Hello(proto=1, name="d1max-persons", version="0.1"), Heartbeat(seq=3),
              Persons(**_p())):
        assert parse(encode(m)) == m
    m = parse(encode(Persons(**_p())))
    assert m.people[0] == Person(bearing_deg=10.0, range_m=6.5, score=0.9)
    assert m.nearest().range_m == 6.5


def test_最近的_没距离就挑分数最高的():
    m = Persons(**_p(people=[{"bearing_deg": 0.0, "range_m": None, "score": 0.4},
                             {"bearing_deg": 5.0, "range_m": None, "score": 0.8}]))
    assert m.nearest().score == 0.8
    assert Persons(**_p(people=[])).nearest() is None


@pytest.mark.parametrize("bad", [
    {"camera": "top"}, {"check": "weird"}, {"seq": 0}, {"stamp_ns": -1},
    {"people": [{"bearing_deg": 181.0, "range_m": 1.0, "score": 0.5}]},
    {"people": [{"bearing_deg": 0.0, "range_m": -1.0, "score": 0.5}]},
    {"people": [{"bearing_deg": 0.0, "range_m": 1.0, "score": 1.5}]},
    {"people": [{"bearing_deg": 0.0, "range_m": 1.0}]},
    {"people": [{"bearing_deg": 0.0, "range_m": 1.0, "score": 0.5, "x": 1}]},
    {"people": [{"bearing_deg": 0.0, "range_m": 1.0, "score": 0.5}] * (MAX_PEOPLE + 1)},
    {"check": "no_model", "reason": "没模型"},                   # 不是 ok 还带着人
    {"snapshot": "../x.jpg"}, {"snapshot": "a.png"}, {"snapshot": "/tmp/a.jpg"},
    {"bearing_deg": 1.0},                                         # 未知字段
])
def test_不合规矩的整行拒(bad):
    d = _p(**bad) if "bearing_deg" not in bad else _p() | bad
    with pytest.raises(ContractError):
        parse(json.dumps({"t": "persons", **d}).encode())


def test_check不是ok_不带人_带原因():
    m = Persons(**_p(check="no_camera", reason="RTSP 拉不到", people=[], snapshot=""))
    assert parse(encode(m)) == m


def test_一行太长_不认识的类型_不是对象():
    with pytest.raises(ContractError, match="太长"):
        parse(b"x" * (MAX_LINE + 1))
    with pytest.raises(ContractError, match="不认识"):
        parse(b'{"t":"grid","seq":1}')
    with pytest.raises(ContractError):
        parse(b"[1]")


# ---------------------------------------------------------------- 复查 P212-I1:双向认证 + 逐行签名

def test_复查P212_双向认证_代理也要证明自己有密钥():
    k = bytes(range(32))
    na, nd = "ab" * 16, "cd" * 16
    assert pb.agent_mac(k, nd, na) != pb.hello_mac(k, na, "x")
    assert pb.agent_mac(k, nd, na) != pb.agent_mac(b"\x01" * 32, nd, na)
    assert pb.Hello(proto=pb.PROTO, mac="ab" * 32, nonce=nd).nonce == nd
    with pytest.raises(ContractError):
        pb.Hello(proto=pb.PROTO, nonce="不是十六进制")


def test_复查P212_逐行签名_改一个字或者换方向重放旧序号都不认():
    k = bytes(range(32))
    ks = pb.session_key(k, "ab" * 16, "cd" * 16)
    line = pb.seal(ks, pb.TO_AGENT, 5, pb.encode(pb.Heartbeat(seq=9)))
    seq, m = pb.unseal(ks, pb.TO_AGENT, line, last_seq=4)
    assert seq == 5 and m == pb.Heartbeat(seq=9)
    with pytest.raises(ContractError, match="序号"):
        pb.unseal(ks, pb.TO_AGENT, line, last_seq=5)               # 重放
    with pytest.raises(ContractError, match="签名"):
        pb.unseal(ks, pb.TO_DETECTOR, line, last_seq=0)            # 换方向
    with pytest.raises(ContractError, match="签名"):
        pb.unseal(ks, pb.TO_AGENT, line.replace(b'\\"seq\\":9', b'\\"seq\\":8'), last_seq=0)
    with pytest.raises(ContractError, match="签名"):
        pb.unseal(pb.session_key(k, "ab" * 16, "ef" * 16), pb.TO_AGENT, line, last_seq=0)
    with pytest.raises(ContractError):
        pb.unseal(ks, pb.TO_AGENT, pb.encode(pb.Heartbeat(seq=1)), last_seq=0)   # 没封
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 300_000 + b"\xff\xd9"
    snap = pb.seal(ks, pb.TO_AGENT, 6, pb.encode(pb.Snap.of("a.jpg", jpeg)))
    assert isinstance(pb.unseal(ks, pb.TO_AGENT, snap, last_seq=5)[1], pb.Snap)
