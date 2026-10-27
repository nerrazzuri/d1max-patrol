"""W44 第三期:「布置」的历史版本(设计 §5.2 第 2 条)和摄像头在图上的位置。

每次保存(防区、禁行区、路线、待命点、摄像头)留一份快照,谁、几点;一键退回走原来的保存路径 ——
禁行区放宽照样要确认,路线照样带修订号;摄像头的快照不带口令。
"""

from __future__ import annotations

import json

import pytest
from test_site_api import MAP, PW, 站
from test_site_intercept_reach import _屋子
from test_site_maps import _files

from d1max_site.areas import AreaBook
from d1max_site.db import SiteDB
from d1max_site.layout_history import KEEP, LayoutHistory

POND = {"id": "pond", "kind": "nogo", "label": "池子",
        "polygon": [[5, 5], [6, 5], [6, 6], [5, 6]]}
BED = {"id": "bed", "kind": "nogo", "label": "花坛",
       "polygon": [[1, 1], [2, 1], [2, 2], [1, 2]]}
SQ = [[0, 0], [4, 0], [4, 4], [0, 4]]
SQ2 = [[10, 10], [14, 10], [14, 14], [10, 14]]


class 钟:
    def __init__(self) -> None:
        self.t = 1000

    def __call__(self) -> int:
        self.t += 1
        return self.t


def test_每次保存记一份_新的在前_只留50份(tmp_path):
    h = LayoutHistory(SiteDB(tmp_path / "s.db"), now_ms=钟())
    for i in range(KEEP + 5):
        h.record("zones", "m", "1", "", [{"zone": f"z{i}", "points": SQ}], by="alice")
    h.record("zones", "m", "2", "", [], by="olga")
    rows = h.list("zones", "m", "1")
    assert len(rows) == KEEP and KEEP == 50
    assert rows[0]["by"] == "alice" and "body" not in rows[0]
    assert rows[0]["at_ms"] > rows[1]["at_ms"]
    assert h.get(rows[0]["id"])["body"] == [{"zone": f"z{KEEP + 4}", "points": SQ}]
    assert len(h.list("zones", "m", "2")) == 1


# ------------------------------------------------------------ 接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.accounts.add("gina", PW, role="guard")
    src = _files(tmp_path / "mapsrc")
    _屋子(src, 1.6)
    s.maps.import_dir(src, map_id=MAP[0], version=MAP[1])
    s.api.areas = AreaBook(s.db, now_ms=lambda: 1)
    yield s
    s.close()


def _登(s, name="alice"):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


BASE = f"/api/maps/{MAP[0]}/{MAP[1]}"


def _hist(s, tok, kind, item=None, map_id=MAP[0], version=MAP[1]):
    q = f"kind={kind}&map_id={map_id}&version={version}" + (f"&item={item}" if item else "")
    code, d = s.req("GET", f"/api/layout/history?{q}", token=tok)
    assert code == 200, d
    return d["history"]


def _restore(s, tok, hid):
    """退回要说清是哪一层、哪张图(外审 R3):按那一条自己的填,跟网页一样。"""
    rows = s.db.query("SELECT kind, map_id, map_version FROM layout_history WHERE id=?", (hid,))
    exp = ({"kind": rows[0]["kind"], "map_id": rows[0]["map_id"],
            "version": rows[0]["map_version"]} if rows else {})
    return s.req("POST", f"/api/layout/history/{hid}/restore", {"expect": exp}, token=tok)


def test_退回防区(站点):
    s = 站点
    tok = _登(s)
    s.req("POST", f"{BASE}/areas", {"zone": "A", "points": SQ}, token=tok)
    s.req("POST", f"{BASE}/areas", {"zone": "B", "points": SQ2}, token=tok)
    rows = _hist(s, tok, "zones")
    assert len(rows) == 2 and rows[0]["by"] == "alice"
    code, d = _restore(s, tok, rows[1]["id"])
    assert code == 200, d
    areas = s.req("GET", f"{BASE}/areas", token=tok)[1]["areas"]
    assert [a["zone"] for a in areas] == ["A"]
    rows = _hist(s, tok, "zones")
    assert len(rows) == 3 and rows[0]["note"].startswith("restored")


def test_退回禁行区_放宽的照样要确认(站点):
    s = 站点
    tok = _登(s)
    zp = f"{BASE}/zones"
    s.req("POST", zp, {"zones": [POND], "base_revision": 0}, token=tok)
    s.req("POST", zp + "/confirm", {"revision": 1, "confirm": True}, token=tok)
    s.req("POST", zp, {"zones": [POND, BED], "base_revision": 1}, token=tok)   # 收紧,当场算确认
    rows = _hist(s, tok, "nogo")
    assert len(rows) == 2
    # 退回到只有泳池的那一份 = 删花坛 = 放宽:要确认,在生效的照旧有花坛
    code, d = _restore(s, tok, rows[1]["id"])
    assert code == 200, d
    v = s.req("GET", zp, token=tok)[1]
    assert [z["id"] for z in v["zones"]] == ["pond"]
    assert v["confirmed"]["current"] is False
    assert [z["id"] for z in v["held"]] == ["bed"]


