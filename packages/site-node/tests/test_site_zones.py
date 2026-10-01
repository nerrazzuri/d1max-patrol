"""W10 站点:禁行区、限速区的存储(修订号、并发、确认)、接口(看、改、确认、权限)、改完下发给狗、
补发、派单门槛(修订对不上、狗不守禁行区)、时间权威分类、狗在禁行区里的告警。"""

from __future__ import annotations

import pytest
from test_site_api import MAP, PW, _等, target, 站

from d1max_site.db import SiteDB
from d1max_site.nav_zones import CONFIRM_TEXT, NavZones, ZonesConflict, ZonesError
from d1max_site.temporal import GATED, SAFE, temporal_class

POND = {"id": "pond", "kind": "nogo", "label": "池子",
        "polygon": [[5, 5], [6, 5], [6, 6], [5, 6]]}
SLOW = {"id": "lawn", "kind": "slow", "max_speed_mps": 0.3,
        "polygon": [[-5, -5], [-4, -5], [-4, -4], [-5, -4]]}


# ------------------------------------------------------------ 存储

def test_存储_修订号_并发_收紧判定_确认(tmp_path):
    t = [1000]
    z = NavZones(SiteDB(tmp_path / "s.db"), now_ms=lambda: t[0])
    assert z.current("m", "1").revision == 0 and not z.confirmed_current("m", "1")
    v = z.view("m", "1")
    assert v["revision"] == 0 and v["confirmed"] is None and v["confirm_text"] == CONFIRM_TEXT
    z.confirm("m", "1", revision=0, by="alice")                  # 没什么要画的也要确认
    assert z.confirmed_current("m", "1")
    zs, tight = z.put("m", "1", [POND], base_revision=0, by="alice")
    assert zs.revision == 1 and tight
    assert not z.confirmed_current("m", "1"), "改过要重新确认"
    assert z.view("m", "1")["confirmed"] == {"revision": 0, "by": "alice", "at_ms": 1000,
                                             "current": False}
    with pytest.raises(ZonesConflict):
        z.put("m", "1", [], base_revision=0, by="bob")          # 看到的是旧的:不许盖
    with pytest.raises(ZonesConflict):
        z.confirm("m", "1", revision=0, by="alice")
    with pytest.raises(ZonesError):
        z.put("m", "1", [{"id": "x", "kind": "nogo", "polygon": [[0, 0]]}], base_revision=1,
              by="a")
    for bad in (None, "1", True, 1.0):
        with pytest.raises(ZonesError):
            z.put("m", "1", [], base_revision=bad, by="a")
        with pytest.raises(ZonesError):
            z.confirm("m", "1", revision=bad, by="a")
    assert z.current("m", "1").revision == 1                     # 坏的没写进去
    zs, tight = z.put("m", "1", [], base_revision=1, by="alice")
    assert zs.revision == 2 and not tight                         # 删禁行区:放宽
    t[0] = 2000
    z.confirm("m", "1", revision=2, by="carol")
    assert z.confirmed_current("m", "1")
    assert z.view("m", "1")["updated_by"] == "alice"
    assert z.current("m", "2").revision == 0                      # 别的版本各是各的


def test_时间权威_区域一律照发():
    # 内审应修 8:一律照发(修订号挡重放、放宽狗上等空闲)
    for p in ({"tighten": True}, {"tighten": False}, {}):
        assert temporal_class("zones_set", p) == SAFE
    assert GATED != SAFE


# ------------------------------------------------------------ 经站点 API 到仿真狗

@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.accounts.add("gina", PW, role="guard")
    d = tmp_path / "cur"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"x")
    s.maps.import_dir(d, map_id=MAP[0], version=MAP[1])        # 狗正在用的那一版进目录
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _zcaps(s):
    c = s.disp.clients["A"].capabilities
    return None if c is None else c.tasks.get("zones_set")


ZP = f"/api/maps/{MAP[0]}/{MAP[1]}/zones"


