"""设位置(W00c6e):站点接口 → ``relocalize`` 命令;单狗视图带定位状态。真 HTTP、真代理(仿真狗)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _能设(s):
    _等(lambda: s.disp.clients["A"].capabilities is not None
        and "relocalize" in s.disp.clients["A"].capabilities.tasks)


def test_保安给位置_转成命令带上狗加载的图_审计记谁给的_业主不行(站点):
    s = 站点
    gina, olga = _登(s, "gina"), _登(s, "olga")
    _能设(s)
    assert s.req("POST", "/api/robots/A/relocalize", {"x": 1, "y": 2, "yaw": 0},
                 token=olga)[0] == 403
    code, d = s.req("POST", "/api/robots/A/relocalize", {"x": 1.5, "y": 2.0, "yaw": 0.3},
                    token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    [cmd] = [c for c in s.disp.commands("A") if c["kind"] == "relocalize"]
    p = cmd["payload"]
    assert (p["x"], p["y"], p["yaw"]) == (1.5, 2.0, 0.3)
    caps = s.disp.clients["A"].capabilities.tasks["patrol"]
    assert (p["map_id"], p["map_version"]) == (caps["map_id"], caps["map_version"])
    assert cmd["issued_by"] == "gina"
    audit = s.req("GET", "/api/audit", token=_登(s, "alice"))[1]["audit"]
    hit = [a for a in audit if "relocalize" in str(a.get("action", ""))]
    assert hit and hit[0]["actor"] == "gina" and hit[0]["target"] == "A", audit[:3]


def test_狗在原点_一键(站点):
    s = 站点
    gina = _登(s, "gina")
    _能设(s)
    code, d = s.req("POST", "/api/robots/A/relocalize", {"at_home": True}, token=gina)
    assert code == 200 and d["ack"]["result"] in ("accepted", "rejected"), d
    [cmd] = [c for c in s.disp.commands("A") if c["kind"] == "relocalize"]
    assert cmd["payload"]["at_home"] is True and "x" not in cmd["payload"]


@pytest.mark.parametrize("bad", [{}, {"x": "a", "y": 0, "yaw": 0}, {"x": 1, "y": 2},
                                 {"x": 1e400, "y": 0, "yaw": 0}, {"at_home": "yes"}])
def test_坐标不像话_400(站点, bad):
    s = 站点
    gina = _登(s, "gina")
    _能设(s)
    assert s.req("POST", "/api/robots/A/relocalize", bad, token=gina)[0] == 400


def test_老代理没有这项_409(站点):
    s = 站点
    gina = _登(s, "gina")
    _能设(s)
    s.disp.clients["A"].capabilities.tasks.pop("relocalize")
    code, d = s.req("POST", "/api/robots/A/relocalize", {"x": 0, "y": 0, "yaw": 0}, token=gina)
    assert code == 409 and "relocalize" in d["error"]


def test_单狗视图带定位状态(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: s.req("GET", "/api/robots/A", token=alice)[1].get("loc"))
    loc = s.req("GET", "/api/robots/A", token=alice)[1]["loc"]
    assert loc["source"] == "odom_identity" and loc["anchored"] is True
    assert loc["quality"] == 1.0 and loc["pose"] is not None and "x" in loc["pose"]


def test_老代理没报定位块_单狗视图不猜():
    """W00c6e 内审应修 4:老代理的遥测没有 ``loc``,以前视图还是给了 ``{quality, pose}``,手机就显示成
    「没设位置」。"""
    from types import SimpleNamespace

    from d1max_contract.messages import Telemetry
    from d1max_site.dispatcher import Dispatcher
    old = Telemetry(stamp=1, pose=None, battery_pct=50.0, task_state=None, loc_quality=1.0, net={})
    assert Dispatcher._loc_view(SimpleNamespace(telemetry=old)) is None
    assert Dispatcher._loc_view(None) is None


def test_设位置命令的有效期是30秒(站点, monkeypatch):
    """W00c6e 内审:晚到的旧命令会把「当时的位置」锚到「现在」—— 有效期比一般命令(60 s)短。"""
    s = 站点
    gina = _登(s, "gina")
    _能设(s)
    seen = []
    real = s.disp._send

    async def 记(*a, **k):
        seen.append(k.get("ttl_ms"))
        return await real(*a, **k)
    monkeypatch.setattr(s.disp, "_send", 记)
    s.req("POST", "/api/robots/A/relocalize", {"x": 0, "y": 0, "yaw": 0}, token=gina)
    assert seen == [30_000]


def test_客户端说了在哪张图上点的_跟狗加载的不一样就不发_一样照发(站点):
    """外审 W39-I2(2026-10-15):网页底图跟选中的狗可能不是同一张图,原来坐标按狗加载的图补上图号
    就发了。现在网页、手机带上「我是在哪张图上点的」,站点核对;不带的(老客户端)照旧。"""
    s = 站点
    gina = _登(s, "gina")
    _能设(s)
    caps = s.disp.clients["A"].capabilities.tasks["patrol"]
    code, d = s.req("POST", "/api/robots/A/relocalize",
                    {"x": 5, "y": 3, "yaw": 0, "map_id": "other-map", "map_version": "1"},
                    token=gina)
    assert code == 409 and "other-map" in d["error"], d
    code, d = s.req("POST", "/api/robots/A/relocalize",
                    {"x": 5, "y": 3, "yaw": 0, "map_id": caps["map_id"], "map_version": "999"},
                    token=gina)
    assert code == 409, d
    assert not [c for c in s.disp.commands("A") if c["kind"] == "relocalize"], "被拒的不能发出去"
    code, d = s.req("POST", "/api/robots/A/relocalize",
                    {"x": 5, "y": 3, "yaw": 0, "map_id": caps["map_id"],
                     "map_version": caps["map_version"]}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d


@pytest.mark.parametrize("bad", [{"map_id": "m"}, {"map_version": "1"},
                                 {"map_id": 3, "map_version": "1"},
                                 {"map_id": "", "map_version": "1"}])
def test_图号版本要成对_要是字符串(站点, bad):
    s = 站点
    gina = _登(s, "gina")
    _能设(s)
    assert s.req("POST", "/api/robots/A/relocalize", {"x": 1, "y": 2, "yaw": 0} | bad,
                 token=gina)[0] == 400


def test_W41_自动找位置_狗报了才发_不是true_400(站点):
    s = 站点
    gina = _登(s, "gina")
    _能设(s)
    assert s.req("POST", "/api/robots/A/relocalize", {"global": 1}, token=gina)[0] == 400
    code, d = s.req("POST", "/api/robots/A/relocalize", {"global": True}, token=gina)
    assert code == 409 and "自动找位置" in d["error"], "仿真机器人没配避障和定位器:不能"
    s.disp.clients["A"].capabilities.tasks["relocalize"]["global"] = True
    code, d = s.req("POST", "/api/robots/A/relocalize", {"global": True}, token=gina)
    assert code == 200, d
    [cmd] = [c for c in s.disp.commands("A") if c["kind"] == "relocalize"]
    assert cmd["payload"]["global"] is True and "x" not in cmd["payload"]
    assert d["ack"]["result"] == "rejected" and "不能自动找位置" in d["ack"]["reason"], \
        "能力是测试里硬改的,机器人其实没配:机器人拒,回执原样回"
