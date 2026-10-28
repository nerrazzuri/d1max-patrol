"""运营可视化:定位历史(``loc_episodes``)—— 开一段 / 结束一段、去抖、接上刚结束的、转成另一种、
掉线 / 遥测断 / 站点重启怎么封、有界(每台狗、全站、保留天数)、查询过滤、接口、老库升上来。"""

from __future__ import annotations

import sqlite3

import pytest

from d1max_contract.messages import MapPose, Ready, Status, TaskState, TaskSummary, Telemetry
from d1max_site.db import SCHEMA_VERSION, SiteDB
from d1max_site.loc_history import (
    KEEP_MS,
    MAX_ROWS,
    MERGE_GAP_MS,
    PER_ROBOT,
    PRUNE_EVERY_MS,
    SILENT_MS,
    START_AFTER_MS,
    LocHistory,
)

OK = {"source": "bridge", "anchored": True, "sigma_m": 0.1, "reason": "", "localizer": "bridge"}


class 钟:
    def __init__(self) -> None:
        self.ms = 1_000_000_000

    def __call__(self) -> int:
        return self.ms


def _tele(loc, x=None, y=0.0, map_v="7"):
    pose = None if x is None else MapPose(map_id="m", map_version=map_v, frame_id="map", x=x,
                                          y=y, yaw=0.0)
    return Telemetry(stamp=1, pose=pose, battery_pct=80.0, task_state=None, loc_quality=1.0,
                     loc=loc)


def _lost(why="定位器给的位置是 3.0 秒前的"):
    return OK | {"reason": why, "anchored": False}


def _degraded(why="定位偏差可能到 0.60 m"):
    return OK | {"degraded": why, "sigma_m": 0.6}


def _status(running=True, online=True):
    task = TaskSummary(task_id="patrol-3", kind="patrol", state=TaskState.RUNNING) \
        if running else None
    return Status(online=online, boot_id="b", ready=Ready(True, True, True, True), control_epoch=1,
                  last_seen=1, task=task)


@pytest.fixture
def 台(tmp_path):
    c = 钟()
    db = SiteDB(tmp_path / "s.db")
    h = LocHistory(db, now_ms=c)
    yield c, h, db
    db.close()


def _拍(c, h, loc, n=1, dt=1000, x=None, rid="A"):
    for _ in range(n):
        c.ms += dt
        h.on_telemetry(rid, _tele(loc, x=x))


def test_丢了_连着几秒才开一段_恢复了封上_记地点原因任务(台):
    c, h, _ = 台
    h.on_status("A", _status())
    h.on_telemetry("A", _tele(OK, x=4.0, y=2.0))                 # 最后一个可信位置
    _拍(c, h, _lost(), 2)
    assert h.list() == [], "不到去抖时间:不记"
    _拍(c, h, _lost(), 2)
    [e] = h.list()
    assert e["ongoing"] and e["kind"] == "lost" and e["robot_id"] == "A"
    assert (e["x"], e["y"], e["map_id"], e["map_version"]) == (4.0, 2.0, "m", "7")
    assert e["cause"].startswith("定位器给的位置") and e["task_id"] == "patrol-3"
    assert e["start_ms"] == c.ms - 3000, "开始的时刻是第一次看见它不对的时刻"
    _拍(c, h, OK, 1)
    [e] = h.list()
    assert not e["ongoing"] and e["end_how"] == "recovered" and e["duration_s"] == 4


def test_闪一两拍不记(台):
    c, h, _ = 台
    _拍(c, h, _lost(), 2)
    _拍(c, h, OK, 1)
    _拍(c, h, _lost(), 2)
    assert h.list() == []


def test_变差_可信但degraded_也记_丢了转变差是两段(台):
    c, h, _ = 台
    _拍(c, h, _degraded(), 4)
    _拍(c, h, _lost(), 5)
    _拍(c, h, OK, 1)
    es = h.list()
    assert [e["kind"] for e in es] == ["lost", "degraded"]
    assert es[1]["end_how"] == "became_lost" and es[0]["end_how"] == "recovered"


def test_刚结束又来同样的_接着上一段_不拆成碎段(台):
    c, h, _ = 台
    _拍(c, h, _lost(), 4)
    _拍(c, h, OK, 1)
    c.ms += 1000
    _拍(c, h, _lost(), 4)                                         # 间隔远小于 MERGE_GAP_MS
    es = h.list()
    assert len(es) == 1 and es[0]["ongoing"]
    # 间隔够久才是新的一段
    _拍(c, h, OK, 1)
    c.ms += MERGE_GAP_MS + START_AFTER_MS + 1000
    _拍(c, h, _lost(), 4)
    assert len(h.list()) == 2