def test_接口_看改确认_权限_404(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    code, d = s.req("GET", ZP, token=gina)
    assert code == 200 and d["revision"] == 0 and d["zones"] == []
    assert s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=gina)[0] == 403
    assert s.req("POST", ZP + "/confirm", {"revision": 0, "confirm": True}, token=gina)[0] == 403
    assert s.req("GET", f"/api/maps/{MAP[0]}/99/zones", token=gina)[0] == 404
    code, d = s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    assert code == 200 and d["revision"] == 1 and d["tighten"] is True, d
    code, d = s.req("POST", ZP, {"zones": [], "base_revision": 0}, token=alice)
    assert code == 409 and "改过" in d["error"]
    assert s.req("POST", ZP, {"zones": [{"id": "x"}], "base_revision": 1}, token=alice)[0] == 400
    assert s.req("POST", ZP + "/confirm", {"revision": 1}, token=alice)[0] == 400
    assert s.req("POST", ZP + "/confirm", {"revision": 0, "confirm": True}, token=alice)[0] == 409
    code, d = s.req("POST", ZP + "/confirm", {"revision": 1, "confirm": True}, token=alice)
    assert code == 200 and d["confirmed"]["current"] is True
    audit = s.db.query("SELECT action FROM audit WHERE action LIKE '%zones%'")
    assert len(audit) >= 2


def test_改了区域_发给狗_狗上换上_能力里的修订跟着变(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None)
    assert _zcaps(s) == {"rev": 0, "enforced": False}
    code, d = s.req("POST", ZP, {"zones": [SLOW], "base_revision": 0}, token=alice)
    assert code == 200 and d["pushed"] == {"A": ""}, d
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    assert s.agent._zones.revision == 1
    row = s.db.query("SELECT payload FROM commands WHERE kind='zones_set'")[-1]
    assert '"tighten": true' in row["payload"]


def test_派单门槛_修订对不上拒_补发之后放行(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    assert s.loop.call(lambda: s.disp.sync_zones()) == [], "都是第 0 版:不发"
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 409 and "安全确认" in d["error"], "W10 之前就在用的图(修订 0)也要确认(外审 2)"
    # 站点这头改了、没发出去(直接写库):狗上还是第 0 版
    s.disp.zones.put(MAP[0], MAP[1], [SLOW], base_revision=0, by="alice")
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 409 and "安全确认" in d["error"], "改过之后没重新确认也不派"
    s.disp.zones.confirm(MAP[0], MAP[1], revision=1, by="alice")
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 409 and "还没同步" in d["error"], (code, d)
    sent = s.loop.call(lambda: s.disp.sync_zones())
    assert sent == ["A"]
    assert s.loop.call(lambda: s.disp.sync_zones()) == [], "同一版 30 s 内不重发"
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    assert s.loop.call(lambda: s.disp.sync_zones()) == [], "对上了不发"
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 409 and "不守区域" in d["error"], "直线桥不守限速区(内审小 11)"
    _zcaps(s)["enforced"] = True                        # 当它是规划后端的狗
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", (code, d)


def test_派单门槛_这张图有禁行区_直线桥的狗不派(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    code, d = s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    assert code == 200
    s.disp.zones.confirm(MAP[0], MAP[1], revision=1, by="alice")
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(1.0)}, token=alice)
    assert code == 409 and "不守区域" in d["error"], (code, d)
    assert "不守区域" in s.disp.dispatchable("A", "patrol")
    assert s.disp.dispatchable("A", "teleop") == "", "遥控是人开的:不挡"


def test_狗在禁行区里_出P1告警(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None)
    s.loop.call(lambda: _emit(s))
    alerts = _等(lambda: [a for a in s.req("GET", "/api/alerts", token=alice)[1]["alerts"]
                          if a["kind"] == "nogo"], timeout=8)
    assert alerts[0]["level"] == "P1" and "池子" in alerts[0]["title"]


