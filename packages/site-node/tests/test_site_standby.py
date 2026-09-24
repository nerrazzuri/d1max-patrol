"""抢占与待命点(W00c2b)。台子同 ``test_site_dispatcher.py``;
排程的包与钟同 ``test_site_schedule.py``。"""

from __future__ import annotations

import pytest
from test_site_dispatcher import target, 台子
from test_site_schedule import 打包, 毫秒

from d1max_site.catalog import import_bundle
from d1max_site.priorities import MANUAL, STANDBY_RETURN, schedule_priority
from d1max_site.scheduler import SiteScheduler
from d1max_site.standby import StandbyError, StandbyManager


@pytest.fixture
async def 站(tmp_path):
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)
    await t.start()
    t.stb = StandbyManager(t.db, t.site, now_ms=t.clock)
    yield t
    await t.close()


def _cmds(t, kind=None):
    return [c for c in reversed(t.site.commands("A", 100)) if kind is None or c["kind"] == kind]


def test_优先级表():
    assert STANDBY_RETURN < schedule_priority(0) <= schedule_priority(1000) < MANUAL
    assert schedule_priority(-5) == schedule_priority(0) == 10 and schedule_priority(1000) == 49


async def test_登记待命点_默认只有一个_不合规矩的拒(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", x=0.0, y=0.0, yaw=0.0, default=True)
    t.stb.set("A", "gate", map_id="estate-1", x=1.0, y=0.0, yaw=0.0, default=True)
    assert t.stb.default("A")["name"] == "gate"
    assert sorted(p["name"] for p in t.stb.list("A")) == ["dock", "gate"]
    with pytest.raises(StandbyError):
        t.stb.set("ghost", "x", map_id="estate-1", x=0, y=0, yaw=0)
    with pytest.raises(StandbyError):
        t.stb.set("A", "bad name", map_id="estate-1", x=0, y=0, yaw=0)
    with pytest.raises(StandbyError):
        t.stb.set("A", "nan", map_id="estate-1", x=float("nan"), y=0, yaw=0)


async def test_任务结束自动回默认待命点_回程结束不再回(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", x=0.0, y=0.0, yaw=0.0, default=True)
    await t.send(t.site.goto("A", target(0.8), 0.8, issued_by="alice", priority=MANUAL))
    await t.run(200)
    goto = _cmds(t, "goto")
    assert len(goto) == 2 and goto[1]["task_id"].startswith("standby-")
    assert goto[1]["issued_by"] == "standby:auto"
    await t.run(300)
    assert len(_cmds(t, "goto")) == 2, "回程结束不再回"
    o = await t.dog.odometry()
    assert abs(o.x) < 0.2 and abs(o.y) < 0.2


async def test_没有默认待命点就不回(站):
    t = 站
    await t.send(t.site.goto("A", target(0.5), 0.8, issued_by="alice", priority=MANUAL))
    await t.run(200)
    assert len(_cmds(t, "goto")) == 1


async def test_巡检被手动派单抢占_账记preempted_派单完了回待命点(站, tmp_path):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", x=0.0, y=0.0, yaw=0.0, default=True)
    远 = {"mission": "loop", "map_id": "estate-1", "policy": {}, "waypoints": [
        {"name": "far", "pose": {"position": {"x": 6.0, "y": 0.0},
                                 "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}]}
    import_bundle(t.db, 打包(tmp_path, 1, mission=远), imported_by="alice", now_ms=t.clock())
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await t.send(s.tick())
    assert _cmds(t, "patrol")[0]["payload"] and s.runs("nightly")[0]["outcome"] == "started"
    await t.run(30)
    await t.send(t.site.goto("A", target(0.5, 0.5), 0.8, issued_by="bob", priority=MANUAL))
    await t.run(300)
    assert s.runs("nightly")[0]["result"] == "preempted"
    goto = _cmds(t, "goto")
    assert [g["task_id"].startswith("standby-") for g in goto] == [False, True], goto


async def test_回待命点途中排程到点_照派并抢占回程(站, tmp_path):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", x=-6.0, y=0.0, yaw=0.0, default=True)
    import_bundle(t.db, 打包(tmp_path, 1), imported_by="alice", now_ms=t.clock())
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    await t.send(t.stb.return_to("A", issued_by="alice"))
    await t.run(20)
    assert t.site.busy("A") is None, "回待命点不算忙"
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await t.send(s.tick())
    assert s.runs("nightly")[0]["outcome"] == "started"
    await t.run(100)
    events = [e["kind"] for e in reversed(t.site.recent_events("A", 200))]
    assert "task_preempted" in events