def test_老狗没有定位块_不猜_不记(台):
    c, h, _ = 台
    for _ in range(10):
        c.ms += 1000
        h.on_telemetry("A", _tele(None))
    assert h.list() == []


def test_掉线封上_遥测断了封上_站点重启封上(台, tmp_path):
    c, h, db = 台
    _拍(c, h, _lost(), 4)
    h.on_status("A", _status(online=False))
    assert h.list()[0]["end_how"] == "offline"
    _拍(c, h, _lost(), 4, rid="B")
    c.ms += SILENT_MS
    h.tick()
    assert h.list(robot="B")[0]["end_how"] == "no_data"
    _拍(c, h, _lost(), 4, rid="C")
    h2 = LocHistory(db, now_ms=c)                                 # 站点重启
    c_row = h2.list(robot="C")[0]
    assert not c_row["ongoing"] and c_row["end_how"] == "site_restart"


def test_每台狗的段数有上限_全站有上限_留存有期限(台):
    c, h, db = 台
    with db.tx() as cx:
        for i in range(PER_ROBOT + 20):
            cx.execute("INSERT INTO loc_episodes(robot_id, kind, start_ms, end_ms, end_how) "
                       "VALUES ('A','lost',?,?,'recovered')", (i, i + 1))
    _拍(c, h, _lost(), 4)                                         # 开新段时修剪这台狗的
    n = db.query("SELECT COUNT(*) n FROM loc_episodes WHERE robot_id='A'")[0]["n"]
    assert n == PER_ROBOT
    assert db.query("SELECT MIN(start_ms) m FROM loc_episodes WHERE robot_id='A'")[0]["m"] >= 20
    # 全站上限
    with db.tx() as cx:
        for i in range(MAX_ROWS + 50):
            cx.execute("INSERT INTO loc_episodes(robot_id, kind, start_ms, end_ms, end_how) "
                       "VALUES (?,'lost',?,?,'recovered')", (f"R{i % 40}", c.ms - 1000 + i, c.ms))
    c.ms += PRUNE_EVERY_MS
    h.tick()
    assert db.query("SELECT COUNT(*) n FROM loc_episodes")[0]["n"] == MAX_ROWS
    # 期限:太老的删,没结束的不删
    with db.tx() as cx:
        cx.execute("DELETE FROM loc_episodes")
        cx.execute("INSERT INTO loc_episodes(robot_id, kind, start_ms, end_ms) VALUES "
                   "('A','lost',1,2), ('A','lost',3,NULL)")
    c.ms += PRUNE_EVERY_MS + KEEP_MS
    h.tick()
    assert [r["start_ms"] for r in db.query("SELECT start_ms FROM loc_episodes")] == [3]


def test_查询_按狗_种类_图_时间过滤_数量有上限(台):
    c, h, db = 台
    with db.tx() as cx:
        rows = [("A", "lost", 100, 200, "m", "7"), ("A", "degraded", 300, 400, "m", "7"),
                ("B", "lost", 500, None, "m", "8")]
        for r in rows:
            cx.execute("INSERT INTO loc_episodes(robot_id, kind, start_ms, end_ms, map_id, "
                       "map_version) VALUES (?,?,?,?,?,?)", r)
    assert [e["start_ms"] for e in h.list()] == [500, 300, 100]
    assert [e["start_ms"] for e in h.list(robot="A")] == [300, 100]
    assert [e["start_ms"] for e in h.list(kind="degraded")] == [300]
    assert [e["start_ms"] for e in h.list(map_version="8")] == [500]
    assert [e["start_ms"] for e in h.list(since_ms=250)] == [500, 300]
    assert [e["start_ms"] for e in h.list(until_ms=250)] == [100]
    assert len(h.list(limit=2)) == 2 and len(h.list(limit=10**9)) == 3
    assert h.list(since_ms=450)[0]["ongoing"]


def test_老库升上来_表建出来_空的_版本号涨到当前(tmp_path):
    p = tmp_path / "old.db"
    SiteDB(p).close()
    con = sqlite3.connect(p)
    con.execute("DROP TABLE loc_episodes")
    con.execute("UPDATE meta SET value='57' WHERE key='schema'")
    con.commit()
    con.close()
    db = SiteDB(p)
    assert db.query("SELECT COUNT(*) n FROM loc_episodes")[0]["n"] == 0
    assert db.query("SELECT value FROM meta WHERE key='schema'")[0]["value"] == str(SCHEMA_VERSION)
    db.close()


