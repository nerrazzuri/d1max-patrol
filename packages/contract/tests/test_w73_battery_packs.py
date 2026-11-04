"""W73 第四期:遥测带两块电池各自的电量(设计 §7.4);老狗不带这个键,站点照收。"""

from __future__ import annotations

import pytest

from nexwar_contract.errors import ContractError
from nexwar_contract.hal import Battery
from nexwar_contract.messages import Telemetry


def _t(**kw):
    return Telemetry(stamp=1, pose=None, battery_pct=30.0, task_state=None, loc_quality=1.0, **kw)


def test_两块电池来回不变_拔了的是null():
    t = _t(battery_packs=[30.0, None])
    w = t.to_wire()
    assert w["battery_packs"] == [30.0, None]
    assert Telemetry.from_wire(w).battery_packs == [30.0, None]


def test_老狗不带_也不发这个键():
    w = _t().to_wire()
    assert "battery_packs" not in w
    assert Telemetry.from_wire(w).battery_packs is None


def test_写错的拒收():
    for bad in ("x", [1, 2, 3, 4, 5], ["a"], [150.0], [-1.0]):
        w = {**_t().to_wire(), "battery_packs": bad}
        with pytest.raises(ContractError):
            Telemetry.from_wire(w)


def test_hal_电池默认不报分块():
    assert Battery(percent=50.0, charging=False).packs == ()
