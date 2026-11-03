"""W73 第二期:布防值守怎么跑(设计 §5.2–§5.6)。派遣器 + 真代理 + 仿真狗(走直线,1 m/s)。

一条防区路线 A(2,0) B(4,0) 在东区(x 0–5),C(7,0) D(9,0) 在西区(x 5–10)。东区值守位 (1,1),西区 (6,1)。
布防着 → 一圈圈跑;两圈之间有空档去值守位站着盯;留守撤掉的防区跳过;全撤了收队;被事件抢走,回来从最久
没看的防区接着跑、这一圈看过的不重复;人中止歇 10 分钟;太久没看报 P2、看到了自动解决;站点重启接着办。
"""

from __future__ import annotations

import json

import pytest
from test_site_dispatcher import MAP, target, 台子

from d1max_site.areas import AreaBook
from d1max_site.guard_duty import GuardDuty, PlanError
from d1max_site.modes import ArmingDesk
from d1max_site.routes import RouteError, RouteStore

ROUTE = [{"name": n, "x": x, "y": 0.0, "yaw": 0.0}
         for n, x in (("A", 2.0), ("B", 4.0), ("C", 7.0), ("D", 9.0))]
EAST = [[0.0, -2.0], [5.0, -2.0], [5.0, 2.0], [0.0, 2.0]]
WEST = [[5.0, -2.0], [10.0, -2.0], [10.0, 2.0], [5.0, 2.0]]
POSTS = {"东区": {"view": {"x": 1.0, "y": 0.0}, "watch": {"x": 1.0, "y": 1.0, "yaw": 0.0}},
         "西区": {"view": {"x": 6.0, "y": 0.0}, "watch": {"x": 6.0, "y": 1.0, "yaw": 0.0}}}


class 告警台:
    def __init__(self):
        self.open: list[dict] = []
        self.done: list[dict] = []

    def raise_alert(self, **kw):
        self.open.append(kw)

    def has_open(self, robot, kind):
        return any(a["robot"] == robot and a["kind"] == kind for a in self.open)

    def resolve_all(self, robot, kind, *, who):
        hit = [a for a in self.open if a["robot"] == robot and a["kind"] == kind]
        self.open = [a for a in self.open if a not in hit]
        self.done += hit
        return len(hit)

    def resolve_except(self, kind, prefix, keep, *, who):
        hit = [a for a in self.open if a["kind"] == kind and a["robot"].startswith(prefix)
               and a["robot"][len(prefix):] not in keep]
        self.open = [a for a in self.open if a not in hit]
        self.done += hit
        return len(hit)


def _值守(t):
    return GuardDuty(t.db, t.site, routes=t.store, areas=t.areas, arming=t.arming,
                     posts=lambda m, v: POSTS, now_ms=t.clock)


@pytest.fixture
async def 站(tmp_path):
    t = 台子(tmp_path)
    await t.start()
    t.store = RouteStore(t.db, now_ms=t.clock)
    t.store.put_route("night-guard", {"map_id": MAP[0], "map_version": MAP[1], "kind": "guard",
                                      "waypoints": ROUTE, "base_revision": 0}, by="alice")
    t.areas = AreaBook(t.db, now_ms=t.clock)
    t.areas.set(MAP[0], MAP[1], "东区", EAST, by="alice")
    t.areas.set(MAP[0], MAP[1], "西区", WEST, by="alice")
    t.arming = ArmingDesk(t.db, now_ms=t.clock)
    t.duty = _值守(t)
    t.duty.alerts = 告警台()
    yield t
    await t.close()


def _方案(t, *, robot="A", gap_min=0, shuffle=False, limits=None):
    return t.duty.put_plan({"entries": [{"route": "night-guard", "robot": robot, "gap_min": gap_min,
                                         "shuffle": shuffle}],
                            "limits": limits or {}, "base_revision": t.duty.plan()["revision"]},
                           by="alice")


