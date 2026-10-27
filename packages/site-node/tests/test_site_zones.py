"""W10 站点:禁行区、限速区的存储(修订号、并发、确认)、接口(看、改、确认、权限)、改完下发给狗、
补发、派单门槛(修订对不上、狗不守禁行区)、时间权威分类、狗在禁行区里的告警。"""

from __future__ import annotations

import json

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
    # 决策 66(2026-10-14):收紧免确认 —— 以前这里断言「改过要重新确认」(任何改动都作废确认);
    # 现在加一个禁行区算改的人确认了这一版(留了怎么确认的),放宽的仍要重新确认(见下面删禁行区那一步)
    assert z.confirmed_current("m", "1"), "收紧:改的人就算确认了"
    assert z.view("m", "1")["confirmed"] == {"revision": 1, "by": "alice", "at_ms": 1000,
                                             "current": True, "how": "tighten"}
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
    assert not z.confirmed_current("m", "1"), "放宽要重新确认"
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
    # 决策 65 起多一项 teleop_hard,地图台账 44 起多一项 erase,W41 起多一项 ignore
    assert _zcaps(s) == {"rev": 0, "enforced": False, "teleop_hard": True, "erase": True,
                         "ignore": True}
    code, d = s.req("POST", ZP, {"zones": [SLOW], "base_revision": 0}, token=alice)
    assert code == 200 and d["pushed"] == {"A": ""}, d
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    assert s.agent._zones.revision == 1
    row = s.db.query("SELECT payload FROM commands WHERE kind='zones_set'")[-1]
    assert '"tighten": true' in row["payload"]


ERASE = {"id": "junk", "kind": "erase", "label": "抹掉", "hard": True,
         "polygon": [[2, 2], [3, 2], [3, 3], [2, 3]]}


def _sent(s):
    import json
    row = s.db.query("SELECT payload FROM commands WHERE kind='zones_set' ORDER BY rowid")[-1]
    return json.loads(row["payload"])["zones"]


def test_抹掉区_认得的狗原样发_老狗换成同一块禁行区_补发也一样(站点):
    """地图台账 44:老代理(能力里没有 ``zones_set.erase``)不认 ``erase``,会把整份区域集拒掉 ——
    站点发给它时换成同一块禁行区(id、名字、几何、硬都不变),它照收、照守;认得的狗原样收。加抹掉区是
    收紧(决策 66)。"""
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    code, d = s.req("POST", ZP, {"zones": [POND, ERASE], "base_revision": 0}, token=alice)
    assert code == 200 and d["pushed"] == {"A": ""} and d["tighten"] is True, d
    assert [z["kind"] for z in _sent(s)["zones"]] == ["nogo", "erase"]
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    assert [z.kind for z in s.agent._zones.zones] == ["nogo", "erase"]
    del _zcaps(s)["erase"]                              # 当它是认不得抹掉区的老狗
    code, d = s.req("POST", ZP, {"zones": [POND, ERASE, SLOW], "base_revision": 1}, token=alice)
    assert code == 200 and d["pushed"] == {"A": ""}, d
    old = _sent(s)
    assert old["revision"] == 2 and [z["kind"] for z in old["zones"]] == ["nogo", "nogo", "slow"]
    assert old["zones"][1] == {"id": "junk", "kind": "nogo", "label": "抹掉", "hard": True,
                               "polygon": [[2, 2], [3, 2], [3, 3], [2, 3]]}
    _等(lambda: _zcaps(s).get("rev") == 2, timeout=8)
    # 站点自己那份还是抹掉区(画图时抹掉、手机网页认得)
    mine = s.req("GET", ZP, token=alice)[1]["zones"]
    assert [z["kind"] for z in mine] == ["nogo", "erase", "slow"]
    s.disp.zones.put(MAP[0], MAP[1], [ERASE], base_revision=2, by="alice")   # 没发出去:补发
    _zcaps(s).pop("erase", None)
    assert s.loop.call(lambda: s.disp.sync_zones()) == ["A"]
    sent = _sent(s)["zones"]
    assert [z["kind"] for z in sent if z["id"] == "junk"] == ["nogo"], "补发给老机器人也换成禁行区"
    assert "erase" not in {z["kind"] for z in sent}
    # 外审 R1:这一版从没确认过,删掉的池子、限速区是放宽 —— 没确认前照样守着(随在生效的一起补发)
    assert {z["id"] for z in sent} == {"junk", "pond", "lawn"}


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
    # 决策 66 之前这里直接把空的第 0 版存成空的第 1 版(什么都没删);现在没改动的「改」不算放宽,
    # 所以先真画一个(收紧,免确认),再真删光(放宽)
    code, d = s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    assert code == 200 and d["auto_confirmed"] is True
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    code, d = s.req("POST", ZP, {"zones": [], "base_revision": 1}, token=alice)   # 修订 2,删光
    assert code == 200 and d["auto_confirmed"] is False
    _等(lambda: _zcaps(s)["rev"] == 2, timeout=8)
    assert "安全确认" in s.disp.dispatchable("A", "goto")
    assert "安全确认" in s.disp.dispatchable("A", "patrol")
    assert s.disp.dispatchable("A", "teleop") == ""
    code, d = s.req("POST", ZP + "/confirm", {"revision": 2, "confirm": True, "empty_ok": True},
                    token=alice)
    assert code == 200, d
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
    assert s.disp.dispatchable("A", "goto") == "", "不新鲜是一两秒的事:机器人那头原地等"
    tasks["obstacles"] = {"state": "ok", "rear": True}
    assert s.disp.dispatchable("A", "goto") == ""


