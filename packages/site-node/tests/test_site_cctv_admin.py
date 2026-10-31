"""网页上登记 / 改 / 删固定摄像头(运营可视化):校验、权限、审计、防区检查、口令不外泄。"""

from __future__ import annotations

import json
import logging

import pytest

from d1max_site.cctv import load_cameras
from d1max_site.cctv_admin import check_camera

SECRET = "s3cret-Pa55"
GOOD = {"name": "gate-cam", "onvif_url": "http://192.168.1.50", "username": "admin",
        "password": SECRET, "zone": "front-gate", "rtsp_url": "rtsp://192.168.1.50/stream1",
        "motion": False}


@pytest.fixture
def 站点(tmp_path):
    from test_site_api import PW, 站
    s = 站(tmp_path)
    s.accounts.add("olga", PW, role="owner")
    s.accounts.add("gus", PW, role="guard")
    s.PW = PW
    yield s
    s.close()


def _tok(s, name="alice"):
    return s.req("POST", "/api/login", {"name": name, "password": s.PW})[1]["token"]


def _映射防区(db, zone="front-gate", polygon=True):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO intercepts(name, map_id, map_version, x, y, yaw) "
                  "VALUES ('gate-pt','m','1',1,1,0)")
        c.execute("INSERT OR REPLACE INTO zones(zone, intercept) VALUES (?, 'gate-pt')", (zone,))
        if polygon:
            c.execute("INSERT OR REPLACE INTO zone_areas(map_id, map_version, zone, points, "
                      "updated_by, updated_ms) VALUES ('m','1',?,'[[0,0],[2,0],[2,2]]','x',1)",
                      (zone,))


# ------------------------------------------------------------ 校验


@pytest.mark.parametrize("patch,word", [
    ({"name": ""}, "name"), ({"name": "a b"}, "名字"), ({"name": "../x"}, "名字"),
    ({"name": "x" * 65}, "name"), ({"zone": ""}, "zone"), ({"zone": "a\nb"}, "控制字符"),
    ({"zone": "z" * 129}, "zone"), ({"onvif_url": ""}, "onvif_url"),
    ({"onvif_url": "ftp://x"}, "http"), ({"onvif_url": "http://u:p@1.2.3.4"}, "账号密码"),
    ({"onvif_url": "1.2.3.4 x"}, "空格"), ({"onvif_url": "http://"}, "主机"),
    ({"onvif_url": "http://:80"}, "主机"), ({"rtsp_url": "http://x/y"}, "rtsp"),
    ({"rtsp_url": "rtsp://a b"}, "空格"), ({"motion": "yes"}, "motion"),
    ({"username": "u" * 65}, "username"), ({"name": 5}, "文字"),
])
def test_校验_不对的一律拒(patch, word):
    with pytest.raises(ValueError) as e:
        check_camera(GOOD | patch)
    assert word in str(e.value)


def test_校验_好的收下_没有协议头的地址也行_空的rtsp行():
    got = check_camera(GOOD | {"onvif_url": "192.168.1.50:8080", "rtsp_url": ""})
    assert got["onvif_url"] == "192.168.1.50:8080" and got["rtsp_url"] == ""
    assert check_camera(GOOD | {"rtsp_url": "rtsp://user:pw@10.0.0.2/s"})["rtsp_url"]
    assert check_camera({"name": "a", "zone": "z", "onvif_url": "https://nvr.local/onvif"})


def test_校验_要一个对象():
    with pytest.raises(ValueError):
        check_camera([1])


# ------------------------------------------------------------ 接口


def test_登记_列出_不带口令_口令加密落库(站点, caplog):
    s = 站点
    tok = _tok(s)
    caplog.set_level(logging.DEBUG)
    code, d = s.req("POST", "/api/admin/cameras", GOOD, token=tok)
    assert code == 200 and d["ok"] and SECRET not in json.dumps(d)
    assert d["camera"]["has_password"] is True and "password" not in d["camera"]
    [cam] = load_cameras(s.db)
    assert cam.password == SECRET and cam.zone == "front-gate" and cam.rtsp_url
    # 落库走命令行同一条路(add_camera → seal_value:站点配了口令密钥就加密,这里测试库没配)
    code, lst = s.req("GET", "/api/admin/cameras", token=tok)
    assert code == 200 and [c["name"] for c in lst["cameras"]] == ["gate-cam"]
    assert SECRET not in json.dumps(lst)
    assert SECRET not in caplog.text


