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
        s.now[0] = T0 + m * 60_000                        # 一分钟收齐一段
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



# ------------------------------------------------------------ W18 外审:留存按站点收齐的时刻;
# 删不掉不算


HALF_YEAR = 182 * 86400_000


def test_W18外审_狗钟慢半年快半年_都从站点收齐那天起留30天(库):
    s = 库
    slow = "20260404T010000Z"                             # 狗钟慢半年
    fast = "20270404T010000Z"                             # 狗钟快半年
    _存(s, robot="A", stamp=slow)
    _存(s, robot="B", stamp=fast)
    assert s.prune() == (0, 0), "刚收到的:慢半年的不许当成过期"
    s.now[0] = T0 + KEEP_DAYS * 86400_000 - 1
    assert s.prune() == (0, 0)
    s.now[0] = T0 + KEEP_DAYS * 86400_000 + 1
    assert s.prune() == (2, 0), "快半年的也是 30 天到期,不多留"
    assert s.list() == []


def test_W18外审_几只狗钟差不同_盘紧按收齐先后删(库):
    s = 库
    s.now[0] = T0
    _存(s, robot="A", stamp="20261004T010000Z")           # 先收到,钟准
    s.now[0] = T0 + 60_000
    _存(s, robot="B", stamp="20260404T010100Z")           # 后收到,钟慢半年
    def usage(p):                                         # 删一段就够
        n = len(s.list())
        return (1000, 950 if n == 2 else 800, 50 if n == 2 else 200)
    s._disk_usage = usage
    assert s.prune() == (0, 1)
    [left] = s.list()
    assert left["robot_id"] == "B", "先删的是先收齐的那段,不是狗钟最慢的那段"


def test_W18外审_重传不刷新留存起点(库):
    s = 库
    _存(s, stamp="20261004T010000Z")
    s.now[0] = T0 + 20 * 86400_000
    _存(s, stamp="20261004T010000Z")                       # 同一段又传了一遍
    [r] = s.list()
    assert r["received_ms"] == T0
    s.now[0] = T0 + KEEP_DAYS * 86400_000 + 1
    assert s.prune() == (1, 0)


def test_W18外审_文件删不掉_登记留着_不算腾了_报一次_好了之后下一拍删掉(库, monkeypatch):
    from pathlib import Path
    s = 库
    for m in range(3):
        s.now[0] = T0 + m * 60_000
        _存(s, stamp=f"20261004T01{m:02d}00Z")
    s.disk[0] = (1000, 950, 50)                           # 盘紧
    real = Path.unlink
    broken = [True]

    def 只读(self, missing_ok=False):
        if broken[0]:
            raise PermissionError("只读挂载")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", 只读)
    stuck = []
    s.on_stuck = lambda n, why: stuck.append((n, why))
    trimmed = []
    s.on_trimmed = lambda n, oldest: trimmed.append(n)
    s.now[0] = T0 + KEEP_DAYS * 86400_000 + 10 * 60_000
    assert s.prune() == (0, 0), "一段都没删掉:按期的、腾盘的都不算"
    assert len(s.list()) == 3 and len(list(s.root.rglob("*.mp4"))) == 3, "登记、文件都还在"
    assert trimmed == [] and stuck == [(3, "只读挂载")]
    broken[0] = False
    s.disk[0] = (1000, 500, 500)
    assert s.prune() == (3, 0), "好了之后下一拍照样删掉"
    assert s.list() == [] and not list(s.root.rglob("*.mp4"))



def test_W18复查_前50段删不掉_第51段照样删_不被饿死(库, monkeypatch):
    from pathlib import Path
    s = 库
    for m in range(51):
        s.now[0] = T0 + m * 60_000
        _存(s, stamp=f"20261004T{1 + m // 60:02d}{m % 60:02d}00Z")
    rows = sorted(s.list(), key=lambda r: r["received_ms"])
    bad = {s.path(r) for r in rows[:50]}
    real = Path.unlink

    def 只读(self, missing_ok=False):
        if self in bad:
            raise PermissionError("只读")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", 只读)
    s._disk_usage = lambda p: (1000, 950, 50) if len(s.list()) == 51 else (1000, 800, 200)
    stuck, trimmed = [], []
    s.on_stuck = lambda n, why: stuck.append(n)
    s.on_trimmed = lambda n, oldest: trimmed.append(n)
    assert s.prune() == (0, 1), "第 51 段删掉、算进腾盘"
    left = {r["id"] for r in s.list()}
    assert left == {r["id"] for r in rows[:50]}, "前 50 段(删不掉的)登记还在"
    assert all(p.exists() for p in bad)
    assert trimmed == [1] and stuck == [50], "删不掉的报一次"