def test_头尾调过来了_不派会自己走的_遥控照常(站点):
    """W11a:能力里 head.direction 不是 head 就不派 goto、巡检。"""
    s = 站点
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    s.disp.zones.confirm(MAP[0], MAP[1], revision=0, by="alice")
    assert s.disp.dispatchable("A", "goto") == ""
    tasks = s.disp.clients["A"].capabilities.tasks
    for d, words in (("tail", "机器人尾为前"), ("unknown", "不知道")):
        for h in ({"direction": d}, {"direction": d, "autonomy": False}):   # 老狗、新狗
            tasks["head"] = h
            why = s.disp.dispatchable("A", "goto")
            assert "头尾方向" in why and words in why, why
            assert "头尾方向" in s.disp.dispatchable("A", "patrol")
            assert s.disp.dispatchable("A", "teleop") == ""
    tasks["head"] = {"direction": "tail", "autonomy": True}    # W09i:后雷达标过
    assert s.disp.dispatchable("A", "goto") == ""
    tasks["head"] = {"direction": "head"}
    assert s.disp.dispatchable("A", "goto") == ""


# ---------------------------------- 决策 66(2026-10-14):收紧免确认、放宽要确认、临时区到期

BIG = {"id": "pond2", "kind": "nogo", "label": "池子大一圈",
       "polygon": [[4.5, 4.5], [6.5, 4.5], [6.5, 6.5], [4.5, 6.5]]}
HARD = POND | {"hard": True}


def _z(tmp_path, t0=1000):
    t = [t0]
    return NavZones(SiteDB(tmp_path / "s.db"), now_ms=lambda: t[0]), t


def test_抹掉区_加了免确认_删了要确认(tmp_path):
    """地图台账 44 + 决策 66:画一块抹掉区是收紧(改的人就算确认了,派单不断);删掉是放宽(要管理员
    确认)。"""
    z, _ = _z(tmp_path)
    z.put("m", "1", [POND], base_revision=0, by="alice")
    z.confirm("m", "1", revision=1, by="alice")
    zs, tight = z.put("m", "1", [POND, ERASE], base_revision=1, by="bob")
    assert tight and z.confirmed_current("m", "1")
    assert [x.kind for x in zs.zones] == ["nogo", "erase"]
    assert z.view("m", "1")["confirmed"]["how"] == "tighten"
    zs, tight = z.put("m", "1", [POND], base_revision=2, by="bob")
    assert not tight and not z.confirmed_current("m", "1"), "删抹掉区要确认"


