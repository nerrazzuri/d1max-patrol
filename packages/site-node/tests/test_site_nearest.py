"""W44:让最近的机器人过去看看(设计 §4.3):按规划路程挑,不按直线;只挑同一张图同一版上的。

图是 test_site_intercept_reach 那两间屋子:x=4 m 一道墙,墙正中(y=2 m)开门;
wide 门 1.6 m、narrow 门 0.8 m(机身过不去)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, 站
from test_site_intercept_reach import 台  # noqa: F401  (pytest 夹具)

from d1max_site.nearest import NOMINAL_MPS, nearest


def test_路程_走得通回米数_走不通回None(台):  # noqa: F811
    m = 台.reach.path_m("house", "wide", (1.5, 2.0), (6.5, 2.0))
    assert m is not None and 5.0 <= m <= 6.0
    assert 台.reach.path_m("house", "narrow", (1.5, 2.0), (6.5, 2.0)) is None
    assert 台.reach.path_m("house", "wide", (1.5, 2.0), (50.0, 2.0)) is None, "出了图"
    assert 台.reach.path_m("house", "nope", (1.5, 2.0), (2.5, 2.0)) is None, "站点没有这一版"


def test_最近的_按路程不按直线(台):  # noqa: F811
    """R1 在左屋,离目标直线 2 m,但要绕门(约 3 m);R2 在右屋,直线约 2.2 m、能直走。挑 R2。"""
    cands = [("R1", "house", "wide", 3.0, 0.9), ("R2", "house", "wide", 6.4, 2.6)]
    got = nearest(台.reach, cands, "house", "wide", 5.0, 0.9)
    assert got is not None and got["robot_id"] == "R2"
    assert got["eta_s"] == round(got["path_m"] / NOMINAL_MPS)


def test_最近的_只挑同一张图同一版(台):  # noqa: F811
    cands = [("R1", "house", "narrow", 4.9, 0.9), ("R2", "house", "wide", 1.5, 2.0),
             ("R3", "other", "wide", 5.0, 0.9)]
    got = nearest(台.reach, cands, "house", "wide", 5.0, 0.9)
    assert got is not None and got["robot_id"] == "R2"
    assert nearest(台.reach, cands[:1] + cands[2:], "house", "wide", 5.0, 0.9) is None


def test_最近的_走不到的不选(台):  # noqa: F811
    cands = [("R1", "house", "narrow", 1.5, 2.0)]
    assert nearest(台.reach, cands, "house", "narrow", 6.5, 2.0) is None


# ------------------------------------------------------------ 接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def test_最近的接口_没有能派的404(站点):
    s = 站点
    gina = _登(s, "gina")
    code, d = s.req("GET", "/api/robots/nearest?map_id=nope&map_version=1&x=1&y=2", token=gina)
    assert code == 404 and d["error"] == "这张图上没有能派的机器人"


def test_最近的接口_参数和权限(站点):
    s = 站点
    gina, olga = _登(s, "gina"), _登(s, "olga")
    assert s.req("GET", "/api/robots/nearest?map_id=m&map_version=1&x=a&y=2", token=gina)[0] == 400
    assert s.req("GET", "/api/robots/nearest?map_id=m&x=1&y=2", token=gina)[0] == 400
    assert s.req("GET", "/api/robots/nearest?map_id=m&map_version=1&x=1&y=2", token=olga)[0] == 403
