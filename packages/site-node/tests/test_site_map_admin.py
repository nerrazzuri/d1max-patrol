"""地图管理页的后端:总览、备注、发布到几只狗、回退不重新下载、删除的规矩、对比(经站点 API)。"""

from __future__ import annotations

import json
import time

import pytest
from test_site_api import MAP, PW, _等, 站

from d1max_contract.maps import map_digest

YAML = ("image: floor.pgm\nresolution: 0.1\norigin: [0.0, 0.0, 0.0]\nnegate: 0\n"
        "occupied_thresh: 0.65\nfree_thresh: 0.196\n")


def pgm(w: int, h: int, cells: dict[tuple[int, int], int] | None = None,
        room: tuple[int, int, int, int] = (5, 5, 35, 35)) -> bytes:
    """一间房:房外未知(205)、房里空闲(254)、墙一圈(0)。``cells``:(列, 行) → 灰度,盖在上面。"""
    x0, y0, x1, y1 = room
    px = bytearray([205]) * (w * h)
    for r in range(h):
        for c in range(w):
            if x0 <= c <= x1 and y0 <= r <= y1:
                px[r * w + c] = 0 if c in (x0, x1) or r in (y0, y1) else 254
    for (c, r), v in (cells or {}).items():
        px[r * w + c] = v
    return f"P5\n{w} {h}\n255\n".encode() + bytes(px)


def put_version(s, tmp_path, version: str, *, grid: bytes | None = None, build: dict | None = None,
                frames: bytes = b"{}") -> None:
    d = tmp_path / f"src-{version}"
    d.mkdir()
    (d / "floor.yaml").write_text(YAML)
    (d / "floor.pgm").write_bytes(grid or pgm(40, 40))
    (d / "frames.json").write_bytes(frames)
    if build is not None:
        (d / "build.json").write_text(json.dumps(build))
    s.maps.import_dir(d, map_id=MAP[0], version=version, note="")
    time.sleep(0.01)                                   # 登记时刻错开:哪个是最新的有定论


BUILD = {"version": 1, "source": "office-bag-1013", "builder": "mola-lidar-odometry",
         "frames_count": 812, "grid": {"res": 0.1, "size": [40, 40], "origin": [0, 0], "rays": "x"},
         "loop_closure": {"used": True, "closures": 3}, "handedness": {"verdict": "ok"}}


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True, maps={})
    s.accounts.add("gina", PW, role="guard")
    s.disp.maps = s.maps                                       # 主程序接的;测试台没接
    d = tmp_path / "cur"
    d.mkdir()
    (d / "m.pgm").write_bytes(b"x")
    s.maps.import_dir(d, map_id=MAP[0], version=MAP[1])        # 狗正在用的那一版(7)
    put_version(s, tmp_path, "8", build=BUILD)
    put_version(s, tmp_path, "9", grid=pgm(40, 40, {(20, 20): 0, (21, 20): 0}))
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def _rc(s):
    c = s.disp.clients["A"].capabilities
    return None if c is None else c.tasks.get("running_config")


def _確认(s, token, version):
    code, d = s.req("POST", f"/api/maps/{MAP[0]}/{version}/zones/confirm",
                    {"revision": 0, "confirm": True, "empty_ok": True}, token=token)
    assert code == 200, (code, d)


def _publish(s, token, version, robots=("A",)):
    return s.req("POST", f"/api/admin/maps/{MAP[0]}/{version}/publish",
                 {"robots": list(robots), "without_home": True}, token=token)


def _wait_loaded(s, version):
    _等(lambda: (_rc(s) or {}).get("map_version") == version, timeout=10)