def test_收紧免确认_放宽要确认_又紧又松算放宽(tmp_path):
    z, t = _z(tmp_path)
    z.put("m", "1", [POND], base_revision=0, by="alice")
    assert not z.confirmed_current("m", "1"), "从没确认过的:收紧也不算确认(那一句还没人说过)"
    z.confirm("m", "1", revision=1, by="alice")
    t[0] = 2000
    # 收紧:加硬禁区标记、加一个限速区、调低限速、放大 —— 改的人就算确认了
    for rev, zones in ((2, [HARD]), (3, [HARD, SLOW]), (4, [HARD, SLOW | {"max_speed_mps": 0.2}]),
                       (5, [HARD | {"polygon": BIG["polygon"]}, SLOW | {"max_speed_mps": 0.2}])):
        zs, tight = z.put("m", "1", zones, base_revision=rev - 1, by="bob")
        assert zs.revision == rev and tight
        assert z.confirmed_current("m", "1"), rev
        assert z.view("m", "1")["confirmed"] == {"revision": rev, "by": "bob", "at_ms": 2000,
                                                 "current": True, "how": "tighten"}
    # 放宽:去掉硬禁区标记 → 要确认
    zs, tight = z.put("m", "1", [BIG, SLOW | {"max_speed_mps": 0.2}], base_revision=5, by="bob")
    assert not tight and not z.confirmed_current("m", "1")
    # 改回去(跟最后确认的那一版比是收紧):又算确认了
    z.put("m", "1", [HARD | {"polygon": BIG["polygon"]}, SLOW | {"max_speed_mps": 0.2}],
          base_revision=6, by="bob")
    assert z.confirmed_current("m", "1"), "撤销误删:跟最后确认的那一版比是收紧"
    # 又紧又松(加一个、删一个)= 放宽
    z.put("m", "1", [HARD | {"polygon": BIG["polygon"]}, {"id": "new", "kind": "nogo",
          "polygon": [[0, 0], [1, 0], [1, 1]]}], base_revision=7, by="bob")
    assert not z.confirmed_current("m", "1")
    z.confirm("m", "1", revision=8, by="carol")
    assert z.view("m", "1")["confirmed"]["how"] == "confirm"


def test_临时区_到期站点删_带确认_不带确认的照样不带(tmp_path):
    z, t = _z(tmp_path, t0=10_000)
    z.confirm("m", "1", revision=0, by="alice")
    z.put("m", "1", [POND, SLOW | {"until_ms": 70_000}], base_revision=0, by="alice")
    assert z.confirmed_current("m", "1"), "加一个临时限速区:收紧"
    assert z.expire() == [], "还没到期"
    t[0] = 70_000
    got = z.expire()
    assert [(s.map_id, s.revision, [x.id for x in s.zones]) for s in got] == [("m", 2, ["pond"])]
    assert z.current("m", "1").revision == 2
    assert [x.id for x in z.current("m", "1").zones] == ["pond"]
    v = z.view("m", "1")
    assert v["confirmed"]["current"] and v["confirmed"]["how"] == "expiry", \
        "到期是放宽,但到期时间是确认过的那一版里定的:预先确认过"
    assert v["updated_by"] == "site:expiry"
    assert z.expire() == []
    audit = z.db.query("SELECT actor, action, detail FROM audit WHERE action='zones_expired'")
    assert len(audit) == 1 and audit[0]["actor"] == "site:expiry"
    # 当前这一版没确认(有放宽还没人确认):到期删掉之后照样没确认
    z.put("m", "1", [SLOW | {"until_ms": 80_000}], base_revision=2, by="alice")      # 删了池子
    assert not z.confirmed_current("m", "1")
    t[0] = 80_000
    assert [s.revision for s in z.expire()] == [4]
    assert not z.confirmed_current("m", "1")


