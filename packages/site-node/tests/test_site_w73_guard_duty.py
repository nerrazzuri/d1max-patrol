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

from nexwar_site.alert_store import AlertDesk
from nexwar_site.areas import AreaBook
from nexwar_site.arming_schedule import ScheduleError
from nexwar_site.guard_duty import GuardDuty, PlanError
from nexwar_site.modes import ArmingDesk
from nexwar_site.routes import RouteError, RouteStore

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

    def open_of(self, robot, kind):
        from types import SimpleNamespace
        cur = [a for a in self.open if a["robot"] == robot and a["kind"] == kind]
        return SimpleNamespace(context=dict(cur[-1].get("context") or {})) if cur else None

    def amend_open(self, robot, kind, *, title=None, detail=None, context=None):
        inspect.signature(AlertDesk.amend_open).bind(None, robot, kind, title=title,
                                                     detail=detail, context=context)
        cur = [a for a in self.open if a["robot"] == robot and a["kind"] == kind]
        if not cur:
            return None
        cur[-1]["context"] = {**(cur[-1].get("context") or {}), **(context or {})}
        return cur[-1]

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
    await _跑(t, 8)                                     # 停下、连着听得见 5 秒没任务才收
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
    from nexwar_site.guard_clash import ClashCheck
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
    from nexwar_site import energy
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
    from nexwar_site import energy
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


