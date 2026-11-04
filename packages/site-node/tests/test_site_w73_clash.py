"""W73 第三期:巡逻排程跟布防值守撞没撞(设计 §6.3)。纯计算:按机器人、按一周的分钟算占用,跨午夜、
星期日连星期一都算对;只拦这一次改动新造出来的冲突。"""

from __future__ import annotations

from nexwar_site.guard_clash import Patrol, clashes, guard_busy, patrol_busy

MON, TUE, SUN = 0, 1, 6


def _p(eid="night-1", robot="A", at=(21, 50), days=("mon",), window=5, dur_s=1800.0, every=0,
       until=(0, 0)):
    return Patrol(id=eid, route="r", robot=robot, at_min=at[0] * 60 + at[1], days=frozenset(days),
                  window_min=window, every_min=every, until_min=until[0] * 60 + until[1],
                  duration_s=dur_s)


def _g(eid=1, route="night-guard", robot="A", wins=((MON * 1440 + 22 * 60, MON * 1440 + 24 * 60),)):
    return {"id": eid, "route": route, "robot": robot, "windows": list(wins)}


def test_巡逻占用_开始到开始加窗口加保守用时加两分钟():
    # 30 分钟的路线,保守按 ×1.3 = 39 分钟;窗口 5 分钟;回待命点余量 2 分钟
    start = MON * 1440 + 21 * 60 + 50
    assert patrol_busy(_p()) == [(start, start + 5 + 39 + 2)]


def test_重复的每一轮都算():
    got = patrol_busy(_p(at=(22, 0), every=60, until=(23, 0), dur_s=600))
    assert [a for a, _ in got] == [MON * 1440 + 22 * 60, MON * 1440 + 23 * 60]


def test_同一台_巡逻拖进布防_拦_写清楚哪天几点():
    got = clashes([_p()], [_g()], robots=["A", "B"])
    assert len(got) == 1
    msg = got[0]
    assert "A" in msg and "周一 21:50–22:36" in msg and "22:00" in msg and "night-1" in msg


def test_别的机器人不撞():
    assert clashes([_p(robot="B")], [_g()], robots=["A", "B"]) == []


def test_跨午夜_星期日的布防连到星期一凌晨():
    g = _g(wins=((SUN * 1440 + 22 * 60, 7 * 1440 + 6 * 60),))     # 周日 22:00 到周一 06:00
    early = _p(at=(5, 0), days=("mon",), dur_s=600)
    late = _p(at=(7, 0), days=("mon",), dur_s=600)
    assert len(clashes([early], [g], robots=["A"])) == 1
    assert clashes([late], [g], robots=["A"]) == []


def test_巡逻跨午夜拖进第二天的布防():
    g = _g(wins=((TUE * 1440 + 0, TUE * 1440 + 120),))
    p = _p(at=(23, 50), days=("mon",), dur_s=1200)
    assert len(clashes([p], [g], robots=["A"])) == 1


def test_值守任意一台_巡逻指定那台_还剩一台够值守就不拦():
    g = _g(robot="")
    assert clashes([_p(robot="A")], [g], robots=["A", "B"]) == []
    assert len(clashes([_p(robot="A")], [g], robots=["A"])) == 1, "只有一台:被巡逻占了就没人值守"


def test_巡逻任意一台_至少一台没被布防占就行():
    p = _p(robot="")
    assert clashes([p], [_g(robot="A")], robots=["A", "B"]) == []
    assert len(clashes([p], [_g(robot="A"), _g(eid=2, robot="B")], robots=["A", "B"])) == 1


def test_值守占用就是它路线经过的防区布防的那几段():
    assert guard_busy(_g()) == [(MON * 1440 + 22 * 60, MON * 1440 + 24 * 60)]


def test_布防值守中的机器人_到点的巡逻不派_说在布防值守(tmp_path):
    """设计 §6.4:布防期间到点的巡逻排程,这台在值守 → 记跳过「布防中」,不派。"""
    from types import SimpleNamespace

    from nexwar_contract.mission import parse_mission
    from nexwar_contract.schedule import parse_schedule
    from nexwar_site.catalog import ActiveBundle
    from nexwar_site.db import SiteDB
    from nexwar_site.dispatcher import Dispatcher
    from nexwar_site.scheduler import SiteScheduler
    now = 1_800_000_000_000
    db = SiteDB(tmp_path / "db")
    mission = parse_mission({"mission": "loop", "map_id": "m", "policy": {}, "waypoints": [
        {"name": "p", "pose": {"position": {"x": 1, "y": 0},
                               "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}]})
    schedule = parse_schedule({"timezone": "UTC", "entries": [
        {"id": "s", "mission": "loop", "at": "22:00", "days": ["mon"], "window_min": 30,
         "on_missed": "skip"}]})
    d = SimpleNamespace(
        clients={"A": SimpleNamespace(
            status=SimpleNamespace(task=None),
            capabilities=SimpleNamespace(tasks={"patrol": {"map_id": "m"}}))},
        registry=SimpleNamespace(list=lambda: [SimpleNamespace(robot_id="A", revoked=False)]),
        dispatchable=lambda rid, kind: "", autonomy=lambda rid: "autonomous",
        on_event=lambda cb: None, on_ack=lambda cb: None)
    d.busy = lambda rid: Dispatcher.busy(d, rid)
    sched = SiteScheduler(db, d, now_ms=lambda: now)
    sched.guard_busy = lambda: {"A"}
    act = ActiveBundle("b", 1, schedule, {"loop": mission}, {"s": now})
    ok, _, why, kinds = sched._candidates(act, schedule.entries[0], {})
    assert ok == [] and "在布防值守" in why and kinds == {"busy"}
    db.close()