def test_临时区_到期时间要在以后_按站点的钟_给时长也行(tmp_path):
    z, t = _z(tmp_path, t0=10_000)
    with pytest.raises(ZonesError, match="已经过了"):
        z.put("m", "1", [POND | {"until_ms": 10_000}], base_revision=0, by="a")
    with pytest.raises(ZonesError, match="太远"):
        z.put("m", "1", [POND | {"until_ms": 10_000 + 400 * 86_400_000}], base_revision=0, by="a")
    zs, _ = z.put("m", "1", [POND | {"for_s": 3600}], base_revision=0, by="a")
    assert zs.zones[0].until_ms == 10_000 + 3_600_000, \
        "时长按站点的钟换成到期时刻(手机的钟可能不准)"
    for bad in (0, -1, True, 1.5, "60", 400 * 86_400):
        with pytest.raises(ZonesError):
            z.put("m", "1", [POND | {"for_s": bad}], base_revision=1, by="a")
    with pytest.raises(ZonesError):
        z.put("m", "1", [POND | {"for_s": 60, "until_ms": 99_999_999}], base_revision=1, by="a")
    # 已经到期、站点还没来得及删的那一个原样带回来:悄悄去掉,不报错
    t[0] = 10_000 + 3_600_000
    zs, _ = z.put("m", "1", [POND | {"until_ms": 10_000 + 3_600_000}, SLOW], base_revision=1,
                  by="a")
    assert [x.id for x in zs.zones] == ["lawn"]


def test_从56升上来_当前确认过的记下确认过的那一份_没确认的留空(tmp_path):
    """决策 66 的迁移:收紧免确认要跟「最后确认过的那一份」比;升上来时只有「确认的就是当前修订」的
    那几行知道那一份是什么,别的留空(之后的改动一律要人确认)。"""
    import json as _json
    import sqlite3
    from pathlib import Path
    snap = Path(__file__).parent / "fixtures" / "schema" / "site-v56.sql"
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(snap.read_text(encoding="utf-8"))
    body = lambda rev, zs: _json.dumps({"map_id": "m", "map_version": "1", "revision": rev,  # noqa: E731
                                        "zones": zs})
    con.execute("INSERT INTO nav_zones VALUES ('m','1',1,?,'a',1,1,'a',1)", (body(1, [POND]),))
    con.execute("INSERT INTO nav_zones VALUES ('m','2',2,?,'a',1,1,'a',1)",
                (body(2, [POND]).replace('"1"', '"2"'),))
    con.commit()
    con.close()
    z = NavZones(SiteDB(p), now_ms=lambda: 5)
    assert z.confirmed_current("m", "1") and not z.confirmed_current("m", "2")
    z.put("m", "1", [POND, SLOW], base_revision=1, by="b")
    assert z.confirmed_current("m", "1"), "确认过的那一份就是升上来时的当前一版:收紧照样免确认"
    z.put("m", "2", [POND, SLOW], base_revision=2, by="b")
    assert not z.confirmed_current("m", "2"), "那一份不知道:往保守那边,要人确认"


# ---------------------------------- R2:新版本把上一版的区域、待命点、拦截点带过来给人核

def _图(db, version, ms, frames=b"F1"):
    import hashlib
    import json as _json
    files = [{"name": "floor.pgm", "size": 1, "sha256": "0" * 64}]
    if frames is not None:
        files.append({"name": "frames.json", "size": len(frames),
                      "sha256": hashlib.sha256(frames).hexdigest()})
    with db.tx() as c:
        c.execute("INSERT INTO maps(map_id, version, source, created_ms, files, note) "
                  "VALUES (?,?,?,?,?,?)", ("m", version, "import", ms, _json.dumps(files), ""))


def _上一版(db, z):
    _图(db, "1", 100)
    z.put("m", "1", [POND, SLOW, POND | {"id": "old", "until_ms": 1500}], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    with db.tx() as c:
        c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                  "is_default) VALUES ('A','dock','m','1',1.0,2.0,0.5,1)")
        c.execute("INSERT INTO intercepts(name, map_id, map_version, x, y, yaw) "
                  "VALUES ('gate','m','1',3.0,4.0,0.0)")


