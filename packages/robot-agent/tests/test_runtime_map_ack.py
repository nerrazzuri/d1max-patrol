"""R5、R3、R4(狗这一头):换图要等定位器确认才报成功、不收就退回原来那张(不重新下载);
能力里报狗实际在跑的配置(图摘要、区域修订与摘要、任务修订)。"""

from __future__ import annotations

import pytest
from test_runtime_localizer import 台子, 真狗样
from test_runtime_maps import _cmd, _跑, 站点

import d1max_agent.runtime as rtmod
from d1max_agent.maps import MapKeeper
from d1max_contract.maps import MapRef, map_digest
from d1max_contract.messages import Capabilities
from d1max_contract.zones import ZoneSet, zones_digest

FILES = {"prior.mm": b"p", "frames.json": b"{}"}


def _events(t, kind):
    return [e for e in t.ears.by.get("event", []) if e["kind"] == kind]


def _caps(t):
    return Capabilities.from_wire(t.ears.by["capabilities"][-1])


@pytest.fixture
async def 台(tmp_path):
    site = 站点()
    keeper = MapKeeper(tmp_path / "keep", fetch=site.fetch)
    t = 台子()
    await t.起(tmp_path / "agent", dog=真狗样, maps=keeper)
    await t.连()
    yield t, site, keeper
    await t.收()


async def _activate(t, site, version, cid):
    wire = site.add("m", version, {**FILES, "x": version.encode()})
    await t.rt._on_cmd(_cmd("map_activate", wire, cid, t.c))
    await _跑(t.rt, t.broker, settle_s=20.0)
    await t.拍(3)
    return MapRef.from_wire(wire)


async def test_换图_定位器收下了才发map_activated_事件带confirmed(台):
    t, site, keeper = 台
    ref = await _activate(t, site, "2", "a1")
    ev = _events(t, "map_activated")
    assert len(ev) == 1 and ev[0]["data"]["confirmed"] == "localizer", ev
    assert [(p.map_id, p.map_version) for p in t.loc.priors][-1] == ("m", "2"), "先验先到了定位器"
    assert t.rt.loaded_map == ("m", "2") and keeper.active() == ref


async def test_定位器不收_不报成功_退回原来那张_不重新下载(台):
    t, site, keeper = 台
    a = await _activate(t, site, "2", "a1")
    site.calls = []
    t.loc.prior_answer = (False, "先验坏了")
    b = site.add("m", "3", {**FILES, "x": b"3"})
    n_ok = len(_events(t, "map_activated"))
    await t.rt._on_cmd(_cmd("map_activate", b, "a2", t.c))
    await _跑(t.rt, t.broker, settle_s=20.0)
    await t.拍(3)
    failed = _events(t, "map_activate_failed")
    assert len(failed) == 1
    why = failed[0]["data"]["reason"]
    assert "先验坏了" in why and "已退回原来的 m:2" in why
    assert len(_events(t, "map_activated")) == n_ok, "没有为这次换图发成功"
    assert t.rt.loaded_map == ("m", "2") and keeper.active() == a, "狗还在原来那张"
    assert keeper.previous() is None, "失败的 3 不记成上一张"
    assert not keeper.dir_of(MapRef.from_wire(b)).exists(), "失败的新图清掉"
    # 退回原来那张时没有再去站点下载它
    assert all(c[1] != "2" for c in site.calls), site.calls
    # 能力里狗报的图也是原来那张
    assert _caps(t).tasks["running_config"]["map_version"] == "2"


async def test_定位器一直不回_超时算没成_也退回(台, monkeypatch):
    t, site, keeper = 台
    a = await _activate(t, site, "2", "a1")
    monkeypatch.setattr(rtmod, "PRIOR_ACK_WAIT_S", 0.5)
    t.loc.prior_answer = None                          # 不回话:模拟定位器卡住
    orig = t.loc.prior_answer
    assert orig is None
    await t.loc.close()                                # 断开:先验发不出去
    await t.拍(2)
    b = site.add("m", "3", {**FILES, "x": b"3"})
    await t.rt._on_cmd(_cmd("map_activate", b, "a2", t.c))
    await _跑(t.rt, t.broker, settle_s=20.0)
    await t.rt.step(0.1)
    await t.broker.drain()
    failed = _events(t, "map_activate_failed")
    assert failed and "没确认" in failed[-1]["data"]["reason"], failed
    assert t.rt.loaded_map == ("m", "2") and keeper.active() == a


async def test_第一次换图就被拒_没有别的图可退_事件说清楚(tmp_path):
    site = 站点()
    keeper = MapKeeper(tmp_path / "keep", fetch=site.fetch)
    t = 台子()
    await t.起(tmp_path / "agent", dog=真狗样, maps=keeper)
    try:
        await t.连()
        t.loc.prior_answer = (False, "不认")
        wire = site.add("m", "2", {**FILES})
        await t.rt._on_cmd(_cmd("map_activate", wire, "a1", t.c))
        await _跑(t.rt, t.broker, settle_s=20.0)
        await t.拍(3)
        failed = _events(t, "map_activate_failed")
        assert failed and "原来没有别的图可退" in failed[-1]["data"]["reason"], failed
        assert not _events(t, "map_activated")
    finally:
        await t.收()


async def test_running_config_图摘要_区域摘要_上一张_确认方式(台):
    t, site, keeper = 台
    a = await _activate(t, site, "2", "a1")
    b = await _activate(t, site, "3", "a2")
    rc = _caps(t).tasks["running_config"]
    assert rc["map_id"] == "m" and rc["map_version"] == "3"
    assert rc["map_digest"] == map_digest(b) != map_digest(a)
    assert rc["previous"] == ["m", "2"] and ["m", "2"] in rc["kept"] and ["m", "3"] in rc["kept"]
    assert rc["confirms"] == "localizer"
    assert rc["zones_rev"] == 0
    assert rc["zones_digest"] == zones_digest(ZoneSet("m", "3", 0, ()))


async def test_没有定位器的狗_载入成功就算_confirmed是load(tmp_path):
    t = 台子()
    site = 站点()
    keeper = MapKeeper(tmp_path / "keep", fetch=site.fetch)
    await t.起(tmp_path / "agent", localizer="anchor", maps=keeper)
    try:
        wire = site.add("m", "2", {"x.pgm": b"x"})
        await t.rt._on_cmd(_cmd("map_activate", wire, "a1", t.c))
        await _跑(t.rt, t.broker, settle_s=20.0)
        await t.拍(3)
        ev = _events(t, "map_activated")
        assert ev and ev[0]["data"]["confirmed"] == "load"
        assert _caps(t).tasks["running_config"]["confirms"] == "load"
    finally:
        await t.收()


async def test_running_config_带最近一趟巡检命令的任务修订_站点给了原样记_没给自己算(台):
    from types import SimpleNamespace

    from d1max_contract.digest import canonical_digest
    t, site, keeper = 台
    mission = {"mission": "night", "waypoints": []}
    m = SimpleNamespace(mission="night", map_version="2")
    t.rt._note_patrol(SimpleNamespace(task_id="p1", payload={"mission": mission,
                                                              "mission_rev": "abc123"}), m)
    assert t.rt._running_config()["last_patrol"] == {"task_id": "p1", "mission": "night",
                                                     "map_version": "2", "rev": "abc123"}
    t.rt._note_patrol(SimpleNamespace(task_id="p2", payload={"mission": mission}), m)
    assert t.rt._running_config()["last_patrol"]["rev"] == canonical_digest(mission)
