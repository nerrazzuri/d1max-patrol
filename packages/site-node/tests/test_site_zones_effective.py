"""区域的「草稿」和「在生效的」分开(外审 W38-I1,2026-10-15)。

原来改了区域就把**新的那一份**发给狗:删掉一个硬禁区、还没经管理员确认,狗已经收下、落了盘,遥控守卫里
已经没有它了 —— 确认只挡住了站点之后派的自主任务。

现在发给狗(改、确认、到期、补发)、跟狗报的核对(派单闸、运行配置摘要)、遥控开场提醒、拦截点可达,
都按**在生效的那一份**(:meth:`NavZones.effective`):

- 没有确认过的底(新版本、从没确认过)、或者当前这一份就是确认过的:在生效的 = 当前这一份;
- 当前这一份还没确认(放宽过):在生效的 = 当前这一份 + 最后确认过的那一份里**没被当前这一份盖住**的
  区域(到期了的不算)。狗守它手上的每一个区域,所以这一份不比两者任何一份松:收紧的当场生效(决策 66),
  删、缩、去掉硬标记、调高限速都等管理员确认;
- 确认一份放宽了的:在生效的内容变了,修订号加一(狗只认更高的修订号),马上发。
"""

from __future__ import annotations

import pytest
from test_site_api import MAP, PW, _等, 站
from test_site_zones import POND, SLOW

from d1max_contract.zones import ZoneSet
from d1max_site.db import SiteDB
from d1max_site.nav_zones import NavZones

HARD = POND | {"hard": True, "label": "泳池"}
BIG = {"id": "pond", "kind": "nogo", "label": "池子大一圈",
       "polygon": [[4, 4], [7, 4], [7, 7], [4, 7]]}
STEP = {"id": "step", "kind": "nogo", "hard": True, "label": "台阶",
        "polygon": [[10, 10], [11, 10], [11, 11], [10, 11]]}


def _z(tmp_path, t0=1000):
    t = [t0]
    return NavZones(SiteDB(tmp_path / "s.db"), now_ms=lambda: t[0]), t


def _ids(zs: ZoneSet) -> set[str]:
    return {z.id for z in zs.zones}


def _硬的几何(zs: ZoneSet) -> set:
    return {z.polygon for z in zs.hard()}


# ------------------------------------------------------------ 存储层

def test_从没确认过_在生效的就是当前的(tmp_path):
    z, _ = _z(tmp_path)
    z.put("m", "1", [HARD], base_revision=0, by="a")
    assert not z.confirmed_current("m", "1")
    assert z.effective("m", "1") == z.current("m", "1")


