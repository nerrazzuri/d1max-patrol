"""巡检路线库(2026-10-14,用户定:站点存的路线、排程是唯一真理源):存取与校验(修订号、并发、逐点
可达)、排程(时区、起算时刻)、老库迁移、任务包导入(按名字并进)与导出(能原样导回)、权限与审计、
派遣器与排程执行器读的就是库。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_site_api import MAP, PW, 站
from test_site_intercept_reach import _屋子
from test_site_maps import _files
from test_site_schedule import 任务, 排程, 毫秒

from d1max_contract.bundle_format import SCHEDULE_NAME, build_bundle
from d1max_site.catalog import (
    CatalogError,
    active_bundle,
    active_bundle_schema,
    import_bundle,
    import_summary,
)
from d1max_site.db import SCHEMA_VERSION, SiteDB
from d1max_site.dispatcher import MAX_PATROL_BYTES, MAX_PATROL_WAYPOINTS
from d1max_site.maps import MapCatalog
from d1max_site.routes import (
    MAX_BYTES,
    MAX_WAYPOINTS,
    RouteConflict,
    RouteError,
    RouteNotFound,
    RouteStore,
)

SNAP = Path(__file__).parent / "fixtures" / "schema"


def pt(name: str, x: float, y: float, yaw: float = 0.0, **more) -> dict:
    return {"name": name, "x": x, "y": y, "yaw": yaw, **more}


GOOD = [pt("门", 1.0, 2.0), pt("后院", 6.0, 2.0, 1.57, check="看围栏",
                              actions=[{"type": "dwell", "seconds": 3},
                                       {"type": "photo", "camera": "front"}])]


def body(version: str = "wide", pts=None, base: int = 0, **more) -> dict:
    return {"map_id": "house", "map_version": version,
            "waypoints": GOOD if pts is None else pts, "base_revision": base, **more}


@pytest.fixture
def 台(tmp_path):
    t = [1_000_000]
    db = SiteDB(tmp_path / "site.db")
    cat = MapCatalog(tmp_path, db, now_ms=lambda: 1000)
    src = _files(tmp_path / "src")
    _屋子(src, 1.6)
    cat.import_dir(src, map_id="house", version="wide")
    cat.import_dir(_files(tmp_path / "src2", **{"a.pgm": b"x"}), map_id="house", version="old")
    store = RouteStore(db, maps=cat, now_ms=lambda: t[0])
    yield type("T", (), {"db": db, "cat": cat, "store": store, "t": t, "tmp": tmp_path})
    db.close()


# ------------------------------------------------------------ 存取、修订号、并发


def test_新建_读回_平的点_修订号从1起(台):
    r = 台.store.put_route("night", body(), by="alice")
    assert (r["id"], r["revision"], r["map_version"], r["bound"]) == ("night", 1, "wide", True)
    assert [(w["name"], w["x"], w["y"]) for w in r["waypoints"]] == [("门", 1.0, 2.0),
                                                                    ("后院", 6.0, 2.0)]
    assert r["waypoints"][1]["yaw"] == pytest.approx(1.57, abs=1e-6)
    assert r["waypoints"][1]["actions"] == [{"type": "dwell", "seconds": 3},
                                            {"type": "photo", "camera": "front"}]
    assert r["waypoints"][1]["check"] == "看围栏"
    assert r["updated_by"] == "alice" and r["problems"] == []
    assert 台.store.view()["revision"] == 1
    # 派遣器读的是契约线格式,点是四元数,名字就是路线名
    m = active_bundle(台.db).missions["night"]
    assert m.mission == "night" and m.map_version == "wide"
    assert m.waypoints[1].pose.yaw == pytest.approx(1.57, abs=1e-6)


def test_改_带基准修订号_对不上不许盖_新建撞名也是冲突(台):
    s = 台.store
    s.put_route("night", body(), by="alice")
    with pytest.raises(RouteConflict, match="已经被改过"):
        s.put_route("night", body(base=0), by="bob")              # 新建撞了现有的
    r = s.put_route("night", body(pts=[pt("门", 1.0, 2.0)], base=1), by="bob")
    assert r["revision"] == 2 and len(r["waypoints"]) == 1
    with pytest.raises(RouteConflict, match="第 2 版.*第 1 版"):
        s.put_route("night", body(base=1), by="alice")             # alice 看的是旧的
    assert s.get("night")["revision"] == 2 and s.get("night")["updated_by"] == "bob"
    with pytest.raises(RouteConflict, match="已经被删"):
        s.put_route("ghost", body(base=3), by="alice")
    for bad in (None, "1", True, -1, 1.5):
        with pytest.raises(RouteError, match="base_revision"):
            s.put_route("night", body(base=bad), by="alice")


def test_没有的路线_404类(台):
    with pytest.raises(RouteNotFound):
        台.store.get("ghost")
    with pytest.raises(RouteNotFound):
        台.store.delete_route("ghost", base_revision=1, by="a")


def test_按地图版本筛(台):
    s = 台.store
    s.put_route("a", body(), by="x")
    with 台.db.tx() as c:                                       # 另一版地图上的路线(迁移来的老路线)
        c.execute("UPDATE routes SET map_version='old'")
    s.put_route("b", body(), by="x")
    assert [r["id"] for r in s.view("house", "wide")["routes"]] == ["b"]
    assert [r["id"] for r in s.view("house", "old")["routes"]] == ["a"]
    assert len(s.view()["routes"]) == 2


def test_删路线_修订号对得上才删_还有排程用不许删(台):
    s = 台.store
    s.put_route("night", body(), by="alice")
    s.set_timezone("Asia/Kuala_Lumpur", by="alice")
    s.put_schedule("s1", {"mission": "night", "at": "22:00", "days": ["mon"], "window_min": 30,
                          "on_missed": "skip", "base_revision": 0}, by="alice")
    with pytest.raises(RouteConflict):
        s.delete_route("night", base_revision=9, by="alice")
    with pytest.raises(RouteError, match="s1 还在用"):
        s.delete_route("night", base_revision=1, by="alice")
    assert "night" in active_bundle(台.db).missions, "没删"
    s.delete_schedule("s1", base_revision=1, by="alice")
    s.delete_route("night", base_revision=1, by="alice")
    assert s.view()["routes"] == [] and active_bundle(台.db) is None


# ------------------------------------------------------------ 校验


def test_逐点校验_墙里_太远_都报_带序号(台):
    pts = [pt("好", 1.0, 2.0), pt("墙里", 4.0, 0.5), pt("好2", 2.0, 2.0), pt("图外", 20.0, 2.0)]
    with pytest.raises(RouteError) as ei:
        台.store.put_route("r", body(pts=pts), by="a")
    errs = ei.value.errors
    assert [(e["index"], e["name"]) for e in errs] == [(1, "墙里"), (3, "图外")], errs
    assert "不是可通行" in errs[0]["problem"] and "走过的地方" in errs[1]["problem"]
    assert 台.store.view()["routes"] == [], "有错的不存"


def test_地图版本必填_站点要有这一版(台):
    s = 台.store
    with pytest.raises(RouteError, match="map_version 必填"):
        s.put_route("r", {"map_id": "house", "waypoints": GOOD, "base_revision": 0}, by="a")
    with pytest.raises(RouteError, match="map_version 必填"):
        s.put_route("r", body(version=""), by="a")
    with pytest.raises(RouteError) as ei:
        s.put_route("r", body(version="99"), by="a")
    assert "站点没有地图 house:99" in ei.value.errors[0]["problem"]
    # 没有 coverage 的老版本不查可达(同派遣器),但版本得存在
    assert s.put_route("r", body(version="old"), by="a")["map_version"] == "old"


def test_点的形状_名字_重名_上限_陌生字段(台):
    s = 台.store
    with pytest.raises(RouteError) as ei:
        s.put_route("r", body(pts=[pt("好", 1.0, 2.0), {"name": "坏", "x": "1", "y": 2}]), by="a")
    assert ei.value.errors[0]["index"] == 1
    for badv in (float("nan"), float("inf"), True):
        with pytest.raises(RouteError):
            s.put_route("r", body(pts=[pt("a", badv, 2.0)]), by="a")
    with pytest.raises(RouteError, match="点位名重复"):
        s.put_route("r", body(pts=[pt("a", 1.0, 2.0), pt("a", 2.0, 2.0)]), by="a")
    with pytest.raises(RouteError, match="点位名不能含"):
        s.put_route("r", body(pts=[pt("a/b", 1.0, 2.0)]), by="a")
    with pytest.raises(RouteError, match="动作类型不认识"):
        s.put_route("r", body(pts=[pt("a", 1.0, 2.0, actions=[{"type": "teleport"}])]), by="a")
    with pytest.raises(RouteError, match="waypoints"):
        s.put_route("r", body(pts=[]), by="a")
    with pytest.raises(RouteError, match="不认识的字段"):
        s.put_route("r", body(route={"source": "vendor_path"}), by="a")
    for badname in ("", "a b", "../x", "路线", "x" * 65, ".hidden"):
        with pytest.raises(RouteError, match="路线名"):
            s.put_route(badname, body(), by="a")


def test_上限跟派遣器一样(台):
    assert (MAX_WAYPOINTS, MAX_BYTES) == (MAX_PATROL_WAYPOINTS, MAX_PATROL_BYTES)
    many = [pt(f"p{i}", 1.0 + (i % 50) * 0.1, 2.0) for i in range(MAX_WAYPOINTS + 1)]
    with pytest.raises(RouteError, match="最多 500"):
        台.store.put_route("r", body(pts=many), by="a")
    huge = [pt("p", 1.0, 2.0, check="字" * (MAX_BYTES + 1))]
    with pytest.raises(RouteError, match="字节"):
        台.store.put_route("r", body(pts=huge), by="a")


def test_策略能带上_存了读回(台):
    r = 台.store.put_route("r", body(policy={"loops": 2, "order": "shuffle"}), by="a")
    assert r["policy"]["loops"] == 2 and r["policy"]["order"] == "shuffle"
    with pytest.raises(RouteError, match="loops"):
        台.store.put_route("r2", body(policy={"loops": 0}), by="a")


# ------------------------------------------------------------ 排程


def _sched(**kw):
    return {"mission": "night", "at": "22:00", "days": ["mon", "tue"], "window_min": 30,
            "on_missed": "skip", "base_revision": 0, **kw}


def test_排程_先设时区_路线要存在_语义照契约(台):
    s = 台.store
    s.put_route("night", body(), by="a")
    with pytest.raises(RouteError, match="时区"):
        s.put_schedule("s1", _sched(), by="a")
    with pytest.raises(RouteError, match="认不出来"):
        s.set_timezone("Mars/Base", by="a")
    s.set_timezone("Asia/Kuala_Lumpur", by="a")
    with pytest.raises(RouteError, match="不存在"):
        s.put_schedule("s1", _sched(mission="ghost"), by="a")
    with pytest.raises(RouteError, match="window_min"):
        s.put_schedule("s1", _sched(window_min=0), by="a")
    with pytest.raises(RouteError, match="on_missed"):
        s.put_schedule("s1", _sched(on_missed="maybe"), by="a")
    v = s.put_schedule("s1", _sched(priority=3, robot="A", every_min=60, until="23:30",
                                    window_min=20), by="a")
    [e] = v["schedules"]
    assert (e["id"], e["revision"], e["priority"], e["robot"], e["every_min"]) \
        == ("s1", 1, 3, "A", 60)
    act = active_bundle(台.db)
    assert act.schedule.timezone == "Asia/Kuala_Lumpur" and act.schedule.entries[0].id == "s1"


def test_排程_起算时刻_新加和改了从现在起_没改不动_换时区全部重算(台):
    s, t = 台.store, 台.t
    s.put_route("night", body(), by="a")
    s.set_timezone("Asia/Kuala_Lumpur", by="a")
    t[0] = 2_000_000
    s.put_schedule("s1", _sched(), by="a")
    act = active_bundle(台.db)
    assert act.since("s1") == 2_000_000
    t[0] = 3_000_000
    s.put_schedule("s1", _sched(base_revision=1), by="a")        # 一样的内容:不算改
    assert active_bundle(台.db).since("s1") == 2_000_000
    assert s.view()["schedules"][0]["revision"] == 1
    s.put_schedule("s1", _sched(base_revision=1, at="23:00"), by="a")
    assert active_bundle(台.db).since("s1") == 3_000_000
    assert s.view()["schedules"][0]["revision"] == 2
    with pytest.raises(RouteConflict):
        s.put_schedule("s1", _sched(base_revision=1, at="23:30"), by="b")
    t[0] = 4_000_000
    s.set_timezone("Asia/Kuala_Lumpur", by="a")                    # 没换
    assert active_bundle(台.db).since("s1") == 3_000_000
    s.set_timezone("UTC", by="a")
    assert active_bundle(台.db).since("s1") == 4_000_000
    with pytest.raises(RouteNotFound):
        s.delete_schedule("nope", base_revision=1, by="a")
    with pytest.raises(RouteConflict):
        s.delete_schedule("s1", base_revision=1, by="a")


# ------------------------------------------------------------ 老库迁移


def _v57(tmp_path, name="old.db") -> Path:
    p = tmp_path / name
    con = sqlite3.connect(p)
    con.executescript((SNAP / "site-v57.sql").read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return p


def _老包(con, bid, ver, active, missions, sched, at):
    con.execute("INSERT INTO bundles(bundle_id, version, content_sha256, timezone, schedule, "
                "imported_at, imported_by, active, schema) VALUES (?,?,?,?,?,?,?,?,2)",
                (bid, ver, "0" * 64, sched["timezone"], json.dumps(sched), at, "cli:old", active))
    for mid, d in missions.items():
        con.execute("INSERT INTO missions VALUES (?,?,?,?)", (bid, ver, mid, json.dumps(d)))


def test_迁移_当前包抄进库_排程从那次导入起算_老包不抄(台, tmp_path):
    p = _v57(tmp_path)
    con = sqlite3.connect(p)
    sch = {"timezone": "Asia/Kuala_Lumpur", "entries": [
        {"id": "nightly", "mission": "loop", "at": "22:00", "days": ["mon"], "window_min": 30,
         "on_missed": "skip", "priority": 0}]}
    _老包(con, "est", 1, 0, {"gone": dict(任务, mission="gone")}, sch, 100)
    _老包(con, "est", 2, 1, {"loop": 任务, "unbound": {k: v for k, v in dict(
        任务, mission="unbound").items() if k != "map_version"}}, sch, 5000)
    con.commit()
    con.close()
    db = SiteDB(p)
    try:
        v = RouteStore(db, now_ms=lambda: 1).view()
        assert [(r["id"], r["map_version"], r["bound"]) for r in v["routes"]] \
            == [("loop", "7", True), ("unbound", "", False)], "没绑版本的老任务照抄、标出来"
        assert v["timezone"] == "Asia/Kuala_Lumpur"
        [e] = v["schedules"]
        assert e["id"] == "nightly" and e["active_from_ms"] == 5000 and e["updated_by"] == "cli:old"
        act = active_bundle(db)
        assert act.since("nightly") == 5000 and act.missions["loop"].map_version == "7"
        assert db.query("SELECT value FROM meta WHERE key='schema'")[0][0] == str(SCHEMA_VERSION)
        assert active_bundle_schema(db) == ("est v2", 2), "升级前检查照旧看最近导入的包"
        assert db.query("SELECT count(*) FROM bundles")[0][0] == 2, "导入记录留着"
    finally:
        db.close()
    db = SiteDB(p)                                                # 重开不重抄
    try:
        assert len(RouteStore(db, now_ms=lambda: 1).view()["routes"]) == 2
    finally:
        db.close()


def test_迁移_没有当前包就是空库_没带排程文件的包不占时区(tmp_path):
    p = _v57(tmp_path)
    db = SiteDB(p)
    try:
        assert active_bundle(db) is None
        assert RouteStore(db, now_ms=lambda: 1).view()["timezone"] is None
    finally:
        db.close()
    p2 = _v57(tmp_path, "second.db")
    con = sqlite3.connect(p2)
    _老包(con, "est", 1, 1, {"loop": 任务}, {"timezone": "UTC", "entries": []}, 9)   # 占位的 UTC
    con.commit()
    con.close()
    db = SiteDB(p2)
    try:
        v = RouteStore(db, now_ms=lambda: 1).view()
        assert v["timezone"] is None and v["schedules"] == [] and len(v["routes"]) == 1
    finally:
        db.close()


# ------------------------------------------------------------ 任务包导入、导出


def _打(tmp_path, version, tag, missions: dict, sched: str | None, bid="estate-kl"):
    src = tmp_path / f"s-{tag}"
    (src / "missions").mkdir(parents=True)
    for k, v in missions.items():
        (src / "missions" / f"{k}.json").write_text(json.dumps(v), encoding="utf-8")
    if sched is not None:
        (src / SCHEDULE_NAME).write_text(sched, encoding="utf-8")
    return build_bundle(src, tmp_path / f"o-{tag}", bundle_id=bid, version=version,
                        built_at="2026-10-14T10:00:00+08:00")


def test_导入_按名字并进_留着没提到的_摘要说清楚(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    other = dict(任务, mission="other")
    two = 排程 + """\
  - id: dawn
    mission: other
    at: "05:00"
    days: [sat]
    window_min: 20
    on_missed: alarm
