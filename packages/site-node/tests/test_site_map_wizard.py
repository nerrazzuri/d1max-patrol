"""W54 建图向导:结论三档、自动起版本号、向导状态(站点上一份)、接口权限。"""

from __future__ import annotations

import json

import pytest
from test_site_api import MAP, PW, 站
from test_site_map_admin import BUILD, put_version

from d1max_site.map_wizard import (
    MapWizard,
    WizardError,
    next_version,
    quality,
)


def _lc(**kw):
    return {**BUILD, "loop_closure": {"ran": True, **kw}}


# ------------------------------------------------------------ 结论


def test_结论_闭合了修正小走得够_能用():
    q = quality(_lc(used=True, closures=2, path_m=120.0, max_shift_m=0.08), None)
    assert q["verdict"] == "usable" and q["reasons"] == []
    assert q["closures"] == 2 and q["path_m"] == 120.0 and q["max_shift_m"] == 0.08


@pytest.mark.parametrize("lc, reason", [
    ({"used": False, "closures": 0, "why": "没找到回环"}, "no_loop"),
    ({"used": False, "why": "没有回环工具"}, "no_loop"),
    ({"used": True, "closures": 1, "path_m": 80.0, "max_shift_m": 0.3}, "big_shift"),
    # 回环挪得太多被丢掉了:图没闭合,原因是修正太大
    ({"used": False, "closures": 2, "path_m": 80.0, "max_shift_m": 1.4}, "big_shift"),
    ({"used": True, "closures": 1, "path_m": 14.9, "max_shift_m": 0.05}, "short_path"),
])
def test_结论_有问题_说原因(lc, reason):
    q = quality({**BUILD, "loop_closure": lc}, None)
    assert q["verdict"] == "problem" and reason in q["reasons"], q


def test_结论_没有回环路程_按覆盖轨迹算():
    path = [[0, 0], [10, 0], [10, 10]]               # 20 米
    q = quality({**BUILD, "loop_closure": {"ran": False}}, path)
    assert q["path_m"] == pytest.approx(20.0) and "short_path" not in q["reasons"]
    q = quality({**BUILD, "loop_closure": {"ran": False}}, [[0, 0], [3, 4]])
    assert q["path_m"] == pytest.approx(5.0) and "short_path" in q["reasons"]


def test_结论_坏数字不炸():
    for bad in (10**400, float("inf"), float("nan"), -3, True, "1"):
        q = quality({**BUILD, "loop_closure": {"used": True, "closures": 1, "path_m": bad,
                                               "max_shift_m": bad}}, [[0, 0], [10**400, 0]])
        json.dumps(q, allow_nan=False)
        assert q["path_m"] is None and q["max_shift_m"] is None


def test_结论_导入的老图没有建图记录_说不清():
    assert quality(None, None)["verdict"] == "unknown"


# ------------------------------------------------------------ 版本号


def test_版本号_按日期加序号_不撞已有的():
    assert next_version(set(), "20261031") == "20261031-1"
    assert next_version({"20261031-1", "20261031-3", "20261030-9", "7"}, "20261031") == \
        "20261031-4"
    assert len(next_version(set(), "20261031")) <= 16


# ------------------------------------------------------------ 向导(用站点的地图目录、设置表)


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.disp.maps = s.maps
    s.accounts.add("gina", PW, role="guard")
    s.eng = s.accounts.add_engineer("交付工程师", 4, by="alice")
    yield s
    s.close()


def _tok(s, name="alice"):
    if name == "eng":
        name, pw = s.eng[0], s.eng[1]
    else:
        pw = PW
    code, d = s.req("POST", "/api/login", {"name": name, "password": pw})
    assert code == 200, d
    return d["token"]


class _W:
    """测试用:跟着手机一样,开始以后每一步都带上这份向导的编号。"""

    def __init__(self, w: MapWizard) -> None:
        self.w = w

    def view(self):
        return self.w.view()

    def apply(self, d, *, by):
        if d.get("action") != "start":
            cur = self.w.view()
            d = {"wizard_id": cur["id"] if cur else "", **d}
        return self.w.apply(d, by=by)


def _wiz(s):
    return _W(MapWizard(s.api.settings, s.maps, s.db, now_ms=lambda: 1_700_000_000_000,
                        day=lambda: "20261031", known=lambda r: r == "A", zones=s.api.zones))


def test_向导_开始_起地图号版本_记在站点上(站点):
    w = _wiz(站点)
    assert w.view() is None
    v = w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote",
                 "label": "一楼"}, by="alice")
    assert v["step"] == "prepare" and v["map_id"] == "main" and v["version"] == "20261031-1"
    assert v["label"] == "一楼" and v["by"] == "alice" and v["drive"] == "remote"
    assert _wiz(站点).view()["version"] == "20261031-1"      # 存在站点上,换一个对象照样读到