def test_新版本_上一版的东西带过来给人核_不生效(tmp_path):
    z, t = _z(tmp_path)
    _上一版(z.db, z)
    t[0] = 2000                                       # 「old」那个临时区已经到期:不带
    _图(z.db, "2", 200)
    v = z.view("m", "2")
    assert v["revision"] == 0 and v["zones"] == [] and not z.confirmed_current("m", "2"), \
        "带过来的只是待核的建议,不生效"
    p = v["proposal"]
    assert p["from_version"] == "1" and p["from_revision"] == 1
    assert [x["id"] for x in p["zones"]] == ["pond", "lawn"]
    assert p["standby"] == [{"robot_id": "A", "name": "dock", "x": 1.0, "y": 2.0, "yaw": 0.5,
                             "default": True}]
    assert p["intercepts"] == [{"name": "gate", "x": 3.0, "y": 4.0, "yaw": 0.0}]
    assert p["frame"]["same"] is True and "坐标" in p["frame"]["note"]
    assert v["previous"] == {"version": "1", "revision": 1, "zones": 3}
    # 上一版的东西原样不动
    assert z.db.query("SELECT map_version FROM standby_points")[0]["map_version"] == "1"
    assert z.current("m", "2").zones == ()
    assert z.view("m", "1").get("proposal") is None, "第一版没有上一版"


def test_新版本_坐标系变了明说(tmp_path):
    z, _ = _z(tmp_path)
    _上一版(z.db, z)
    _图(z.db, "2", 200, frames=b"F2")
    p = z.view("m", "2")["proposal"]
    assert p["frame"]["same"] is False and "frames.json" in p["frame"]["note"]
    _图(z.db, "3", 300, frames=None)
    assert z.view("m", "3").get("proposal") is None, "上一版(2)什么都没有:不带"


def test_新版本_采纳一部分并确认_丢掉_都只一次(tmp_path):
    z, _ = _z(tmp_path)
    _上一版(z.db, z)
    _图(z.db, "2", 200)
    z.view("m", "2")
    z.put("m", "2", [{"id": "pond", "kind": "nogo", "polygon": [[0, 0], [1, 0], [1, 1]]}],
          base_revision=0, by="a")                     # 新版本上先画了一个 id 撞车的
    with pytest.raises(ZonesError):
        z.confirm("m", "2", revision=1, by="a", adopt={"zones": ["nope"]})
    got = z.confirm("m", "2", revision=1, by="a", adopt={"zones": ["pond"]})
    zs = z.current("m", "2")
    assert zs.revision == 2 and len(zs.zones) == 2 and z.confirmed_current("m", "2")
    assert {x.id for x in zs.zones} == {"pond", got["zones"]["pond"]}, got
    assert z.view("m", "2").get("proposal") is None, "采纳过就收起来"
    assert z.view("m", "2")["confirmed"]["how"] == "confirm"
    # 丢掉:收起来,不会再生成
    _图(z.db, "3", 300)
    assert z.view("m", "3")["proposal"] is not None
    z.discard_proposal("m", "3", by="a")
    assert z.view("m", "3").get("proposal") is None
    with pytest.raises(ZonesError):
        z.confirm("m", "3", revision=0, by="a", adopt={"zones": ["pond"]})


