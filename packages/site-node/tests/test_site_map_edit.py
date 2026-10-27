"""W44 第四期:地图编辑(橡皮擦、画障碍,另存新版本)、显示设置(摆正、底图、区域命名、特殊地面)、
网页上换图要再输密码。"""

from __future__ import annotations

import json
import urllib.request

import pytest
from test_site_api import MAP, PW, 站
from test_site_intercept_reach import _屋子
from test_site_maps import _files

from d1max_site import map_edit as me
from d1max_site import map_preview as mp
from d1max_site.areas import AreaBook
from d1max_site.map_display import DisplayError, check

# 屋子:8 m × 4 m,分辨率 0.1,x=4 m 处一道墙(门 1.6 m 宽),外圈一圈墙
META = {"resolution": 0.1, "origin": [0.0, 0.0, 0.0], "negate": 0, "occupied_thresh": 0.65,
        "free_thresh": 0.196}


def _图(tmp_path):
    src = _files(tmp_path / "m")
    _屋子(src, 1.6)
    return (src / "floor.pgm").read_bytes()


def _值(pgm, x, y):
    w, h, body = mp.parse_pgm(pgm)
    c, r = int(x / 0.1), h - 1 - int(y / 0.1)
    return body[r * w + c]


def test_橡皮擦改成空地(tmp_path):
    pgm = _图(tmp_path)
    assert _值(pgm, 4.0, 0.5) == 0                      # 墙
    ops = me.check_ops([{"op": "erase", "shape": "stroke", "points": [[4.0, 0.3], [4.0, 1.0]],
                         "width_m": 0.4}])
    new, st = me.apply_ops(pgm, META, ops)
    assert _值(new, 4.0, 0.5) == 254 and st["erased_m2"] > 0 and st["obstacle_m2"] == 0


def test_画障碍改成占用_直线和方框(tmp_path):
    pgm = _图(tmp_path)
    ops = me.check_ops([{"op": "obstacle", "shape": "line", "points": [[1.0, 1.0], [2.0, 1.0]],
                         "width_m": 0.1},
                        {"op": "obstacle", "shape": "rect", "points": [[5.0, 3.0], [5.5, 3.5]]}])
    new, st = me.apply_ops(pgm, META, ops)
    assert _值(new, 1.5, 1.0) == 0 and _值(new, 5.25, 3.25) == 0
    assert _值(new, 1.5, 2.0) == 254
    assert st["obstacle_m2"] > 0.25


@pytest.mark.parametrize("bad", [[], [{"op": "paint", "shape": "stroke", "points": [[0, 0]]}],
                                 [{"op": "erase", "shape": "rect", "points": [[0, 0]]}],
                                 [{"op": "erase", "shape": "stroke", "points": [[0, 0]],
                                   "width_m": 5}],
                                 [{"op": "erase", "shape": "stroke", "points": [[0, 0]]}] * 501])
def test_操作不合规矩(bad):
    with pytest.raises(me.EditError):
        me.check_ops(bad)


# ------------------------------------------------------------ 接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.accounts.add("gina", PW, role="guard")
    src = _files(tmp_path / "mapsrc")
    _屋子(src, 1.6)
    s.maps.import_dir(src, map_id=MAP[0], version=MAP[1])
    s.api.areas = AreaBook(s.db, now_ms=lambda: 1)
    s.api.home = tmp_path / "home"
    s.api.home.mkdir()
    yield s
    s.close()


def _登(s, name="alice"):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


ADM = f"/api/admin/maps/{MAP[0]}/{MAP[1]}"
ERASE = [{"op": "erase", "shape": "stroke", "points": [[4.0, 0.3], [4.0, 1.0]], "width_m": 0.4}]


def test_只另存新版本_原版不动(站点):
    s = 站点
    tok = _登(s)
    dg = s.req("GET", f"{ADM}/noise", token=tok)[1]["digest"]
    before = (s.maps.root / MAP[0] / MAP[1] / "floor.pgm").read_bytes()
    code, d = s.req("POST", f"{ADM}/edit", {"digest": dg, "ops": ERASE}, token=tok)
    assert code == 200, d
    assert d["version"] != MAP[1] and d["erased_m2"] > 0
    assert (s.maps.root / MAP[0] / MAP[1] / "floor.pgm").read_bytes() == before
    new = (s.maps.root / MAP[0] / d["version"] / "floor.pgm").read_bytes()
    assert _值(new, 4.0, 0.5) == 254


def test_栅格换了不让存(站点):
    s = 站点
    code, d = s.req("POST", f"{ADM}/edit", {"digest": "0" * 16, "ops": ERASE}, token=_登(s))
    assert code == 409


