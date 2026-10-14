"""R3、R4:派单闸比狗上区域的内容摘要和地图的内容摘要;老狗(能力里没有 running_config)退回按修订号比;
站点从旧备份恢复后「修订号一样、内容不一样」被认出来。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from test_site_api import MAP, PW, _等, target, 站

from d1max_contract.digest import canonical_digest
from d1max_contract.maps import map_digest
from d1max_contract.zones import ZoneSet, zones_digest
from d1max_site.dispatcher import DispatchRefused

POND = {"id": "pond", "kind": "nogo", "label": "池子",
        "polygon": [[5, 5], [6, 5], [6, 6], [5, 6]]}
LAWN = {"id": "lawn", "kind": "slow", "max_speed_mps": 0.3,
        "polygon": [[-5, -5], [-4, -5], [-4, -4], [-5, -4]]}
ZP = f"/api/maps/{MAP[0]}/{MAP[1]}/zones"


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.disp.maps = s.maps
    d = tmp_path / "cur"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"x")
    s.maps.import_dir(d, map_id=MAP[0], version=MAP[1])
    s.disp.zones.confirm(*MAP, revision=0, by="alice")   # 空表先确认过:之后收紧免确认
    yield s
    s.close()


def _登(s):
    return s.req("POST", "/api/login", {"name": "alice", "password": PW})[1]["token"]


def _zc(s):
    return s.disp.clients["A"].capabilities.tasks["zones_set"]


def _rc(s):
    return s.disp.clients["A"].capabilities.tasks.get("running_config")


def _ready(s):
    _等(lambda: s.disp.clients["A"].capabilities is not None and _rc(s) is not None
        and s.disp.clock_skew_s("A") is not None)


# ------------------------------------------------------------ 摘要本身

def test_区域摘要_不含修订号_内容一变就变_顺序无关():
    a = ZoneSet.from_wire({"map_id": "m", "map_version": "1", "revision": 3, "zones": [POND, LAWN]})
    b = ZoneSet.from_wire({"map_id": "m", "map_version": "1", "revision": 9, "zones": [LAWN, POND]})
    assert zones_digest(a) == zones_digest(b), "修订号不算;区域顺序不算"
    c = ZoneSet.from_wire({"map_id": "m", "map_version": "1", "revision": 3,
                           "zones": [POND | {"polygon": [[5, 5], [6, 5], [6, 6.1], [5, 6]]}, LAWN]})
    assert zones_digest(c) != zones_digest(a)
    assert zones_digest(ZoneSet("m", "1", 0, ())) != zones_digest(ZoneSet("m", "2", 0, ()))
    # 稳定:同一份经 JSON 往返(站点库存的、发给狗的、狗落盘的)摘要不变
    import json
    assert zones_digest(ZoneSet.from_wire(json.loads(json.dumps(a.to_wire())))) == zones_digest(a)


# ------------------------------------------------------------ R3:修订号一样、内容不一样

def test_站点从旧备份恢复_修订号一样内容不一样_拒派_再保存一次出新修订放行(站点):
    s = 站点
    alice = _登(s)
    _ready(s)
    code, d = s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    assert code == 200 and d["revision"] == 1
    _等(lambda: _zc(s)["rev"] == 1, timeout=8)
    _等(lambda: _rc(s)["zones_rev"] == 1)
    _zc(s)["enforced"] = True                           # 当它是规划后端的狗
    assert _rc(s)["zones_digest"] == zones_digest(s.disp.zones.current(*MAP))
    r = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert r[0] == 200, r
    # 恢复备份又改了一次:站点库里第 1 版现在是另一份内容(确认记录跟着修订号,还是「确认过」)
    other = ZoneSet.from_wire({"map_id": MAP[0], "map_version": MAP[1], "revision": 1,
                               "zones": [LAWN]})
    import json
    with s.db.tx() as c:
        c.execute("UPDATE nav_zones SET body=?, confirmed_body=? WHERE map_id=?",
                  (json.dumps(other.to_wire()), json.dumps(other.to_wire()), MAP[0]))
    assert s.disp.zones.confirmed_current(*MAP) and s.disp.zones.current(*MAP).revision == 1
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 409 and "内容不一样" in d["error"] and "第 1 版" in d["error"], (code, d)
    assert "内容不一样" in s.disp.dispatchable("A", "patrol")
    assert s.disp.dispatchable("A", "teleop") == "", "遥控不挡"
    # 管理员在地图页再保存一次(出第 2 版)→ 发给狗 → 狗的摘要对上 → 放行
    code, d = s.req("POST", ZP, {"zones": [LAWN, POND], "base_revision": 1}, token=alice)
    assert code == 200 and d["revision"] == 2, d
    s.disp.zones.confirm(*MAP, revision=2, by="alice")
    _等(lambda: _zc(s)["rev"] == 2 and _rc(s)["zones_rev"] == 2, timeout=8)
    _zc(s)["enforced"] = True
    r = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert r[0] == 200, r


def test_robot_view里有running_每项match和依据(站点):
    s = 站点
    _ready(s)
    v = s.disp.robot_view("A")["running"]
    assert v["legacy"] is False and v["problems"] == []
    assert v["zones"]["basis"] == "digest" and v["zones"]["match"] is True
    assert v["map"]["match"] is None, "狗开机加载的图没有清单(命令行指定的):查不了,不挡"
    assert v["mission"] is None


# ------------------------------------------------------------ 老狗:退回修订号

def test_老狗没有running_config_只比修订号_view标legacy(站点):
    s = 站点
    alice = _登(s)
    _ready(s)
    s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    _等(lambda: _zc(s)["rev"] == 1, timeout=8)
    _zc(s)["enforced"] = True
    caps = s.disp.clients["A"].capabilities
    caps.tasks.pop("running_config")                    # 老代理不报
    assert s.disp.robot_view("A")["running"] == {"legacy": True, "problems": [], "map": None,
                                                 "zones": None}
    # 站点内容变了而修订号没变:老狗查不出来(这是回退到按修订号比的代价,能力视图里标「旧版」)
    r = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert r[0] == 200, r


def test_老狗报running_config但没有摘要字段_区域退回修订号_基准标revision(站点):
    s = 站点
    alice = _登(s)
    _ready(s)
    s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    _等(lambda: _zc(s)["rev"] == 1 and _rc(s)["zones_rev"] == 1, timeout=8)
    rc = _rc(s)
    rc.pop("zones_digest")
    v = s.disp.robot_view("A")["running"]
    assert v["zones"]["basis"] == "revision" and v["zones"]["match"] is True


# ------------------------------------------------------------ R4:图摘要

def _fake_client(rc, tasks=None):
    caps = SimpleNamespace(tasks={"running_config": rc, **(tasks or {})})
    return SimpleNamespace(capabilities=caps)


def test_图摘要不一样_拒派_说清楚是哪一版_一样放行(tmp_path):
    from d1max_site.db import SiteDB
    from d1max_site.dispatcher import Dispatcher
    from d1max_site.maps import MapCatalog

    db = SiteDB(tmp_path / "s.db")
    cat = MapCatalog(tmp_path, db, now_ms=lambda: 1000)
    src = tmp_path / "src"
    src.mkdir()
    (src / "floor.pgm").write_bytes(b"P5 data")
    ref = cat.import_dir(src, map_id="m", version="3")
    d = object.__new__(Dispatcher)
    d.maps, d.zones = cat, None
    good = {"map_id": "m", "map_version": "3", "map_digest": map_digest(ref)}
    assert d.running_view(_fake_client(good))["map"]["match"] is True
    d._check_running("A", _fake_client(good), "goto")
    bad = good | {"map_digest": "0" * 64}
    v = d.running_view(_fake_client(bad))
    assert v["map"]["match"] is False and "m:3" in v["problems"][0]
    with pytest.raises(DispatchRefused, match="m:3.*不一样"):
        d._check_running("A", _fake_client(bad), "goto")
    with pytest.raises(DispatchRefused):
        d._check_running("A", _fake_client(bad), "patrol")
    d._check_running("A", _fake_client(bad), "teleop")           # 遥控不挡
    # 站点不认识这一版(目录里没有):查不了,不挡
    unknown = {"map_id": "m", "map_version": "99", "map_digest": "1" * 64}
    assert d.running_view(_fake_client(unknown))["map"]["match"] is None
    d._check_running("A", _fake_client(unknown), "goto")
    # 狗没报图摘要(命令行指定的图):不挡
    no_digest = {"map_id": "m", "map_version": "3"}
    assert d.running_view(_fake_client(no_digest))["map"]["match"] is None
    db.close()


def test_站点这边换了同一版的文件_狗上的旧内容被认出来(站点, tmp_path):
    """用真的换图走一遍:狗下载 8、报摘要;站点目录里 8 的登记被改(文件清单变了)→ 摘要对不上。"""
    s = 站点
    alice = _登(s)
    src = tmp_path / "v8"
    src.mkdir()
    (src / "floor.pgm").write_bytes(b"P5 eight")
    s.maps.import_dir(src, map_id=MAP[0], version="8")
    _ready(s)
    s.req("POST", f"/api/maps/{MAP[0]}/8/zones/confirm",
          {"revision": 0, "confirm": True, "empty_ok": True}, token=alice)
    code, _ = s.req("POST", "/api/robots/A/map", {"map_id": MAP[0], "version": "8",
                                                  "without_home": True}, token=alice)
    assert code == 200
    _等(lambda: (_rc(s) or {}).get("map_version") == "8" and _rc(s).get("map_digest"), timeout=10)
    assert s.disp.robot_view("A")["running"]["map"]["match"] is True
    # 站点这边这一版的清单被换了(比如从别的备份恢复了另一份 8)
    with s.db.tx() as c:
        c.execute("UPDATE maps SET files=? WHERE map_id=? AND version='8'",
                  ('[{"name": "floor.pgm", "size": 8, "sha256": "' + "a" * 64 + '"}]', MAP[0]))
    _zc(s)["enforced"] = True
    code, d = s.req("POST", "/api/robots/A/goto", {"target": {**target(1.0), "map_version": "8"}},
                    token=alice)
    assert code == 409 and "不一样" in d["error"] and "重新下发" in d["error"], (code, d)


# ------------------------------------------------------------ 任务修订

def test_巡检命令带任务修订_狗记下_能力里报(站点):
    """站点发 patrol 带 ``mission_rev``(任务定义的内容摘要);狗原样记、running_config 报。"""
    s = 站点
    from d1max_contract.mission import Mission, MissionWaypoint
    from d1max_patrol.protocol.nav_types import Pose
    alice = _登(s)
    _ready(s)
    s.req("POST", ZP + "/confirm", {"revision": 0, "confirm": True, "empty_ok": True}, token=alice)
    m = Mission(mission="night", map_id=MAP[0], map_version=MAP[1],
                waypoints=(MissionWaypoint(name="a", pose=Pose.from_xy_yaw(1.0, 0.0)),))
    r = s.loop.call(lambda: s.disp.patrol("A", m.to_wire(), issued_by="alice"))
    assert r["ack"]["result"] == "accepted", r
    want = canonical_digest(m.to_wire())
    s.loop.call(s.agent._publish_caps)               # 能力在下一次发布时带上(不为这个特意重发)
    _等(lambda: (_rc(s) or {}).get("last_patrol", {}).get("rev") == want, timeout=8)
    assert _rc(s)["last_patrol"]["mission"] == "night"
    time.sleep(0.05)
