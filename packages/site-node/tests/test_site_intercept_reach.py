"""拦截点走不走得到(W23,决策 38)。两间屋子中间一道墙、墙上一个门:待命点在左屋,拦截点在右屋。
门宽够(1.6 m)走得到;门窄(0.8 m,机身外接圆半径 0.52 m 过不去)走不到。跟狗同一份规划
(``d1max_contract.planning``)。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_site_maps import _files

from d1max_site.db import SiteDB
from d1max_site.incidents import IncidentDesk, IncidentError
from d1max_site.intercept_reach import InterceptReach
from d1max_site.maps import MapCatalog
from d1max_site.nav_zones import NavZones

RES = 0.1
W_M, H_M = 8.0, 4.0                                     # 8 m × 4 m,原点 (0, 0)
WALL_X = 4.0


def _屋子(d, door_m: float):
    """左右两间,x=4 m 处一道 0.2 m 厚的墙,墙正中开 ``door_m`` 宽的门;外圈一圈墙。"""
    w, h = int(W_M / RES), int(H_M / RES)
    rows = []
    for r in range(h):                                   # PGM 第一行是 y 最大的
        y = (h - 1 - r + 0.5) * RES
        line = []
        for c in range(w):
            x = (c + 0.5) * RES
            edge = c in (0, w - 1) or r in (0, h - 1)
            wall = abs(x - WALL_X) < 0.1 and abs(y - H_M / 2) > door_m / 2
            line.append("#" if edge or wall else ".")
        rows.append("".join(line))
    v = {".": 254, "#": 0}
    (d / "floor.pgm").write_bytes(f"P5\n{w} {h}\n255\n".encode()
                                  + bytes(v[ch] for row in rows for ch in row))
    (d / "floor.yaml").write_text(f"image: floor.pgm\nresolution: {RES}\norigin: [0.0, 0.0, 0.0]\n"
                                  "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
                                  "mode: trinary\n")
    (d / "coverage.json").write_text(json.dumps(
        {"version": 1, "step_m": 0.5, "path": [[0.5 + 0.5 * i, 2.0] for i in range(15)]}))


class 假派遣:
    def __init__(self):
        self.feed = SimpleNamespace(publish=lambda item: None)

    def on_event(self, cb):
        pass


@pytest.fixture
def 台(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    cat = MapCatalog(tmp_path, db, now_ms=lambda: 1000)
    zones = NavZones(db, now_ms=lambda: 1000)
    for ver, door in (("wide", 1.6), ("narrow", 0.8)):
        src = _files(tmp_path / f"src-{ver}")
        _屋子(src, door)
        cat.import_dir(src, map_id="house", version=ver)
    for ver in ("wide", "narrow"):
        db.query("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                 "is_default) VALUES ('A', ?, 'house', ?, 1.5, 2.0, 0, 1)", (f"dock-{ver}", ver))
    reach = InterceptReach(db, cat, zones)
    desk = IncidentDesk(db, 假派遣(), now_ms=lambda: 1000)
    desk.reach = reach
    yield SimpleNamespace(db=db, cat=cat, zones=zones, reach=reach, desk=desk)
    db.close()


def test_门够宽_走得到_设得上(台):
    t = 台
    assert t.reach.check("backyard", "house", "wide", 6.0, 2.0).problem == ""
    t.desk.set_intercept("backyard", map_id="house", map_version="wide", x=6.0, y=2.0, yaw=0.0)
    p = t.desk.intercept("backyard")
    assert p["reach"] == "" and p["reach_key"]


def test_门太窄_狗过不去_设不上_说清楚(台):
    t = 台
    r = t.reach.check("backyard", "house", "narrow", 6.0, 2.0)
    assert "从待命点走不到 backyard" in r.problem and "dock-narrow" in r.problem
    assert r.note == ""
    with pytest.raises(IncidentError, match="设不了.*走不到"):
        t.desk.set_intercept("backyard", map_id="house", map_version="narrow", x=6.0, y=2.0,
                             yaw=0.0)
    assert t.desk.intercept("backyard") is None
    assert t.reach.check("左屋", "house", "narrow", 2.5, 2.0).problem == "", "同一间屋里走得到"


def test_贴墙站不下_墙里_图外_离走过的路太远(台):
    t = 台
    assert "站不下" in t.reach.check("墙根", "house", "wide", 3.7, 1.0).problem
    assert "墙" in t.reach.check("墙里", "house", "wide", 4.0, 0.5).problem
    assert "离建图时走过的地方" in t.reach.check("图外", "house", "wide", 20.0, 2.0).problem


def test_在禁行区里_设不上(台):
    t = 台
    t.zones.put("house", "wide", [{"id": "pond", "kind": "nogo", "label": "池子",
                                   "polygon": [[5.5, 1.5], [6.5, 1.5], [6.5, 2.5], [5.5, 2.5]]}],
                base_revision=0, by="alice")
    assert "禁行区" in t.reach.check("池边", "house", "wide", 6.0, 2.0).problem


def test_查不了的不挡_写明没查(台, tmp_path):
    t = 台
    r = t.reach.check("x", "house", "v9", 6.0, 2.0)
    assert r.problem == "" and "站点没有" in r.note
    src = _files(tmp_path / "vendor", **{"house.pgm": b"P5\n1 1\n255\n\x00", "house.yaml": b"x"})
    t.cat.import_dir(src, map_id="house", version="vendor")
    r = t.reach.check("x", "house", "vendor", 6.0, 2.0)
    assert r.problem == "" and "没有规划栅格" in r.note
    t.db.query("DELETE FROM standby_points")
    r = t.reach.check("x", "house", "wide", 6.0, 2.0)
    assert r.problem == "" and "没有待命点" in r.note
    t.desk.set_intercept("x", map_id="house", map_version="wide", x=6.0, y=2.0, yaw=0.0)
    assert "没有待命点" in t.desk.intercept("x")["reach_note"]


def test_原点也算起点(台):
    t = 台
    t.db.query("DELETE FROM standby_points")
    t.db.query("INSERT INTO homes(robot_id, map_id, map_version, name, x, y, yaw) "
               "VALUES ('A', 'house', 'wide', 'home', 1.5, 2.0, 0)")
    r = t.reach.check("backyard", "house", "wide", 6.0, 2.0)
    assert r.problem == "" and r.note == "", "原点也能当起点"


def test_对账_门口画了禁行区_变成走不到_来了入侵不派_删了又走得到(台):
    t = 台
    t.desk.set_intercept("backyard", map_id="house", map_version="wide", x=6.0, y=2.0, yaw=0.0)
    t.desk.map_zone("back", "backyard")
    assert t.desk.recheck_intercepts() == 0, "指纹没变:不重查"
    t.zones.put("house", "wide", [{"id": "door", "kind": "nogo", "label": "门",
                                   "polygon": [[3.6, 1.0], [4.4, 1.0], [4.4, 3.0], [3.6, 3.0]]}],
                base_revision=0, by="alice")
    assert t.desk.recheck_intercepts() == 1
    assert "走不到" in t.desk.intercept("backyard")["reach"]
    told = []
    t.desk.on_outcome = told.append
    row = t.desk._claim("nvr", {"event_id": "e1", "type": "intrusion", "zone": "back",
                                "occurred_at": None, "detail": None})
    assert row["outcome"] == "unreachable" and "拦截点走不到" in row["note"]
    t.desk._publish(row)
    assert [r["outcome"] for r in told] == ["unreachable"], "照样报「没狗去」"
    t.zones.put("house", "wide", [], base_revision=1, by="alice")
    assert t.desk.recheck_intercepts() == 1
    assert t.desk.intercept("backyard")["reach"] == ""


def test_对账_待命点挪了也重查(台):
    t = 台
    t.desk.set_intercept("backyard", map_id="house", map_version="wide", x=6.0, y=2.0, yaw=0.0)
    t.db.query("UPDATE standby_points SET x=6.5 WHERE map_version='wide'")
    assert t.desk.recheck_intercepts() == 1


def test_图有新版本_拦截点标出来要重设(台, tmp_path):
    t = 台
    t.desk.set_intercept("backyard", map_id="house", map_version="wide", x=6.0, y=2.0, yaw=0.0)
    assert t.desk.intercepts()["intercepts"][0]["newer_version"] == "narrow", \
        "站点上这张图最新的是后导入的那一版"
    src = _files(tmp_path / "v3")
    _屋子(src, 1.6)
    t.cat.import_dir(src, map_id="house", version="v3")
    assert t.desk.intercepts()["intercepts"][0]["newer_version"] == "v3"