def test_删硬禁区没确认_在生效的里还有它_修订号跟草稿一样(tmp_path):
    z, _ = _z(tmp_path)
    z.put("m", "1", [HARD], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    z.put("m", "1", [], base_revision=1, by="a")
    assert not z.confirmed_current("m", "1")
    eff = z.effective("m", "1")
    assert eff.revision == 2
    assert _硬的几何(eff) == {tuple(map(tuple, POND["polygon"]))}, "没确认的删除不能让硬禁区消失"


def test_又加又删_加的当场生效_删的等确认(tmp_path):
    z, _ = _z(tmp_path)
    z.put("m", "1", [HARD], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    z.put("m", "1", [STEP], base_revision=1, by="a")             # 加台阶、删泳池:混合 = 放宽
    assert not z.confirmed_current("m", "1")
    eff = z.effective("m", "1")
    assert _ids(eff) >= {"step"}
    assert len(eff.hard()) == 2, "新加的台阶生效,删掉的泳池还守着"


def test_去掉硬标记没确认_还按硬的守_同名换个名字(tmp_path):
    z, _ = _z(tmp_path)
    z.put("m", "1", [HARD], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    z.put("m", "1", [POND], base_revision=1, by="a")             # 同一个 id,不硬了
    eff = z.effective("m", "1")
    assert len(eff.hard()) == 1
    assert len(_ids(eff)) == len(eff.zones), "id 不能重复(狗会整份拒)"
    assert ZoneSet.from_wire(eff.to_wire()) == eff, "发给狗的要过契约校验"


def test_调高限速没确认_按低的守(tmp_path):
    z, _ = _z(tmp_path)
    z.put("m", "1", [SLOW], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    z.put("m", "1", [SLOW | {"max_speed_mps": 1.0}], base_revision=1, by="a")
    eff = z.effective("m", "1")
    assert min(x.max_speed_mps for x in eff.slow()) == 0.3


def test_被草稿盖住的不重复带(tmp_path):
    """放大的泳池盖住了原来的:不用再带原来那个(只收紧,本来就当场算确认了)。"""
    z, _ = _z(tmp_path)
    z.put("m", "1", [POND], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    z.put("m", "1", [BIG, STEP], base_revision=1, by="a")
    assert z.confirmed_current("m", "1")
    assert z.effective("m", "1") == z.current("m", "1")


def test_确认放宽_修订号加一_在生效的变成草稿(tmp_path):
    z, _ = _z(tmp_path)
    z.put("m", "1", [HARD], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    z.put("m", "1", [], base_revision=1, by="a")
    before = z.effective("m", "1")
    z.confirm("m", "1", revision=2, by="admin")
    cur = z.current("m", "1")
    assert cur.revision == 3 and cur.zones == (), "内容变了要新修订号,狗只认更高的"
    assert z.confirmed_current("m", "1")
    assert z.effective("m", "1") == cur and cur.revision > before.revision


def test_确认没放宽的_修订号不动(tmp_path):
    z, _ = _z(tmp_path)
    z.put("m", "1", [HARD], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    assert z.current("m", "1").revision == 1


def test_到期了的临时区_站点删的时候确认过的底里一起删_出新修订号(tmp_path):
    z, t = _z(tmp_path)
    temp = STEP | {"until_ms": 5000}
    z.put("m", "1", [temp, HARD], base_revision=0, by="a")
    z.confirm("m", "1", revision=1, by="a")
    z.put("m", "1", [], base_revision=1, by="a")                  # 删光:没确认
    before = z.effective("m", "1")
    assert len(before.hard()) == 2
    t[0] = 6000
    assert z.effective("m", "1") == before, "同一个修订号内容不能自己变(狗不收、摘要对不上)"
    assert [x.map_version for x in z.expire()] == ["1"], "到期由站点删:确认过的那一份里的也删"
    eff = z.effective("m", "1")
    assert eff.revision == before.revision + 1
    assert {x.polygon for x in eff.hard()} == {tuple(map(tuple, POND["polygon"]))}
    assert not z.confirmed_current("m", "1"), "删掉的泳池还没确认"


# ------------------------------------------------------------ 经站点 API 到仿真狗

ZP = f"/api/maps/{MAP[0]}/{MAP[1]}/zones"


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    d = tmp_path / "cur"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"x")
    s.maps.import_dir(d, map_id=MAP[0], version=MAP[1])
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _rev(s):
    c = s.disp.clients["A"].capabilities
    z = None if c is None else c.tasks.get("zones_set")
    return None if z is None else z.get("rev")


def test_接口_删硬禁区没确认_狗上还守着_确认之后才没(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _rev(s) is not None)
    assert s.req("POST", ZP, {"zones": [HARD], "base_revision": 0}, token=alice)[0] == 200
    assert s.req("POST", ZP + "/confirm", {"revision": 1, "confirm": True}, token=alice)[0] == 200
    _等(lambda: _rev(s) == 1, timeout=8)
    assert len(s.agent._hard_zones()) == 1
    code, d = s.req("POST", ZP, {"zones": [], "base_revision": 1}, token=alice)
    assert code == 200 and d["auto_confirmed"] is False, d
    _等(lambda: _rev(s) == 2, timeout=8)
    assert len(s.agent._hard_zones()) == 1, "没确认的删除不能到狗上"
    assert len(s.agent.zonebook.get(*MAP).hard()) == 1, "落盘的也还有(狗重启照样守)"
    # 补发也按在生效的那一份
    assert len(s.disp.zones.effective(*MAP).hard()) == 1
    code, d = s.req("POST", ZP + "/confirm", {"revision": 2, "confirm": True, "empty_ok": True},
                    token=alice)
    assert code == 200 and d["revision"] == 3, d
    _等(lambda: _rev(s) == 3, timeout=8)
    _等(lambda: s.agent._zones is not None and not s.agent._zones.zones, timeout=8)
    assert s.agent._hard_zones() == []


def test_接口_又加又删_加的马上到狗上(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _rev(s) is not None)
    s.req("POST", ZP, {"zones": [HARD], "base_revision": 0}, token=alice)
    s.req("POST", ZP + "/confirm", {"revision": 1, "confirm": True}, token=alice)
    _等(lambda: _rev(s) == 1, timeout=8)
    s.req("POST", ZP, {"zones": [STEP], "base_revision": 1}, token=alice)
    _等(lambda: _rev(s) == 2, timeout=8)
    assert len(s.agent._hard_zones()) == 2


def test_派单闸和运行配置_按在生效的那一份核(站点):
    """狗收下的是在生效的那份,站点拿草稿算摘要会一直报「修订一样内容不一样」。"""
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _rev(s) is not None)
    s.req("POST", ZP, {"zones": [HARD], "base_revision": 0}, token=alice)
    s.req("POST", ZP + "/confirm", {"revision": 1, "confirm": True}, token=alice)
    s.req("POST", ZP, {"zones": [STEP], "base_revision": 1}, token=alice)
    _等(lambda: _rev(s) == 2, timeout=8)

    def 摘要对上():
        v = s.disp.running_view(s.disp.clients["A"])
        return v is not None and (v["zones"] or {}).get("match") is True
    _等(摘要对上, timeout=8)