def test_总览_版本信息_构建信息_数量_缩略图_只有管理员能看(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    _等(lambda: _rc(s) is not None)
    assert s.req("GET", "/api/admin/maps", token=gina)[0] == 403
    assert s.req("GET", "/api/admin/maps")[0] == 401
    code, d = s.req("GET", "/api/admin/maps", token=alice)
    assert code == 200
    (m,) = d["maps"]
    assert m["map_id"] == MAP[0]
    assert [v["version"] for v in m["versions"]] == ["9", "8", "7"], "新的在前"
    v9, v8, v7 = m["versions"]
    assert v9["latest"] and not v8["latest"]
    assert v8["build"]["source"] == "office-bag-1013"
    assert v8["build"]["loop_closure"] == {"used": True, "closures": 3, "why": None}
    assert v8["build"]["handedness"]["verdict"] == "ok"
    assert v7["build"] is None, "没有 build.json 的(导入的)不编"
    assert v8["size_bytes"] > 1000 and v8["thumbnail"].endswith("/8/preview.png")
    assert v8["counts"] == {"zones": 0, "nogo": 0, "slow": 0, "zones_rev": 0,
                            "zones_confirmed": False, "standby": 0, "intercepts": 0}
    assert v7["loaded_on"] == ["A"], "狗开机加载的是 7"
    (r,) = d["robots"]
    assert r["robot_id"] == "A" and r["loaded"] == {"map_id": MAP[0], "version": "7"}
    assert r["online"] and r["activation"] is None and r["rollback"] is None


def test_备注_只有管理员能改_记进审计(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    path = f"/api/admin/maps/{MAP[0]}/8/note"
    assert s.req("POST", path, {"note": "x"}, token=gina)[0] == 403
    assert s.req("POST", path, {"note": 5}, token=alice)[0] == 400
    assert s.req("POST", f"/api/admin/maps/{MAP[0]}/99/note", {"note": "x"}, token=alice)[0] == 404
    code, d = s.req("POST", path, {"note": "  十月十三日大厅重建  "}, token=alice)
    assert code == 200 and d["note"] == "十月十三日大厅重建"
    v8 = next(v for v in s.req("GET", "/api/admin/maps", token=alice)[1]["maps"][0]["versions"]
              if v["version"] == "8")
    assert v8["note"] == "十月十三日大厅重建"
    row = s.db.query("SELECT actor, target, detail FROM audit WHERE action LIKE '%/note'")[-1]
    assert (row["actor"], row["target"]) == ("alice", f"{MAP[0]}:8")
    assert "大厅重建" in row["detail"]
    assert len(s.req("POST", path, {"note": "n" * 500}, token=alice)[1]["note"]) == 200


def test_发布_每只狗一个结果_禁行区没确认拒_确认后狗换上_状态pending到confirmed(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    _等(lambda: _rc(s) is not None)
    assert _publish(s, gina, "8")[0] == 403
    bad = s.req("POST", f"/api/admin/maps/{MAP[0]}/8/publish", {"robots": []}, token=alice)
    assert bad[0] == 400
    code, d = _publish(s, alice, "8", ("A", "ghost"))
    assert code == 200
    by = {r["robot_id"]: r for r in d["results"]}
    assert by["A"]["state"] == "refused" and by["A"]["code"] == "zones_unconfirmed", by
    assert by["ghost"]["state"] == "refused" and "没有这台狗" in by["ghost"]["reason"]
    _確认(s, alice, "8")
    code, d = _publish(s, alice, "8", ("A", "ghost"))
    by = {r["robot_id"]: r for r in d["results"]}
    assert by["A"]["ok"] and by["A"]["state"] == "pending", by
    _wait_loaded(s, "8")
    _等(lambda: s.req("GET", "/api/admin/maps", token=alice)[1]["robots"][0]["activation"]["state"]
        == "confirmed")
    r = s.req("GET", "/api/admin/maps", token=alice)[1]["robots"][0]
    assert r["activation"]["version"] == "8" and r["activation"]["confirmed"] == "load"
    assert r["loaded"] == {"map_id": MAP[0], "version": "8"}
    # R4:狗报的图摘要跟站点登记的一样
    assert r["running"]["map"]["match"] is True, (r["running"], _rc(s))
    assert _rc(s)["map_digest"] == map_digest(s.maps.get(MAP[0], "8"))
    audit = s.db.query("SELECT detail, target FROM audit WHERE action LIKE '%/publish' "
                       "ORDER BY id DESC LIMIT 1")[0]
    assert audit["target"] == f"{MAP[0]}:8" and '"A": "pending"' in audit["detail"]


def test_回退_狗本地留着上一张_不向站点下载一个字节(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _rc(s) is not None)
    for v in ("8", "9"):
        _確认(s, alice, v)
    assert _publish(s, alice, "8")[1]["results"][0]["ok"]
    _wait_loaded(s, "8")
    assert _publish(s, alice, "9")[1]["results"][0]["ok"]
    _wait_loaded(s, "9")
    _等(lambda: _rc(s).get("previous") == [MAP[0], "8"])
    r = s.req("GET", "/api/admin/maps", token=alice)[1]
    assert r["robots"][0]["rollback"] == {"map_id": MAP[0], "version": "8", "local": True}
    v8 = next(v for v in r["maps"][0]["versions"] if v["version"] == "8")
    assert v8["previous_on"] == ["A"]
    reads = []
    orig = s.maps.file_path
    monkeypatch.setattr(s.maps, "file_path", lambda *a: reads.append(a) or orig(*a))
    assert s.req("POST", "/api/admin/maps/rollback", {"robot_id": "ghost"}, token=alice)[0] == 404
    code, d = s.req("POST", "/api/admin/maps/rollback", {"robot_id": "A", "without_home": True},
                    token=alice)
    assert code == 200 and d["result"]["ok"] and d["target"]["local"] is True, d
    _wait_loaded(s, "8")
    _等(lambda: _rc(s).get("previous") == [MAP[0], "9"])
    assert reads == [], "回退用狗本地留着的那张:站点没被要过任何文件"
    row = s.db.query("SELECT target, detail FROM audit WHERE action LIKE '%/rollback' "
                     "ORDER BY id DESC LIMIT 1")[0]
    assert row["target"] == "A" and f"{MAP[0]}:8" in row["detail"]


def test_回退_没有上一版就409(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _rc(s) is not None)
    code, d = s.req("POST", "/api/admin/maps/rollback", {"robot_id": "A"}, token=alice)
    assert code == 409 and d["reason"] == "no_previous"


def test_删除_有狗在用拒_最新一版拒_旧的删得掉_文件和挂着的东西一起清(站点, tmp_path):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    _等(lambda: _rc(s) is not None)
    def dele(v, t=None):
        return s.req("POST", f"/api/admin/maps/{MAP[0]}/{v}/delete", {}, token=t or alice)

    assert dele("7", gina)[0] == 403
    code, d = dele("7")                                   # 狗开机加载的就是 7
    assert code == 409 and d["reason"] == "in_use" and d["robots"] == ["A"]
    code, d = dele("9")
    assert code == 409 and d["reason"] == "latest"
    assert dele("99")[0] == 404
    _確认(s, alice, "8")
    assert _publish(s, alice, "8")[1]["results"][0]["ok"]
    _wait_loaded(s, "8")
    # W41:再换到 9,8 就是狗的退回目标(狗上留着,出事一键退回去),下面删它要拒
    _確认(s, alice, "9")
    assert _publish(s, alice, "9")[1]["results"][0]["ok"]
    _wait_loaded(s, "9")
    _等(lambda: _rc(s).get("previous") == [MAP[0], "8"])
    # 7 上挂了待命点、区域:删掉之后一起没
    with s.db.tx() as c:
        c.execute("INSERT INTO standby_points(robot_id, map_id, map_version, name, x, y, yaw, "
                  "is_default) VALUES ('A', ?, '7', 'gate', 1, 1, 0, 1)", (MAP[0],))
    s.disp.zones.put(MAP[0], "7", [], base_revision=0, by="alice")
    folder = s.maps.root / MAP[0] / "7"
    assert folder.is_dir()
    code, d = dele("7")
    assert code == 200 and d["deleted"] == f"{MAP[0]}:7" and d["freed_bytes"] > 0, d
    assert d["removed_rows"]["standby_points"] == 1 and d["removed_rows"]["nav_zones"] == 1
    assert not folder.exists()
    assert [m["version"] for m in s.maps.list()] == ["9", "8"]
    assert s.req("GET", f"/api/maps/{MAP[0]}/7/preview", token=alice)[0] == 404
    row = s.db.query("SELECT target, detail FROM audit WHERE action LIKE '%/delete' "
                     "ORDER BY id DESC LIMIT 1")[0]
    assert row["target"] == f"{MAP[0]}:7" and "freed_bytes" in row["detail"]
    code, d = dele("8")                                   # 8 现在是退回目标
    assert code == 409 and d["reason"] == "rollback_target" and d["robots"] == ["A"], d


def test_删除_正在换过去的那一版也拒(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: _rc(s) is not None)
    with s.db.tx() as c:                                  # 命令账里有一条还没结果的换图
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by,"
                  " issued_at, ack_result) VALUES ('c1','map_activate-x','A','map_activate',?,"
                  "'alice',?, 'accepted')",
                  (json.dumps({"map_id": MAP[0], "version": "8"}), int(time.time() * 1000)))
    code, d = s.req("POST", f"/api/admin/maps/{MAP[0]}/8/delete", {}, token=alice)
    assert code == 409 and d["reason"] == "pending"


def test_换图状态_从命令账和事件推_老代理的成功标出没等定位器_超时算没成(站点):
    s = 站点
    adm = __import__("d1max_site.maps_admin", fromlist=["x"]).MapAdmin(s.api)
    now = int(time.time() * 1000)

    def cmd(i, t_ms, ack="accepted", reason=None):
        with s.db.tx() as c:
            c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, "
                      "issued_by, issued_at, ack_result, ack_reason) VALUES (?,?,?,?,?,?,?,?,?)",
                      (f"c{i}", f"t{i}", "A", "map_activate",
                       json.dumps({"map_id": MAP[0], "version": str(i)}), "alice", t_ms, ack,
                       reason))

    def ev(i, kind, data):
        with s.db.tx() as c:
            c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                      "received_at) VALUES ('A','b',?,?,?,?,?,?)",
                      (i, f"e{i}", kind, json.dumps(data), now, now))
    cmd(1, now - 5000)
    ev(1, "map_activated", {"task_id": "t1"})                                   # 老代理
    cmd(2, now - 4000)
    ev(2, "map_activated", {"task_id": "t2", "confirmed": "localizer"})
    cmd(3, now - 3000)
    ev(3, "map_activate_failed", {"task_id": "t3", "reason": "定位器不收这张图的先验:坏了"})
    cmd(4, now - 2000, ack="rejected", reason="busy")
    cmd(5, now - 1000)
    cmd(6, now - 50 * 60_000)
    got = {a["version"]: a for a in adm.activations("A", 20)}
    assert got["1"]["state"] == "confirmed" and got["1"]["confirmed"] == "legacy"
    assert got["2"]["state"] == "confirmed" and got["2"]["confirmed"] == "localizer"
    assert got["3"]["state"] == "failed" and "定位器不收" in got["3"]["reason"]
    assert got["4"]["state"] == "failed" and got["4"]["reason"] == "busy"
    assert got["5"]["state"] == "pending"
    assert got["6"]["state"] == "timeout"
    assert [a["version"] for a in adm.activations("A", 20)][:2] == ["5", "4"], "新的在前"


# ------------------------------------------------------------ 对比

def test_对比接口_统计加图_带必读的提醒_权限(站点):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    base = f"/api/admin/maps/{MAP[0]}/8/diff"
    assert s.req("GET", base + "?with=9", token=gina)[0] == 403
    assert s.req("GET", base + "?with=8", token=alice)[0] == 400
    assert s.req("GET", base + "?with=nope", token=alice)[0] == 404
    code, d = s.req("GET", base + "?with=9", token=alice)
    assert code == 200
    assert d["counts"]["wall_only_b"] == 2 and d["counts"]["wall_only_a"] == 0
    assert d["caveat"] and "坐标系" in d["caveat"]
    assert d["frames_json_same"] is True and d["likely_misaligned"] is False
    import urllib.request
    r = urllib.request.Request(s.api.url + base + ".png?with=9",
                               headers={"Authorization": f"Bearer {alice}"})
    with urllib.request.urlopen(r, timeout=15) as resp:
        body = resp.read()
        assert resp.headers["Content-Type"] == "image/png"
    assert body.startswith(b"\x89PNG")
