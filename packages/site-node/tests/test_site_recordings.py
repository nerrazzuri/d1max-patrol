"""连续录像在站点(W18,决策 31、32):登记、按时间查、留 30 天、标留着的不删、盘紧先删录像、
接口权限。"""

from __future__ import annotations

import os
import urllib.request

import pytest
from test_site_api import PW, 站

from d1max_site.db import SiteDB
from d1max_site.recordings import KEEP_DAYS, RecordingStore, stamp_ms

T0 = stamp_ms("20261004T010000Z")


def _存(store, robot="A", camera="front", stamp="20261004T010000Z", n=1000):
    data = os.urandom(n)
    store.put(robot, f"{camera}/{stamp}", "video.mp4", offset=0, data=data, total=len(data))
    return data


@pytest.fixture
def 库(tmp_path):
    now = [T0]
    disk = [(1000, 100, 900)]
    s = RecordingStore(SiteDB(tmp_path / "s.db"), tmp_path / "rec", now_ms=lambda: now[0],
                       disk_usage=lambda p: disk[0])
    s.now, s.disk = now, disk
    return s


def test_收齐才登记_按时间查盖住那一刻的(库):
    s = 库
    data = b"abc" * 1000
    s.put("A", "front/20261004T010000Z", "video.mp4", offset=0, data=data[:1000], total=len(data))
    assert s.list() == [], "没收齐不登记"
    s.put("A", "front/20261004T010000Z", "video.mp4", offset=1000, data=data[1000:],
          total=len(data))
    _存(s, stamp="20261004T010100Z")
    _存(s, stamp="20261004T010200Z")
    _存(s, robot="B", stamp="20261004T010100Z")
    _存(s, camera="back", stamp="20261004T010100Z")
    [r] = [x for x in s.list(robot_id="A", camera="front") if x["stamp"] == "20261004T010000Z"]
    assert r["bytes"] == len(data) and s.path(r).read_bytes() == data
    got = s.list(robot_id="A", camera="front", since_ms=T0 + 90_000, until_ms=T0 + 100_000)
    assert [x["stamp"] for x in got] == ["20261004T010100Z"], "01:01 那段盖住 01:01:30"
    assert [x["stamp"] for x in s.list(robot_id="A", camera="front")] == [
        "20261004T010200Z", "20261004T010100Z", "20261004T010000Z"], "最新的在前"


def test_留30天_标了留着的不删(库):
    s = 库
    _存(s, stamp="20261004T010000Z")
    _存(s, stamp="20261004T010100Z")
    keep = s.list()[0]
    s.set_keep(keep["id"], True)
    s.now[0] = T0 + KEEP_DAYS * 86400_000 - 1
    assert s.prune() == (0, 0)
    s.now[0] = T0 + KEEP_DAYS * 86400_000 + 120_000
    assert s.prune() == (1, 0)
    [left] = s.list()
    assert left["id"] == keep["id"] and left["keep"] == 1
    assert not any(p.name == "20261004T010000Z.mp4" for p in s.root.rglob("*.mp4"))


def test_盘紧了从最旧的删_删到够_报一次(库):
    s = 库
    for m in range(5):
        _存(s, stamp=f"20261004T01{m:02d}00Z")
    s.set_keep(s.list()[-1]["id"], True)                  # 最旧那段标了留着:跳过
    told = []
    s.on_trimmed = lambda n, oldest: told.append((n, oldest))

    def usage(p):                                         # 每删一段腾出 3% 的盘
        n = len(s.list())
        return (1000, 1000 - 50 - 30 * (5 - n), 50 + 30 * (5 - n))
    s._disk_usage = usage
    assert s.prune() == (0, 4)
    assert told == [(4, T0 + 60_000)]
    [r] = s.list()
    assert r["keep"] == 1
    s._disk_usage = lambda p: (1000, 500, 500)
    assert s.prune() == (0, 0) and len(told) == 1, "盘不紧不删、不报"


def test_老库升级_有录像表(tmp_path):
    import sqlite3
    p = tmp_path / "s.db"
    sqlite3.connect(p).close()
    db = SiteDB(p)
    assert db.query("SELECT COUNT(*) AS n FROM recordings")[0]["n"] == 0


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.rec = RecordingStore(s.db, tmp_path / "rec", now_ms=lambda: T0)
    s.api.recordings = s.rec
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def test_接口_谁都能看能放_值班的人能标留着(站点):
    s = 站点
    data = _存(s.rec, n=5000)
    olga, gina = _登(s, "olga"), _登(s, "gina")
    code, d = s.req("GET", f"/api/recordings?robot=A&since={T0 + 10_000}&until={T0 + 20_000}",
                    token=olga)
    assert code == 200 and d["segment_s"] == 60
    [r] = d["recordings"]
    host, port = s.api.httpd.server_address[:2]
    req = urllib.request.Request(f"http://{host}:{port}/api/recordings/{r['id']}/video",
                                 headers={"Authorization": f"Bearer {olga}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers["Content-Type"] == "video/mp4" and resp.read() == data
    assert s.req("POST", f"/api/recordings/{r['id']}/keep", {"keep": True}, token=olga)[0] == 403
    code, d = s.req("POST", f"/api/recordings/{r['id']}/keep", {"keep": True}, token=gina)
    assert code == 200 and d["recording"]["keep"] == 1
    assert s.req("POST", f"/api/recordings/{r['id']}/keep", {"keep": "yes"}, token=gina)[0] == 400
    assert s.req("GET", "/api/recordings/999/video", token=olga)[0] == 404
    assert s.req("GET", "/api/recordings?since=x", token=olga)[0] == 400
    assert s.req("GET", "/api/recordings")[0] == 401
    s.rec.path(r).unlink()
    assert s.req("GET", f"/api/recordings/{r['id']}/video", token=olga)[0] == 404


def test_接口_没开录像的站点404(tmp_path):
    s = 站(tmp_path)
    try:
        code, d = s.req("POST", "/api/login", {"name": "alice", "password": PW})
        assert s.req("GET", "/api/recordings", token=d["token"])[0] == 404
    finally:
        s.close()


def test_狗报录像断了_删了没传的_站点为腾盘删了_都是P2(tmp_path):
    from d1max_contract.messages import Event
    from d1max_site.alert_sources import SiteAlertSources
    from d1max_site.alert_store import AlertDesk
    db = SiteDB(tmp_path / "s.db")
    desk = AlertDesk(db, now_ms=lambda: T0)
    src = SiteAlertSources(desk, now_ms=lambda: T0)

    def ev(kind, data):
        return Event(event_id=kind, kind=kind, stamp=T0, data=data, boot_id="b", seq=1)
    src.on_event("A", ev("recording_failed", {"camera": "front", "reason": "相机不通"}))
    src.on_event("A", ev("recording_dropped", {"count": 3, "oldest": "front/x", "reason": "disk"}))
    got = {a["kind"]: a for a in desk.open()}
    assert got["recording_failed"]["level"] == "P2" and "front" in got["recording_failed"]["title"]
    assert got["recording_dropped"]["level"] == "P2" and "3 段" in got["recording_dropped"]["title"]
    assert "盘紧" in got["recording_dropped"]["detail"]
    desk.raise_alert(kind="recording_trimmed", robot="site", title="x")
    assert {a["kind"] for a in desk.open()} >= {"recording_trimmed"}