async def _跑(t, secs, *, duty=None):
    d = duty or t.duty
    for i in range(int(secs * 10)):
        await t.run(1)
        if i % 10 == 0:
            await t.send(d.tick())


def _patrols(t):
    return [c for c in reversed(t.site.commands("A", 500)) if c["kind"] == "patrol"]


def _gotos(t, prefix):
    return [c for c in reversed(t.site.commands("A", 500))
            if c["kind"] == "goto" and c["task_id"].startswith(prefix)]


def _names(cmd):
    p = cmd["payload"]
    p = json.loads(p) if isinstance(p, str) else p
    return [w["name"] for w in p["mission"]["waypoints"]]


def _seen(t):
    return {r["zone"]: r["last_ms"] for r in t.db.query("SELECT zone, last_ms FROM zone_seen")}


def _log(t, kind=None):
    rows = t.db.query("SELECT kind, zone, point, detail FROM guard_log ORDER BY id")
    return [dict(r) for r in rows if kind is None or r["kind"] == kind]


async def test_没有方案什么都不派(站):
    await _跑(站, 5)
    assert _patrols(站) == []
    assert 站.duty.status()["duty"] is None


async def test_布防了_一圈圈跑_每个点记到防区上(站):
    t = 站
    _方案(t)
    await _跑(t, 45)
    ps = _patrols(t)
    assert len(ps) >= 2, "跑完一圈接着下一圈"
    assert all(p["task_id"].startswith("guard-") and p["priority"] == 55 for p in ps)
    assert _names(ps[0]) == ["A", "B", "C", "D"]
    assert set(_seen(t)) == {"东区", "西区"}
    pts = _log(t, "point")
    assert [(p["zone"], p["point"]) for p in pts[:4]] == [("东区", "A"), ("东区", "B"),
                                                          ("西区", "C"), ("西区", "D")]
    assert _log(t, "lap_done")
    st = t.duty.status()
    assert st["duty"] is not None and st["runs"][0]["robot_id"] == "A"
    assert {z["zone"] for z in st["zones"]} == {"东区", "西区"}


async def test_任意一台_挑在线的那台(站):
    t = 站
    _方案(t, robot="")
    await _跑(t, 3)
    assert _patrols(t) and t.duty.status()["runs"][0]["robot_id"] == "A"


async def test_第二圈从最久没看的防区开始(站):
    t = 站
    _方案(t)
    await _跑(t, 45)
    second = _patrols(t)[1]
    assert _names(second)[0] == "A", "东区比西区先看的:从东区第一个点接着"


async def test_两圈之间有空档_去值守位站着盯_到点再跑(站):
    t = 站
    _方案(t, gap_min=1)
    await _跑(t, 20)                                    # 一圈约 18 s,空档约 40 s
    for _ in range(40):
        if t.duty.watching("A"):
            break
        await _跑(t, 0.5)
    [w] = _gotos(t, "guard-watch-")
    assert w["priority"] == 52
    assert abs(t.dog.x - 1.0) < 0.3 and abs(t.dog.y - 1.0) < 0.3, "去了东区的值守位(这一圈最早看的)"
    assert t.duty.watching("A") == "东区"
    assert len(_patrols(t)) == 1
    before = _seen(t)["东区"]
    await _跑(t, 5)
    assert _seen(t)["东区"] > before, "站在值守位上就算看着这个防区"
    await _跑(t, 35)
    assert len(_patrols(t)) == 2, "到点下一圈"
    assert t.duty.watching("A") is None


async def test_留守撤掉一个防区_下一圈跳过_全撤了收队(站):
    t = 站
    _方案(t)
    t.arming.set_zone_home("西区", False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 3)
    assert _names(_patrols(t)[0]) == ["A", "B"], "西区撤防:那两个点不去"
    t.arming.set_zone_home("东区", False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 3)
    aborts = [c for c in t.site.commands("A", 500) if c["kind"] == "abort"]
    assert aborts, "整条路线都撤防了:这一趟停下(回待命点交给待命点管理器)"
    assert t.duty.status()["runs"] == [] and t.duty.status()["duty"] is None
    assert _log(t, "duty_end")
    assert not t.duty.holds("A", _patrols(t)[0]["task_id"]), "收队了:放给待命点管理器派回程"


