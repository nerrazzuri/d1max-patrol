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
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    t.stb.set("A", "gate", map_id="estate-1", map_version="7", x=1.0, y=0.0, yaw=0.0, default=True)
    assert t.stb.default("A")["name"] == "gate"
    assert sorted(p["name"] for p in t.stb.list("A")) == ["dock", "gate"]
    with pytest.raises(StandbyError):
        t.stb.set("ghost", "x", map_id="estate-1", map_version="7", x=0, y=0, yaw=0)
    with pytest.raises(StandbyError):
        t.stb.set("A", "bad name", map_id="estate-1", map_version="7", x=0, y=0, yaw=0)
    with pytest.raises(StandbyError):
        t.stb.set("A", "nan", map_id="estate-1", map_version="7", x=float("nan"), y=0, yaw=0)
    with pytest.raises(StandbyError):
        t.stb.set("A", "big", map_id="estate-1", map_version="7", x=10**400, y=0, yaw=0)
    t.stb.set("A", "gate", map_id="estate-1", map_version="7", x=2.0, y=0.0, yaw=0.0)
    assert t.stb.default("A")["name"] == "gate", "改点时没写 default,原来的默认标记保留"
    t.stb.remove("A", "gate")
    assert t.stb.default("A") is None and [p["name"] for p in t.stb.list("A")] == ["dock"]


async def test_任务结束自动回默认待命点_回程结束不再回(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
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
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
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
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=-6.0, y=0.0, yaw=0.0, default=True)
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
    evs = list(reversed(t.site.recent_events("A", 200)))
    pre = [e for e in evs if e["kind"] == "task_preempted"]
    assert pre and pre[0]["data"]["task_id"].startswith("standby-"), "被抢占的是回程"
    patrol = [c for c in t.site.commands("A") if c["kind"] == "patrol"][0]
    assert patrol["ack_result"] == "accepted"


async def test_人按了abort_狗停在原地_不自动回(站):
    """abort 是站点唯一的停止键:人按它是要狗停下,不是要它开去别处。"""
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0,
              default=True)
    r = await t.send(t.site.goto("A", target(3.0), 0.8, issued_by="alice", priority=MANUAL))
    await t.run(20)
    await t.send(t.site.abort("A", r["task_id"], issued_by="alice"))
    await t.run(100)
    assert len(_cmds(t, "goto")) == 1


async def test_任务失败_不自动回_哪怕狗此刻看起来就绪(站, monkeypatch):
    """失败可能是安全裁定(比如前面有人):狗的就绪标志不一定变,站点不能靠「没就绪」来拦。
    这里让 HAL 拒速度(导航 Failed → 航点跳过 → goto failed),狗一直是就绪的。"""
    from d1max_contract.hal import VelocityResult
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0,
              default=True)

    async def 拒(cmd):
        return VelocityResult(0.0, 0.0, clamped=False, rejected=True, reason="blocked")

    monkeypatch.setattr(t.dog, "set_velocity", 拒)
    await t.send(t.site.goto("A", target(3.0), 0.8, issued_by="alice", priority=MANUAL))
    await t.run(200)
    kinds = [e["kind"] for e in reversed(t.site.recent_events("A", 100))]
    assert "task_failed" in kinds, kinds
    assert t.site.dispatchable("A", "goto") == "", "狗看起来是就绪的"
    assert len(_cmds(t, "goto")) == 1


async def test_待命点的地图版本对不上就不回(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="6", x=0.0, y=0.0, yaw=0.0,
              default=True)
    from d1max_site.dispatcher import DispatchRefused
    with pytest.raises(DispatchRefused, match="版本"):
        await t.send(t.stb.return_to("A", issued_by="alice"))
    sub = t.site.feed.subscribe()
    await t.send(t.site.goto("A", target(0.4), 0.8, issued_by="alice", priority=MANUAL))
    await t.run(200)
    assert len(_cmds(t, "goto")) == 1
    got = []
    while (item := sub.get(0)) is not None:
        got.append(item["kind"])
    assert "standby_failed" in got, "回不去要让值守的人看见"


# ---------------------------------------------------- 巡检之后沿来路回(W00c6b)


