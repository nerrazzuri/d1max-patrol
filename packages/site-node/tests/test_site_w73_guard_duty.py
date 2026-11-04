"""W73 第二期:布防值守怎么跑(设计 §5.2–§5.6)。派遣器 + 真代理 + 仿真狗(走直线,1 m/s)。

一条防区路线 A(2,0) B(4,0) 在东区(x 0–5),C(7,0) D(9,0) 在西区(x 5–10)。东区值守位 (1,1),西区 (6,1)。
布防着 → 一圈圈跑;两圈之间有空档去值守位站着盯;留守撤掉的防区跳过;全撤了收队;被事件抢走,回来从最久
没看的防区接着跑、这一圈看过的不重复;人中止歇 10 分钟;太久没看报 P2、看到了自动解决;站点重启接着办。
"""

from __future__ import annotations

import inspect
import json

import pytest
from test_site_dispatcher import MAP, target, 台子

from d1max_site.alert_store import AlertDesk
from d1max_site.areas import AreaBook
from d1max_site.arming_schedule import ScheduleError
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
        inspect.signature(AlertDesk.raise_alert).bind(None, **kw)   # 跟真的告警台同一个签名
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


# ---------------------------------------------------------------- 第三期:保存时查冲突(设计 §6.3)

def _冲突台(t):
    from d1max_site.guard_clash import ClashCheck
    t.store.set_timezone("UTC", by="alice")
    t.store.put_route("day", {"map_id": MAP[0], "map_version": MAP[1], "waypoints": ROUTE,
                              "base_revision": 0}, by="alice")
    c = ClashCheck(t.db, schedule=t.arming.schedule, guard=t.duty, routes=t.store, now_ms=t.clock)
    t.store.clash = t.arming.schedule.clash = t.duty.clash = c
    t.arming.schedule.put({"enabled": True, "slots": [{"zone": "", "day": 0, "start": "22:00",
                                                       "end": "24:00"}]}, by="olga")
    return t


def _排(t, eid, at, robot="A", days=("mon",)):
    rows = t.db.query("SELECT revision FROM route_schedules WHERE entry_id=?", (eid,))
    return t.store.put_schedule(eid, {"mission": "day", "at": at, "days": list(days),
                                      "window_min": 5, "on_missed": "skip", "robot": robot,
                                      "base_revision": rows[0]["revision"] if rows else 0},
                                by="alice")


def test_新排程拖进布防值守_不让存_写清楚(站):
    t = _冲突台(站)
    _方案(t)
    with pytest.raises(RouteError, match=r"跟布防值守撞了.*A 在周一 21:55–22:0\d.*22:00"):
        _排(t, "s1", "21:55")
    _排(t, "s2", "21:00")                                # 早一点不撞
    _排(t, "s3", "21:55", robot="B")                     # 别的机器人不撞


def test_反过来_改布防时间撞上已有巡逻_也不让存(站):
    t = _冲突台(站)
    _方案(t)
    _排(t, "s1", "21:00")
    with pytest.raises(ScheduleError, match="跟巡逻排程撞了.*s1"):
        t.arming.schedule.put({"enabled": True, "slots": [{"zone": "", "day": 0, "start": "20:30",
                                                           "end": "24:00"}]}, by="olga")
    with pytest.raises(ScheduleError, match="跟巡逻排程撞了"):
        t.arming.schedule.add_exception({"first_day": "2027-01-18", "last_day": "2027-01-18",
                                         "state": "armed"}, by="olga")   # 2027-01-18 是星期一
    t.arming.schedule.add_exception({"first_day": "2027-01-19", "last_day": "2027-01-19",
                                     "state": "armed"}, by="olga")       # 星期二没巡逻


def test_反过来_改值守方案撞上已有巡逻_也不让存(站):
    t = _冲突台(站)
    _排(t, "s1", "22:30")
    with pytest.raises(PlanError, match="跟巡逻排程撞了.*s1"):
        _方案(t)
    _方案(t, robot="B")