async def test_值守中的任务不自动回待命点(站):
    t = 站
    _方案(t)
    await _跑(t, 3)
    tid = _patrols(t)[0]["task_id"]
    assert t.duty.holds("A", tid)
    assert not t.duty.holds("A", "incident-x")


async def test_被事件抢走_回来接着跑最久没看的_这一圈看过的不重复(站):
    t = 站
    _方案(t)
    for _ in range(200):
        await _跑(t, 0.5)
        if "东区" in _seen(t) and t.dog.x > 4.3:
            break
    assert "西区" not in _seen(t)
    await t.send(t.site.goto("A", target(3.0), 0.8, issued_by="incident", priority=80,
                             task_id="incident-x"))
    await _跑(t, 15)
    assert _log(t, "interrupted")
    ps = _patrols(t)
    assert _names(ps[1]) == ["C", "D"], "东区这一圈看过了:只补西区"
    await _跑(t, 30)
    assert _names(_patrols(t)[2]) == ["A", "B", "C", "D"], "补完了照常一整圈"


async def test_被人中止_歇10分钟(站):
    t = 站
    _方案(t)
    await _跑(t, 3)
    tid = _patrols(t)[0]["task_id"]
    await t.send(t.site.abort("A", tid, issued_by="gina"))
    await _跑(t, 20)
    assert len(_patrols(t)) == 1, "人中止的:先不派"
    assert t.duty.status()["runs"][0]["state"] == "paused"
    t.clock.advance(600)
    await _跑(t, 5)
    assert len(_patrols(t)) == 2


async def test_太久没看_报防区没巡到_看到了自动解决(站):
    t = 站
    _方案(t, robot="B", limits={"东区": 1, "西区": 1})          # B 不存在:没人去看
    await _跑(t, 2)
    t.clock.advance(65)
    await _跑(t, 2)
    alerts = {a["robot"]: a for a in t.duty.alerts.open if a["kind"] == "zone_unwatched"}
    assert set(alerts) == {"zone:东区", "zone:西区"}
    assert "B" in alerts["zone:东区"]["detail"]
    n = len(t.duty.alerts.open)
    await _跑(t, 2)
    assert len(t.duty.alerts.open) == n, "挂着的不重复报"
    _方案(t, robot="A", limits={"东区": 1, "西区": 1})
    await _跑(t, 20)
    assert not [a for a in t.duty.alerts.open if a["kind"] == "zone_unwatched"], "看到了自动解决"


async def test_电量低_不开新的一圈(站):
    t = 站
    t.dog.inject_battery(20.0)
    _方案(t)
    await _跑(t, 5)
    assert _patrols(t) == []
    assert any("20%" in r["detail"] for r in _log(t, "waiting")), "等的原因写明电量"


async def test_站点重启_接着按库里的进度办(站):
    t = 站
    _方案(t)
    await _跑(t, 3)
    assert len(_patrols(t)) == 1
    again = _值守(t)
    again.alerts = 告警台()
    await _跑(t, 5, duty=again)
    assert len(_patrols(t)) == 1, "那一圈还在跑:不重派"
    await _跑(t, 30, duty=again)
    assert len(_patrols(t)) >= 2


