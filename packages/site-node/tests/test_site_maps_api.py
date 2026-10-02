"""W00c5d 第二部分:地图经站点,走站点 API 到仿真狗(内存 MQTT)。管理员下发一张图 → 狗下载、核对、
载入 → 能力里的地图版本变了 → 站点按新版本派单;录包、重建的命令到狗。"""

from __future__ import annotations

import pytest
from test_site_api import MAP, PW, _等, target, 站


class 假建图:
    def __init__(self):
        self.recording = False
        self.last_bag = ""
        self.calls = []

    async def start(self, name, target=None, task_id=""):
        self.recording, self.last_bag = True, name
        self.calls.append(("start", name) if target is None else ("start", name, *target))

    async def stop(self):
        self.recording = False
        self.calls.append(("stop", self.last_bag))

    async def build(self, bag, map_id, version):
        self.calls.append(("build", bag, map_id, version))


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={"mapper": 假建图()})
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _caps(s):
    return s.disp.clients["A"].capabilities


def _确认(s, token, version, revision=0):
    """W10:下发之前人工确认禁行区都画了(没什么要画的也要确认一次)。"""
    code, d = s.req("POST", f"/api/maps/{MAP[0]}/{version}/zones/confirm",
                    {"revision": revision, "confirm": True}, token=token)
    assert code == 200, (code, d)


def test_管理员下发一张图_狗装上_能力变了_按新版本派单(站点, tmp_path):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    _等(lambda: _caps(s) is not None and "map_activate" in _caps(s).tasks)
    src = tmp_path / "vendor"
    src.mkdir()
    (src / "estate-1.pgm").write_bytes(b"P5 v8")
    (src / "home.json").write_text('{"x": 0.5, "y": 0, "yaw": 0}')
    s.maps.import_dir(src, map_id=MAP[0], version="8")
    code, d = s.req("GET", "/api/maps", token=gina)
    assert code == 200 and [m["version"] for m in d["maps"]] == ["8"]
    assert s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "8"},
                 token=gina)[0] == 403, "下发图只给管理员"
    assert s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "99"},
                 token=alice)[0] == 404
    code, d = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "8"}, token=alice)
    assert code == 409 and d["reason"] == "zones_unconfirmed", "W10:禁行区没确认不许下发"
    _确认(s, alice, "8")
    code, d = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "8"}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _caps(s).tasks["patrol"]["map_version"] == "8", timeout=8)
    assert s.dog.loaded_map[:2] == (MAP[0], "8")
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 409 or d["ack"]["result"] != "accepted", "旧版本的目标点不许再派"


def test_录包与重建的命令到狗_重建的版本不许撞(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None and "mapping" in _caps(s).tasks)
    code, d = s.req("POST", "/api/robots/A/mapping", {"action": "start", "name": "yard"},
                    token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    code, d = s.req("POST", "/api/robots/A/mapping", {"action": "stop"}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    assert s.req("POST", "/api/robots/A/mapping", {"action": "go"}, token=alice)[0] == 400
    code, d = s.req("POST", "/api/robots/A/map_build",
                    {"bag": "yard", "map_id": MAP[0], "version": "9"}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: ("build", "yard", MAP[0], "9") in s.agent.mapper.calls)
    assert s.agent.mapper.calls[:2] == [("start", "yard"), ("stop", "yard")]
    s.maps.import_dir(_dir(s, "x"), map_id=MAP[0], version="10")
    assert s.req("POST", "/api/robots/A/map_build", {"bag": "yard", "map_id": MAP[0],
                                                     "version": "10"}, token=alice)[0] == 409
    code, d = s.req("POST", "/api/robots/A/map_build", {"bag": "yard", "map_id": MAP[0],
                                                        "version": "9"}, token=alice)
    assert code == 409 and "正在建" in d["error"], "已经让狗建 9 了(还没收齐):不许再派一次"
    assert s.req("POST", "/api/robots/A/map_build", {"bag": "yard", "map_id": "m" * 41,
                                                     "version": "1"}, token=alice)[0] == 400
    assert s.req("POST", "/api/robots/A/map_build", {"bag": "yard", "map_id": "m" * 40,
                                                     "version": "1" * 17}, token=alice)[0] == 400


def _dir(s, name, *, home=True):
    d = s.maps.root.parent / name
    d.mkdir(exist_ok=True)
    (d / "m.pgm").write_bytes(b"x")
    if home:
        (d / "home.json").write_text('{"x": 0, "y": 0, "yaw": 0}')
    return d


def test_狗报换图失败_站点出告警(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None and "map_activate" in _caps(s).tasks)
    s.maps.import_dir(_dir(s, "bad"), map_id=MAP[0], version="11")
    s.dog.fail_load = "定位起不来"
    _确认(s, alice, "11")
    code, d = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "11"},
                    token=alice)
    assert code == 200
    alerts = _等(lambda: [a for a in s.req("GET", "/api/alerts", token=alice)[1]["alerts"]
                          if a["kind"] == "map_failed"], timeout=8)
    assert "定位起不来" in alerts[0]["detail"]
    assert _caps(s).tasks["patrol"]["map_version"] == MAP[1], "没装上:照旧用原来那张"


def test_狗没报这项能力_站点不发(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})                # 狗没有录包重建
    try:
        alice = _登(s, "alice")
        _等(lambda: _caps(s) is not None)
        assert "mapping" not in _caps(s).tasks
        code, d = s.req("POST", "/api/robots/A/mapping", {"action": "start", "name": "y"},
                        token=alice)
        assert code == 409 and "不支持" in d["error"], (code, d)
    finally:
        s.close()


