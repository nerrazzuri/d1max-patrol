"""W11 障碍桥报文:位图打包、栅格校验、来回一致、坏行。"""

from __future__ import annotations

import json

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.obsbridge import (
    MAX_LINE,
    Grid,
    Heartbeat,
    Hello,
    encode,
    nbytes,
    pack_bits,
    parse,
    unpack_bits,
)


def _grid(size=10, **kw):
    occ = [False] * (size * size)
    occ[0] = occ[size * size - 1] = True
    known = [True] * (size * size)
    d = dict(seq=1, stamp_ns=5, res=0.1, size=size, occ=pack_bits(occ, size),
             known=pack_bits(known, size))
    d.update(kw)
    return Grid(**d)


def test_位图来回_位序():
    bits = [i % 3 == 0 for i in range(100)]
    s = pack_bits(bits, 10)
    assert unpack_bits(s, 10) == bytes(int(b) for b in bits)
    assert nbytes(10) == 13 and nbytes(80) == 800
    one = [False] * 100
    one[0] = True
    import base64
    assert base64.b64decode(pack_bits(one, 10))[0] == 0x80, "第 0 位是第一个字节的最高位"
    with pytest.raises(ContractError):
        pack_bits([True] * 99, 10)
    with pytest.raises(ContractError):
        unpack_bits("!!!", 10)
    with pytest.raises(ContractError):
        unpack_bits(pack_bits([True] * 144, 12), 10)


def test_栅格来回_80格一行装得下():
    g = _grid(size=80, rear=True, rear_cal=True, check="ok")
    raw = encode(g)
    assert len(raw) < MAX_LINE // 4
    back = parse(raw)
    assert back == g
    occ, known = back.bits()
    assert occ[0] == 1 and occ[-1] == 1 and sum(occ) == 2 and all(known)


@pytest.mark.parametrize("bad", [
    dict(seq=0), dict(seq=True), dict(stamp_ns=-1), dict(res=0.01), dict(res=0.6), dict(size=11),
    dict(res=float("nan")), dict(res=True), dict(size=9), dict(size=8), dict(size=122),
    dict(rear=1), dict(rear_cal=1), dict(check="good"), dict(reason="x" * 201), dict(occ=5),
    dict(occ="AAAA"), dict(known="not base64!"),
])
def test_坏栅格拒(bad):
    with pytest.raises(ContractError):
        _grid(**bad)


def test_解析():
    assert parse(encode(Hello(proto=1, name="obstacles"))) == Hello(proto=1, name="obstacles")
    assert parse(encode(Heartbeat(seq=3))) == Heartbeat(seq=3)
    for line in (b"x" * (MAX_LINE + 1), b"\xff\n", b"[]\n", b'{"t":"pose"}\n',
                 json.dumps({"t": "hb", "seq": 1, "extra": 2}).encode(),
                 json.dumps({"t": "grid", "seq": 1}).encode()):
        with pytest.raises(ContractError):
            parse(line)
