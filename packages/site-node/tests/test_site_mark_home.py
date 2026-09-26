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
    # 审计在回了响应之后才写:等它。
    [a] = _等(lambda: [a for a in s.req("GET", "/api/audit", token=alice)[1]["audit"]
                       if a["actor"] == "alice" and "home/here" in a["action"]
                       and a["status"] == 200])
    det = a["detail"]
    assert det["name"] == "dock" and det["x"] == p["x"] and det["y"] == p["y"], det
    assert det["map"] == f"{p['map_id']}:{p['map_version']}" and det["command_id"], det


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


def test_重投_原命令被拒_409带原来的原因_不说狗上换了(站点, monkeypatch):
    """内审应修 2:重投回执里的原结果是拒收,以前取不到位置、回 500「狗上的原点已经换了」。"""
    s = 站点
    alice = _登(s, "alice")
    _能标(s)

    seen = {}

    async def 重投(*a, **k):
        seen.update(k)
        return {"command_id": "c1", "task_id": "mark_home-1",
                "ack": {"result": "duplicate", "reason": "",
                        "original": {"result": "rejected", "reason": "moving"}}}
    monkeypatch.setattr(s.disp, "map_command", 重投)
    code, d = s.req("POST", "/api/robots/A/home/here", {"name": "gate"}, token=alice)
    assert code == 409 and "moving" in d["error"] and "换了" not in d["error"], (code, d)
    assert seen["ttl_ms"] == 30_000, "有效期同设位置:晚到的旧命令少一点"


def test_站点登记没成_500带狗上的位置和命令号_审计也有(站点, monkeypatch):
    """内审应修 7:狗上的原点已经换了、站点没登记上 —— 管理员要拿到坐标才能手工补登。"""
    from d1max_site.standby import StandbyError
    s = 站点
    alice = _登(s, "alice")
    _能标(s)

    def 坏(*a, **k):
        raise StandbyError("库写不进去")
    monkeypatch.setattr(s.api.standby, "set", 坏)
    code, d = s.req("POST", "/api/robots/A/home/here", {"name": "dock"}, token=alice)
    assert code == 500 and "换了" in d["error"], (code, d)
    assert d["command_id"] and isinstance(d["data"]["x"], float) and d["data"]["map_id"], d
    [a] = _等(lambda: [a for a in s.req("GET", "/api/audit", token=alice)[1]["audit"]
                       if "home/here" in a["action"] and a["status"] == 500])
    assert a["detail"]["x"] == d["data"]["x"] and a["detail"]["command_id"] == d["command_id"]


def test_同名待命点在别的图上_409_明说替换才搬(站点):
    """内审应修 5:同名的点按(狗, 名字)合并 —— 以前会把别的图上的 ``home`` 悄悄搬过来。"""
    s = 站点
    alice = _登(s, "alice")
    _能标(s)
    s.api.standby.set("A", "home", map_id="other", map_version="9", x=1.0, y=1.0, yaw=0.0,
                      default=False)
    code, d = s.req("POST", "/api/robots/A/home/here", {}, token=alice)
    assert code == 409 and d["name_taken"] == {"map_id": "other", "map_version": "9"}, (code, d)
    assert not s.db.query("SELECT 1 FROM commands WHERE kind='mark_home'"), "没发给狗"
    [p] = s.stb_list("A")
    assert (p["map_id"], p["x"]) == ("other", 1.0)
    code, d = s.req("POST", "/api/robots/A/home/here", {"replace": True}, token=alice)
    assert code == 200, d
    [p] = s.stb_list("A")
    assert p["map_id"] != "other" and p["default"]
    assert s.req("POST", "/api/robots/A/home/here", {}, token=alice)[0] == 200, "同一张图:直接改"
    assert s.req("POST", "/api/robots/A/home/here", {"replace": 1}, token=alice)[0] == 400


def test_回执超时_狗其实标了_站点按事件补登记_504说可能标了(站点, monkeypatch):
    """内审应修 1:站点等回执超时,命令照样到了狗那儿、狗标了;站点按狗发的 ``home_marked`` 补登记。"""
    from d1max_contract.dispatch import DispatchTimeout
    s = 站点
    alice = _登(s, "alice")
    _能标(s)
    cl = s.disp.clients["A"]
    原来的 = cl.send

    async def 回执丢了(cmd, *, timeout_s):
        await 原来的(cmd, timeout_s=timeout_s)
        raise DispatchTimeout("回执丢了")
    monkeypatch.setattr(cl, "send", 回执丢了)
    code, d = s.req("POST", "/api/robots/A/home/here", {"name": "dock"}, token=alice)
    assert code == 504 and "可能已经标了" in d["error"], (code, d)
    [p] = _等(lambda: [x for x in s.stb_list("A") if x["name"] == "dock"])
    assert p["default"]
    h = s.agent.parts.home.pose.position
    assert (round(p["x"], 2), round(p["y"], 2)) == (round(h.x, 2), round(h.y, 2))


def _命令(s, cid, task_id, at, result):
    with s.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, ack_result) VALUES (?,?,'A','mark_home','{}','alice',?,?)",
                  (cid, task_id, at, result))


def _标了(s, task_id, x, seq):
    from d1max_contract.messages import Event
    return Event(event_id=f"e{seq}", seq=seq, boot_id="b", stamp=1, kind="home_marked",
                 data={"task_id": task_id, "name": "home", "map_id": "estate-1",
                       "map_version": "7", "x": x, "y": 0.0, "yaw": 0.0, "sigma_m": 0.2})


def test_补登记只认这台狗最新那条没被拒的标原点(站点):
    """晚到的旧事件不许盖掉后来标的;后来那次狗拒了,狗上的原点就是前一次的 —— 那就认前一次的。"""
    s = 站点
    stb = s.api.standby
    _命令(s, "c1", "mark_home-1", 1000, "timeout")
    _命令(s, "c2", "mark_home-2", 2000, "accepted")
    stb._on_event("A", _标了(s, "mark_home-2", 2.0, 2))
    stb._on_event("A", _标了(s, "mark_home-1", 1.0, 1))              # 晚到的旧的
    [p] = s.stb_list("A")
    assert p["x"] == 2.0 and p["default"]
    _命令(s, "c3", "mark_home-3", 3000, "rejected")
    _命令(s, "c4", "mark_home-4", 4000, None)
    stb._on_event("A", _标了(s, "mark_home-4", 4.0, 4))
    assert s.stb_list("A")[0]["x"] == 4.0
    with s.db.tx() as c:
        c.execute("UPDATE commands SET ack_result='expired' WHERE command_id='c4'")
    stb._on_event("A", _标了(s, "mark_home-2", 2.5, 5))
    assert s.stb_list("A")[0]["x"] == 2.5, "3 被拒、4 过期:狗上是 2 那次的"
    stb._on_event("A", _标了(s, "mark_home-9", 9.0, 6))
    assert s.stb_list("A")[0]["x"] == 2.5, "站点没发过的不认"