def test_地址里带账号口令的rtsp_列表里抹掉(站点):
    s = 站点
    tok = _tok(s)
    s.req("POST", "/api/admin/cameras", GOOD | {"rtsp_url": f"rtsp://bob:{SECRET}@10.0.0.2/s"},
          token=tok)
    lst = s.req("GET", "/api/admin/cameras", token=tok)[1]
    assert lst["cameras"][0]["rtsp_set"] is True
    assert SECRET not in json.dumps(lst) and "bob" not in lst["cameras"][0]["rtsp_url"]
    assert load_cameras(s.db)[0].rtsp_url == f"rtsp://bob:{SECRET}@10.0.0.2/s", "库里原样留着"


def test_重名登记409_改的时候没有这台404式409_改要带edit(站点):
    s = 站点
    tok = _tok(s)
    assert s.req("POST", "/api/admin/cameras", GOOD, token=tok)[0] == 200
    code, d = s.req("POST", "/api/admin/cameras", GOOD, token=tok)
    assert code == 409 and "edit" in d["error"]
    code, d = s.req("POST", "/api/admin/cameras", GOOD | {"name": "nope", "edit": True}, token=tok)
    assert code == 409 and "没有摄像头" in d["error"]
    assert s.req("POST", "/api/admin/cameras", GOOD | {"edit": "yes"}, token=tok)[0] == 400


def test_改_没给口令就保留原来的_给了才换_给空串是清掉(站点):
    s = 站点
    tok = _tok(s)
    s.req("POST", "/api/admin/cameras", GOOD, token=tok)
    before = s.db.query("SELECT password FROM cameras")[0]["password"]
    new = {k: v for k, v in GOOD.items() if k != "password"} | {"edit": True, "zone": "back-gate",
                                                                  "motion": True}
    code, d = s.req("POST", "/api/admin/cameras", new, token=tok)
    assert code == 200 and d["camera"]["zone"] == "back-gate" and d["camera"]["motion"] is True
    assert s.db.query("SELECT password FROM cameras")[0]["password"] == before, "原样留着"
    assert load_cameras(s.db)[0].password == SECRET
    s.req("POST", "/api/admin/cameras", new | {"password": "other-pw"}, token=tok)
    assert load_cameras(s.db)[0].password == "other-pw"
    s.req("POST", "/api/admin/cameras", new | {"password": ""}, token=tok)
    assert load_cameras(s.db)[0].password == ""
    assert s.req("GET", "/api/admin/cameras", token=tok)[1]["cameras"][0]["has_password"] is False
    assert s.req("POST", "/api/admin/cameras", new | {"password": 5}, token=tok)[0] == 400


def test_删_没有的404_有的删掉(站点):
    s = 站点
    tok = _tok(s)
    s.req("POST", "/api/admin/cameras", GOOD, token=tok)
    assert s.req("POST", "/api/admin/cameras/nope/remove", {}, token=tok)[0] == 404
    assert s.req("POST", "/api/admin/cameras/gate-cam/remove", {}, token=tok)[0] == 200
    assert load_cameras(s.db) == []


def test_校验错误是400_带人话_不是500(站点):
    s = 站点
    tok = _tok(s)
    code, d = s.req("POST", "/api/admin/cameras", GOOD | {"zone": ""}, token=tok)
    assert code == 400 and "zone" in d["error"]
    assert load_cameras(s.db) == []


# ------------------------------------------------------------ 权限


def test_只有管理员_管理员保安不行_没登录401(站点):
    s = 站点
    assert s.req("GET", "/api/admin/cameras")[0] == 401
    assert s.req("POST", "/api/admin/cameras", GOOD)[0] == 401
    for who in ("olga", "gus"):
        tok = _tok(s, who)
        assert s.req("GET", "/api/admin/cameras", token=tok)[0] == 403, who
        assert s.req("POST", "/api/admin/cameras", GOOD, token=tok)[0] == 403, who
        assert s.req("POST", "/api/admin/cameras/gate-cam/remove", {}, token=tok)[0] == 403, who
    assert load_cameras(s.db) == []


# ------------------------------------------------------------ 审计