def test_向导_已有图就给这张图出新版本(站点):
    d = 站点.maps.root.parent / "src"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"x")
    站点.maps.import_dir(d, map_id="main", version="20261031-1")
    v = _wiz(站点).apply({"action": "start", "robot_id": "A", "site_type": "indoor",
                         "drive": "phone"}, by="alice")
    assert (v["map_id"], v["version"]) == ("main", "20261031-2")


@pytest.mark.parametrize("bad, why", [
    ({"site_type": "outdoor"}, "coming_soon"),
    ({"site_type": "glass"}, "coming_soon"),
    ({"site_type": "moon"}, None),
    ({"drive": "car"}, None),
    ({"robot_id": "Z"}, None),
    ({"label": "x" * 41}, None),
])
def test_向导_开始_参数不对拒(站点, bad, why):
    body = {"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote", **bad}
    with pytest.raises(WizardError) as ei:
        _wiz(站点).apply(body, by="alice")
    assert ei.value.status == 400
    if why:
        assert ei.value.reason == why


def test_向导_一个站点只有一份_再开要明说换掉(站点):
    w = _wiz(站点)
    w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote"},
            by="alice")
    with pytest.raises(WizardError) as ei:
        w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "phone"},
                by="eng")
    assert ei.value.status == 409 and ei.value.reason == "wizard_active"
    v = w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "phone",
                 "replace": True}, by="eng")
    assert v["by"] == "eng" and v["drive"] == "phone"


def test_向导_走步_重走换新版本_放弃清掉(站点):
    w = _wiz(站点)
    w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote"},
            by="alice")
    v = w.apply({"action": "step", "step": "walk", "drive": "phone"}, by="alice")
    assert v["step"] == "walk" and v["drive"] == "phone"
    v = w.apply({"action": "rewalk"}, by="alice")
    assert v["step"] == "walk" and v["version"] == "20261031-2"
    assert v["tried"] == ["20261031-1"]
    with pytest.raises(WizardError):
        w.apply({"action": "step", "step": "fly"}, by="alice")
    w.apply({"action": "abandon"}, by="alice")
    assert w.view() is None


def test_向导_没建好不能去禁行区_建好了带结论_记显示名(站点):
    w = _wiz(站点)
    v = w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote",
                 "label": "一楼"}, by="alice")
    w.apply({"action": "step", "step": "build"}, by="alice")
    assert w.view()["build"]["state"] == "pending"
    with pytest.raises(WizardError) as ei:
        w.apply({"action": "step", "step": "zones"}, by="alice")
    assert ei.value.status == 409 and ei.value.reason == "not_built"
    put_version_main(站点, v["version"], build=_lc(used=True, closures=1, path_m=60.0,
                                                  max_shift_m=0.05))
    b = w.view()["build"]
    assert b["state"] == "ready" and b["quality"]["verdict"] == "usable"
    v = w.apply({"action": "step", "step": "zones"}, by="alice")
    assert v["step"] == "zones"
    assert 站点.maps.get("main", v["version"]) is not None
    assert [m for m in 站点.maps.list() if m["version"] == v["version"]][0]["note"] == "一楼"


def test_向导_建图失败_带原因(站点):
    w = _wiz(站点)
    v = w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote"},
                by="alice")
    with 站点.db.tx() as c:
        c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                  "received_at) VALUES ('A','b',1,'e1','map_build_failed',?,0,?)",
                  (json.dumps({"map_id": "main", "version": v["version"],
                               "reason": "MappingError: 录包是空的"}), 1_700_000_000_001))
        c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                  "received_at) VALUES ('A','b',2,'e2','map_build_failed',?,0,?)",
                  (json.dumps({"map_id": "main", "version": "别的", "reason": "x"}),
                   1_700_000_000_002))
    b = w.view()["build"]
    assert b["state"] == "failed" and "录包是空的" in b["reason"]


def test_向导_完成_要禁行区确认了_原点登记了(站点):
    w = _wiz(站点)
    v = w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote"},
                by="alice")
    put_version_main(站点, v["version"], build=BUILD)
    w.apply({"action": "step", "step": "zones"}, by="alice")
    with pytest.raises(WizardError) as ei:
        w.apply({"action": "finish"}, by="alice")
    assert ei.value.reason == "zones_unconfirmed"
    zs = 站点.api.zones
    zs.confirm("main", v["version"], revision=zs.current("main", v["version"]).revision,
               by="alice")
    with pytest.raises(WizardError) as ei:
        w.apply({"action": "finish"}, by="alice")
    assert ei.value.reason == "no_home"
    with 站点.db.tx() as c:
        c.execute("INSERT INTO homes(robot_id, map_id, map_version, name, x, y, yaw, "
                  "marked_at_ms) VALUES ('A', 'main', ?, 'home', 0, 0, 0, 1)", (v["version"],))
    out = w.apply({"action": "finish"}, by="alice")
    assert out["step"] == "done" and w.view() is None