def test_接口_要登录_过滤_坏参数400(tmp_path):
    import time

    from test_site_api import 站
    s = 站(tmp_path)
    try:
        h = LocHistory(s.db, now_ms=lambda: int(time.time() * 1000))
        s.api.loc_history = h
        assert s.req("GET", "/api/loc-episodes")[0] == 401
        tok = s.login()
        with s.db.tx() as cx:
            cx.execute("INSERT INTO loc_episodes(robot_id, kind, start_ms, end_ms, cause, x, y, "
                       "map_id, map_version) VALUES ('A','lost',1000,2000,'雷达被挡',1.5,2.5,"
                       "'m','7')")
        code, d = s.req("GET", "/api/loc-episodes?robot=A&kind=lost", token=tok)
        assert code == 200 and d["episodes"][0]["cause"] == "雷达被挡"
        assert d["episodes"][0]["x"] == 1.5 and d["episodes"][0]["duration_s"] == 1
        assert s.req("GET", "/api/loc-episodes?kind=nope", token=tok)[0] == 400
        assert s.req("GET", "/api/loc-episodes?since_ms=abc", token=tok)[0] == 400
        assert s.req("POST", "/api/loc-episodes", {}, token=tok)[0] == 405
        assert s.req("GET", "/api/loc-episodes?robot=B", token=tok)[1]["episodes"] == []
    finally:
        s.close()


def test_W41_最初的原因不丢_闪一下被打断的写在前面_开着时换了说法接在后面(台):
    """2026-10-26 现场:盖住雷达那一次只剩后来的「刚跳过…等它稳下来」,最初的「雷达被挡」没了。"""
    c, h, _ = 台
    _拍(c, h, _lost("雷达被挡住了"), 2)
    _拍(c, h, _degraded(), 1)                                      # 闪了一下变差
    _拍(c, h, _lost("刚跳过,等它稳下来"), 4)
    [e] = h.list()
    assert e["cause"] == "雷达被挡住了;后来:刚跳过,等它稳下来", e["cause"]
    _拍(c, h, _lost("匹配在来回跳"), 1)
    _拍(c, h, _lost("匹配在来回跳"), 1)
    [e] = h.list()
    assert e["cause"] == "雷达被挡住了;后来:刚跳过,等它稳下来;后来:匹配在来回跳", e["cause"]
    _拍(c, h, _lost("x" * 190), 1)
    assert h.list()[0]["cause"] == e["cause"], "太长了不接"
    # 刚结束又来、接着上一段:新说法也接上
    _拍(c, h, OK, 1)
    _拍(c, h, _lost("又被挡"), 4)
    [e] = h.list()
    assert e["ongoing"] and e["cause"].endswith(";后来:又被挡"), e["cause"]


def test_W41_被打断的那一小段太久以前_不往前接(台):
    c, h, _ = 台
    _拍(c, h, _lost("雷达被挡住了"), 2)
    _拍(c, h, OK, 1)
    c.ms += MERGE_GAP_MS + 1000
    _拍(c, h, _lost("刚跳过"), 4)
    [e] = h.list()
    assert e["cause"] == "刚跳过"


def test_工单13_只是数字变了的原因合成一段_不刷屏():
    """2026-10-28 3z.6:「…;后来:定位偏差可能到 1.5 m;后来:1.3 m;后来:1.2 m;…」
    只是数字变也接一句。"""
    from d1max_site.loc_history import _join
    s = "雷达被挡住了"
    for v in ("在按最后的位置重定位", "定位偏差可能到 1.5 m", "定位偏差可能到 1.3 m",
              "定位偏差可能到 1.2 m", "定位偏差可能到 0.7 m"):
        s = _join(s, v)
    assert s == "雷达被挡住了;后来:在按最后的位置重定位;后来:定位偏差可能到 1.5→0.7 m"
    assert _join(s, "定位偏差可能到 0.7 m") == s
    assert _join("a 1 b 2", "a 3 b 2") == "a 1→3 b 2", "只改变了的那个数"
    assert _join("偏差 1 m", "人给的位置差 3 m").endswith(";后来:人给的位置差 3 m"), "字不一样照接"