def test_权限(站点):
    s = 站点
    gina = _登(s, "gina")
    assert s.req("POST", f"{ADM}/edit", {"digest": "x", "ops": ERASE}, token=gina)[0] == 403
    assert s.req("POST", f"{ADM}/display", {}, token=gina)[0] == 403


def test_存显示设置读回来_预览带上(站点):
    s = 站点
    tok = _登(s)
    body = {"rotate_deg": 12.5, "regions": [{"name": "车库", "polygon": [[0, 0], [2, 0], [2, 2]]}]}
    code, d = s.req("POST", f"{ADM}/display", body, token=tok)
    assert code == 200, d
    assert s.req("GET", f"{ADM}/display", token=tok)[1]["regions"][0]["name"] == "车库"
    pv = s.req("GET", f"/api/maps/{MAP[0]}/{MAP[1]}/preview", token=_登(s, "gina"))[1]
    assert pv["rotation_deg"] == 12.5 and pv["display"]["regions"][0]["name"] == "车库"


@pytest.mark.parametrize("bad", [{"rotate_deg": 200}, {"regions": [{"name": "", "polygon": []}]},
                                 {"terrain": [{"id": "a", "kind": "lava", "polygon": []}]},
                                 {"basemap": {"file": "../x.png"}}])
def test_显示设置不合规矩(bad):
    with pytest.raises(DisplayError):
        check(bad)


def test_楼梯默认放慢_生成限速区_草地不放慢_删了只删它生成的(站点):
    s = 站点
    tok = _登(s)
    zp = f"/api/maps/{MAP[0]}/{MAP[1]}/zones"
    hand = {"id": "lawn", "kind": "slow", "max_speed_mps": 0.5,
            "polygon": [[6, 3], [7, 3], [7, 3.8]]}
    s.req("POST", zp, {"zones": [hand], "base_revision": 0}, token=tok)
    sq = [[1, 1], [2, 1], [2, 2], [1, 2]]
    code, d = s.req("POST", f"{ADM}/display", {"terrain": [
        {"id": "s1", "kind": "stairs", "polygon": sq},
        {"id": "g1", "kind": "grass", "polygon": sq}]},
        token=tok)
    assert code == 200, d
    assert d["terrain"][0]["slow_mps"] == 0.3 and d["terrain"][1]["slow_mps"] is None
    zs = s.req("GET", zp, token=tok)[1]["zones"]
    assert {z["id"] for z in zs} == {"lawn", "t-s1"}
    assert next(z for z in zs if z["id"] == "t-s1")["max_speed_mps"] == 0.3
    s.req("POST", f"{ADM}/display", {"terrain": []}, token=tok)
    assert [z["id"] for z in s.req("GET", zp, token=tok)[1]["zones"]] == ["lawn"]


def _传(s, tok, data, ctype):
    r = urllib.request.Request(s.api.url + f"{ADM}/basemap", data=data, method="POST")
    r.add_header("Content-Type", ctype)
    r.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_底图上传和大小限制(站点):
    s = 站点
    tok = _登(s)
    code, d = _传(s, tok, b"\x89PNG\r\n\x1a\n" + b"0" * 100, "image/png")
    assert code == 200 and d["file"] == "basemap.png"
    assert (s.api.home / "map-display" / MAP[0] / MAP[1] / "basemap.png").is_file()
    assert _传(s, tok, b"GIF89a", "image/gif")[0] == 400
    assert _传(s, tok, b"not png", "image/png")[0] == 400


def test_区域命名_报警写在车库_没命名用防区名(站点):
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.map_display import MapDisplay
    s = 站点
    disp = MapDisplay(s.db, now_ms=lambda: 1)
    disp.put(MAP[0], MAP[1], {"regions": [{"name": "车库", "polygon": [[0, 0], [2, 0], [2, 2],
                                                                       [0, 2]]}]}, by="a")
    s.api.areas.set(MAP[0], MAP[1], "后院", [[5, 0], [8, 0], [8, 4], [5, 4]], by="a")
    assert disp.region_at(MAP[0], MAP[1], 1, 1) == "车库"
    assert disp.region_at(MAP[0], MAP[1], 6, 1) is None
    src = SiteAlertSources(s.desk, now_ms=lambda: 1)
    src.where_of = lambda p: (disp.region_at(p["map_id"], p["map_version"], p["x"], p["y"])
                              or s.api.areas.zone_at(p["map_id"], p["map_version"], p["x"], p["y"]))
    src._mem["A"] = type("M", (), {"pose": {"map_id": MAP[0], "map_version": MAP[1], "x": 6.0,
                                            "y": 1.0}, "pose_ms": 1, "running": False,
                                   "started": None})()
    assert src.here("A")["where"] == "后院"