def test_向导_换掉以后旧手机改不了新的(站点):
    raw = _wiz(站点).w
    old = raw.apply({"action": "start", "robot_id": "A", "site_type": "indoor",
                     "drive": "remote"}, by="alice")
    new = raw.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "phone",
                     "replace": True}, by="eng")
    assert new["id"] != old["id"]
    for act in ({"action": "abandon"}, {"action": "step", "step": "drive"}, {"action": "rewalk"}):
        with pytest.raises(WizardError) as ei:
            raw.apply({**act, "wizard_id": old["id"]}, by="alice")
        assert ei.value.status == 409 and ei.value.reason == "stale_wizard"
    with pytest.raises(WizardError):
        raw.apply({"action": "abandon"}, by="alice")      # 不带编号也不行
    assert raw.view()["by"] == "eng"


def test_向导_两个人同时开始_只有一个成(站点):
    import threading
    raw = _wiz(站点).w
    got, errs = [], []
    gate = threading.Barrier(8)

    def go(i):
        gate.wait()
        try:
            got.append(raw.apply({"action": "start", "robot_id": "A", "site_type": "indoor",
                                  "drive": "remote"}, by=f"u{i}"))
        except WizardError as exc:
            errs.append(exc.reason)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert len(got) == 1 and errs == ["wizard_active"] * 7
    assert raw.view()["by"] == got[0]["by"]


def put_version_main(s, version: str, *, build: dict) -> None:
    d = s.maps.root.parent / f"src-{version}"
    d.mkdir()
    (d / "floor.yaml").write_text("image: floor.pgm\nresolution: 0.1\norigin: [0, 0, 0]\n"
                                  "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    (d / "floor.pgm").write_bytes(b"P5\n2 2\n255\n" + bytes([254] * 4))
    (d / "build.json").write_text(json.dumps(build))
    s.maps.import_dir(d, map_id="main", version=version, note="")


# ------------------------------------------------------------ 接口


def test_接口_保安能看_不能改_工程师能建(站点):
    gina, eng = _tok(站点, "gina"), _tok(站点, "eng")
    code, d = 站点.req("GET", "/api/map-wizard", token=gina)
    assert code == 200 and d["wizard"] is None and d["maps"] == 0
    code, d = 站点.req("POST", "/api/map-wizard", {"action": "start", "robot_id": "A",
                                                  "site_type": "indoor", "drive": "remote"},
                      token=gina)
    assert code == 403
    code, d = 站点.req("POST", "/api/map-wizard", {"action": "start", "robot_id": "A",
                                                  "site_type": "indoor", "drive": "remote"},
                      token=eng)
    assert code == 200 and d["wizard"]["map_id"] == "main", d
    code, d = 站点.req("GET", "/api/map-wizard", token=gina)
    assert d["wizard"]["by"] == 站点.eng[0] and d["wizard"]["robot_id"] == "A"
    code, d = 站点.req("POST", "/api/map-wizard", {"action": "start", "robot_id": "A",
                                                  "site_type": "outdoor", "drive": "remote",
                                                  "replace": True}, token=eng)
    assert code == 400 and d.get("reason") == "coming_soon", d


def test_接口_一版的结论_总览里也带(站点):
    put_version(站点, 站点.maps.root.parent, "8", build=_lc(used=True, closures=1, path_m=50.0,
                                                          max_shift_m=0.1))
    tok = _tok(站点)
    code, d = 站点.req("GET", f"/api/admin/maps/{MAP[0]}/8/quality", token=tok)
    assert code == 200 and d["verdict"] == "usable", d
    ov = 站点.req("GET", "/api/admin/maps", token=tok)[1]
    v8 = [v for m in ov["maps"] for v in m["versions"] if v["version"] == "8"][0]
    assert v8["quality"]["verdict"] == "usable"
    assert 站点.req("GET", f"/api/admin/maps/{MAP[0]}/8/quality",
                   token=_tok(站点, "gina"))[0] == 403


def test_向导记录_稍后建图记在账号上(站点):
    tok = _tok(站点)
    code, d = 站点.req("POST", "/api/me/guide", {"map_later": True}, token=tok)
    assert code == 200 and d["map_later"] is True
    assert 站点.req("GET", "/api/me", token=tok)[1]["guide"]["map_later"] is True


def test_版本号的日期按站点时区(站点):
    from datetime import datetime, timezone
    # 2026-10-31 20:00 UTC:站点在吉隆坡(+8)已经是 11-01
    t = int(datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
    with 站点.db.tx() as c:
        c.execute("INSERT OR REPLACE INTO route_store(id, timezone) VALUES (1, ?)",
                  ("Asia/Kuala_Lumpur",))
    w = MapWizard(站点.api.settings, 站点.maps, 站点.db, now_ms=lambda: t, known=lambda r: True)
    v = w.apply({"action": "start", "robot_id": "A", "site_type": "indoor", "drive": "remote"},
                by="alice")
    assert v["version"] == "20261101-1"
