"""R1(地图与导航能力对账 2026-10-14,已核实的漏洞):巡检任务绑地图版本。

以前任务只带 ``map_id``,站点派单时把**狗自己**加载的版本填进命令,狗那头 ``(map_id, version) !=
loaded_map`` 的核对永远通过;排程也只比 ``map_id``。同一张图重建出第 8 版以后,第 7 版上量的点照派,
只剩「离建图路线 5 m 以内、落在能走的格子上」挡着 —— 每次建图原点不同,老点可能刚好落在新图上一个
能走、但不对的地方。台子同 ``test_site_dispatcher.py``(狗加载 estate-1 第 7 版)。
"""

from __future__ import annotations

import json

import pytest
from test_site_dispatcher import MAP, 台子
from test_site_schedule import 任务, 打包, 排程, 毫秒

from d1max_contract.bundle_format import (
    MISSIONS_DIR,
    SCHEDULE_NAME,
    BundleManifest,
    bundle_sha256,
    write_manifest,
)
from d1max_site.catalog import CatalogError, active_bundle, import_bundle
from d1max_site.dispatcher import DispatchRefused
from d1max_site.scheduler import SiteScheduler


def _pt(x: float) -> dict:
    return {"position": {"x": x, "y": 0.0}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}


def _任务(**改) -> dict:
    m = {"mission": "m", "map_id": MAP[0], "map_version": MAP[1], "policy": {},
         "waypoints": [{"name": "门口", "pose": _pt(0.5)}]}
    m.update(改)
    return {k: v for k, v in m.items() if v is not None}


@pytest.fixture
async def 台(tmp_path):
    t = 台子(tmp_path)
    await t.start()
    yield t
    await t.close()


def _patrols(t) -> list[dict]:
    return [c for c in t.site.commands("A") if c["kind"] == "patrol"]


# ------------------------------------------------------------ 派遣器


async def test_任务绑的版本跟狗加载的对不上_不派_不记账(台):
    """第 6 版上量的点,狗加载的是第 7 版:以前站点照派(命令里填狗的 7,狗核对通过)。"""
    t = 台
    await t.run(3)
    with pytest.raises(DispatchRefused) as ei:
        await t.site.patrol("A", _任务(map_version="6"), issued_by="alice")
    assert "第 6 版" in str(ei.value) and "第 7 版" in str(ei.value)
    assert not _patrols(t), "拒了不记账、不发"


async def test_没绑版本的任务_不派_不拿狗的版本去补(台):
    t = 台
    await t.run(3)
    with pytest.raises(DispatchRefused, match="没写 map_version") as ei:
        await t.site.patrol("A", _任务(map_version=None), issued_by="alice")
    assert "第 7 版" not in str(ei.value), "说的是没绑版本,不是拿机器人的第 7 版去比"
    assert not _patrols(t)


async def test_版本对得上照派_命令里是任务自己的版本_狗收下(台):
    t = 台
    await t.run(3)
    r = await t.send(t.site.patrol("A", _任务(), issued_by="alice"))
    assert r["ack"]["result"] == "accepted", r
    [cmd] = _patrols(t)
    assert cmd["payload"]["map_version"] == "7"
    assert cmd["payload"]["mission"]["map_version"] == "7", "任务里也带着,机器人两样都核"


async def test_站点以为狗是第7版_狗其实换成了第8版_狗拒收(台):
    """命令里是任务的版本,狗那头的核对才有意义:站点手里的能力报文旧了(狗刚换图)也挡得住。"""
    t = 台
    await t.run(3)
    t.agent.loaded_map = ("estate-1", "8")
    t.agent.processor.loaded_map = ("estate-1", "8")
    r = await t.send(t.site.patrol("A", _任务(), issued_by="alice"))
    assert r["ack"]["result"] == "rejected" and r["ack"]["reason"] == "map_mismatch", r


async def test_查图外的点按任务绑的那一版查(台):
    t = 台
    asked = []

    class 图库:
        def reach_problem(self, map_id, version, points):
            asked.append((map_id, version))
            return ""
    t.site.maps = 图库()
    await t.run(3)
    await t.send(t.site.patrol("A", _任务(), issued_by="alice"))
    assert asked == [("estate-1", "7")]


# ------------------------------------------------------------ 导入任务包


def _老包(tmp_path, mission: dict, version: int = 1):
    """schema 1 的老包:任务没绑版本(老打包器打的)。指纹照算,过得了校验。"""
    d = tmp_path / f"estate-kl-{version}"
    (d / MISSIONS_DIR).mkdir(parents=True)
    (d / MISSIONS_DIR / "loop.json").write_text(json.dumps(mission), encoding="utf-8")
    (d / SCHEDULE_NAME).write_text(排程, encoding="utf-8")
    write_manifest(d, BundleManifest("estate-kl", version, 1, bundle_sha256(d),
                                     "2026-09-07T14:03:00+08:00"))
    return d