def test_老数据本来就撞_不挡别的改动(站):
    t = _冲突台(站)
    _排(t, "s1", "22:30")                                # 那时还没值守方案
    t.duty.clash = None
    _方案(t)                                             # 绕开检查造出一个老冲突
    t.duty.clash = t.store.clash
    _排(t, "s2", "10:00")                                # 跟布防不沾边的改动照存



def test_外审_改路线让巡逻变长撞进布防_不让存_原样不动(站):
    t = _冲突台(站)
    _方案(t)
    _排(t, "s1", "21:00")                                # 原来一圈几十秒,不撞
    rev = t.store.get("day")["revision"]
    far = ROUTE + [{"name": "E", "x": 4000.0, "y": 0.0, "yaw": 0.0}]   # 走一个多小时
    with pytest.raises(RouteError, match="改了以后跟布防值守撞了.*s1"):
        t.store.put_route("day", {"map_id": MAP[0], "map_version": MAP[1], "waypoints": far,
                                  "base_revision": rev}, by="alice")
    assert t.store.get("day")["revision"] == rev, "整个回滚了"
    t.store.put_route("day", {"map_id": MAP[0], "map_version": MAP[1], "waypoints": ROUTE[:2],
                              "base_revision": rev}, by="alice")      # 不撞的照存
    assert t.store.get("day")["revision"] == rev + 1

# ---------------------------------------------------------------- 第四期:电量线(设计 §7.2–7.3)

async def test_电量过不了一圈的出站线_不开_要补能_回充按这条线起(站):
    """30% 以前能开(老 MIN_PCT),现在要「回撤线 + 一圈耗电 × 1.2」;到了这条线回充也该起,
    充到够撑到布防结束(最多 95%)。"""
    from d1max_site import energy
    t = 站
    _方案(t)
    info = t.duty._route("night-guard")
    line = energy.lap_line(t.duty._lap_s(info) or 0.0)
    assert 25.0 < line < 40.0
    t.dog.inject_battery(line - 0.5)
    await _跑(t, 5)
    assert _patrols(t) == []
    assert any("先补能" in r["detail"] for r in _log(t, "waiting"))
    assert t.duty.topup_line("A") == pytest.approx(line)
    assert 25.0 < t.duty.resume_pct("A") <= 95.0
    t.dog.inject_battery(line + 3)
    await _跑(t, 5)
    assert _patrols(t), "电够了照常开"
    assert t.duty.topup_line("A") is None


async def test_值守中_还有多少分钟要补能_按一圈出站线和耗电算(站):
    from d1max_site import energy
    t = 站
    _方案(t)
    assert t.duty.topup_eta_min("A") is None, "没在值守"
    t.dog.inject_battery(60.0)
    await _跑(t, 3)
    line = energy.lap_line(t.duty._lap_s(t.duty._route("night-guard")) or 0.0)
    got = t.duty.topup_eta_min("A")
    assert got is not None
    assert abs(got - (60.0 - line) / (energy.DRAIN_PCT_PER_H / 60)) < 3


async def test_布防前30分钟_电量撑不到第一次补能_提醒先充电换电池(站):
    """设计 §7.1:开始前 30 分钟查一次;够了不吵;一段布防只提醒一次。"""
    t = 站
    t.store.set_timezone("UTC", by="alice")              # 钟在 2027-01-15(周五)08:00 UTC
    _方案(t)
    t.arming.schedule.put({"enabled": True, "slots": [{"zone": "", "day": 4, "start": "08:40",
                                                       "end": "10:00"}]}, by="olga")
    t.dog.inject_battery(24.0)
    await _跑(t, 3)
    assert not t.duty.alerts.has_open("A", "guard_prep"), "还有 40 分钟,不急"
    t.clock.ms += 15 * 60_000
    await _跑(t, 3)
    [a] = [x for x in t.duty.alerts.open if x["kind"] == "guard_prep"]
    assert "布防前请先充电" in a["detail"] and "08:40" in a["detail"]
    await _跑(t, 3)
    assert len([x for x in t.duty.alerts.open if x["kind"] == "guard_prep"]) == 1


# ------------------------------------------------------------ 外审 r1


