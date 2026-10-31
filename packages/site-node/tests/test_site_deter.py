"""上装(W21):手机 → 站点 → 代理 → 仿真狗的上装。真 HTTP、真代理。保安、管理员能用,管理员不能;
喇叭的优先级由站点定(请求体里的不认);没装上装的狗回 409;关永远放行;进审计。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, payload=True)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _新鲜(s, tok):
    _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    _等(lambda: "deter" in ((s.req("GET", "/api/robots/A", token=tok)[1].get("capabilities")
                             or {}).get("tasks") or {}))


def test_保安开警笛_狗上开了_到点自己关_进审计(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    code, d = s.req("POST", "/api/robots/A/deter",
                    {"output": "siren", "on": True, "max_s": 1}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    assert s.dog.deter_on("siren")
    _等(lambda: not s.dog.deter_on("siren"), timeout=5)
    code, d = s.req("POST", "/api/robots/A/deter", {"output": "siren", "on": False}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted"
    rows = [r for r in s.api.audit.list() if r["action"] == "POST /api/robots/A/deter"]
    assert rows and rows[-1]["actor"] == "gina" and rows[-1]["detail"]["output"] == "siren"


def test_管理员不能用_没登录401_载荷不对400(站点):
    s = 站点
    olga, gina = _登(s, "olga"), _登(s, "gina")
    _新鲜(s, gina)
    body = {"output": "strobe", "on": True, "max_s": 5}
    assert s.req("POST", "/api/robots/A/deter", body, token=olga)[0] == 403
    assert s.req("POST", "/api/robots/A/deter", body)[0] == 401
    for bad in ({"output": "strobe", "on": True}, {"output": "horn", "on": True, "max_s": 5},
                {"output": "strobe", "on": True, "max_s": 601}, [1]):
        assert s.req("POST", "/api/robots/A/deter", bad, token=gina)[0] == 400, bad
    assert not s.dog.deter_on("strobe")


def test_喇叭优先级站点定_请求体里的不认(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    code, d = s.req("POST", "/api/robots/A/deter",
                    {"output": "speaker", "on": True, "max_s": 30, "clip": "warn-zh",
                     "priority": 100}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    assert s.dog.deter_clip == "warn-zh"
    cmd = [c for c in s.disp.commands("A", 20) if c["kind"] == "deter"][0]
    assert cmd["payload"]["priority"] == 60, "手动的一律 MANUAL"


def test_没装上装的狗_回409(tmp_path):
    s = 站(tmp_path)
    try:
        tok = s.login()
        _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
        code, d = s.req("POST", "/api/robots/A/deter",
                        {"output": "siren", "on": True, "max_s": 5}, token=tok)
        assert code == 409 and "上装" in d["error"]
    finally:
        s.close()


def test_狗报的上装能力_站点原样给手机(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    caps = s.req("GET", "/api/robots/A", token=gina)[1]["capabilities"]["tasks"]["deter"]
    assert caps["outputs"] == ["strobe", "siren", "spotlight", "speaker"]
    assert caps["clips"] == ["warn-zh", "warn-en"]


def test_W21复查_狗报上装关不上_站点出P1_好了自己解决(tmp_path):
    """端到端:仿真狗报 ``payload_siren`` 故障 → 真代理发 robot_fault → 真站点告警源出告警;
    消了就解决。"""
    from d1max_contract.hal import Fault
    s = 站(tmp_path, payload=True, alerts=True)
    try:
        tok = s.login()
        _新鲜(s, tok)
        s.dog._faults = [Fault(code="payload_siren", fatal=False, text="上装警笛状态不明")]

        def 开着的():
            return [a for a in s.req("GET", "/api/alerts", token=tok)[1]["alerts"]
                    if a["kind"] == "payload_siren"]
        [a] = _等(开着的)
        assert a["level"] == "P1" and a["robot"] == "A"
        s.dog._faults = []
        _等(lambda: not 开着的())
    finally:
        s.close()


# -------------------------------------------- W40:上装 V1.2(左右灯、警灯颜色)


def test_W40_只开左边聚光灯_挑警灯颜色_狗上照做(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    caps = s.req("GET", "/api/robots/A", token=gina)[1]["capabilities"]["tasks"]["deter"]
    assert caps["spotlight_sides"] == ["left", "right"] and "yellow" in caps["strobe_colors"]
    assert caps["siren_via"] == "speaker"
    code, d = s.req("POST", "/api/robots/A/deter",
                    {"output": "spotlight", "on": True, "max_s": 30, "side": "left"}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    assert s.dog.deter_on("spotlight_left") and not s.dog.deter_on("spotlight_right")
    code, d = s.req("POST", "/api/robots/A/deter",
                    {"output": "strobe", "on": True, "max_s": 30, "color": "yellow"}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    assert s.dog.deter_color == "yellow"
    rows = [r for r in s.api.audit.list() if r["action"] == "POST /api/robots/A/deter"]
    assert any(r["detail"].get("color") == "yellow" for r in rows)
    assert any(r["detail"].get("side") == "left" for r in rows)


def test_W40_狗没报左右_颜色_站点挡住不发(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    deter = s.dog.deter_options
    # 当成老狗(上装 W21 那套):能力里没有左右、颜色。改了要等狗重报能力
    s.dog.deter_options = lambda: {}
    s.loop.call(s.agent._publish_caps)
    _等(lambda: "spotlight_sides" not in (s.req("GET", "/api/robots/A", token=gina)[1]
                                          ["capabilities"]["tasks"]["deter"]), timeout=5)
    n = len([c for c in s.disp.commands("A", 50) if c["kind"] == "deter"])
    code, d = s.req("POST", "/api/robots/A/deter",
                    {"output": "spotlight", "on": True, "max_s": 30, "side": "left"}, token=gina)
    assert code == 409 and "不分左右" in d["error"], d
    code, d = s.req("POST", "/api/robots/A/deter",
                    {"output": "strobe", "on": True, "max_s": 30, "color": "red"}, token=gina)
    assert code == 409 and "red" in d["error"], d
    assert len([c for c in s.disp.commands("A", 50) if c["kind"] == "deter"]) == n, "未发出去"
    s.dog.deter_options = deter


def test_W40_side_color写错400(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    for bad in ({"output": "spotlight", "on": True, "max_s": 5, "side": "middle"},
                {"output": "strobe", "on": True, "max_s": 5, "side": "left"},
                {"output": "spotlight", "on": True, "max_s": 5, "color": "red"},
                {"output": "strobe", "on": False, "color": "red"},
                {"output": "strobe", "on": True, "max_s": 5, "color": "Red!"}):
        assert s.req("POST", "/api/robots/A/deter", bad, token=gina)[0] == 400, bad


def test_W40_设备健康_狗带上来_单狗视图里看得到(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    _等(lambda: (s.req("GET", "/api/robots/A", token=gina)[1].get("devices") or {}).get("items"),
        timeout=5)
    d = s.req("GET", "/api/robots/A", token=gina)[1]["devices"]
    got = {x["id"]: x["state"] for x in d["items"]}
    assert got["relay"] == "online" and got["speaker"] == "online" and d["stale"] is False
    s.dog.device_faults["relay"] = ("error", "左聚光灯未确认")
    s.agent._next_devices_ms = 0                     # 不等 10 s
    _等(lambda: {x["id"]: x["state"] for x in s.req("GET", "/api/robots/A", token=gina)[1]
                ["devices"]["items"]}["relay"] == "error", timeout=5)
    # 过时了标出来(狗 10 s 带一次,丢了两三份)
    s.disp.devices["A"] = (s.disp.devices["A"][0], s.disp.devices["A"][1] - 60_000)
    assert s.req("GET", "/api/robots/A", token=gina)[1]["devices"]["stale"] is True