"""
    got = import_bundle(db, _打(tmp_path, 1, "a", {"loop": 任务, "other": other}, two),
                        imported_by="alice", now_ms=10)
    assert got["routes"]["added"] == ["loop", "other"] and sorted(got["schedules"]["added"]) \
        == ["dawn", "nightly"]
    rev1 = RouteStore(db, now_ms=lambda: 1).view()["revision"]
    # 第二个包:只带 loop(改了一点)和一条新排程;other、dawn、nightly 不在里面
    loop2 = dict(任务, waypoints=任务["waypoints"][:1])
    one = """\
timezone: Asia/Kuala_Lumpur
entries:
  - id: noon
    mission: loop
    at: "12:00"
    days: [sun]
    window_min: 30
    on_missed: skip
"""
    got = import_bundle(db, _打(tmp_path, 2, "b", {"loop": loop2}, one),
                        imported_by="bob", now_ms=20)
    assert got["routes"] == {"added": [], "replaced": ["loop"], "unchanged": []}
    assert got["schedules"]["added"] == ["noon"]
    assert got["kept_routes"] == ["other"] and got["kept_schedules"] == ["dawn", "nightly"], \
        "站点上原有的排程绝不悄悄丢"
    s = import_summary(got)
    assert "替换 loop" in s and "留着的路线 other" in s and "留着的排程 dawn, nightly" in s
    st = RouteStore(db, now_ms=lambda: 1)
    assert [r["id"] for r in st.view()["routes"]] == ["loop", "other"]
    assert len(st.get("loop")["waypoints"]) == 1 and st.get("loop")["revision"] == 2
    assert st.view()["revision"] > rev1
    assert st.view()["last_import"]["by"] == "bob" and st.view()["last_import"]["version"] == 2
    # 原样再导一份更高版本:没变的不动修订号
    got = import_bundle(db, _打(tmp_path, 3, "c", {"loop": loop2}, one), imported_by="bob",
                        now_ms=30)
    assert got["routes"]["unchanged"] == ["loop"] and got["schedules"]["unchanged"] == ["noon"]
    assert got["changed"] is False and st.get("loop")["revision"] == 2
    db.close()


def test_导入_没带排程文件_排程时区都不动_带了不同时区而有留着的排程_拒(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    import_bundle(db, _打(tmp_path, 1, "a", {"loop": 任务}, 排程), imported_by="a", now_ms=10)
    got = import_bundle(db, _打(tmp_path, 2, "b", {"loop": 任务}, None), imported_by="a", now_ms=20)
    assert got["schedule_entries"] == [] and got["timezone"] is None
    assert got["kept_schedules"] == ["nightly"]
    assert RouteStore(db, now_ms=lambda: 1).view()["timezone"] == "Asia/Kuala_Lumpur"
    utc = "timezone: UTC\nentries: []\n"
    with pytest.raises(CatalogError, match="nightly.*换时区"):
        import_bundle(db, _打(tmp_path, 3, "c", {"loop": 任务}, utc), imported_by="a", now_ms=30)
    assert RouteStore(db, now_ms=lambda: 1).view()["timezone"] == "Asia/Kuala_Lumpur"
    assert db.query("SELECT count(*) FROM bundles")[0][0] == 2, "拒掉的整个回滚"
    db.close()


def test_导入_改了钟点的排程重新起算_排程提到的路线库里有也行(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    import_bundle(db, _打(tmp_path, 1, "a", {"loop": 任务}, 排程), imported_by="a", now_ms=10)
    later = 排程.replace('"22:00"', '"23:00"')
    got = import_bundle(db, _打(tmp_path, 2, "b", {}, later), imported_by="a", now_ms=500)
    assert got["schedules"]["replaced"] == ["nightly"]
    assert active_bundle(db).since("nightly") == 500
    ghost = 排程.replace("mission: loop", "mission: ghost")
    with pytest.raises(CatalogError, match="ghost"):
        import_bundle(db, _打(tmp_path, 3, "c", {}, ghost), imported_by="a", now_ms=600)
    db.close()


def test_导入_文件名不合规矩_拒(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    with pytest.raises(CatalogError, match="任务文件名"):
        import_bundle(db, _打(tmp_path, 1, "a", {"夜间": 任务}, None), imported_by="a", now_ms=1)
    db.close()


def test_导出再导入_原样回来_自己导出的包站点收得下(台):
    s = 台.store
    s.put_route("night", body(policy={"loops": 2}), by="alice")
    s.put_route("day", body(pts=[pt("a", 2.0, 2.0)]), by="alice")
    s.set_timezone("Asia/Kuala_Lumpur", by="alice")
    s.put_schedule("s1", _sched(priority=2, every_min=60, until="23:30", window_min=20), by="alice")
    out = s.export_bundle(台.tmp / "exp", by="alice")
    assert out["routes"] == ["day", "night"] and out["schedules"] == ["s1"]
    assert Path(out["path"]).name == f"site-routes-{out['version']}"
    db2 = SiteDB(台.tmp / "other.db")
    got = import_bundle(db2, Path(out["path"]), imported_by="cli:x", now_ms=77)
    assert got["routes"]["added"] == ["day", "night"] and got["schedules"]["added"] == ["s1"]
    a, b = s.view(), RouteStore(db2, now_ms=lambda: 1).view()
    assert a["timezone"] == b["timezone"] == "Asia/Kuala_Lumpur"
    assert [{k: r[k] for k in ("id", "map_id", "map_version", "waypoints", "policy")}
            for r in a["routes"]] == [{k: r[k] for k in ("id", "map_id", "map_version",
                                                         "waypoints", "policy")}
                                      for r in b["routes"]]
    keep = ("id", "mission", "at", "days", "window_min", "on_missed", "priority", "every_min",
            "until")
    assert [{k: e[k] for k in keep} for e in a["schedules"]] == [{k: e[k] for k in keep}
                                                                 for e in b["schedules"]]
    # 同一个站点再导自己的包:版本号只许往上走,下一次导出的比已导入的大
    s2 = RouteStore(db2, now_ms=lambda: 1)
    out2 = s2.export_bundle(台.tmp / "exp2", by="x")
    assert out2["version"] > got["version"]
    db2.close()


def test_导出_没绑版本的拒_空库拒(台):
    with pytest.raises(RouteError, match="没什么可导出"):
        台.store.export_bundle(台.tmp / "e", by="a")
    台.store.put_route("night", body(), by="a")
    with 台.db.tx() as c:
        c.execute("UPDATE routes SET map_version=''")
    with pytest.raises(RouteError, match="night 没绑地图版本"):
        台.store.export_bundle(台.tmp / "e", by="a")


# ------------------------------------------------------------ 接口:权限、审计、错误码


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    src = _files(tmp_path / "mapsrc")
    _屋子(src, 1.6)
    s.maps.import_dir(src, map_id=MAP[0], version=MAP[1])
    s.api.home = tmp_path / "home"
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _b(**kw):
    return {"map_id": MAP[0], "map_version": MAP[1], "waypoints": GOOD, "base_revision": 0, **kw}


def test_接口_权限_看谁都能_改只管理员(站点):
    s = 站点
    alice, gina, olga = _登(s, "alice"), _登(s, "gina"), _登(s, "olga")
    assert s.req("GET", "/api/routes")[0] == 401
    code, d = s.req("POST", "/api/routes/night", _b(), token=alice)
    assert code == 200 and d["revision"] == 1, d
    for tok in (gina, olga):
        assert s.req("POST", "/api/routes/night", _b(base_revision=1), token=tok)[0] == 403
        assert s.req("POST", "/api/routes/night/delete", {"base_revision": 1}, token=tok)[0] == 403
        assert s.req("POST", "/api/route-timezone", {"timezone": "UTC"}, token=tok)[0] == 403
        assert s.req("POST", "/api/route-schedules/s1", _sched(), token=tok)[0] == 403
        assert s.req("POST", "/api/route-export", {}, token=tok)[0] == 403
        code, d = s.req("GET", "/api/routes", token=tok)
        assert code == 200 and [r["id"] for r in d["routes"]] == ["night"]
        assert s.req("GET", "/api/routes/night", token=tok)[0] == 200
        assert s.req("GET", "/api/route-schedules", token=tok)[0] == 200
    assert s.req("GET", "/api/routes/ghost", token=gina)[0] == 404
    got = s.req("GET", f"/api/routes?map_id={MAP[0]}&map_version=999", token=gina)[1]
    assert got["routes"] == []


def test_接口_错误码_400带逐点_409冲突_审计(站点):
    s = 站点
    alice = _登(s, "alice")
    bad = [pt("墙里", 4.0, 0.5), pt("好", 1.0, 2.0), pt("图外", 20.0, 2.0)]
    code, d = s.req("POST", "/api/routes/night", _b(waypoints=bad), token=alice)
    assert code == 400 and [e["index"] for e in d["errors"]] == [0, 2], d
    assert s.req("POST", "/api/routes/night", _b(), token=alice)[0] == 200
    code, d = s.req("POST", "/api/routes/night", _b(), token=alice)         # 基准 0 对不上了
    assert code == 409 and "已经被改过" in d["error"]
    assert s.req("POST", "/api/routes/night", {"map_id": MAP[0], "waypoints": GOOD,
                                              "base_revision": 1}, token=alice)[0] == 400
    code, d = s.req("POST", "/api/route-schedules/s1", _sched(), token=alice)
    assert code == 400 and "时区" in d["error"]
    assert s.req("POST", "/api/route-timezone", {"timezone": "Asia/Kuala_Lumpur"},
                 token=alice)[0] == 200
    code, d = s.req("POST", "/api/route-schedules/s1", _sched(), token=alice)
    assert code == 200 and d["schedules"][0]["id"] == "s1"
    code, d = s.req("POST", "/api/routes/night/delete", {"base_revision": 1}, token=alice)
    assert code == 400 and "s1 还在用" in d["error"]
    assert s.req("POST", "/api/route-schedules/s1/delete", {"base_revision": 1},
                 token=alice)[0] == 200
    assert s.req("POST", "/api/routes/night/delete", {"base_revision": 1}, token=alice)[0] == 200
    acts = [r["action"] for r in s.db.query("SELECT action FROM audit ORDER BY id")]
    for want in ("POST /api/routes/night", "POST /api/routes/night/delete",
                 "POST /api/route-timezone", "POST /api/route-schedules/s1",
                 "POST /api/route-schedules/s1/delete"):
        assert want in acts, (want, acts)
    row = s.db.query("SELECT target, status FROM audit WHERE action='POST /api/routes/night' "
                     "ORDER BY id")[0]
    assert (row["target"], row["status"]) == ("night", 400)


def test_接口_导入有摘要_导出落在站点目录_再导入收得下(站点, tmp_path):
    s = 站点
    alice = _登(s, "alice")
    pkg = _打(tmp_path, 1, "api", {"loop": 任务}, 排程)
    code, d = s.req("POST", "/api/bundles", {"path": str(pkg)}, token=alice)
    assert code == 200 and d["routes"]["added"] == ["loop"] and "路线 新加 loop" in d["summary"], d
    assert s.req("GET", "/api/routes/loop", token=alice)[1]["map_version"] == "7"
    assert s.req("GET", "/api/route-schedules", token=alice)[1]["schedules"][0]["id"] == "nightly"
    code, d = s.req("POST", "/api/route-export", {}, token=alice)
    assert code == 200 and d["path"].startswith(str(s.api.home / "route-exports")), d
    assert (Path(d["path"]) / "bundle.yaml").is_file()
    code, d2 = s.req("POST", "/api/bundles", {"path": d["path"]}, token=alice)
    assert code == 200 and d2["routes"]["unchanged"] == ["loop"], d2
    again = s.req("POST", "/api/bundles", {"path": d["path"]}, token=alice)
    assert again[0] == 409, "同版本不许再来"


# ------------------------------------------------------------ 派遣器、排程执行器读的就是库


async def test_库里改的排程_排程执行器照跑_旧钟点不补(tmp_path):
    from test_site_dispatcher import 台子

    from d1max_site.scheduler import SiteScheduler
    t = 台子(tmp_path)
    t.clock.ms = 毫秒(21, 59)
    t.dog.inject_battery(100.0)
    await t.start()
    try:
        store = RouteStore(t.db, now_ms=t.clock)
        # 路线在库里新建(编辑器存的):点在狗加载的 estate-1 第 7 版上
        store.put_route("loop", {"map_id": "estate-1", "map_version": "7", "base_revision": 0,
                                 "waypoints": [pt("a", 0.6, 0.0), pt("b", 0.6, 0.6)]}, by="alice")
        store.set_timezone("Asia/Kuala_Lumpur", by="alice")
        store.put_schedule("nightly", {"mission": "loop", "at": "22:00", "days": [
            "mon", "tue", "wed", "thu", "fri", "sat", "sun"], "window_min": 30,
            "on_missed": "skip", "base_revision": 0}, by="alice")
        sched = SiteScheduler(t.db, t.site, now_ms=t.clock)
        t.clock.ms = 毫秒(22, 0, 30)
        await t.run(2)
        await t.send(sched.tick())
        [run] = sched.runs("nightly")
        assert run["outcome"] == "started"
        [cmd] = [c for c in t.site.commands("A") if c["kind"] == "patrol"]
        assert cmd["payload"]["mission"]["mission"] == "loop"
        assert cmd["payload"]["map_version"] == "7" and cmd["issued_by"] == "schedule:nightly"
        # 把钟点改到 22:10(此刻之后)再改回 22:00:改了就从现在起算,22:00 那一轮不补
        t.clock.ms = 毫秒(22, 50)
        store.put_schedule("nightly", {"mission": "loop", "at": "22:10", "days": [
            "mon", "tue", "wed", "thu", "fri", "sat", "sun"], "window_min": 30,
            "on_missed": "alarm", "base_revision": 1}, by="alice")
        await t.send(sched.tick())
        assert [r["outcome"] for r in sched.runs("nightly")] == ["started"], \
            "22:10 那一轮在改动之前,不补记、不告警"
        assert sched.view()["bundle"] == {"bundle_id": "site-routes",
                                          "version": store.view()["revision"]}
    finally:
        await t.close()


# ------------------------------------------------------------ W44 第三期:逐段查走不走得通


def _接规划(台, door: float | None = None):
    from d1max_site.intercept_reach import InterceptReach
    from d1max_site.nav_zones import NavZones
    zones = NavZones(台.db, now_ms=lambda: 1000)
    if door is not None:                                 # 另一版图:门宽 door
        src = _files(台.tmp / f"src-door{door}")
        _屋子(src, door)
        台.cat.import_dir(src, map_id="house", version=f"d{door}")
    台.store.reach = InterceptReach(台.db, 台.cat, zones)
    return zones


def test_两点之间走不通_标那一段(台):
    _接规划(台, door=0.0)                                 # 门关死:左右两间不通
    with pytest.raises(RouteError) as e:
        台.store.put_route("r", body("d0.0", [pt("左", 1.0, 2.0), pt("右", 6.0, 2.0)]), by="a")
    errs = e.value.errors
    assert [x.get("segment") for x in errs] == [[0, 1]]
    assert errs[0]["index"] is None and "左" in errs[0]["problem"] and "右" in errs[0]["problem"]


def test_禁行区隔开也算走不通(台):
    zones = _接规划(台)
    door = {"id": "d", "kind": "nogo", "label": "门口",
            "polygon": [[3.5, 0.0], [4.5, 0.0], [4.5, 4.0], [3.5, 4.0]]}
    zones.put("house", "wide", [door], base_revision=0, by="a")
    with pytest.raises(RouteError) as e:
        台.store.put_route("r", body("wide", [pt("左", 1.0, 2.0), pt("右", 6.0, 2.0)]), by="a")
    assert [x.get("segment") for x in e.value.errors] == [[0, 1]]


def test_都走得通不报(台):
    _接规划(台)
    out = 台.store.put_route("r", body("wide", [pt("左", 1.0, 2.0), pt("右", 6.0, 2.0),
                                                pt("回", 1.5, 1.0)]), by="a")
    assert out["revision"] == 1


def test_没接规划不查(台):
    out = 台.store.put_route("r", body("wide", [pt("左", 1.0, 2.0), pt("右", 6.0, 2.0)]), by="a")
    assert out["revision"] == 1