def _pose(x, y):
    return {"position": {"x": x, "y": y}, "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}


_巡检 = {"mission": "L", "map_id": "estate-1", "policy": {},
         "waypoints": [{"name": "a", "pose": _pose(1.0, 0.0),
                        "actions": [{"type": "dwell", "seconds": 0.2}]},
                       {"name": "b", "pose": _pose(1.0, 1.0)}]}


async def test_巡检跑完_直线狗沿来路回待命点(站):
    """W00c6b:以前巡检跑完,站点自动派一条直线 goto 回待命点 —— 每趟巡检之后、没人值守、
    直线、不避障。直线的狗(能力里 ``goto.path == straight``)改成沿来路回:那一趟的航点倒序
    + 待命点。"""
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    assert t.site.clients["A"].capabilities.tasks["goto"]["path"] == "straight"
    await t.send(t.site.patrol("A", _巡检, issued_by="alice", priority=MANUAL))
    await t.run(400)
    patrols = _cmds(t, "patrol")
    assert len(patrols) == 2 and not _cmds(t, "goto"), "回程是一趟巡检,不是直线 goto"
    back = patrols[1]
    assert back["task_id"].startswith("standby-") and back["issued_by"] == "standby:auto"
    assert back["priority"] == STANDBY_RETURN
    wps = back["payload"]["mission"]["waypoints"]
    assert [(w["pose"]["position"]["x"], w["pose"]["position"]["y"]) for w in wps] == [
        (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)], "倒序 + 待命点"
    assert all("actions" not in w for w in wps), "回程不拍照、不停留"
    pol = back["payload"]["mission"]["policy"]
    assert pol["on_waypoint_failed"] == "skip"
    await t.run(400)
    assert len(_cmds(t, "patrol")) == 2, "回程结束不再回"
    o = await t.dog.odometry()
    assert abs(o.x) < 0.2 and abs(o.y) < 0.2


async def test_巡检跑完_读不到path按直线算(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    t.site.clients["A"].capabilities.tasks["goto"].pop("path")
    await t.send(t.site.patrol("A", _巡检, issued_by="alice", priority=MANUAL))
    await t.run(400)
    assert len(_cmds(t, "patrol")) == 2 and not _cmds(t, "goto")


async def test_巡检跑完_规划的狗照旧派goto回待命点(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = "planned"
    await t.send(t.site.patrol("A", _巡检, issued_by="alice", priority=MANUAL))
    await t.run(400)
    goto = _cmds(t, "goto")
    assert len(goto) == 1 and goto[0]["task_id"].startswith("standby-")


async def test_巡检跑完_取不到那一趟的任务定义_不回_推standby_failed(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    sub = t.site.feed.subscribe()
    await t.send(t.site.patrol("A", _巡检, issued_by="alice", priority=MANUAL))
    with t.db.tx() as c:
        c.execute("UPDATE commands SET payload='{}' WHERE kind='patrol'")
    await t.run(400)
    assert len(_cmds(t, "patrol")) == 1 and not _cmds(t, "goto"), "不回,也不退回直线"
    got = []
    while (item := sub.get(0)) is not None:
        got.append(item)
    failed = [i for i in got if i["kind"] == "standby_failed"]
    assert failed and "来路" in failed[-1]["reason"]


async def test_要人监护的狗没人监护_不自动回待命点_推standby_failed(站):
    """W00c6i:站点这一道关也管巡检/任务后的自动回待命点(回滚到旧版代理时狗自己不查)。"""
    t = 站
    t.stb.refusal = lambda rid: f"{rid} 要人现场监护"
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    sub = t.site.feed.subscribe()
    await t.send(t.site.goto("A", target(0.4), 0.8, issued_by="alice", priority=MANUAL))
    await t.run(200)
    assert len(_cmds(t, "goto")) == 1, "不自动回"
    got = []
    while (item := sub.get(0)) is not None:
        got.append(item)
    failed = [i for i in got if i["kind"] == "standby_failed"]
    assert failed and "监护" in failed[-1]["reason"]


async def test_狗拒收回待命点_推standby_failed(站, monkeypatch):
    """以前 ``_auto`` 只在出异常时推 ``standby_failed``;狗拒收(回执不是收下)是正常返回,没人知道。"""
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)

    async def 拒(*a, **k):
        return {"ack": {"result": "rejected", "reason": "unsupervised"}}
    monkeypatch.setattr(t.stb, "return_to", 拒)
    sub = t.site.feed.subscribe()
    await t.send(t.site.goto("A", target(0.4), 0.8, issued_by="alice", priority=MANUAL))
    await t.run(200)
    got = []
    while (item := sub.get(0)) is not None:
        got.append(item)
    failed = [i for i in got if i["kind"] == "standby_failed"]
    assert failed and "unsupervised" in failed[-1]["reason"]