def test_接口_收紧免确认_派单不断_放宽要确认才派(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    s.disp.zones.confirm(MAP[0], MAP[1], revision=0, by="alice")
    _zcaps(s)["enforced"] = True                        # 当它是规划后端的狗
    assert s.disp.dispatchable("A", "goto") == ""
    code, d = s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    assert code == 200 and d["tighten"] is True and d["auto_confirmed"] is True, d
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    _zcaps(s)["enforced"] = True
    assert s.disp.dispatchable("A", "goto") == "", "加一个禁行区:入侵派遣、巡检照派"
    code, d = s.req("POST", ZP, {"zones": [], "base_revision": 1}, token=alice)
    assert code == 200 and d["tighten"] is False and d["auto_confirmed"] is False, d
    _等(lambda: _zcaps(s)["rev"] == 2, timeout=8)
    _zcaps(s)["enforced"] = True
    assert "安全确认" in s.disp.dispatchable("A", "goto"), "删禁行区:要人确认"
    audit = s.db.query("SELECT detail FROM audit WHERE target=? ORDER BY id",
                       (f"{MAP[0]}:{MAP[1]}",))
    assert any('"auto_confirmed": true' in a["detail"] for a in audit), audit


def test_接口_确认空的区域集要明说(站点):
    s = 站点
    alice = _登(s, "alice")
    code, d = s.req("POST", ZP + "/confirm", {"revision": 0, "confirm": True}, token=alice)
    assert code == 409 and d.get("reason") == "zones_empty", d
    code, d = s.req("POST", ZP + "/confirm", {"revision": 0, "confirm": True, "empty_ok": True},
                    token=alice)
    assert code == 200 and d["confirmed"]["current"] is True


def test_接口_到期删掉之后发给狗(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    code, d = s.req("POST", ZP, {"zones": [POND, SLOW | {"for_s": 60}], "base_revision": 0},
                    token=alice)
    assert code == 200 and d["zones"][1]["until_ms"] > 0, d
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    assert s.loop.call(lambda: s.disp.expire_zones()) == []
    real = s.disp.zones._now
    s.disp.zones._now = lambda: real() + 61_000
    try:
        assert s.loop.call(lambda: s.disp.expire_zones()) == [f"{MAP[0]}:{MAP[1]}"]
    finally:
        s.disp.zones._now = real
    _等(lambda: _zcaps(s)["rev"] == 2, timeout=8)
    assert [z.id for z in s.agent._zones.zones] == ["pond"]


def test_接口_新版本带过来的_采纳并确认_待命点拦截点跟着过去(站点, tmp_path):
    from d1max_site.incidents import IncidentDesk
    from d1max_site.standby import StandbyManager
    s = 站点
    s.api.standby = StandbyManager(s.db, s.disp, now_ms=lambda: 1000)
    s.api.incidents = IncidentDesk(s.db, s.disp, now_ms=lambda: 1000)
    alice = _登(s, "alice")
    s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=alice)
    with s.db.tx() as c:
        c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                  "is_default) VALUES ('A','dock',?,?,1.0,2.0,0.5,1)", MAP)
        c.execute("INSERT INTO intercepts(name, map_id, map_version, x, y, yaw) "
                  "VALUES ('gate',?,?,3.0,4.0,0.0)", MAP)
    d = tmp_path / "v8"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"y")
    s.maps.import_dir(d, map_id=MAP[0], version="8")
    p8 = f"/api/maps/{MAP[0]}/8/zones"
    code, v = s.req("GET", p8, token=alice)
    assert code == 200 and v["proposal"]["from_version"] == MAP[1], v
    code, v = s.req("POST", p8 + "/confirm", {"revision": 0, "confirm": True, "adopt": {
        "zones": ["pond"], "standby": [["A", "dock"]], "intercepts": ["gate"]}}, token=alice)
    assert code == 200, v
    assert [z["id"] for z in v["zones"]] == ["pond"] and v["confirmed"]["current"]
    assert v["adopted"]["standby"] == {"A/dock": ""} and v["adopted"]["intercepts"] == {"gate": ""}
    assert s.db.query("SELECT map_version FROM standby_points")[0]["map_version"] == "8"
    assert s.db.query("SELECT map_version FROM intercepts")[0]["map_version"] == "8"
    assert v.get("proposal") is None
    # 丢掉
    d9 = tmp_path / "v9"
    d9.mkdir()
    (d9 / "m.pgm").write_bytes(b"z")
    s.maps.import_dir(d9, map_id=MAP[0], version="9")
    p9 = f"/api/maps/{MAP[0]}/9/zones"
    assert s.req("GET", p9, token=alice)[1]["proposal"] is not None
    code, v = s.req("POST", p9 + "/proposal", {"action": "discard"}, token=alice)
    assert code == 200 and v.get("proposal") is None
    gina = _登(s, "gina")
    assert s.req("POST", p9 + "/proposal", {"action": "discard"}, token=gina)[0] == 403