async def _emit(s):
    s.agent.events.emit("inside_nogo", {"zone": "pond", "label": "池子", "x": 5.5, "y": 5.5,
                                        "map_id": MAP[0], "map_version": MAP[1]})


def test_补发_狗没收下_30秒内不重发_过了再发(站点):
    s = 站点
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)

    def 写不进(zs):
        raise OSError("盘满了")
    s.agent.zonebook.put = 写不进                        # 狗上落不了盘:回 store_failed,修订不变
    s.disp.zones.put(MAP[0], MAP[1], [SLOW], base_revision=0, by="alice")
    assert s.loop.call(lambda: s.disp.sync_zones()) == ["A"]
    assert s.loop.call(lambda: s.disp.sync_zones()) == [], "同一版 30 s 内不重发"
    assert _zcaps(s)["rev"] == 0
    assert s.loop.call(lambda: s.disp.sync_zones(retry_ms=0)) == ["A"], "过了间隔再发"


def test_换图之后删光禁行区_没重新确认_不派自主任务_遥控照常(站点):
    """W10 外审 2:确认不只卡换图。放宽(删禁行区)同步到狗之后,没重新确认,goto、巡检都不派;
    遥控不受影响。"""
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    s.disp.zones.confirm(MAP[0], MAP[1], revision=0, by="alice")
    assert s.disp.dispatchable("A", "goto") == ""
    code, d = s.req("POST", ZP, {"zones": [], "base_revision": 0}, token=alice)   # 修订 1,空的
    assert code == 200
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    assert "安全确认" in s.disp.dispatchable("A", "goto")
    assert "安全确认" in s.disp.dispatchable("A", "patrol")
    assert s.disp.dispatchable("A", "teleop") == ""
    s.req("POST", ZP + "/confirm", {"revision": 1, "confirm": True}, token=alice)
    assert s.disp.dispatchable("A", "goto") == ""


def test_避障用不了_不派会自己走的_遥控照常(站点):
    """W11:配了感知节点的狗,能力里 ``obstacles.state`` 不是 ``ok`` 就不派 goto、巡检。"""
    s = 站点
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    s.disp.zones.confirm(MAP[0], MAP[1], revision=0, by="alice")
    assert s.disp.dispatchable("A", "goto") == ""
    tasks = s.disp.clients["A"].capabilities.tasks
    for st, words in (("lost", "感知断了"), ("extrinsic_bad", "外参自检没过"),
                      ("initializing", "还在自检")):
        tasks["obstacles"] = {"state": st, "rear": False, "reason": "雷达离地 2.9 m"}
        why = s.disp.dispatchable("A", "goto")
        assert "避障用不了" in why and words in why and "2.9" in why, why
        assert "避障用不了" in s.disp.dispatchable("A", "patrol")
        assert s.disp.dispatchable("A", "teleop") == ""
    tasks["obstacles"] = {"state": "stale", "rear": True}
    assert s.disp.dispatchable("A", "goto") == "", "不新鲜是一两秒的事:狗那头原地等"
    tasks["obstacles"] = {"state": "ok", "rear": True}
    assert s.disp.dispatchable("A", "goto") == ""


def test_头尾调过来了_不派会自己走的_遥控照常(站点):
    """W11a:能力里 head.direction 不是 head 就不派 goto、巡检。"""
    s = 站点
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    s.disp.zones.confirm(MAP[0], MAP[1], revision=0, by="alice")
    assert s.disp.dispatchable("A", "goto") == ""
    tasks = s.disp.clients["A"].capabilities.tasks
    for d, words in (("tail", "狗尾为前"), ("unknown", "不知道")):
        tasks["head"] = {"direction": d}
        why = s.disp.dispatchable("A", "goto")
        assert "头尾方向" in why and words in why, why
        assert "头尾方向" in s.disp.dispatchable("A", "patrol")
        assert s.disp.dispatchable("A", "teleop") == ""
    tasks["head"] = {"direction": "head"}
    assert s.disp.dispatchable("A", "goto") == ""