async def test_外审_换电拔着一块电池_值守不派下一圈(站):
    t = 站
    _方案(t)
    t.duty.swap = type("换电", (), {"busy": staticmethod(
        lambda rid: f"{rid} 的 1 号电池拔着,装回去之前不派")})()
    await _跑(t, 5)
    assert _patrols(t) == []
    assert any("1 号电池拔着" in r["detail"] for r in _log(t, "waiting"))


async def test_外审_跑到一半西区撤防_这一圈停下_接着只跑还布防着的(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)                                    # 还在去 A 的路上
    first = _patrols(t)[0]["task_id"]
    t.arming.set_zone_home("西区", False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 6)
    aborts = [c for c in t.site.commands("A", 500) if c["kind"] == "abort"]
    assert any(c["task_id"] == first for c in aborts), "这一圈要进撤防的西区:停下"
    assert _log(t, "zone_disarmed")
    later = _patrols(t)[1:]
    assert later and all(set(_names(p)) <= {"A", "B"} for p in later)
    await _跑(t, 20)
    assert "西区" not in _seen(t)


async def test_外审_看见人_原地停下盯着_保安处理了接着值守(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    tid = _patrols(t)[0]["task_id"]
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 3)
    assert any(c["kind"] == "abort" and c["task_id"] == tid
               for c in t.site.commands("A", 500)), "停在原地"
    assert t.duty.status()["runs"][0]["state"] == "held"
    x = t.dog.x
    await _跑(t, 5)
    assert abs(t.dog.x - x) < 0.05 and len(_patrols(t)) == 1, "盯着:不派下一圈"
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, 3)
    assert len(_patrols(t)) == 2 and _log(t, "person_resume")


async def test_外审_看见人盯满10分钟没人管_接着值守_同一条不再拦(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 2)
    t.clock.advance(601)
    await _跑(t, 4)
    assert len(_patrols(t)) == 2, "没人管:接着值守"
    assert t.duty.status()["runs"][0]["state"] != "held"


async def test_外审_掉线的机器人_站在值守位上也不算看着(站):
    t = 站
    _方案(t, gap_min=1)
    await _跑(t, 20)
    for _ in range(40):
        if t.duty.watching("A"):
            break
        await _跑(t, 0.5)
    await _跑(t, 2)
    before = _seen(t)["东区"]
    t.site._fresh = lambda c: False                     # 掉线 / 状态旧了
    await _跑(t, 5)
    assert _seen(t)["东区"] == before


def test_外审_固定方案的机器人别的方案占着_不硬派_任意一台不挑留给固定方案的(站):
    t = 站
    info = {"id": "night-guard", "map_id": MAP[0], "map_version": MAP[1]}
    assert t.duty._pick({"robot": "A"}, info, {"A"}) == ("", "A 在跑另一条值守方案")
    t.duty._reserved = {"A"}
    rid, why = t.duty._pick({"robot": ""}, info, set())
    assert rid == "" and "没有登记的机器人" in why


def _中止先失败(t, n):
    """派遣器的 abort 前 ``n`` 次抛错(没发出去),之后照常发。返回调用记录。"""
    real, calls = t.site.abort, []

    async def abort(rid, task_id, *, issued_by):
        calls.append(task_id)
        if len(calls) <= n:
            raise RuntimeError("abort 没发出去")
        return await real(rid, task_id, issued_by=issued_by)
    t.site.abort = abort
    return calls


async def test_外审r2_看见人_中止没发成不当已停_重发_真停了才算盯着(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    calls = _中止先失败(t, 1)
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 2)
    assert t.duty.status()["runs"][0]["state"] == "stopping", "中止没成:不算已经停下盯着"
    assert len(calls) == 1
    await _跑(t, 8)
    assert len(calls) >= 2, "没停下:过几秒再发中止"
    assert t.duty.status()["runs"][0]["state"] == "held"
    x = t.dog.x
    await _跑(t, 5)
    assert abs(t.dog.x - x) < 0.05 and len(_patrols(t)) == 1