def test_审计记谁登记了哪台_口令不进审计(站点):
    s = 站点
    tok = _tok(s)
    s.req("POST", "/api/admin/cameras", GOOD | {"rtsp_url": f"rtsp://bob:{SECRET}@10.0.0.2/s"},
          token=tok)
    s.req("POST", "/api/admin/cameras", GOOD | {"edit": True, "zone": "z2"}, token=tok)
    s.req("POST", "/api/admin/cameras/gate-cam/remove", {}, token=tok)
    rows = s.req("GET", "/api/audit", token=tok)[1]["audit"]
    cam = [r for r in rows if r["action"].endswith("/api/admin/cameras")
           or "/api/admin/cameras/" in r["action"]]
    assert len(cam) == 3 and {r["actor"] for r in cam} == {"alice"} and {
        r["target"] for r in cam} == {"camera/gate-cam"}
    assert SECRET not in json.dumps(rows) and "bob:" not in json.dumps(rows)
    add = [r for r in cam if not r["detail"].get("edit") and "remove" not in r["action"]][0]
    assert add["detail"]["password_changed"] is True and add["detail"]["zone"] == "front-gate"
    assert add["status"] == 200


def test_校验失败也进审计_状态码400(站点):
    s = 站点
    tok = _tok(s)
    s.req("POST", "/api/admin/cameras", GOOD | {"zone": ""}, token=tok)
    rows = s.req("GET", "/api/audit", token=tok)[1]["audit"]
    assert any(r["action"].endswith("/api/admin/cameras") and r["status"] == 400 for r in rows)


# ------------------------------------------------------------ 防区检查


def test_防区没映射也没画多边形_警告但照样登记(站点):
    s = 站点
    tok = _tok(s)
    code, d = s.req("POST", "/api/admin/cameras", GOOD, token=tok)
    assert code == 200
    assert {w["code"] for w in d["warnings"]} == {"zone_unmapped", "zone_no_polygon"}
    assert d["camera"]["zone_mapped"] is False and d["camera"]["zone_polygon"] is False
    assert load_cameras(s.db), "警告不挡登记"


def test_防区映射了没画多边形_只警告多边形_都有了就没有警告(站点):
    s = 站点
    tok = _tok(s)
    _映射防区(s.db, polygon=False)
    d = s.req("POST", "/api/admin/cameras", GOOD, token=tok)[1]
    assert [w["code"] for w in d["warnings"]] == ["zone_no_polygon"]
    _映射防区(s.db, polygon=True)
    lst = s.req("GET", "/api/admin/cameras", token=tok)[1]["cameras"][0]
    assert lst["warnings"] == [] and lst["zone_mapped"] and lst["zone_polygon"]


def test_列表里每台自带防区检查_改防区后重新检查(站点):
    s = 站点
    tok = _tok(s)
    _映射防区(s.db)
    s.req("POST", "/api/admin/cameras", GOOD, token=tok)
    moved = GOOD | {"edit": True, "zone": "elsewhere"}
    d = s.req("POST", "/api/admin/cameras", moved, token=tok)[1]
    assert {w["code"] for w in d["warnings"]} == {"zone_unmapped", "zone_no_polygon"}


def test_原来的只读接口不变(站点):
    s = 站点
    tok = _tok(s)
    assert s.req("POST", "/api/cameras", {}, token=tok)[0] == 405


def test_实测_网页加删摄像头_站点当场跟上_不等下一拍(tmp_path):
    """演示站点实测:网页上删了摄像头,值班用的摄像头列表(和订阅)还要等下一次同步(最多 30 秒),
    这几秒里它报的入侵照样报警。网页加、改、删以后当场同步。"""
    import time

    from test_site_api import PW, 站
    from test_site_cctv import _假订阅

    from d1max_site.cctv import CctvManager
    s = 站(tmp_path)
    try:
        s.cctv = CctvManager(s.db, report=lambda c, e: None, now_ms=lambda: 1,
                             watch_factory=_假订阅)
        s.api.cctv = s.cctv
        s.PW = PW
        tok = _tok(s)
        body = {"name": "gate-cam", "onvif_url": "http://10.0.0.9", "username": "admin",
                "password": "p", "zone": "front-gate"}
        assert s.req("POST", "/api/admin/cameras", body, token=tok)[0] == 200
        names = lambda: [c["name"] for c in s.cctv.view()]  # noqa: E731
        deadline = time.monotonic() + 3
        while "gate-cam" not in names() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "gate-cam" in names()
        assert s.req("POST", "/api/admin/cameras/gate-cam/remove", {}, token=tok)[0] == 200
        deadline = time.monotonic() + 3
        while "gate-cam" in names() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "gate-cam" not in names()
    finally:
        s.close()