def _原点(s, version, x):
    with s.db.tx() as c:
        c.execute("INSERT INTO homes(robot_id, map_id, map_version, name, x, y, yaw) VALUES "
                  "('A', ?, ?, 'home', ?, 0, 0)", (MAP[0], version, x))


def _待命点(s, version, x, name="门口"):
    with s.db.tx() as c:
        c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                  "is_default) VALUES ('A', ?, ?, ?, ?, 0, 0, 1)", (name, MAP[0], version, x))


def test_图里没带原点_没标原点不下发_待命点不顶替(站点):
    """W13a 外审阻断:待命点是运营调度的点,不能变成安全返航的原点。"""
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None and "map_activate" in _caps(s).tasks)
    s.maps.import_dir(_dir(s, "nohome", home=False), map_id=MAP[0], version="12")
    _确认(s, alice, "12")
    body = {"map_id": MAP[0], "version": "12"}
    code, d = s.req("POST", "/api/robots/A/map", body, token=alice)
    assert code == 409 and d["reason"] == "no_home" and "标原点" in d["error"], (code, d)
    _待命点(s, "12", 3.0)
    code, d = s.req("POST", "/api/robots/A/map", body, token=alice)
    assert code == 409 and d["reason"] == "no_home", "只有待命点:还是不下发"
    assert not s.db.query("SELECT 1 FROM commands WHERE kind='map_activate'"), "没发给狗"
    assert s.req("POST", "/api/robots/A/map", body | {"without_home": 1}, token=alice)[0] == 400
    _原点(s, "12", 1.5)
    code, d = s.req("POST", "/api/robots/A/map", body, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _caps(s).tasks["patrol"]["map_version"] == "12", timeout=8)
    assert s.agent.parts.home is not None and s.agent.parts.home.pose.position.x == 1.5


def test_新图明说没原点也下发_狗上没有原点_自己走的任务起飞前就拒(站点):
    """标原点要狗先载上这张图:管理员明说 ``without_home`` 才下发;狗上没原点就不接 goto
    (遥控、设位置、标原点照常)。新版本也不继承旧版本的原点。"""
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None and "map_activate" in _caps(s).tasks)
    _原点(s, "15", 2.0)                                   # 旧版本有原点
    s.maps.import_dir(_dir(s, "v16", home=False), map_id=MAP[0], version="16")
    _确认(s, alice, "16")
    body = {"map_id": MAP[0], "version": "16"}
    code, d = s.req("POST", "/api/robots/A/map", body, token=alice)
    assert code == 409 and d["reason"] == "no_home", "新版本不继承旧版本的原点"
    code, d = s.req("POST", "/api/robots/A/map", body | {"without_home": True}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _caps(s).tasks["patrol"]["map_version"] == "16", timeout=8)
    assert s.agent.parts.home is None, "狗上没有原点"
    target = {"schema": "1.0", "map_id": MAP[0], "map_version": "16", "frame_id": "map",
              "x": 1.0, "y": 0.0, "yaw": 0.0}
    code, g = s.req("POST", "/api/robots/A/goto", {"target": target}, token=alice)
    assert code == 200, g
    [e] = _等(lambda: [e for e in s.disp.recent_events("A")
                       if e["kind"] in ("task_failed", "task_done")
                       and e["data"].get("task_id") == g["task_id"]], timeout=8)
    assert e["kind"] == "task_failed" and "原点" in e["data"].get("reason", ""), e
    [a] = _等(lambda: [a for a in s.req("GET", "/api/audit", token=alice)[1]["audit"]
                       if a["action"].endswith("/map") and a["status"] == 200])
    assert a["detail"].get("without_home") is True, a


def test_图里带了原点_待命点不盖过它_原点表才盖(站点):
    """内审应修 4(W00c6f):站点登记的原点是权威,图里的 ``home.json`` 只是没登记时的垫底;W13a 起
    待命点不算原点。"""
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None and "map_activate" in _caps(s).tasks)
    s.maps.import_dir(_dir(s, "withhome"), map_id=MAP[0], version="13")
    _确认(s, alice, "13")
    _待命点(s, "13", 2.5, name="dock")
    code, d = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "13"}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _caps(s).tasks["patrol"]["map_version"] == "13", timeout=8)
    assert s.agent.parts.home.pose.position.x == 0.0, "按图里的 home.json,不是待命点"


def test_原点表优先_挪删待命点不动原点(站点):
    """W13a(决策 16):下发地图时发原点表里的点;待命点(哪怕是默认的)不当原点,挪了、删了原点也不变。"""
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None and "map_activate" in _caps(s).tasks)
    s.maps.import_dir(_dir(s, "split", home=False), map_id=MAP[0], version="14")
    _确认(s, alice, "14")
    _待命点(s, "14", 3.0, name="gate")
    _原点(s, "14", 0.5)
    from d1max_site.standby import StandbyManager
    stb = s.api.standby or StandbyManager(s.db, s.disp, now_ms=s.disp._now)
    stb.set("A", "gate", map_id=MAP[0], map_version="14", x=9.0, y=9.0, yaw=0.0)
    stb.remove("A", "gate")
    assert stb.home("A", MAP[0], "14")["x"] == 0.5
    code, d = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "14"}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _caps(s).tasks["patrol"]["map_version"] == "14", timeout=8)
    assert s.agent.parts.home.pose.position.x == 0.5
    code, d = s.req("GET", f"/api/maps/{MAP[0]}/14/preview", token=alice)
    if code == 200:                                       # 这张假图没有栅格就没有预览
        assert [h["name"] for h in d["homes"]] == ["home"]