async def test_外审r2_撤防后中止一直发不出去_不派新的一圈_报警(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    calls = _中止先失败(t, 99)
    t.arming.set_zone_home("西区", False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 20)
    assert len(_patrols(t)) == 1, "旧的一圈没确认停下:不派新的(两圈叠着跑)"
    assert len(calls) >= 3
    assert any(a["kind"] == "guard_stuck" and "停不下" in a["title"]
               for a in t.duty.alerts.open)


async def test_现场1104_跑圈时断网超过90秒_不当被打断_连回来不重派(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    tid = _patrols(t)[0]["task_id"]
    real_running, real_fresh = t.duty._running, t.site._fresh
    t.duty._running = lambda rid: ""                     # 断网:站点收到 offline 状态,手上没任务
    t.site._fresh = lambda c: False
    for _ in range(4):
        t.clock.advance(30)
        await t.send(t.duty.tick())
    assert len(_patrols(t)) == 1, "断网期间:不当被打断、不重派"
    t.site._fresh = real_fresh                           # 连回来了,事件还没补上来
    await t.send(t.duty.tick())
    assert len(_patrols(t)) == 1 and t.duty.status()["runs"][0]["state"] == "lap"
    t.clock.advance(30)
    t.duty._running = real_running                       # 状态补上来:还在跑这一圈
    await t.send(t.duty.tick())
    assert len(_patrols(t)) == 1 and t.duty.status()["runs"][0]["task_id"] == tid


async def test_现场1104_连回来以后90秒还没消息_才当被打断(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    t.duty._running = lambda rid: ""
    t.site._fresh = lambda c: False
    t.clock.advance(120)
    await t.send(t.duty.tick())
    t.site._fresh = lambda c: True                       # 连回来了(状态新鲜),但没在跑、事件也没来
    t.clock.advance(60)
    await t.send(t.duty.tick())
    assert len(_patrols(t)) == 1, "连回来才 60 秒:等它把事件补上来"
    t.clock.advance(31)
    await _跑(t, 1.5)
    assert len(_patrols(t)) == 2 and _log(t, "interrupted"), (t.duty.status()["runs"], _log(t)[-4:])


async def test_现场1104_要停这一圈时断网_不当已经停了(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    calls = _中止先失败(t, 99)
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await t.send(t.duty.tick())
    t.duty._running = lambda rid: ""                     # 断网:离线状态里手上没任务
    t.site._fresh = lambda c: False
    t.clock.advance(6)
    await t.send(t.duty.tick())
    assert t.duty.status()["runs"][0]["state"] == "stopping", "联系不上:没确认停下"
    assert len(calls) >= 2, "接着发中止(连回来会补投)"


async def test_现场1104_被人派的goto打断后断线又连回来_不抢人派的那一趟(站):
    """11-04 G2:人派的 goto 打断了这一圈;机器人断线(离线状态里手上没任务)、连回来那一下,站点当它空了,
    马上接着跑这一圈,把人派的 goto 顶掉。联系不上、刚连回来(还没报手上的任务)都不算空。"""
    t = 站
    _方案(t)
    await _跑(t, 3)
    await t.send(t.site.goto("A", target(30.0), 0.8, issued_by="gina", priority=80,
                             task_id="task-manual"))
    await _跑(t, 3)
    assert _log(t, "interrupted")
    n = len(_patrols(t))
    real_running, real_fresh = t.duty._running, t.site._fresh
    t.duty._running = lambda rid: ""                     # 断线:离线状态里手上没任务
    t.site._fresh = lambda c: False
    for _ in range(3):
        t.clock.advance(2)
        await t.send(t.duty.tick())
    assert len(_patrols(t)) == n, "联系不上:不当它空了"
    t.site._fresh = real_fresh                           # 连回来了,手上的任务还没报上来
    await t.send(t.duty.tick())
    t.clock.advance(1)
    await t.send(t.duty.tick())
    assert len(_patrols(t)) == n, "刚连回来:等它报手上的任务"
    t.duty._running = real_running                       # 报上来了:还在跑人派的那一趟
    for _ in range(4):
        t.clock.advance(2)
        await t.send(t.duty.tick())
    assert len(_patrols(t)) == n, "人派的还在跑:不抢"
