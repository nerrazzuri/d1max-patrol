"""在当前位置标原点(W00c6f):站点发 ``mark_home``,狗用此刻锚定后的位置、定位不好就拒;站点把回执里的
位置登记成这只狗在这张图上的默认待命点。真 HTTP、真代理(仿真狗:按原样锚定,开机就可信)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站


@pytest.fixture
def 站点(tmp_path):
    from d1max_site.standby import StandbyManager
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.api.standby = StandbyManager(s.db, s.disp, now_ms=s.disp._now)
    s.stb_list = s.api.standby.list
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _能标(s):
    _等(lambda: s.disp.clients["A"].capabilities is not None
        and "mark_home" in s.disp.clients["A"].capabilities.tasks)


def test_管理员在这儿标原点_登记成默认待命点_保安不行_审计(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    _能标(s)
    assert s.req("POST", "/api/robots/A/home/here", {"name": "dock"}, token=gina)[0] == 403
    code, d = s.req("POST", "/api/robots/A/home/here", {"name": "dock"}, token=alice)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    [p] = [x for x in s.stb_list("A") if x["name"] == "dock"]
    caps = s.disp.clients["A"].capabilities.tasks["patrol"]
    assert p["default"] and (p["map_id"], p["map_version"]) == (caps["map_id"], caps["map_version"])
    assert (p["x"], p["y"]) == (d["ack"]["data"]["x"], d["ack"]["data"]["y"])
    assert d["standby"]["name"] == "dock"
    audit = s.req("GET", "/api/audit", token=alice)[1]["audit"]
    assert any(a["actor"] == "alice" and "home/here" in a["action"] for a in audit), audit[:3]


def test_没给名字叫home(站点):
    s = 站点
    alice = _登(s, "alice")
    _能标(s)
    code, d = s.req("POST", "/api/robots/A/home/here", {}, token=alice)
    assert code == 200 and d["standby"]["name"] == "home", d


def test_狗拒了_409带原因_不登记(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _能标(s)

    async def 拒(*a, **k):
        return {"ack": {"result": "rejected", "reason": "loc_poor: 位置偏差可能到 0.9 m"}}
    monkeypatch.setattr(s.disp, "map_command", 拒)
    code, d = s.req("POST", "/api/robots/A/home/here", {"name": "dock"}, token=alice)
    assert code == 409 and "偏差" in d["error"], d
    assert not [x for x in s.stb_list("A") if x["name"] == "dock"]


def test_名字不像话_400_老代理_409(站点):
    s = 站点
    alice = _登(s, "alice")
    _能标(s)
    assert s.req("POST", "/api/robots/A/home/here", {"name": "a b"}, token=alice)[0] == 400
    s.disp.clients["A"].capabilities.tasks.pop("mark_home")
    code, d = s.req("POST", "/api/robots/A/home/here", {}, token=alice)
    assert code == 409 and "mark_home" in d["error"]


def test_重投的回执_位置在第一次的结果里(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _能标(s)
    first = {"map_id": "estate-1", "map_version": "7", "x": 4.0, "y": 5.0, "yaw": 0.0,
             "sigma_m": 0.2}

    async def 重投(*a, **k):
        return {"ack": {"result": "duplicate", "reason": "",
                        "original": {"result": "accepted", "data": first}}}
    monkeypatch.setattr(s.disp, "map_command", 重投)
    code, d = s.req("POST", "/api/robots/A/home/here", {"name": "gate"}, token=alice)
    assert code == 200, d
    [p] = [x for x in s.stb_list("A") if x["name"] == "gate"]
    assert (p["x"], p["y"]) == (4.0, 5.0)
