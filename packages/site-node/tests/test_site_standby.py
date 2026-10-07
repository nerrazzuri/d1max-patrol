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
    assert pol["on_waypoint_failed"] == "abort", "内审阻断 3:跳过一点就是一条没走过的直线"
    assert pol["on_battery_low"] == "continue", "内审阻断 2:剩下的路就是回家的路"
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


async def test_回程巡检继承原任务的超时与电量线_只改失败处置(站):
    """内审阻断 3:以前回程巡检的 policy 全是默认(每点 120 s),原任务给长腿的 400 s 丢了,长腿一超时
    就跳点、斜穿。"""
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    m = dict(_巡检, policy={"waypoint_timeout_s": 400.0, "battery_return_pct": 40.0,
                           "battery_abort_pct": 20.0, "on_loc_lost": "abort",
                           "on_control_lost": "abort", "loops": 2, "retention_days": 30,
                           "on_waypoint_failed": "skip", "waypoint_retry": 3})
    await t.send(t.site.patrol("A", m, issued_by="alice", priority=MANUAL))
    await t.run(800)
    patrols = _cmds(t, "patrol")
    assert len(patrols) == 2
    pol = patrols[1]["payload"]["mission"]["policy"]
    assert pol["waypoint_timeout_s"] == 400.0
    assert (pol["battery_return_pct"], pol["battery_abort_pct"]) == (40.0, 20.0)
    assert (pol["on_loc_lost"], pol["on_control_lost"]) == ("abort", "abort")
    assert pol["retention_days"] == 30
    assert pol["on_waypoint_failed"] == "abort" and pol["loops"] == 1
    assert pol["on_battery_low"] == "continue"


def _feed(sub) -> list[dict]:
    got = []
    while (item := sub.get(0)) is not None:
        got.append(item)
    return got


async def _跑完一趟巡检_先不设待命点(t, mission=None) -> str:
    """巡检跑完时还没有默认待命点 → 不自动回;返回那一趟的 task_id,好让测试自己改账再叫
    ``_auto``。"""
    await t.send(t.site.patrol("A", mission or _巡检, issued_by="alice", priority=MANUAL))
    await t.run(400)
    [row] = _cmds(t, "patrol")
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    return row["task_id"]


async def test_命令记录没了_直线狗不回_不退回直线goto(站):
    """内审应修 3:以前查不到行就落到直线 ``goto``。"""
    t = 站
    tid = await _跑完一趟巡检_先不设待命点(t)
    with t.db.tx() as c:
        c.execute("DELETE FROM commands WHERE task_id=?", (tid,))
    sub = t.site.feed.subscribe()
    await t.send(t.stb._auto("A", tid))
    assert not _cmds(t, "goto") and not _cmds(t, "patrol")
    failed = [i for i in _feed(sub) if i["kind"] == "standby_failed"]
    assert failed and "来路" in failed[-1]["reason"]


async def test_同一趟最新一条是abort_照样按巡检的来路回(站):
    """内审应修 3:abort 用的是巡检的 task_id;狗跑完时正好有人按停止,最新一条就是 ``abort``,以前
    查到的类型不是 ``patrol``,落到直线 ``goto``。"""
    t = 站
    tid = await _跑完一趟巡检_先不设待命点(t)
    with t.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at) VALUES ('late-abort', ?, 'A', 'abort', '{}', 'bob', ?)",
                  (tid, t.clock() + 10_000))
    await t.send(t.stb._auto("A", tid))
    assert not _cmds(t, "goto") and len(_cmds(t, "patrol")) == 2


async def test_点位事件对不上_不知道狗停在哪_不回(站):
    """内审阻断 1 的站点兜底:只有这一趟每个点都报了到,才当它是「跑完了、停在最后一个点」。"""
    t = 站
    tid = await _跑完一趟巡检_先不设待命点(t)
    with t.db.tx() as c:
        c.execute("DELETE FROM events WHERE rowid IN (SELECT rowid FROM events WHERE "
                  "kind='patrol_waypoint' ORDER BY seq DESC LIMIT 1)")
    sub = t.site.feed.subscribe()
    await t.send(t.stb._auto("A", tid))
    assert len(_cmds(t, "patrol")) == 1 and not _cmds(t, "goto")
    failed = [i for i in _feed(sub) if i["kind"] == "standby_failed"]
    assert failed and "1/2" in failed[-1]["reason"], failed


