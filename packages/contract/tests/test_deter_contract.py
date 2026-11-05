"""``deter`` 载荷(W21;W40 加 ``side``、``color``):两头同一份校验,来回不变样。"""

from __future__ import annotations

import pytest

from nexwar_contract.deter import DeterRequest, parse_deter
from nexwar_contract.errors import ContractError


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


# ------------------------------------------------ POC 11-06:竞品云台、雨刷(只在 POC 分支)


@pytest.mark.parametrize("p", [
    {"output": "wiper", "on": True, "max_s": 10.0},
    {"output": "wiper", "on": False},
    {"output": "ptz", "on": True, "pan": 30.0, "tilt": -5.0},
])
def test_POC_雨刷云台_来回不变样(p):
    assert parse_deter(p).to_payload() == p


def test_POC_云台不用带max_s_角度按度():
    r = parse_deter({"output": "ptz", "on": True, "pan": -90, "tilt": 10})
    assert (r.pan, r.tilt, r.max_s) == (-90.0, 10.0, 0.0)


@pytest.mark.parametrize("p", [
    {"output": "ptz", "on": False},
    {"output": "ptz", "on": True, "pan": 181, "tilt": 0},
    {"output": "ptz", "on": True, "pan": 0, "tilt": 91},
    {"output": "ptz", "on": True, "pan": "0", "tilt": 0},
    {"output": "ptz", "on": True, "pan": 0},
    {"output": "ptz", "on": True, "pan": float("nan"), "tilt": 0},
    {"output": "wiper", "on": True},
    {"output": "strobe", "on": True, "max_s": 5, "pan": 3},
])
def test_POC_云台雨刷写错就拒(p):
    with pytest.raises(ContractError):
        parse_deter(p)
