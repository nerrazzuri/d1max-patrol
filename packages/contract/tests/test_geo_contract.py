"""W09e 地图与经纬度配准的契约:``geo.json``、经纬度 ↔ ENU ↔ 地图。"""

from __future__ import annotations

import math

import pytest

from d1max_contract.errors import ContractError
from d1max_contract.geo import GeoRef, enu_to_llh, llh_to_enu

LAT0, LON0 = 5.4141, 100.3288


def test_经纬度与ENU往返_一公里内毫米级():
    for e, n in ((0.0, 0.0), (500.0, -300.0), (-800.0, 700.0)):
        lat, lon = enu_to_llh(e, n, LAT0, LON0)
        assert llh_to_enu(lat, lon, LAT0, LON0) == pytest.approx((e, n), abs=1e-6)
    # 纬度 1″ 大约 30.7 m(赤道附近)
    e, n = llh_to_enu(LAT0 + 1 / 3600, LON0, LAT0, LON0)
    assert n == pytest.approx(30.72, abs=0.05) and e == pytest.approx(0.0)


def test_地图与ENU互换_朝向逆时针():
    g = GeoRef(LAT0, LON0, 10.0, yaw_deg=90.0, tx=5.0, ty=-2.0)
    assert g.map_to_enu(1.0, 0.0) == pytest.approx((5.0, -1.0)), "地图 x 轴朝北"
    assert g.enu_to_map(*g.map_to_enu(3.0, 4.0)) == pytest.approx((3.0, 4.0))
    lat, lon = enu_to_llh(*g.map_to_enu(12.0, -7.0), LAT0, LON0)
    assert g.llh_to_map(lat, lon) == pytest.approx((12.0, -7.0), abs=1e-6)


def test_天线在狗身前面_换回狗身中心():
    g = GeoRef(LAT0, LON0, 0.0, 0.0, 0.0, 0.0, antenna_in_base=(0.3, 0.1))
    assert g.body_from_antenna(10.3, 5.1, 0.0) == pytest.approx((10.0, 5.0))
    assert g.body_from_antenna(10.0 - 0.1, 5.0 + 0.3, math.pi / 2) == pytest.approx((10.0, 5.0))


def test_往返与校验():
    g = GeoRef(LAT0, LON0, 12.5, 33.0, 1.0, 2.0, rms_m=0.04, pairs=120,
               antenna_in_base=(0.2, 0.0))
    assert GeoRef.from_wire(g.to_wire()) == g
    for bad in ({}, {"origin": {"lat": 1}}, g.to_wire() | {"yaw_deg": "x"},
                g.to_wire() | {"tx": float("nan")},
                g.to_wire() | {"origin": {"lat": 95, "lon": 0, "alt": 0}}):
        with pytest.raises(ContractError):
            GeoRef.from_wire(bad)