def test_退回路线_记一条新的历史(站点):
    s = 站点
    tok = _登(s)
    wp = [{"name": "a", "x": 1.0, "y": 2.0, "yaw": 0.0},
          {"name": "b", "x": 2.0, "y": 2.0, "yaw": 0.0}]
    body = {"map_id": MAP[0], "map_version": MAP[1], "waypoints": wp, "base_revision": 0}
    code, r1 = s.req("POST", "/api/routes/night", body, token=tok)
    assert code == 200, r1
    wp2 = wp + [{"name": "c", "x": 3.0, "y": 2.0, "yaw": 0.0}]
    s.req("POST", "/api/routes/night", body | {"waypoints": wp2, "base_revision": r1["revision"]},
          token=tok)
    rows = _hist(s, tok, "route", item="night")
    assert [r["item"] for r in rows] == ["night", "night"]
    code, d = _restore(s, tok, rows[1]["id"])
    assert code == 200, d
    got = s.req("GET", "/api/routes/night", token=tok)[1]
    assert [w["name"] for w in got["waypoints"]] == ["a", "b"]
    assert got["revision"] == r1["revision"] + 2
    assert len(_hist(s, tok, "route", item="night")) == 3


def test_退回删掉的路线(站点):
    s = 站点
    tok = _登(s)
    wp = [{"name": "a", "x": 1.0, "y": 2.0, "yaw": 0.0}]
    _, r = s.req("POST", "/api/routes/night", {"map_id": MAP[0], "map_version": MAP[1],
                                               "waypoints": wp, "base_revision": 0}, token=tok)
    s.req("POST", "/api/routes/night/delete", {"base_revision": r["revision"]}, token=tok)
    rows = _hist(s, tok, "route", item="night")
    assert rows[0]["summary"] and len(rows) == 2
    code, d = _restore(s, tok, rows[1]["id"])
    assert code == 200, d
    assert s.req("GET", "/api/routes/night", token=tok)[0] == 200


def test_摄像头历史不存口令_退回删掉的写要重填口令(站点):
    s = 站点
    tok = _登(s)
    cam = {"name": "cam1", "zone": "前门", "onvif_url": "http://10.0.0.5/onvif",
           "username": "admin", "password": "secret-pass-1"}
    assert s.req("POST", "/api/admin/cameras", cam, token=tok)[0] == 200
    s.req("POST", "/api/admin/cameras/cam1/remove", {}, token=tok)
    rows = _hist(s, tok, "cameras", map_id="", version="")
    assert len(rows) == 2
    raw = s.db.query("SELECT body FROM layout_history")
    assert all("secret-pass-1" not in (r["body"] or "") for r in raw)
    code, d = _restore(s, tok, rows[1]["id"])
    assert code == 200, d
    assert d["warnings"] == [{"camera": "cam1", "problem": "password_missing"}]
    got = s.req("GET", "/api/admin/cameras", token=tok)[1]["cameras"]
    assert got[0]["name"] == "cam1" and got[0]["has_password"] is False


def test_历史接口权限(站点):
    s = 站点
    gina = _登(s, "gina")
    assert s.req("GET", f"/api/layout/history?kind=zones&map_id={MAP[0]}&version={MAP[1]}",
                 token=gina)[0] == 403
    assert s.req("POST", "/api/layout/history/1/restore", {}, token=gina)[0] == 403
    assert s.req("POST", "/api/layout/history/999/restore", {}, token=_登(s))[0] == 404


# ------------------------------------------------------------ 摄像头在图上的位置


PL = {"map_id": MAP[0], "map_version": MAP[1], "x": 1.5, "y": 2.0, "yaw": 0.5, "fov_deg": 60}


def test_摄像头放到图上_不改位置时原样留着_清掉(站点):
    s = 站点
    tok = _登(s)
    cam = {"name": "cam1", "zone": "前门", "onvif_url": "http://10.0.0.5/onvif",
           "password": "secret-pass-1", "placement": PL}
    code, d = s.req("POST", "/api/admin/cameras", cam, token=tok)
    assert code == 200, d
    assert d["camera"]["placement"] == PL
    # 只改地址、换口令,不带 placement:位置不动
    code, d = s.req("POST", "/api/admin/cameras", {k: v for k, v in cam.items() if k != "placement"}
                    | {"edit": True, "onvif_url": "http://10.0.0.6/onvif"}, token=tok)
    assert code == 200 and d["camera"]["placement"] == PL
    code, d = s.req("POST", "/api/admin/cameras", {"name": "cam1", "zone": "前门",
                    "onvif_url": "http://10.0.0.6/onvif", "edit": True, "placement": None},
                    token=tok)
    assert code == 200 and d["camera"]["placement"] is None


@pytest.mark.parametrize("bad", [PL | {"fov_deg": 5}, PL | {"x": "1"}, {k: v for k, v in PL.items()
                                 if k != "map_version"}, PL | {"yaw": float("inf")}, "here"])
def test_位置不合规矩(站点, bad):
    s = 站点
    code, _ = s.req("POST", "/api/admin/cameras", {"name": "cam1", "zone": "前门",
                    "onvif_url": "http://10.0.0.5/onvif", "placement": bad},
                    raw=json.dumps({"name": "cam1", "zone": "前门",
                                    "onvif_url": "http://10.0.0.5/onvif", "placement": bad},
                                   allow_nan=True).encode(), token=_登(s))
    assert code == 400


def test_外审R3_退回带的图和层对不上_拒绝不动(站点):
    s = 站点
    tok = _登(s)
    s.req("POST", f"{BASE}/areas", {"zone": "A", "points": SQ}, token=tok)
    s.req("POST", f"{BASE}/areas", {"zone": "B", "points": SQ2}, token=tok)
    old = _hist(s, tok, "zones")[1]
    for exp in ({"kind": "zones", "map_id": "B", "version": MAP[1]},
                {"kind": "zones", "map_id": MAP[0], "version": "99"},
                {"kind": "nogo", "map_id": MAP[0], "version": MAP[1]}, None):
        body = {} if exp is None else {"expect": exp}
        code, d = s.req("POST", f"/api/layout/history/{old['id']}/restore", body, token=tok)
        assert code == 409, (exp, d)
    assert len(_hist(s, tok, "zones")) == 2                # 没退
