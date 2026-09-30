"""W09e RTK 核对定位器(纯逻辑)。"""

from __future__ import annotations

import pytest

from d1max_agent.rtk_check import DIFF_M, HOLD_S, RELOC_COOLDOWN_S, RtkCheck
from d1max_contract.geo import GeoRef, enu_to_llh

GEO = GeoRef(5.41, 100.32, 10.0, yaw_deg=30.0, tx=2.0, ty=-1.0, antenna_in_base=(0.3, 0.0))


def _fix(x, y, yaw=0.0, **kw):
    """狗身中心在地图 (x, y)、朝向 yaw 时,天线处的 RTK 固定解。"""
    import math
    ax, ay = x + 0.3 * math.cos(yaw), y + 0.3 * math.sin(yaw)
    lat, lon = enu_to_llh(*GEO.map_to_enu(ax, ay), GEO.lat0, GEO.lon0)
    return {"fix": "fixed", "lat": lat, "lon": lon, "std_h_m": 0.02, "age_s": 1.0,
            "stale": False} | kw


def _check():
    c = RtkCheck()
    c.on_map(GEO)
    return c


def test_对得上什么都不做():
    c = _check()
    for t in range(10):
        v = c.step(_fix(10, 5, 0.5), (10.1, 5.05, 0.5), float(t))
        assert v.reason == "" and v.reloc is None and v.gap_m == pytest.approx(0.11, abs=0.01)


def test_连续差一米以上三秒_不可信_在RTK的位置重定位_冷却期内不再请():
    c = _check()
    got = [c.step(_fix(10, 5, 0.5), (14, 5, 0.5), t * 0.5) for t in range(int(HOLD_S / 0.5) + 1)]
    assert all(v.reason == "" for v in got[:-1])
    last = got[-1]
    assert "RTK 说位置差 4.0 m" in last.reason
    assert last.reloc == pytest.approx((10, 5, 0.5), abs=1e-6), "朝向用定位器的"
    v = c.step(_fix(10, 5, 0.5), (14, 5, 0.5), HOLD_S + 1)
    assert v.reason and v.reloc is None, "冷却期内不再请"
    v = c.step(_fix(10, 5, 0.5), (14, 5, 0.5), HOLD_S + RELOC_COOLDOWN_S + 0.1)
    assert v.reloc is not None


def test_单帧跳一下不触发():
    c = _check()
    c.step(_fix(10, 5), (14, 5, 0.0), 0.0)
    c.step(_fix(10, 5), (10, 5, 0.0), 1.0)
    v = c.step(_fix(10, 5), (14, 5, 0.0), HOLD_S + 0.5)
    assert v.reason == "", "中间对上过一次:重新数"


def test_对回来连续三秒才解除():
    c = _check()
    for t in range(8):
        c.step(_fix(10, 5), (14, 5, 0.0), float(t))
    assert c.step(_fix(10, 5), (10.1, 5, 0.0), 10.0).reason
    assert c.step(_fix(10, 5), (10.1, 5, 0.0), 12.0).reason
    assert c.step(_fix(10, 5), (10.1, 5, 0.0), 13.0).reason == ""


@pytest.mark.parametrize("kw", [{"fix": "float"}, {"fix": "single"}, {"std_h_m": 0.3},
                                {"age_s": 9.0}, {"stale": True}, {"lat": None}])
def test_不够好的RTK不参与(kw):
    c = _check()
    for t in range(10):
        v = c.step(_fix(10, 5, **kw), (14, 5, 0.0), float(t))
    assert v.reason == "" and v.reloc is None and v.gap_m is None


def test_没有配准不参与_换图作废():
    c = RtkCheck()
    for t in range(10):
        assert c.step(_fix(10, 5), (14, 5, 0.0), float(t)).reason == ""
    c = _check()
    for t in range(8):
        c.step(_fix(10, 5), (14, 5, 0.0), float(t))
    assert c.step(_fix(10, 5), (14, 5, 0.0), 8.0).reason
    c.on_map(None)
    assert c.step(_fix(10, 5), (14, 5, 0.0), 9.0).reason == ""


def test_差在一米以内不算():
    c = _check()
    for t in range(10):
        v = c.step(_fix(10, 5), (10 + DIFF_M * 0.9, 5, 0.0), float(t))
    assert v.reason == ""