async def test_原巡检的地图版本跟待命点对不上_不回(站):
    """内审小问题:``_route_back`` 以前不核原巡检载荷里的地图;来路坐标是旧版本地图上的就不可信。"""
    t = 站
    tid = await _跑完一趟巡检_先不设待命点(t)
    with t.db.tx() as c:
        c.execute("UPDATE commands SET payload=json_set(payload, '$.map_version', '6') "
                  "WHERE task_id=?", (tid,))
    sub = t.site.feed.subscribe()
    await t.send(t.stb._auto("A", tid))
    assert len(_cmds(t, "patrol")) == 1 and not _cmds(t, "goto")
    failed = [i for i in _feed(sub) if i["kind"] == "standby_failed"]
    assert failed and "版本" in failed[-1]["reason"]


async def test_遥控放租之后_直线狗不自动回待命点_也不报(站):
    """内审应修 4:遥控放租报 ``task_done("teleop-N")``,以前站点照样派一条直线 goto —— 起点最随意
    的一种自动回家。直线的狗不回(人刚开完,人知道狗在哪);会规划的狗照旧回。"""
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    sub = t.site.feed.subscribe()
    await t.send(t.stb._auto("A", "teleop-7"))
    assert not _cmds(t, "goto") and not _cmds(t, "patrol")
    assert not [i for i in _feed(sub) if i["kind"] == "standby_failed"]
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = "planned"
    await t.send(t.stb._auto("A", "teleop-8"))
    assert len(_cmds(t, "goto")) == 1


async def test_待命点名带两个点_回程巡检照样成形(站):
    """内审小问题:``SAFE_ID`` 允许 ``a..b``,拼成航点名 ``待命点·a..b`` 被任务校验拒(点位名不许带
    ``..``),每次都推 ``standby_failed``。"""
    t = 站
    t.stb.set("A", "a..b", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0,
              default=True)
    await t.send(t.site.patrol("A", _巡检, issued_by="alice", priority=MANUAL))
    await t.run(400)
    assert len(_cmds(t, "patrol")) == 2


