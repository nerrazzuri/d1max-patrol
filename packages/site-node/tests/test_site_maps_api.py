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

    async def start(self, name):
        self.recording, self.last_bag = True, name
        self.calls.append(("start", name))

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


def test_图里没带原点_用这台狗在这张图上的待命点_都没有不下发(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None and "map_activate" in _caps(s).tasks)
    s.maps.import_dir(_dir(s, "nohome", home=False), map_id=MAP[0], version="12")
    code, d = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "12"}, token=alice)
    assert code == 409 and "待命点" in d["error"], (code, d)
    with s.db.tx() as c:
        c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                  "is_default) VALUES ('A', '门口', ?, '12', 1.5, 0, 0, 1)", (MAP[0],))
    code, d = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "12"}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    _等(lambda: _caps(s).tasks["patrol"]["map_version"] == "12", timeout=8)
    assert s.agent.parts.home is not None and s.agent.parts.home.pose.position.x == 1.5


def test_让狗把隔离的文件再传一次(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _caps(s) is not None)
    code, d = s.req("POST", "/api/robots/A/outbox_retry", {}, token=alice)
    assert code == 409 and "不支持" in d["error"], "这只仿真狗没有发件箱"