EXHIBIT = {"id": "exhibit", "kind": "ignore", "label": "展品",
           "polygon": [[-3, -3], [-2, -3], [-2, -2], [-3, -2]]}


def test_W41_人员忽略区_认得的狗原样发_老狗去掉_摘要对得上_加了不算放宽(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _zcaps(s) is not None and s.disp.clock_skew_s("A") is not None)
    assert _zcaps(s)["ignore"] is True
    code, d = s.req("POST", ZP, {"zones": [POND, EXHIBIT], "base_revision": 0}, token=alice)
    assert code == 200 and d["pushed"] == {"A": ""} and d["tighten"] is True, d
    assert [z["kind"] for z in _sent(s)["zones"]] == ["nogo", "ignore"]
    _等(lambda: _zcaps(s)["rev"] == 1, timeout=8)
    assert [z.kind for z in s.agent._zones.zones] == ["nogo", "ignore"]
    del _zcaps(s)["ignore"]                              # 当它是认不得忽略区的老狗
    code, d = s.req("POST", ZP, {"zones": [POND, EXHIBIT, SLOW], "base_revision": 1}, token=alice)
    assert code == 200 and d["pushed"] == {"A": ""}, d
    assert [z["kind"] for z in _sent(s)["zones"]] == ["nogo", "slow"], "老机器人:去掉忽略区"
    mine = s.req("GET", ZP, token=alice)[1]["zones"]
    assert [z["kind"] for z in mine] == ["nogo", "ignore", "slow"]


# ------------------------------------------------------------ W44 第三期:网页上确认放宽要再输密码


def _网页(s):
    from test_site_webui import WEB, _cookie, _http
    code, h, _ = _http(s, "POST", "/api/login", {"name": "alice", "password": PW, "web": True},
                       headers=WEB)
    assert code == 200
    ck = {"Cookie": _cookie(h)} | WEB

    def call(path, body):
        code, _, raw = _http(s, "POST", path, body, headers=ck)
        return code, json.loads(raw or b"{}")
    return call


def _放宽了(s, tok):
    s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=tok)
    s.req("POST", ZP + "/confirm", {"revision": 1, "confirm": True}, token=tok)
    s.req("POST", ZP, {"zones": [], "base_revision": 1}, token=tok)      # 删了泳池:放宽,没确认
    return s.req("GET", ZP, token=tok)[1]["revision"]


def test_网页放宽要密码(站点):
    s = 站点
    rev = _放宽了(s, _登(s, "alice"))
    web = _网页(s)
    code, d = web(ZP + "/confirm", {"revision": rev, "confirm": True, "empty_ok": True})
    assert code == 403 and "密码" in d["error"]
    code, d = web(ZP + "/confirm", {"revision": rev, "confirm": True, "empty_ok": True,
                                    "confirm_password": "wrong-wrong-wrong"})
    assert code == 403
    code, d = web(ZP + "/confirm", {"revision": rev, "confirm": True, "empty_ok": True,
                                    "confirm_password": PW})
    assert code == 200, d


def test_网页收紧不要密码(站点):
    s = 站点
    tok = _登(s, "alice")
    s.req("POST", ZP, {"zones": [POND], "base_revision": 0}, token=tok)   # 从没确认过:要确认
    code, d = _网页(s)(ZP + "/confirm", {"revision": 1, "confirm": True})
    assert code == 200, d


def test_手机放宽照旧不要密码(站点):
    s = 站点
    tok = _登(s, "alice")
    rev = _放宽了(s, tok)
    code, d = s.req("POST", ZP + "/confirm", {"revision": rev, "confirm": True, "empty_ok": True},
                    token=tok)
    assert code == 200, d