async def test_航点名跟待命点航点重名_回程照样成形(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    m = dict(_巡检, waypoints=[dict(_巡检["waypoints"][0], name="待命点·dock"),
                              _巡检["waypoints"][1]])
    await t.send(t.site.patrol("A", m, issued_by="alice", priority=MANUAL))
    await t.run(400)
    patrols = _cmds(t, "patrol")
    assert len(patrols) == 2
    names = [w["name"] for w in patrols[1]["payload"]["mission"]["waypoints"]]
    assert len(set(names)) == len(names) == 3


async def test_任务完成时钟差还不知道_回程隔一会儿再试_估出来就回(站, monkeypatch):
    """W09h 内审再议 1:代理重连时先补事件、后发状态;补上来的「任务完成」触发回程时站点还没估出
    钟差 —— 原来只试一次、推 standby_failed,狗停在最后一个巡检点。"""
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    real = t.site._skew
    waits = []

    class 不知道:
        _samples: dict = {}

        def note(self, *a):
            pass

        def skew_s(self, rid):
            return None

    async def 等(s):
        waits.append(s)
        if len(waits) == 2:
            t.site._skew = real                          # 两次之后估出来了

    t.stb._sleep = 等
    sub = t.site.feed.subscribe()
    await t.send(t.site.goto("A", target(0.8), 0.8, issued_by="alice", priority=MANUAL))
    t.site._skew = 不知道()
    await t.run(200)
    goto = _cmds(t, "goto")
    assert len(goto) == 2 and goto[1]["task_id"].startswith("standby-"), goto
    assert waits == [2.0, 2.0]
    assert not [i for i in _feed(sub) if i["kind"] == "standby_failed"]


async def test_钟差一直不知道_试够了才推standby_failed(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    waits = []

    class 不知道:
        _samples: dict = {}

        def note(self, *a):
            pass

        def skew_s(self, rid):
            return None

    async def 等(s):
        waits.append(s)
    t.stb._sleep = 等
    sub = t.site.feed.subscribe()
    await t.send(t.site.goto("A", target(0.8), 0.8, issued_by="alice", priority=MANUAL))
    t.site._skew = 不知道()
    await t.run(200)
    from d1max_site.standby import STANDBY_TRANSIENT_RETRIES
    assert len(waits) == STANDBY_TRANSIENT_RETRIES
    failed = [i for i in _feed(sub) if i["kind"] == "standby_failed"]
    assert len(failed) == 1 and "钟差还不知道" in failed[0]["reason"]


# ------------------------------------------------------------ W14


def _带待命点的排程(standby):
    return f"""\
timezone: Asia/Kuala_Lumpur
entries:
  - id: nightly
    mission: loop
    at: "22:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
    window_min: 30
    on_missed: skip
    standby: {standby}
"""


@pytest.mark.parametrize("path", ["straight", "planned"])
async def test_W14_排程写了回哪个待命点_跑完回那一个(站, tmp_path, path):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    t.stb.set("A", "gate", map_id="estate-1", map_version="7", x=0.3, y=0.6, yaw=0.0)
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = path
    import_bundle(t.db, 打包(tmp_path, 1, schedule=_带待命点的排程("gate")), imported_by="alice",
                  now_ms=t.clock())
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await t.send(s.tick())
    await t.run(400)
    back = [c for c in _cmds(t) if c["task_id"].startswith("standby-")]
    assert len(back) == 1, _cmds(t)
    if path == "planned":
        assert (back[0]["payload"]["target"]["x"], back[0]["payload"]["target"]["y"]) == (0.3, 0.6)
    else:
        last = back[0]["payload"]["mission"]["waypoints"][-1]
        assert last["name"] == "待命点·gate"


async def test_W14_排程写的待命点这台狗没有_回默认的(站, tmp_path):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = "planned"
    import_bundle(t.db, 打包(tmp_path, 1, schedule=_带待命点的排程("nope")), imported_by="alice",
                  now_ms=t.clock())
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await t.send(s.tick())
    await t.run(400)
    [back] = [c for c in _cmds(t, "goto") if c["task_id"].startswith("standby-")]
    assert (back["payload"]["target"]["x"], back["payload"]["target"]["y"]) == (0.0, 0.0)


async def test_W14_手动派的巡检照旧回默认的(站):
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    t.stb.set("A", "gate", map_id="estate-1", map_version="7", x=0.3, y=0.6, yaw=0.0)
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = "planned"
    await t.send(t.site.patrol("A", _巡检, issued_by="alice", priority=MANUAL))
    await t.run(400)
    [back] = _cmds(t, "goto")
    assert (back["payload"]["target"]["x"], back["payload"]["target"]["y"]) == (0.0, 0.0)


# ------------------------------------------------------------ W14 随机路线


def _乱序巡检(n=6):
    return {"mission": "R", "map_id": "estate-1", "policy": {"order": "shuffle"},
            "waypoints": [{"name": f"p{i}", "pose": _pose(0.2 * i, 0.0)} for i in range(n)]}


async def test_W14_随机路线_会规划的狗打乱_狗收到的是固定顺序(站):
    import random
    t = 站
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = "planned"
    t.site.rng = random.Random(7)
    orders = set()
    for _ in range(3):
        r = await t.send(t.site.patrol("A", _乱序巡检(), issued_by="alice", priority=MANUAL))
        assert r["ack"]["result"] in ("accepted", "rejected"), r
        m = _cmds(t, "patrol")[-1]["payload"]["mission"]
        assert sorted(w["name"] for w in m["waypoints"]) == [f"p{i}" for i in range(6)]
        assert "order" not in m["policy"], "狗收到的是固定顺序(老代理不受影响)"
        orders.add(tuple(w["name"] for w in m["waypoints"]))
        await t.run(20)
        cur = t.site.busy("A")
        if cur:
            await t.send(t.site.abort("A", cur, issued_by="alice"))
            await t.run(40)
    assert len(orders) > 1, "每一趟打乱一次"


async def test_W14_随机路线_走直线的狗不打乱(站):
    import random
    t = 站
    assert t.site.clients["A"].capabilities.tasks["goto"]["path"] == "straight"
    t.site.rng = random.Random(7)
    await t.send(t.site.patrol("A", _乱序巡检(), issued_by="alice", priority=MANUAL))
    m = _cmds(t, "patrol")[-1]["payload"]["mission"]
    assert [w["name"] for w in m["waypoints"]] == [f"p{i}" for i in range(6)]
    assert "order" not in m["policy"]


@pytest.mark.parametrize("改成", ["garage", None])
async def test_W14外审_跑着的时候换了任务包_照派单时的排程回(站, tmp_path, 改成):
    """新包把同名排程改成回别的点(``garage``),或者干脆删了那条(``None``):这一趟照旧回 gate。"""
    t = 站
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    t.stb.set("A", "gate", map_id="estate-1", map_version="7", x=0.3, y=0.6, yaw=0.0)
    t.stb.set("A", "garage", map_id="estate-1", map_version="7", x=-0.3, y=0.0, yaw=0.0)
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = "planned"
    import_bundle(t.db, 打包(tmp_path, 1, schedule=_带待命点的排程("gate")), imported_by="alice",
                  now_ms=t.clock())
    s = SiteScheduler(t.db, t.site, now_ms=t.clock)
    t.clock.ms = 毫秒(22, 0, 30)
    await t.run(2)
    await t.send(s.tick())
    assert s.runs("nightly")[0]["outcome"] == "started"
    await t.run(3)
    新排程 = (_带待命点的排程(改成) if 改成 else
            _带待命点的排程("x").replace("id: nightly", "id: other"))
    import_bundle(t.db, 打包(tmp_path, 2, schedule=新排程), imported_by="alice", now_ms=t.clock())
    await t.run(400)
    [back] = [c for c in _cmds(t, "goto") if c["task_id"].startswith("standby-")]
    assert (back["payload"]["target"]["x"], back["payload"]["target"]["y"]) == (0.3, 0.6)


async def test_W14外审_站点重启之后晚到的结束事件_照派单时的目标(站, tmp_path):
    t = 站
    t.stb.set("A", "gate", map_id="estate-1", map_version="7", x=0.3, y=0.6, yaw=0.0)
    with t.db.tx() as c:
        c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, task_id, "
                  "note, decided_at, standby_name) VALUES ('nightly', 1, 'started', 'A', "
                  "'sched-abc', '', 1, 'gate')")
        c.execute("INSERT INTO schedule_runs(entry_id, scheduled_ms, outcome, robot_id, task_id, "
                  "note, decided_at) VALUES ('nightly', 2, 'started', 'A', 'sched-old', '', 1)")
    重启后 = StandbyManager(t.db, t.site, now_ms=t.clock)          # 没有任务包也认
    assert 重启后._target("A", "sched-abc") == "gate"
    assert 重启后._target("A", "sched-old") is None, "老记录没写:回默认的"
    assert 重启后._target("A", "task-1") is None


async def _雷暴撤了一半的巡检(t):
    """巡检跑到一半被撤(雷暴撤巡检那样):回程意图落库、撤、等停稳。回 task_id。"""
    from d1max_site.weather import WeatherDesk
    t.stb.set("A", "dock", map_id="estate-1", map_version="7", x=0.0, y=0.0, yaw=0.0, default=True)
    r = await t.send(t.site.patrol("A", _巡检, issued_by="schedule:x", priority=MANUAL))
    tid = r["task_id"]
    await t.run(15)
    with t.db.tx() as c:
        c.execute("INSERT INTO weather_returns(robot_id, task_id, created_ms) VALUES ('A',?,0)",
                  (tid,))
    await t.send(t.site.abort("A", tid, issued_by="weather"))
    await t.run(60)
    w = WeatherDesk(t.db, t.site, now_ms=t.clock, latlon=None)
    w.standby = t.stb
    return tid, w


async def test_W29复查_雷暴撤了一半_直线狗来路不明_不回_原地等_报没回待命点(站):
    t = 站
    tid, w = await _雷暴撤了一半的巡检(t)
    sub = t.site.feed.subscribe()
    await t.send(w.tick())
    assert not _cmds(t, "goto") and len(_cmds(t, "patrol")) == 1, "不派直线回程,也不派回程巡检"
    failed = [i for i in _feed(sub) if i["kind"] == "standby_failed"]
    assert failed and "雷暴" in failed[-1]["reason"] and "原地等" in failed[-1]["reason"]
    assert not t.db.query("SELECT 1 FROM weather_returns"), "回不得:意图作废(已经告警叫人)"


async def test_W29复查_雷暴撤了一半_会规划的狗规划回去(站):
    t = 站
    t.site.clients["A"].capabilities.tasks["goto"]["path"] = "planned"
    tid, w = await _雷暴撤了一半的巡检(t)
    await t.send(w.tick())
    gotos = _cmds(t, "goto")
    assert len(gotos) == 1 and gotos[0]["task_id"].startswith("standby-")
    assert gotos[0]["issued_by"] == "weather:storm"
    assert not t.db.query("SELECT 1 FROM weather_returns")
