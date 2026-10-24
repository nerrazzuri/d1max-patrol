"""``deter`` 载荷(W21;W40 加 ``side``、``color``):两头同一份校验,来回不变样。"""

from __future__ import annotations

import pytest

from d1max_contract.deter import DeterRequest, parse_deter
from d1max_contract.errors import ContractError


@pytest.mark.parametrize("p", [
    {"output": "spotlight", "on": True, "max_s": 30.0, "side": "left"},
    {"output": "spotlight", "on": False, "side": "right"},
    {"output": "spotlight", "on": True, "max_s": 30.0},
    {"output": "strobe", "on": True, "max_s": 45.0, "color": "yellow"},
    {"output": "strobe", "on": False},
    {"output": "speaker", "on": True, "max_s": 10.0, "clip": "warn-zh", "priority": 60},
])
def test_来回不变样(p):
    assert parse_deter(p).to_payload() == p


def test_side缺省两边_不写进载荷():
    r = parse_deter({"output": "spotlight", "on": True, "max_s": 5, "side": "both"})
    assert r.side == "both" and "side" not in r.to_payload()
    assert DeterRequest(output="strobe", on=True, max_s=5).to_payload() == {
        "output": "strobe", "on": True, "max_s": 5}


@pytest.mark.parametrize("p", [
    {"output": "spotlight", "on": True, "max_s": 5, "side": "middle"},
    {"output": "spotlight", "on": True, "max_s": 5, "side": 1},
    {"output": "siren", "on": True, "max_s": 5, "side": "left"},
    {"output": "spotlight", "on": True, "max_s": 5, "color": "red"},
    {"output": "strobe", "on": False, "color": "red"},
    {"output": "strobe", "on": True, "max_s": 5, "color": "Red"},
    {"output": "strobe", "on": True, "max_s": 5, "color": "r" * 17},
    {"output": "strobe", "on": True, "max_s": 5, "color": 3},
])
def test_写错就拒(p):
    with pytest.raises(ContractError):
        parse_deter(p)
