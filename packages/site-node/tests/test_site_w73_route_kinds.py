"""W73 第一期:路线分两类 —— ``patrol`` 巡逻路线、``guard`` 防区路线(只给布防值守用)。

用户 2026-11-03:「防区路线只能是机器狗在布防的时候用……路径不能作为巡逻路径使用」。所以:
防区路线不进派遣器、排程执行器、手动巡逻读的那份任务(``active_bundle``);排程不许挂防区路线;
已经有排程的巡逻路线不许改成防区路线;任务包导入不许盖掉同名的防区路线;老库里的路线都是巡逻路线。
"""

# ruff: noqa: F811 —— 夹具「台」从别的测试文件导入,测试参数同名是 pytest 的用法
from __future__ import annotations

import pytest
from test_site_routes import (
    _接规划,
    body,
    pt,
    台,  # noqa: F401 —— 夹具
)
from test_site_schedule import 任务

from d1max_contract.mission import parse_mission
from d1max_contract.schedule import parse_schedule
from d1max_site.catalog import CatalogError, active_bundle
from d1max_site.routes import MAX_SEGMENT_CHECKS, MAX_WAYPOINTS, RouteError


def _时区(台):
    台.store.set_timezone("Asia/Kuala_Lumpur", by="a")


def test_默认是巡逻路线_可以存成防区路线_读回带类别(台):
    s = 台.store
    assert s.put_route("night", body(), by="a")["kind"] == "patrol"
    g = s.put_route("east", body(kind="guard"), by="a")
    assert g["kind"] == "guard"
    assert {r["id"]: r["kind"] for r in s.view()["routes"]} == {"east": "guard", "night": "patrol"}
    assert [r["id"] for r in s.view(kind="guard")["routes"]] == ["east"]
    assert [r["id"] for r in s.view(kind="patrol")["routes"]] == ["night"]


def test_改的时候不带类别_保留原来的(台):
    s = 台.store
    s.put_route("east", body(kind="guard"), by="a")
    assert s.put_route("east", body(pts=[pt("门", 1.0, 2.0)], base=1), by="b")["kind"] == "guard"


def test_类别只认两种(台):
    with pytest.raises(RouteError, match="kind"):
        台.store.put_route("x", body(kind="random"), by="a")


def test_防区路线不进巡逻那份任务(台):
    s = 台.store
    s.put_route("night", body(), by="a")
    s.put_route("east", body(kind="guard"), by="a")
    act = active_bundle(台.db)
    assert set(act.missions) == {"night"}
    s.delete_route("night", base_revision=1, by="a")
    assert active_bundle(台.db) is None, "只剩防区路线:巡逻那份是空的"


def test_排程不许挂防区路线(台):
    s = 台.store
    _时区(台)
    s.put_route("east", body(kind="guard"), by="a")
    with pytest.raises(RouteError, match="防区路线"):
        s.put_schedule("e1", {"mission": "east", "at": "08:00", "days": ["mon"], "window_min": 30,
                          "on_missed": "skip", "base_revision": 0}, by="a")


def test_有排程的巡逻路线不许改成防区路线(台):
    s = 台.store
    _时区(台)
    s.put_route("night", body(), by="a")
    s.put_schedule("e1", {"mission": "night", "at": "08:00", "days": ["mon"], "window_min": 30,
                          "on_missed": "skip", "base_revision": 0}, by="a")
    with pytest.raises(RouteError, match="排程"):
        s.put_route("night", body(kind="guard", base=1), by="a")
    assert s.get("night")["kind"] == "patrol"


def test_任务包导入不许盖掉同名的防区路线(台):
    from d1max_site.routes import import_into_store
    台.store.put_route("east", body(kind="guard"), by="a")
    def m(name):
        return parse_mission({**任务, "mission": name})
    sch = parse_schedule({"timezone": "Asia/Kuala_Lumpur", "entries": [
        {"id": "e1", "mission": "east", "at": "08:00", "days": ["mon"], "window_min": 30,
         "on_missed": "skip"}]})
    with 台.db.tx() as tx, pytest.raises(CatalogError, match="防区路线"):
        import_into_store(tx, {"east": m("east")}, None, by="a", now=1, source="bundle")
    with 台.db.tx() as tx, pytest.raises(CatalogError, match="防区路线"):
        import_into_store(tx, {"other": m("other")}, sch, by="a", now=1, source="bundle")
    assert 台.store.get("east")["kind"] == "guard"


def test_导出任务包只带巡逻路线(台):
    s = 台.store
    s.put_route("night", body(), by="a")
    s.put_route("east", body(kind="guard"), by="a")
    got = s.export_bundle(台.tmp / "out", by="a")
    assert got["routes"] == ["night"]


def test_逐段查_每一段都查_不再只查前100段(台):
    assert MAX_SEGMENT_CHECKS >= MAX_WAYPOINTS - 1
    _接规划(台, door=0.0)                                 # 门关死
    pts = [pt(f"左{i}", 1.0 + 0.01 * (i % 2), 2.0) for i in range(120)] + [pt("右", 6.0, 2.0)]
    with pytest.raises(RouteError) as e:
        台.store.put_route("r", body("d0.0", pts), by="a")
    assert [x.get("segment") for x in e.value.errors] == [[119, 120]]


def test_老库的路线_升上来都是巡逻路线(台):
    台.store.put_route("night", body(), by="a")
    with 台.db.tx() as c:
        c.execute("UPDATE routes SET kind='patrol'")
    assert 台.store.get("night")["kind"] == "patrol"