async def test_外审_看见人盯满时限没人管_接着值守_同一条不再拦(站):
    from nexwar_site.guard_duty import PERSON_HOLD_MS
    assert PERSON_HOLD_MS == 30 * 60_000, "W74 §九:30 分钟(原来 10 分钟,10 分钟改成报经理)"
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 2)
    t.clock.advance(PERSON_HOLD_MS // 1000 - 60)
    await _跑(t, 2)
    assert len(_patrols(t)) == 1, "还没到时限:盯着"
    t.clock.advance(61)
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
    """11-04 G2:人派的 goto 打断了这一圈;机器人断线(离线状态里手上没任务)、连回来那一下,
    站点当它空了,
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


class _待命点:
    """站点的待命点管理器里值守用到的两样:这台机器人在这张图上的默认待命点、派回待命点。"""

    def __init__(self, point=None):
        self.point = point
        self.sent: list[str] = []

    def swap_point(self, rid, map_id, map_version):
        return self.point

    async def return_to(self, rid, *, issued_by, **kw):
        self.sent.append(rid)
        return {}


_原点 = {"name": "home", "x": 0.0, "y": 0.0, "yaw": 0.0, "map_id": MAP[0], "map_version": MAP[1]}


async def test_现场1104_每圈结束回待命点_默认开_接在路线最后(站):
    """11-04 用户:最后一个点(东廊尽头)信号差会断网 —— 每圈跑完回待命点,不停在最后一个点。"""
    t = 站
    t.duty.standby = _待命点(_原点)
    _方案(t)
    assert t.duty.plan()["entries"][0]["end_standby"] is True
    await _跑(t, 2)
    assert _names(_patrols(t)[0]) == ["A", "B", "C", "D", "回待命点"]
    for _ in range(300):
        await _跑(t, 1)
        if len(_patrols(t)) >= 2:
            break
    assert _log(t, "lap_done")
    assert _names(_patrols(t)[1]) == ["A", "B", "C", "D", "回待命点"], "待命点不算防区:照常一整圈"


async def test_现场1104_每圈结束回待命点_关掉或没登记待命点_就不接(站):
    t = 站
    t.duty.standby = _待命点(_原点)
    t.duty.put_plan({"entries": [{"route": "night-guard", "robot": "A", "end_standby": False}],
                     "base_revision": t.duty.plan()["revision"]}, by="alice")
    assert t.duty.plan()["entries"][0]["end_standby"] is False
    await _跑(t, 2)
    assert _names(_patrols(t)[0]) == ["A", "B", "C", "D"]
    with pytest.raises(PlanError):
        t.duty.put_plan({"entries": [{"route": "night-guard", "end_standby": "yes"}],
                         "base_revision": t.duty.plan()["revision"]}, by="alice")


async def test_现场1104_没登记待命点_照常跑不接(站):
    t = 站
    t.duty.standby = _待命点(None)
    _方案(t)
    await _跑(t, 2)
    assert _names(_patrols(t)[0]) == ["A", "B", "C", "D"]
    await _跑(t, 45)
    assert len(_patrols(t)) >= 2
    warn = _log(t, "no_standby")
    assert len(warn) == 1 and "没有默认待命点" in warn[0]["detail"], "值守记录里说一声,只说一次"


async def test_现场1104_有空档又没有值守位_回待命点等_不停在最后一个点(站):
    t = 站
    t.duty.standby = stb = _待命点(_原点)
    t.duty.posts = lambda m, v: {}
    t.duty.put_plan({"entries": [{"route": "night-guard", "robot": "A", "gap_min": 5,
                                  "end_standby": False}],
                     "base_revision": t.duty.plan()["revision"]}, by="alice")
    for _ in range(300):
        await _跑(t, 1)
        if _log(t, "lap_done"):
            break
    await _跑(t, 2)
    assert stb.sent == ["A"] and _log(t, "watch_none")


# ---------------------------------------------------------------- 外审 r2 #4 #6 #7


async def test_外审r2_4_看过防区一个点后撤防_剩下的点也不跑(站):
    t = 站
    _方案(t)
    for _ in range(60):
        await _跑(t, .5)
        if "东区" in _seen(t) and t.dog.x < 3.3:
            break
    assert "东区" in _seen(t) and t.dog.x < 3.3, "到了 A、还没到 B"
    at = t.clock()
    t.arming.set_zone_home("东区", False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 6)
    later = [dict(r) for r in t.db.query("SELECT * FROM guard_log WHERE at_ms>=?", (at,))]
    assert not any(r["kind"] == "point" and r["point"] == "B" for r in later), "东区撤了,B 不去"
    assert _log(t, "zone_disarmed")


async def test_外审r2_4_别的机器人看过这个防区_不当成这一圈走过了(站):
    t = 站
    _方案(t)
    await _跑(t, 3)
    run = t.duty._runs()[t.duty.plan()["entries"][0]["id"]]
    t.duty._mark_seen(MAP[0], MAP[1], "东区", "B", "point")       # 别的机器人刚看过东区
    info = t.duty._route("night-guard")
    assert t.duty._off_zones(info, ["西区"], run) == ["东区"]


async def test_外审r2_6_任意方案零间隔_圈完让给指定它的方案(站):
    t = 站
    _方案(t, robot="")
    await _跑(t, 3)
    t.duty.put_plan({"entries": [{"route": "night-guard", "robot": ""},
                                 {"route": "night-guard", "robot": "A"}],
                     "base_revision": t.duty.plan()["revision"]}, by="alice")
    await _跑(t, 70)
    runs = t.duty.status()["runs"]
    fixed = next(e["id"] for e in t.duty.plan()["entries"] if e["robot"] == "A")
    assert any(r["entry"] == fixed and r["robot_id"] == "A" for r in runs), runs
    assert _log(t, "stand_down")


async def test_外审r2_7_位置停更或定位失效_站在值守位上也不算看着(站):
    from dataclasses import replace
    t = 站
    _方案(t, gap_min=10)
    await _跑(t, 20)
    for _ in range(60):
        if t.duty.watching("A"):
            break
        await _跑(t, .5)
    assert t.duty.watching("A")
    c = t.site.clients["A"]
    act = [(t.duty.plan()["entries"][0], t.duty._route("night-guard"), ["东区", "西区"])]
    await _跑(t, 1)
    before = _seen(t)["东区"]
    t.clock.advance(30)
    c.status_live_at = t.clock()
    t.site.telemetry_at["A"] = t.clock() - 30_000
    t.duty._post_seen(act)
    assert _seen(t)["东区"] == before, "位置 30 秒没更新:不算看着"
    c.status = replace(c.status, ready=replace(c.status.ready, loc_ok=False))
    t.site.telemetry_at["A"] = t.clock()
    t.duty._post_seen(act)
    assert _seen(t)["东区"] == before, "定位失效:不算看着"
    c.status = replace(c.status, ready=replace(c.status.ready, loc_ok=True))
    t.duty._post_seen(act)
    assert _seen(t)["东区"] > before


async def test_外审r2_11_跑过的路线加了停留_按新的算_冲突照样拦(站):
    from copy import deepcopy
    t = _冲突台(站)
    mission = json.loads(t.db.query("SELECT definition FROM routes WHERE route_id='day'")[0]
                         ["definition"])
    await t.send(t.site.patrol("A", mission, issued_by="alice", priority=60, task_id="learn-day"))
    await t.run(250)
    c = t.store.clash
    before = c._duration("day")
    assert before is not None and before < 60, "跑过一趟:按实际用时"
    pts = deepcopy(ROUTE)
    pts[1]["actions"] = [{"type": "dwell", "seconds": 5400}]
    t.store.put_route("day", {"map_id": MAP[0], "map_version": MAP[1], "waypoints": pts,
                              "base_revision": 1}, by="alice")
    assert c._duration("day") >= 5400, "B 改了停留:旧的实际用时不算数"


# ---------------------------------------------------------------- 外审 r3


async def test_外审r3_1_回换电位的路上拔了一块_先停下再引导装回(站):
    from test_site_dispatcher import target

    from nexwar_site.battery_swap import SwapDesk
    from nexwar_site.standby import StandbyManager
    t = 站
    await t.run(12)
    await t.send(t.site.goto("A", target(6), 0.8, issued_by="alice", priority=60))
    await t.run(120)
    s = StandbyManager(t.db, t.site, now_ms=t.clock)
    s.set("A", "default", map_id=MAP[0], map_version=MAP[1], x=0.0, y=0.0, yaw=0.0, default=True)
    desk = SwapDesk(t.site, now_ms=t.clock, need=lambda _: 35.0, eta_min=lambda _: None,
                    has_charger=lambda _: False, home=s.swap_point)
    desk.standby = s
    desk.alerts = 告警台()
    t.dog.inject_packs(25.1, 60)
    await t.run(12)
    await t.send(desk.tick())
    await t.run(20)
    assert desk.s["A"]["step"] == "going" and t.duty._running("A").startswith("standby-")
    t.dog.inject_packs(None, 60)
    await t.run(12)
    before = t.dog.x
    await t.send(desk.tick())
    assert not desk.alerts.open, "还在走:先别叫人过来装电池"
    for _ in range(10):
        await t.run(10)
        await t.send(desk.tick())
    assert abs(t.dog.x - before) < 0.5, "拔着一块:这一趟回程停下"
    assert not t.duty._running("A")
    assert any("装回" in a["title"] for a in desk.alerts.open), "停下了才引导装回"


async def test_外审r3_2_在停这一圈时断线又连回来_连回来那一拍的空任务不算停了(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    calls = []

    async def 拒(*a, **kw):
        calls.append(a)
        return {"ack": {"result": "rejected"}}
    t.site.abort = 拒
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await t.send(t.duty.tick())
    assert t.duty.status()["runs"][0]["state"] == "stopping"
    real = t.duty._running
    t.duty._running = lambda _: ""
    t.site._fresh = lambda _: False
    await t.send(t.duty.tick())
    t.site._fresh = lambda _: True
    await t.send(t.duty.tick())
    state = t.duty.status()["runs"][0]["state"]
    t.duty._running = real
    assert state == "stopping", "刚恢复联系的空状态不能当停车证据"


async def test_外审r3_3_全撤防_中止被拒_接着重发_停下了才收队回待命点(站):
    t = 站
    t.duty.standby = stb = _待命点(_原点)
    _方案(t)
    await _跑(t, 1.5)
    calls = []
    real_abort = t.site.abort

    async def 拒(*a, **kw):
        calls.append(a)
        return {"ack": {"result": "rejected"}}
    t.site.abort = 拒
    for z in ("东区", "西区"):
        t.arming.set_zone_home(z, False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 12)
    assert len(calls) >= 2, "全撤防也要重发中止"
    assert t.duty.status()["runs"], "没停下之前不删记录"
    assert stb.sent == []
    t.site.abort = real_abort
    await _跑(t, 8)
    assert not t.duty._running("A")
    assert t.duty.status()["runs"] == [], "停下了才收队"
    assert stb.sent == ["A"], "收队后回待命点"


async def test_外审r3_6_每圈回待命点那段算进一圈的电_不够就先补能(站):
    t = 站
    t.duty.standby = stb = _待命点({"x": 400.0, "y": 0.0, "yaw": 0.0})
    stb.home = lambda rid, m, v: {"x": 0.0, "y": 0.0}       # 站点知道狗上的原点
    _方案(t)
    t.dog.inject_battery(40.0)
    await _跑(t, 8)
    assert _patrols(t) == [], "带 400 m 回待命点的一圈 40% 不够:不派"
    assert t.duty.topup_line("A") is not None, "判出不够:进补能"
    info = t.duty._route("night-guard")
    assert t.duty._lap_s(info, (400.0, 0.0)) > t.duty._lap_s(info) + 1000, "用时也算上那段"


async def test_外审r3_6_站点放行的一圈_狗上出发线也过得了(站):
    from nexwar_site import energy
    t = 站
    t.duty.standby = stb = _待命点({"x": 100.0, "y": 0.0, "yaw": 0.0})
    stb.home = lambda rid, m, v: {"x": 0.0, "y": 0.0}
    e = {"id": 1, "robot": "A", "end_standby": True}
    info = t.duty._route("night-guard")
    await t.run(12)
    line, _ = t.duty._lap_need(e, info, "A")
    # 狗上(直线后端):回撤线下限 25 + max(3%,全程耗电) × 1.5;全程 = 现在 → 各点 → 待命点 →
    # 沿来路倒回出发点 → 原点
    from nexwar_agent.engine.homing import route_length_m
    from nexwar_contract.geometry import Pose
    c = t.site.clients["A"]
    here = Pose.from_xy_yaw(c.telemetry.pose.x, c.telemetry.pose.y, 0.0)
    wps = [Pose.from_xy_yaw(p["x"], p["y"], 0.0) for p in info["points"]]
    wps.append(Pose.from_xy_yaw(100.0, 0.0, 0.0))
    d = route_length_m(Pose.from_xy_yaw(0.0, 0.0, 0.0), wps, start=here, back="retrace")
    assert line >= energy.patrol_departure_line(d) - 1e-9
    assert line > t.duty._lap_need({**e, "end_standby": False}, info, "A")[0], "回待命点那段算进去"
    assert energy.patrol_departure_line(0.0) == 25.0 + 3.0 * 1.5


async def test_外审r3_8_路线已经500个点_不接待命点_照样派(站):
    t = 站
    pts = [{"name": f"p{i}", "x": 2.0 + (i % 2) * 0.1, "y": 0.0, "yaw": 0.0} for i in range(500)]
    t.store.put_route("night-guard", {"map_id": MAP[0], "map_version": MAP[1], "kind": "guard",
                                      "waypoints": pts, "base_revision": 1}, by="alice")
    t.duty.standby = _待命点({"x": 0.0, "y": 0.0, "yaw": 0.0})
    _方案(t)
    await _跑(t, 3)
    assert _patrols(t), "合法的 500 点路线照样派"
    assert len(_names(_patrols(t)[0])) == 500
    assert _log(t, "no_standby")



async def test_W74_恢复值守后_最后位置那个防区和相邻的加密巡查一小时_每圈不歇_之后照旧(站):
    from nexwar_site.guard_duty import INTENSE_MS
    assert INTENSE_MS == 60 * 60_000
    t = 站
    t.areas.set(MAP[0], MAP[1], "远区", [[20.0, -2.0], [25.0, -2.0], [25.0, 2.0], [20.0, 2.0]],
                by="alice")
    _方案(t, gap_min=10)
    for _ in range(30):                                   # 走到西区(x > 5.5)
        await _跑(t, 0.5)
        if t.dog.x > 5.5:
            break
    assert t.dog.x > 5.5
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 3)
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, 3)
    [lg] = _log(t, "intense_start")
    assert "西区" in lg["detail"] and "东区" in lg["detail"] and "远区" not in lg["detail"]
    assert _names(_patrols(t)[-1])[:1] == ["C"], "从最后看见人的那个防区先跑"
    n = len(_patrols(t))
    await _跑(t, 25)
    assert len(_patrols(t)) >= n + 1, "每圈之间不歇(方案本来隔 10 分钟,会去值守位)"
    assert not _gotos(t, "guard-watch"), "加密巡查时不去值守位站着"
    t.clock.advance(INTENSE_MS // 1000 + 1)
    await _跑(t, 25)
    assert _log(t, "intense_end")


def _follows(t):
    return [c for c in reversed(t.site.commands("A", 500)) if c["kind"] == "follow"]


async def test_W74_看见人_停下以后派跟人_一回只派一次_机器人不会跟就只盯着(站):
    t = 站
    _方案(t)
    await _跑(t, 1.5)
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 3)
    assert _follows(t) == [], "机器人能力里没有 follow:照旧原地盯着"
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, 3)
    t.site.clients["A"].capabilities.tasks["follow"] = {"state": "idle"}
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="又看见人")
    await _跑(t, 3)
    [f] = _follows(t)
    assert f["task_id"].startswith("follow-") and f["priority"] == 80
    assert _log(t, "follow_start")
    await _跑(t, 5)
    assert len(_follows(t)) == 1, "这一回只派一次"


def _假跟(t):
    """派遣器的 follow 当成机器人接了(仿真代理不会跟人,回 unsupported);机器人手上在跑什么由测试
    说了算(``box["run"]``:``None`` = 照仿真狗报的)。``refuse`` 拒、``boom`` 回执没到。"""
    sent, box = [], {"run": None, "refuse": False, "boom": False}

    async def follow(rid, tid, req, *, issued_by):
        sent.append(tid)
        if box["boom"]:
            raise RuntimeError("回执没到")
        if box["refuse"]:
            return {"task_id": tid, "ack": {"result": "rejected", "reason": "busy"}}
        box["run"] = tid
        return {"task_id": tid, "ack": {"result": "accepted"}}
    t.site.follow = follow
    real = t.duty._running
    t.duty._running = lambda rid: box["run"] if box["run"] is not None else real(rid)
    t.sent, t.box = sent, box
    return sent, box


async def _跟着(t):
    """布防、看见人、派了跟人(机器人接了、在跟);回那一趟的任务号。"""
    from nexwar_site.map_marks import MapMarks
    t.duty.marks = MapMarks(t.db, now_ms=t.clock)
    _假跟(t)
    _方案(t)
    await _跑(t, 1.5)
    t.site.clients["A"].capabilities.tasks["follow"] = {"state": "idle"}
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 3)
    return t.duty._followed["A"]


def _事(kind, **data):
    from types import SimpleNamespace
    return SimpleNamespace(kind=kind, data=data)


async def test_W74三期_人越过出口线_报人离开值守区_机器人回线内3米朝出口(站):
    t = 站
    tid = await _跟着(t)
    t.duty.marks.set(MAP[0], MAP[1], {"kind": "exit", "name": "东门",
                                      "points": [[3.0, -2.0], [3.0, 2.0]]}, by="alice")
    t.duty._on_event("A", _事("follow_track", task_id="别的", points=[
        {"x": 2.0, "y": 0.0, "at_ms": 1}, {"x": 4.0, "y": 0.0, "at_ms": 2}]))
    assert not t.duty.alerts.has_open("A", "person_left"), "不是这一回派的跟人:不管"
    t.duty._on_event("A", _事("follow_track", task_id=tid, points=[
        {"x": 1.0, "y": 0.5, "at_ms": 1}, {"x": 2.5, "y": 0.5, "at_ms": 2}]))
    assert not t.duty.alerts.has_open("A", "person_left")
    t.duty._on_event("A", _事("follow_track", task_id=tid, points=[
        {"x": 3.5, "y": 0.5, "at_ms": 3}], last={"x": 3.5, "y": 0.5, "at_ms": 3},
        heading_deg=0.0))
    [a] = [a for a in t.duty.alerts.open if a["kind"] == "person_left"]
    assert "东门" in a["title"] and a["context"]["exit"] == "东门"
    await _跑(t, 1)
    assert any(c["kind"] == "abort" and c["task_id"] == tid for c in t.site.commands("A", 500))
    t.box["run"] = ""                                    # 跟人那一趟停了
    await _跑(t, 3)
    [g] = _gotos(t, "follow-exit-")
    p = g["payload"]["target"]
    assert abs(p["x"] - 0.0) < 1e-6 and abs(p["y"] - 0.5) < 1e-6 and abs(p["yaw"]) < 1e-6
    assert g["priority"] == 80
    assert _log(t, "person_left")
    t.duty._on_event("A", _事("follow_track", task_id=tid, points=[
        {"x": 2.0, "y": 0.5, "at_ms": 4}, {"x": 4.0, "y": 0.5, "at_ms": 5}]))
    await _跑(t, 2)
    assert len(_gotos(t, "follow-exit-")) == 1, "这一回只报一次、只回一次"
    t.duty._on_event("A", _事("follow_lost", task_id=tid, result="lost",
                              last={"x": 3.5, "y": 0.5, "at_ms": 3}, heading_deg=0.0))
    await _跑(t, 3)
    assert not [c for c in _patrols(t) if c["task_id"].startswith("follow-sweep-")], \
        "人出去了:不去看易藏点"


async def test_W74三期_跟丢了_去附近易藏点看一圈_去向那边先看_看完回最后位置(站):
    t = 站
    tid = await _跟着(t)
    for n, p in (("北楼梯", [4.0, 1.5]), ("南门后", [4.0, -1.5]), ("太远", [40.0, 0.0])):
        t.duty.marks.set(MAP[0], MAP[1], {"kind": "hide", "name": n, "points": [p], "yaw": 0.5},
                         by="alice")
    t.duty._on_event("A", _事("follow_lost", task_id=tid, result="lost",
                              last={"x": 3.0, "y": 0.0, "at_ms": 3}, heading_deg=90.0))
    await _跑(t, 1)
    assert not [c for c in _patrols(t) if c["task_id"].startswith("follow-sweep-")], \
        "机器人还在回最后位置:等它那一趟结束"
    t.box["run"] = ""
    await _跑(t, 3)
    [s] = [c for c in _patrols(t) if c["task_id"].startswith("follow-sweep-")]
    assert _names(s) == ["易藏点1-北楼梯", "易藏点2-南门后", "最后位置"]
    w = s["payload"]["mission"]["waypoints"]
    assert {"type": "dwell", "seconds": 10} in w[0]["actions"]
    assert abs(w[-1]["pose"]["position"]["x"] - 3.0) < 1e-6 and s["priority"] == 80
    assert _log(t, "sweep_start")
    t.box["run"] = s["task_id"]                          # 机器人在看易藏点
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, 3)
    assert any(c["kind"] == "abort" and c["task_id"] == s["task_id"]
               for c in t.site.commands("A", 500)), "保安处理了:看易藏点的那一趟停掉,接着值守"


async def test_W74三期_跟丢了附近没易藏点_或到上限_不去看(站):
    t = 站
    tid = await _跟着(t)
    t.duty._on_event("A", _事("follow_lost", task_id=tid, result="lost",
                              last={"x": 3.0, "y": 0.0, "at_ms": 3}, heading_deg=90.0))
    await _跑(t, 3)
    t.duty.marks.set(MAP[0], MAP[1], {"kind": "hide", "name": "北", "points": [[3.0, 1.0]]},
                     by="alice")
    t.duty._on_event("A", _事("follow_lost", task_id=tid, result="limit",
                              last={"x": 3.0, "y": 0.0, "at_ms": 3}, heading_deg=90.0))
    await _跑(t, 3)
    assert not [c for c in _patrols(t) if c["task_id"].startswith("follow-sweep-")]
    assert _log(t, "follow_lost")


async def test_W74三期_加密巡查_那几个防区里的易藏点先看(站):
    from nexwar_site.map_marks import MapMarks
    t = 站
    t.duty.marks = MapMarks(t.db, now_ms=t.clock)
    for n, p in (("西楼梯", [8.0, 1.0]), ("东仓库", [3.0, 1.0]), ("区外", [30.0, 0.0])):
        t.duty.marks.set(MAP[0], MAP[1], {"kind": "hide", "name": n, "points": [p]}, by="alice")
    _方案(t, gap_min=10)
    for _ in range(30):                                   # 走到西区(x > 5.5)
        await _跑(t, 0.5)
        if t.dog.x > 5.5:
            break
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 3)
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, 3)
    names = _names(_patrols(t)[-1])
    assert names[:3] == ["加密-易藏点1-西楼梯", "加密-易藏点2-东仓库", "C"], names
    assert not any("区外" in n for n in names)



async def test_外审r1_3_跟人和派生的那几趟_盯着的时候跑完不自动回待命点(站):
    t = 站
    tid = await _跟着(t)
    for x in (tid, "follow-sweep-1-ab", "follow-exit-1-ab"):
        assert t.duty.holds("A", x), x
    t.box["run"] = ""                                    # 那一趟结束了
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, 6)
    assert not t.duty.holds("A", tid), "接着值守了:不再拦"


async def test_外审r1_4_撤防_跟人那一趟先停_停了才收队(站):
    t = 站
    tid = await _跟着(t)
    t.arming.set_zone_home("东区", False)
    t.arming.set_zone_home("西区", False)
    t.arming.set_mode("home", by="olga")
    await _跑(t, 3)
    assert any(c["kind"] == "abort" and c["task_id"] == tid for c in t.site.commands("A", 500))
    assert t.duty.status()["runs"], "还在跟:先别删进度"
    t.box["run"] = ""
    await _跑(t, 8)
    assert t.duty.status()["runs"] == [], "停了:收队"
    assert "A" not in t.duty._followed


async def test_外审r1_5_站点重启_认回还在跑的跟人_不另派_越线照办(站):
    t = 站
    tid = await _跟着(t)
    t.duty.marks.set(MAP[0], MAP[1], {"kind": "exit", "name": "东门",
                                      "points": [[3.0, -2.0], [3.0, 2.0]]}, by="alice")
    d2 = _值守(t)
    d2.alerts, d2.marks, d2._running = t.duty.alerts, t.duty.marks, t.duty._running
    t.duty = d2
    await _跑(t, 6)
    assert t.sent == [tid], "重启了:认回还在跑的那一趟,不另派"
    d2._on_event("A", _事("follow_track", task_id=tid, points=[
        {"x": 2.0, "y": 0.5, "at_ms": 1}, {"x": 3.5, "y": 0.5, "at_ms": 2}]))
    assert d2.alerts.has_open("A", "person_left"), "认回来的那一趟的轨迹照样判越线"


async def test_外审r1_6_保安处理了_停跟人没发成_到点再发_停了才接着值守(站):
    from nexwar_site.guard_duty import STOP_RETRY_MS
    t = 站
    tid = await _跟着(t)
    calls = _中止先失败(t, 1)
    n = len(_patrols(t))
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, STOP_RETRY_MS / 1000 + 2)
    assert calls.count(tid) >= 2, calls
    assert len(_patrols(t)) == n, "跟人还没停:不派下一圈"
    t.box["run"] = ""
    await _跑(t, 12)
    assert len(_patrols(t)) > n, "停了:接着值守"


async def _会跟着跑(t, secs):
    """跑着的时候一直补上能力里的 follow(仿真代理不会跟人,每次重发能力都会把它盖掉)。"""
    for _ in range(int(secs * 2)):
        t.site.clients["A"].capabilities.tasks["follow"] = {"state": "idle"}
        await _跑(t, 0.5)


async def test_外审r1_7_派跟人被拒_过一会儿再试_回执没到的过一会儿对账(站):
    from nexwar_site.guard_duty import FOLLOW_RETRY_MS
    t = 站
    sent, box = _假跟(t)
    box["refuse"] = True
    _方案(t)
    await _跑(t, 1.5)
    t.site.clients["A"].capabilities.tasks["follow"] = {"state": "idle"}
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 3)
    assert len(sent) == 1 and "A" not in t.duty._followed
    assert _log(t, "follow_refused")
    box["refuse"], box["boom"] = False, True
    await _会跟着跑(t, FOLLOW_RETRY_MS / 1000 + 2)
    assert len(sent) == 2, "被拒过一阵再试"
    assert t.duty._followed["A"] == sent[-1], "回执没到:先当派出去了(不重复派)"
    box["boom"] = False
    await _会跟着跑(t, FOLLOW_RETRY_MS / 1000 + 2)
    assert len(sent) == 3 and t.duty._followed["A"] == sent[-1], \
        "回执没到、机器人手上也不是它:当没派成,再派"


async def test_外审r1_10_加密巡查_撤防的防区里的易藏点不去_去的易藏点记进这一圈(站):
    from nexwar_site.map_marks import MapMarks
    t = 站
    t.duty.marks = MapMarks(t.db, now_ms=t.clock)
    for n, p in (("西楼梯", [8.0, 1.0]), ("东仓库", [3.0, 1.0])):
        t.duty.marks.set(MAP[0], MAP[1], {"kind": "hide", "name": n, "points": [p]}, by="alice")
    t.arming.set_zone_home("东区", False)
    t.arming.set_mode("home", by="olga")
    _方案(t, gap_min=10)
    for _ in range(30):
        await _跑(t, 0.5)
        if t.dog.x > 5.5:
            break
    t.duty.alerts.raise_alert(kind="dog_sees_person", robot="A", title="看见人")
    await _跑(t, 3)
    t.duty.alerts.resolve_all("A", "dog_sees_person", who="gina")
    await _跑(t, 3)
    names = _names(_patrols(t)[-1])
    assert names[:2] == ["加密-易藏点1-西楼梯", "C"], names
    assert not any("东仓库" in n for n in names), "东区撤防了:不去"
    plan = json.loads(t.duty.status()["runs"][0]["plan"]) if "plan" in t.duty.status()["runs"][0] \
        else json.loads(t.db.query("SELECT plan FROM guard_runs")[0]["plan"])
    assert {"name": "加密-易藏点1-西楼梯", "zone": "西区"} in plan, plan
