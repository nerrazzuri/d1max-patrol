"""POC 第 2 项(用户 11-05 改版):遥控按速度分档。管理员设一张速度表(默认 0.3 / 0.6 / 1.0 /
1.3 / 1.6 / 2.0 m/s);开遥控时站点把表里最快的带给机器人,手机在 ``granted`` 里拿到这台能到的
那几档(低速档实测 0.98 也算 1.0 档);手机每帧带选中的速度 ``vmax``,站点按它夹、转给机器人
—— 超过 1.0 机器人自己换中速档。老机器人照旧一半。真 HTTP、真代理。"""

from __future__ import annotations

import time

import pytest
from test_site_api import PW, 站
from test_site_teleop import _odom, _新鲜, _登, 手机

DEFAULT = [0.3, 0.6, 1.0, 1.3, 1.6, 2.0]


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, teleop={"renew_s": 0.5})
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


@pytest.fixture
def 中速站点(tmp_path):
    s = 站(tmp_path, teleop={"renew_s": 0.5}, dog={"max_vx_mid": 2.0})
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _推(ph, vx, vmax, seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        ph.send({"vx": vx, "wz": 0.0, "vmax": vmax})
        time.sleep(0.1)


def test_管理员设速度表_校验_保安只能看(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    code, d = s.req("GET", "/api/teleop-speeds", token=gina)
    assert code == 200 and d == {"speeds": DEFAULT}, d
    assert s.req("POST", "/api/teleop-speeds", {"speeds": [0.5]}, token=gina)[0] == 403
    for bad in ({"speeds": []}, {"speeds": [0]}, {"speeds": [0.3, 2.1]}, {"speeds": [0.6, 0.3]},
                {"speeds": [0.3, 0.3]}, {"speeds": ["x"]}, {"speeds": [True]}, {"speeds": 1},
                {"speeds": [0.1 * i for i in range(1, 10)]}, {}):
        assert s.req("POST", "/api/teleop-speeds", bad, token=alice)[0] == 400, bad
    code, d = s.req("POST", "/api/teleop-speeds", {"speeds": [0.4, 0.8, 1.2]}, token=alice)
    assert code == 200 and d == {"speeds": [0.4, 0.8, 1.2]}, d
    assert s.req("GET", "/api/teleop-speeds", token=gina)[1]["speeds"] == [0.4, 0.8, 1.2]
    assert any(r["action"] == "POST /api/teleop-speeds"
               and r["detail"].get("speeds") == [0.4, 0.8, 1.2] for r in s.api.audit.list())


def test_机器人只有低速档_手机只拿到不超过1点0的档_满推按选的速度走(站点):
    s = 站点
    tok = _登(s, "gina")
    _新鲜(s, tok)
    ph = 手机(s, tok)
    g = ph.wait_kind("granted")
    assert g["max_vx"] == pytest.approx(1.0) and g["speeds"] == [0.3, 0.6, 1.0], g
    _推(ph, 3.0, 0.6, 1.2)
    o = _odom(s)
    assert 0.45 < o.vx <= 0.6 + 1e-6, o             # 选了 0.6:站点、机器人都按它夹
    ph.close()


def test_机器人能换中速档_手机拿到全部档_选1点6按1点6走(中速站点):
    s = 中速站点
    tok = _登(s, "gina")
    _新鲜(s, tok)
    ph = 手机(s, tok)
    g = ph.wait_kind("granted")
    assert g["max_vx"] == pytest.approx(2.0) and g["speeds"] == DEFAULT, g
    assert g["max_wz"] == pytest.approx(1.5), "转向不跟着中速档变快"
    _推(ph, 0.0, 1.6, 0.5)                          # 先松着杆:机器人换档
    _推(ph, 1.6, 1.6, 1.5)
    o = _odom(s)
    assert 1.3 < o.vx <= 1.6 + 1e-6, o
    ph.close()


def test_老机器人没报机身上限_照旧一半_档位也只给到一半(站点):
    s = 站点
    tok = _登(s, "gina")
    _新鲜(s, tok)
    t = s.disp.clients["A"].capabilities.tasks["teleop"]
    for k in ("max_vx_hal", "max_wz_hal"):
        t.pop(k)
    ph = 手机(s, tok)
    g = ph.wait_kind("granted")
    assert g["max_vx"] == pytest.approx(0.5)
    assert g["speeds"] == [0.3, 0.5], g
    ph.close()