def test_方案校验(站):
    t = 站
    t.store.put_route("day", {"map_id": MAP[0], "map_version": MAP[1], "waypoints": ROUTE,
                              "base_revision": 0}, by="alice")
    rev = t.duty.plan()["revision"]
    bad = [({"route": "day"}, "不是防区路线"), ({"route": "ghost"}, "没有"),
           ({"route": "night-guard", "gap_min": 999}, "gap_min"),
           ({"route": "night-guard", "robot": "A b"}, "robot")]
    for e, why in bad:
        with pytest.raises(PlanError, match=why):
            t.duty.put_plan({"entries": [e], "base_revision": rev}, by="alice")
    two = [{"route": "night-guard", "robot": "A"}, {"route": "night-guard", "robot": "A"}]
    with pytest.raises(PlanError, match="同一台"):
        t.duty.put_plan({"entries": two, "base_revision": rev}, by="alice")
    with pytest.raises(PlanError, match="limits"):
        t.duty.put_plan({"entries": [], "limits": {"东区": 0}, "base_revision": rev}, by="alice")
    with pytest.raises(PlanError, match="别人刚改过"):
        t.duty.put_plan({"entries": [], "base_revision": rev + 5}, by="alice")
    got = _方案(t)
    assert got["revision"] == rev + 1 and got["entries"][0]["route"] == "night-guard"


def test_值守方案在用的防区路线_不许删_不许改成巡逻路线(站):
    t = 站
    _方案(t)
    with pytest.raises(RouteError, match="值守方案"):
        t.store.delete_route("night-guard", base_revision=1, by="alice")
    with pytest.raises(RouteError, match="值守方案"):
        t.store.put_route("night-guard", {"map_id": MAP[0], "map_version": MAP[1], "kind": "patrol",
                                          "waypoints": ROUTE, "base_revision": 1}, by="alice")


async def test_值守记录_按段翻(站):
    t = 站
    _方案(t)
    await _跑(t, 25)
    t.arming.set_zone_home("东区", False)
    t.arming.set_zone_home("西区", False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 2)
    [d] = t.duty.duties()
    assert d["ended_ms"] is not None and d["laps"] >= 1
    z = {x["zone"]: x for x in d["zones"]}
    assert z["东区"]["looks"] >= 2 and z["西区"]["looks"] >= 2
    full = t.duty.duty(d["id"])
    assert full["log"][0]["kind"] == "duty_start" and full["log"][-1]["kind"] == "duty_end"


async def test_在跑排程巡逻_值守一圈抢下来_事件派遣不抢(站):
    t = 站
    mission = {**json.loads(t.db.query("SELECT definition FROM routes WHERE route_id='night-guard'")
                            [0]["definition"])}
    await t.send(t.site.patrol("A", mission, issued_by="scheduler", priority=20, task_id="sched-1"))
    await _跑(t, 1)
    _方案(t)
    await _跑(t, 3)
    ps = _patrols(t)
    assert len(ps) == 2 and ps[1]["task_id"].startswith("guard-"), "排程的让给值守"
    assert t.duty.robots() == {"A"}, "排程别再往值守中的机器人上派"


async def test_在跑事件派遣_值守等它跑完(站):
    t = 站
    await t.send(t.site.goto("A", target(9.0), 0.8, issued_by="incident", priority=80,
                             task_id="incident-x"))
    await _跑(t, 1)
    _方案(t)
    await _跑(t, 3)
    assert _patrols(t) == []
    assert any("incident-x" in r["detail"] for r in _log(t, "waiting"))


async def test_一圈跑完下一圈派不出去_只记一次跑完_等着(站):
    t = 站
    _方案(t)
    await _跑(t, 5)
    t.duty.busy = lambda: {"A"}                         # 跑着的这一圈不受影响;跑完以后派不出下一圈
    await _跑(t, 40)
    assert len(_log(t, "lap_done")) == 1, "跑完那一圈只记一次,不每拍重记"
    assert len(_patrols(t)) == 1
    assert t.duty.status()["runs"][0]["state"] == "wait"
    assert any("驱离" in r["detail"] for r in _log(t, "waiting"))
    t.duty.busy = lambda: set()
    await _跑(t, 3)
    assert len(_patrols(t)) == 2 and _log(t, "lap_start")[-1]["detail"].startswith("第 2 圈")
