"""POC 第 2 项:遥控速度档。管理员设慢/中/快(m/s);开遥控时站点把快档带给机器人(机器人报了机身上限
才带),手机在 ``granted`` 里拿到三档(都不超过这次的上限)。老机器人照旧一半。真 HTTP、真代理。"""

from __future__ import annotations

import pytest
from test_site_api import PW, 站
from test_site_teleop import _odom, _推, _新鲜, _登, 手机


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, teleop={"renew_s": 0.5})
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def test_管理员设速度档_校验_保安只能看(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    code, d = s.req("GET", "/api/teleop-speeds", token=gina)
    assert code == 200 and d == {"slow": 0.3, "mid": 0.6, "fast": 1.0}, d
    assert s.req("POST", "/api/teleop-speeds", {"fast": 0.8}, token=gina)[0] == 403
    for bad in ({"slow": 0}, {"fast": 1.6}, {"slow": 0.7}, {"mid": "x"}, {"fast": True}):
        assert s.req("POST", "/api/teleop-speeds", bad, token=alice)[0] == 400, bad
    code, d = s.req("POST", "/api/teleop-speeds", {"mid": 0.5, "fast": 0.8}, token=alice)
    assert code == 200 and d == {"slow": 0.3, "mid": 0.5, "fast": 0.8}, d
    assert s.req("GET", "/api/teleop-speeds", token=gina)[1]["fast"] == 0.8
    assert any(r["action"] == "POST /api/teleop-speeds" and r["detail"].get("fast") == 0.8
               for r in s.api.audit.list())


def test_开遥控_快档带给机器人_手机拿到三档_满推按快档走(站点):
    s = 站点
    alice, tok = _登(s, "alice"), _登(s, "gina")
    s.req("POST", "/api/teleop-speeds", {"fast": 0.8}, token=alice)
    _新鲜(s, tok)
    ph = 手机(s, tok)
    g = ph.wait_kind("granted")
    assert g["max_vx"] == pytest.approx(0.8) and g["speeds"] == {"slow": 0.3, "mid": 0.6,
                                                                  "fast": 0.8}, g
    assert g["max_wz"] == pytest.approx(1.5 * 0.8 / 1.0)
    _推(ph, 3.0, seconds=1.2)
    o = _odom(s)
    assert 0.6 < o.vx <= 0.8 + 1e-6, o             # 不是老规矩的 0.5
    ph.close()


def test_老机器人没报机身上限_照旧一半_档位也夹到一半(站点):
    s = 站点
    tok = _登(s, "gina")
    _新鲜(s, tok)
    t = s.disp.clients["A"].capabilities.tasks["teleop"]
    for k in ("max_vx_hal", "max_wz_hal"):
        t.pop(k)
    ph = 手机(s, tok)
    g = ph.wait_kind("granted")
    assert g["max_vx"] == pytest.approx(0.5)
    assert g["speeds"] == {"slow": 0.3, "mid": 0.5, "fast": 0.5}, g
    ph.close()