def test_老包_任务没绑版本_不导入_报错列出站点上这张图有哪几版(tmp_path):
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    with db.tx() as c:
        for v, at in (("7", 1), ("8", 2)):
            c.execute("INSERT INTO maps(map_id, version, source, created_ms, files) "
                      "VALUES ('estate-1', ?, 'import', ?, '[]')", (v, at))
    老 = {k: v for k, v in 任务.items() if k != "map_version"}
    with pytest.raises(CatalogError) as ei:
        import_bundle(db, _老包(tmp_path, 老), imported_by="alice", now_ms=1)
    msg = str(ei.value)
    assert "loop.json" in msg and "map_version" in msg and "8、7" in msg, msg
    assert active_bundle(db) is None, "没导入"
    db.close()


def test_新包绑了版本_导入后任务带着版本(tmp_path):
    from d1max_site.db import SiteDB
    db = SiteDB(tmp_path / "s.db")
    import_bundle(db, 打包(tmp_path, 1), imported_by="alice", now_ms=1)
    assert active_bundle(db).missions["loop"].map_version == "7"
    db.close()


# ------------------------------------------------------------ 排程


@pytest.fixture
async def 站(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)
    await t.start()
    t.heard = []
    t.sched = SiteScheduler(t.db, t.site, now_ms=t.clock,
                            on_outcome=lambda *a: t.heard.append(a))
    yield t
    await t.close()


async def _到点(t) -> None:
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await t.send(t.sched.tick())


async def test_排程_任务绑的版本跟狗的不一样_不派_记no_robot写明版本_过宽限期报(站, tmp_path):
    t = 站
    import_bundle(t.db, 打包(tmp_path, 1, mission=dict(任务, map_version="6")),
                  imported_by="alice", now_ms=t.clock())
    await _到点(t)
    [run] = t.sched.runs("nightly")
    assert run["outcome"] == "no_robot" and "第 6 版" in run["note"] and "第 7 版" in run["note"]
    assert not _patrols(t)
    assert t.heard == [], "跟图不对一样:宽限期里先不说"
    t.clock.ms = 毫秒(22, 6)
    await t.send(t.sched.tick())
    assert [(e, o) for e, _, o, _ in t.heard] == [("nightly", "no_robot")]


async def test_排程_版本对得上照派(站, tmp_path):
    t = 站
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    await _到点(t)
    assert [r["outcome"] for r in t.sched.runs("nightly")] == ["started"]
    [cmd] = _patrols(t)
    assert cmd["payload"]["map_version"] == "7"


async def test_排程_升级前导入的老任务没绑版本_不派_当场报(站, tmp_path):
    """站点升级之前就导入的包:库里的任务没有 map_version。读得出来,不派,这一轮当场报 P1。"""
    t = 站
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    老 = {k: v for k, v in 任务.items() if k != "map_version"}
    with t.db.tx() as c:
        c.execute("UPDATE routes SET definition=?, map_version=''", (json.dumps(老),))
    assert active_bundle(t.db).missions["loop"].map_version == "", "老行读得出来,算没绑"
    await _到点(t)
    [run] = t.sched.runs("nightly")
    assert run["outcome"] == "dispatch_failed" and "map_version" in run["note"]
    assert not _patrols(t)
    assert [(e, o) for e, _, o, _ in t.heard] == [("nightly", "dispatch_failed")], "当场说"
    t.clock.ms = 毫秒(22, 1)
    await t.send(t.sched.tick())
    assert len(t.heard) == 1, "一轮只说一次"


async def test_排程_两台狗一台换了版本_挑对得上的那台(站, tmp_path):
    import dataclasses
    t = 站
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    t.reg.enroll("B", fingerprint="sha256:b", issued_at=t.clock.ms - 1,
                 expires_at=t.clock.ms + 10**10)
    await t.site.add_robot("B")
    t.site.clients["B"].status = t.site.clients["A"].status
    caps = t.site.clients["A"].capabilities
    tasks = dict(caps.tasks)
    tasks["patrol"] = dict(tasks["patrol"], map_version="8")
    t.site.clients["B"].capabilities = dataclasses.replace(caps, tasks=tasks)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    t.site.clients["B"].status_live_at = t.clock()
    from d1max_site.scheduler import active_bundle as act_of
    act = act_of(t.db)
    ok, _, why, _ = t.sched._candidates(act, act.schedule.entries[0], {})
    assert ok == ["A"] and "B 加载的是第 8 版" in why, why
