"""上装(W21):手机 → 站点 → 代理 → 仿真狗的上装。真 HTTP、真代理。保安、管理员能用,业主不能;
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


def test_业主不能用_没登录401_载荷不对400(站点):
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
