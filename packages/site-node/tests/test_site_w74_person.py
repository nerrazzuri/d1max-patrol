"""W74 一期:看见人以后 —— 去向不明、保安到场才能关、10 分钟升级经理。

设计 docs/W74-看见人处置-设计.md。

告警台用真的(落库、重启读回);派遣、布防台用假的。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexwar_site.alert_store import AlertDesk, ArrivalNeeded
from nexwar_site.db import SiteDB


class 钟:
    def __init__(self) -> None:
        self.ms = 1_800_000_000_000

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def 台(tmp_path):
    c = 钟()
    db = SiteDB(tmp_path / "site.db")
    desk = AlertDesk(db, now_ms=c)
    return SimpleNamespace(c=c, db=db, desk=desk, path=tmp_path / "site.db")


def _看见人(desk, **ctx):
    return desk.raise_alert(kind="dog_sees_person", robot="A", title="机器人看见人了:1 个人",
                            context={"task_id": "persons", **ctx})


# ------------------------------------------------------------ 告警台:改、到场、到场才能关


def test_改还开着的告警_标题现场都换_不加次数_重启读回(台):
    a = _看见人(台.desk, zone="大厅")
    台.c.ms += 5_000
    b = 台.desk.amend_open("A", "dog_sees_person", title="看不到了,去向不明",
                           context={"lost_ms": 台.c.ms})
    assert b is not None and b.key == a.key and b.count == 1
    assert b.title == "看不到了,去向不明" and b.context["zone"] == "大厅"
    assert b.context["lost_ms"] == 台.c.ms, "现场是合进去,不是整个换掉"
    assert 台.desk.amend_open("A", "stuck", title="x") is None, "没有开着的:不改、不新起"
    again = AlertDesk(台.db, now_ms=台.c)
    [r] = [x for x in again.open() if x["key"] == a.key]
    assert r["title"] == "看不到了,去向不明" and r["context"]["lost_ms"] == 台.c.ms


def test_看见人_没点到场_处置完成和误报都关不了_点了才能关(台):
    a = _看见人(台.desk)
    with pytest.raises(ArrivalNeeded):
        台.desk.resolve(a.key, who="张三", verdict="handled")
    with pytest.raises(ArrivalNeeded):
        台.desk.resolve(a.key, who="张三", verdict="false_alarm")
    台.c.ms += 60_000
    b = 台.desk.arrive(a.key, who="张三")
    assert b.context["arrived_by"] == "张三" and b.context["arrived_ms"] == 台.c.ms
    assert b.acked_by == "张三", "到场了当然算有人管:升级停下"
    assert 台.desk.resolve(a.key, who="张三", verdict="handled").resolved_ms is not None


def test_到场_别的告警不用_重复点不改第一次_空名字不行(台):
    s = 台.desk.raise_alert(kind="stuck", robot="A", title="卡住了")
    assert 台.desk.resolve(s.key, who="张三").resolved_ms is not None, "别的告警照旧"
    a = _看见人(台.desk)
    first = 台.desk.arrive(a.key, who="张三").context["arrived_ms"]
    台.c.ms += 1_000
    assert 台.desk.arrive(a.key, who="李四").context["arrived_ms"] == first
    with pytest.raises(ValueError):
        台.desk.arrive(_看见人(台.desk).key, who="")


# ------------------------------------------------- 看见人:去向不明、最后位置、10 分钟升级


class 假派遣:
    def __init__(self):
        self.clients = {"A": SimpleNamespace(
            status=SimpleNamespace(online=True, task=None),
            capabilities=SimpleNamespace(tasks={"persons": {"state": "ok", "present": False}}),
            telemetry=SimpleNamespace(pose=SimpleNamespace(map_id="m", map_version="1",
                                                           x=3.0, y=4.0, yaw=0.5)))}

    def persons(self, **kw):
        self.clients["A"].capabilities.tasks["persons"] = {"state": "ok", **kw}

    def at(self, x, y):
        p = self.clients["A"].telemetry.pose
        p.x, p.y = x, y


@pytest.fixture
def 看(台):
    from nexwar_site.modes import ArmingDesk
    from nexwar_site.sightings import PersonWatch
    d = 假派遣()
    w = PersonWatch(台.db, d, now_ms=台.c, arming=ArmingDesk(台.db, now_ms=台.c),
                    deterrence=SimpleNamespace(sessions={}))
    w.alerts = 台.desk
    return SimpleNamespace(**vars(台), d=d, w=w)


def _开着的(t, kind="dog_sees_person"):
    return [a for a in t.desk.book.open() if a.kind == kind]


def _记(t):
    return {r["robot_id"]: r["kind"] for r in t.db.query("SELECT * FROM person_sightings")}


def test_看见人_最后位置每5秒更新_不加次数(看):
    from nexwar_site.sightings import TRACK_EVERY_MS
    t = 看
    t.d.persons(present=True, count=1)
    t.w.tick()
    [a] = _开着的(t)
    assert a.context["last_seen"] == {"at_ms": t.c.ms, "map_id": "m", "map_version": "1",
                                      "x": 3.0, "y": 4.0, "yaw": 0.5}
    t.d.at(5.0, 6.0)
    t.c.ms += TRACK_EVERY_MS - 1
    t.w.tick()
    assert _开着的(t)[0].context["last_seen"]["x"] == 3.0, "5 秒内不刷"
    t.c.ms += 1
    t.w.tick()
    [a] = _开着的(t)
    assert a.context["last_seen"]["x"] == 5.0 and a.context["last_seen"]["at_ms"] == t.c.ms
    assert a.count == 1 and len(t.desk.book.all()) == 1


def test_人看不到了_不再自动收掉_改成去向不明_又看见改回来_还是同一条(看):
    t = 看
    t.d.persons(present=True, count=1)
    t.w.tick()
    [a] = _开着的(t)
    seen_at = a.context["last_seen"]["at_ms"]
    t.c.ms += 30_000
    t.d.persons(present=False)                           # 代理:连续 20 秒没看到
    t.w.tick()
    [b] = _开着的(t)
    assert b.key == a.key and "去向不明" in b.title and b.context["lost_ms"] == t.c.ms
    assert b.context["last_seen"]["at_ms"] == seen_at, "最后位置是最后看见时的"
    assert _记(t) == {"A": "lost"}, "这一回没完:记录留着"
    t.w.tick()
    assert _开着的(t)[0].last_ms == b.last_ms, "看不到了只改一次"
    t.c.ms += 10_000
    t.d.persons(present=True, count=1)
    t.w.tick()
    [c] = _开着的(t)
    assert c.key == a.key and "又看见" in c.title and c.context["lost_ms"] is None
    assert _记(t) == {"A": "p1"} and len(t.desk.book.all()) == 1


def test_保安到场处置完了_人走了才清记录_再看见另起一条(看):
    t = 看
    t.d.persons(present=True, count=1)
    t.w.tick()
    [a] = _开着的(t)
    t.desk.arrive(a.key, who="张三")
    t.desk.resolve(a.key, who="张三", verdict="handled")
    t.w.tick()
    assert _开着的(t) == [] and _记(t) == {"A": "p1"}, "人还在:不重报"
    t.d.persons(present=False)
    t.w.tick()
    assert _记(t) == {}, "关了、人也走了:这一回完了"
    t.d.persons(present=True, count=1)
    t.w.tick()
    [b] = _开着的(t)
    assert b.key != a.key


def test_去向不明时保安关了_记录清掉(看):
    t = 看
    t.d.persons(present=True, count=1)
    t.w.tick()
    t.d.persons(present=False)
    t.w.tick()
    [a] = _开着的(t)
    t.desk.arrive(a.key, who="张三")
    t.desk.resolve(a.key, who="张三", verdict="false_alarm")
    t.w.tick()
    assert _记(t) == {}


def test_10分钟没人到场_报经理一次_到场了就收掉(看):
    from nexwar_site.alert_audience import audience
    from nexwar_site.sightings import OVERDUE_MIN
    t = 看
    t.d.persons(present=True, count=1)
    t.w.tick()
    [a] = _开着的(t)
    t.c.ms += OVERDUE_MIN * 60_000 - 1
    t.w.tick()
    assert _开着的(t, "person_overdue") == []
    t.c.ms += 1
    t.d.persons(present=False)
    t.w.tick()
    t.w.tick()
    [o] = _开着的(t, "person_overdue")
    assert o.level.value == "P1" and o.context["alert"] == a.key and "没人到场" in o.title
    assert audience("person_overdue") == "owner", "给经理(管理员那一类)"
    assert len([x for x in t.desk.book.all() if x.kind == "person_overdue"]) == 1, "只报一次"
    t.desk.arrive(a.key, who="张三")
    t.w.tick()
    assert _开着的(t, "person_overdue") == [], "有人到场了:经理那条收掉"


def test_到场早于10分钟_不报经理(看):
    from nexwar_site.sightings import OVERDUE_MIN
    t = 看
    t.d.persons(present=True, count=1)
    t.w.tick()
    t.desk.arrive(_开着的(t)[0].key, who="张三")
    t.c.ms += OVERDUE_MIN * 60_000 + 1
    t.w.tick()
    assert _开着的(t, "person_overdue") == []


# ------------------------------------------------------------ 相邻防区(加密巡查用)


def test_相邻防区_边界相距3米以内_自己在第一个_别的图不算(台):
    from nexwar_site.areas import NEIGHBOUR_M, AreaBook
    assert NEIGHBOUR_M == 3.0
    b = AreaBook(台.db, now_ms=台.c)
    sq = lambda x0, y0: [[x0, y0], [x0 + 4, y0], [x0 + 4, y0 + 4], [x0, y0 + 4]]  # noqa: E731
    b.set("m", "1", "大厅", sq(0, 0), by="t")
    b.set("m", "1", "东门", sq(6.5, 0), by="t")          # 隔 2.5 m
    b.set("m", "1", "仓库", sq(10, 10), by="t")          # 远
    b.set("m", "1", "走廊", sq(1, 4), by="t")            # 挨着(重叠一条边)
    b.set("m", "2", "别图", sq(0, 0), by="t")
    assert b.neighbours("m", "1", "大厅") == ["大厅", "东门", "走廊"]
    assert b.neighbours("m", "1", "没有") == ["没有"]


# ------------------------------------------------- 二期:跟人的轨迹进告警


def _事件(kind, **data):
    return SimpleNamespace(kind=kind, data=data)


def test_跟人的轨迹_进告警现场_人最后在哪_方向_轨迹有上限(看):
    from nexwar_site.sightings import PERSON_TRACK_KEEP
    t = 看
    t.w.on_event("A", _事件("follow_track", task_id="follow-1",
                            points=[{"x": 1.0, "y": 2.0, "at_ms": 1}]))
    assert _开着的(t) == [], "没有开着的告警:不记"
    t.d.persons(present=True, count=1)
    t.w.tick()
    t.w.on_event("A", _事件("follow_track", task_id="follow-1",
                            points=[{"x": 1.0, "y": 2.0, "at_ms": 1},
                                    {"x": 1.5, "y": 2.5, "at_ms": 2}],
                            last={"x": 1.5, "y": 2.5, "at_ms": 2}, heading_deg=45.0))
    [a] = _开着的(t)
    assert a.context["person_track"] == [[1.0, 2.0, 1], [1.5, 2.5, 2]]
    assert a.context["person"] == {"x": 1.5, "y": 2.5, "at_ms": 2, "heading_deg": 45.0}
    assert a.context["follow"] == "follow"
    many = [{"x": float(i), "y": 0.0, "at_ms": 10 + i} for i in range(PERSON_TRACK_KEEP + 50)]
    t.w.on_event("A", _事件("follow_track", task_id="follow-1", points=many))
    [a] = _开着的(t)
    assert len(a.context["person_track"]) == PERSON_TRACK_KEEP
    assert a.context["person_track"][-1][0] == float(PERSON_TRACK_KEEP + 49), "留最新的"
    assert a.count == 1, "不加次数"


def test_跟丢了_告警写跟丢_带最后位置和方向(看):
    t = 看
    t.d.persons(present=True, count=1)
    t.w.tick()
    t.w.on_event("A", _事件("follow_lost", task_id="follow-1", result="lost",
                            last={"x": 4.0, "y": 4.0, "at_ms": 9}, heading_deg=90.0))
    [a] = _开着的(t)
    assert a.context["follow"] == "lost"
    assert a.context["person"] == {"x": 4.0, "y": 4.0, "at_ms": 9, "heading_deg": 90.0}
    t.w.on_event("A", _事件("patrol_waypoint", task_id="x"))   # 别的事件不管
