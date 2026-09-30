"""W09e RTK 核对定位器,代理这一层:真代理 + 仿真狗 + 仿真定位器(本机桥)。RTK 说狗在别处 →
定位不可信、请定位器在 RTK 的位置重定位;对回来 → 解除。"""

from __future__ import annotations

import pytest
from test_relocalize import _遥测
from test_runtime_localizer import 台子

from d1max_contract.geo import GeoRef, enu_to_llh

GEO = GeoRef(5.41, 100.32, 10.0, yaw_deg=0.0, tx=0.0, ty=0.0)


class 假RTK:
    kind = "own"

    def __init__(self):
        self.at = (0.0, 0.0)

    def start(self):
        pass

    def close(self):
        pass

    def feed(self, data):
        pass

    def latest(self):
        lat, lon = enu_to_llh(self.at[0], self.at[1], GEO.lat0, GEO.lon0)
        return {"fix": "fixed", "lat": lat, "lon": lon, "std_h_m": 0.02, "age_s": 1.0,
                "stale": False, "sats": 20}


@pytest.fixture
async def 台(tmp_path):
    t = await 台子().起(tmp_path)
    yield t
    await t.收()


async def test_定位器稳定地错了四米_RTK抓住_不可信_按RTK重定位_对回来解除(台):
    """W09a 的已知限制:定位器自信地跳错、之后一直稳定地错,交叉校验(只跟里程比)抓不住。"""
    t = 台
    await t.连()
    rtk = 假RTK()
    t.rt.rtk = rtk
    t.rt._rtk_check.on_map(GEO)
    t.loc.jump(4.0, 0.0, flag=True)                      # 定位器报跳变、稳稳地错 4 m
    await t.拍(30)
    assert _遥测(t.ears).loc["reason"] == "", "只看里程:它是可信的"
    o = await t.dog.odometry()
    rtk.at = (o.x, o.y)                                   # RTK 说的是真话
    await t.拍(40)
    assert t.loc.relocs, "请定位器在 RTK 的位置重定位了"
    r = t.loc.relocs[-1]
    assert (r.x, r.y) == pytest.approx((o.x, o.y), abs=0.1) and r.map_id == "m"
    await t.拍(60)                                        # 仿真定位器在初值附近对上了真实位置
    assert _遥测(t.ears).loc["reason"] == "", "对回来连续 3 s:解除"


async def test_RTK说不对的那几秒_遥测里说原因(台):
    t = 台
    await t.连()
    rtk = 假RTK()
    t.rt.rtk = rtk
    t.rt._rtk_check.on_map(GEO)
    t.loc.refuse_reloc = "测试:先不让它对回来"
    t.loc.jump(4.0, 0.0, flag=True)
    await t.拍(30)
    o = await t.dog.odometry()
    rtk.at = (o.x, o.y)
    await t.拍(40)
    assert "RTK 说位置差 4.0 m" in _遥测(t.ears).loc["reason"]
    assert not t.rt.parts.nav.anchor.ok(True)


async def test_没有配准_不核(台):
    t = 台
    await t.连()
    rtk = 假RTK()
    rtk.at = (100.0, 100.0)
    t.rt.rtk = rtk
    await t.拍(40)
    assert _遥测(t.ears).loc["reason"] == "" and not t.loc.relocs


async def test_换图_读新图的geo_json(tmp_path):
    import json

    from test_runtime_localizer import 真狗样
    from test_runtime_maps import _cmd, _跑, 站点

    from d1max_agent.maps import MapKeeper
    site = 站点()
    keeper = MapKeeper(tmp_path / "keep", fetch=site.fetch)
    t = 台子()
    await t.起(tmp_path / "agent", dog=真狗样, maps=keeper)
    try:
        await t.连()
        geo = json.dumps(GeoRef(5.4, 100.3, 1.0, 12.0, 3.0, 4.0).to_wire()).encode()
        wire = site.add("m", "2", {"prior.mm": b"p", "frames.json": b"{}", "geo.json": geo})
        await t.rt._on_cmd(_cmd("map_activate", wire, "a1", t.c))
        await _跑(t.rt, t.broker)
        assert t.rt._rtk_check.geo == GeoRef(5.4, 100.3, 1.0, 12.0, 3.0, 4.0)
        wire = site.add("m", "3", {"prior.mm": b"q", "frames.json": b"{}"})
        await t.rt._on_cmd(_cmd("map_activate", wire, "a2", t.c))
        await _跑(t.rt, t.broker)
        assert t.rt._rtk_check.geo is None, "新图没有 geo.json:不核"
    finally:
        await t.收()
