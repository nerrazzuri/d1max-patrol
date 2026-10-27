"""W44:位置轨迹落库(录像页那一刻在哪、地图页多久没巡过;设计 §4.3、§4.4、§8)。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from test_site_api import PW, _等, 站

from d1max_site.db import SiteDB
from d1max_site.pose_track import CELL_M, KEEP_MS, MAX_GAP_MS, STEP_M, PoseTrack


def tel(stamp: int, x: float | None, y: float = 0.0, version: str = "1"):
    pose = None if x is None else SimpleNamespace(map_id="m", map_version=version, frame_id="map",
                                                  x=x, y=y, yaw=0.0)
    return SimpleNamespace(stamp=stamp, pose=pose)


@pytest.fixture
def pt(tmp_path):
    return PoseTrack(SiteDB(tmp_path / "s.db"))


def test_常量():
    assert STEP_M == 1.0 and MAX_GAP_MS == 10_000 and KEEP_MS == 30 * 86_400_000 and CELL_M == 2.0


def test_轨迹按机器人的钟记(pt):
    """录像的开头时刻是机器人的钟;轨迹也要按遥测的 stamp 记,不能用站点收到的时刻。"""
    pt.on_telemetry("A", tel(1000, 0.0))
    assert pt.track("A", 0, 10_000) == [
        {"t": 1000, "map_id": "m", "map_version": "1", "x": 0.0, "y": 0.0}]


def test_没挪不记_挪了1米才记_但每10秒记一个(pt):
    pt.on_telemetry("A", tel(0, 0.0))
    pt.on_telemetry("A", tel(1000, 0.5))           # 挪了 0.5 米、过了 1 秒:不记
    pt.on_telemetry("A", tel(2000, 1.2))           # 挪了 1.2 米:记
    pt.on_telemetry("A", tel(3000, 1.3))           # 不记
    pt.on_telemetry("A", tel(12_000, 1.3))         # 没挪,但离上一个记的过了 10 秒:记
    assert [p["t"] for p in pt.track("A", 0, 20_000)] == [0, 2000, 12_000]


def test_定位不可信那段不记(pt):
    pt.on_telemetry("A", tel(0, None))
    assert pt.track("A", 0, 10_000) == []


def test_换图另记_没巡过只算问的那一版(pt):
    pt.on_telemetry("A", tel(0, 0.5, 0.5, version="1"))
    pt.on_telemetry("A", tel(500, 0.5, 0.5, version="2"))       # 换了一版:马上记
    got = pt.track("A", 0, 1000)
    assert [p["map_version"] for p in got] == ["1", "2"]
    assert pt.last_visit("m", "1", 0) == [{"i": 0, "j": 0, "t": 0}]
    pt.on_telemetry("B", tel(800, 4.5, -0.5, version="1"))
    got = sorted((c["i"], c["j"], c["t"]) for c in pt.last_visit("m", "1", 0))
    assert got == [(0, 0, 0), (2, -1, 800)]


def test_多久没巡过_每格最后一次(pt):
    pt.on_telemetry("A", tel(0, 0.5))
    pt.on_telemetry("A", tel(20_000, 0.7))
    assert pt.last_visit("m", "1", 0) == [{"i": 0, "j": 0, "t": 20_000}]
    assert pt.last_visit("m", "1", 30_000) == []


def test_过期的删掉(pt):
    pt.on_telemetry("A", tel(0, 0.0))
    pt.on_telemetry("A", tel(KEEP_MS + 5000, 5.0))
    assert pt.purge(KEEP_MS + 2000) == 1
    assert [p["t"] for p in pt.track("A", 0, KEEP_MS + 10_000)] == [KEEP_MS + 5000]


def test_点太多均匀抽稀(pt):
    for i in range(30):
        pt.on_telemetry("A", tel(i * 1000, float(i * 2)))
    got = pt.track("A", 0, 100_000, limit=10)
    assert len(got) <= 10 and got[0]["t"] == 0 and got[-1]["t"] == 29_000


# ------------------------------------------------------------ 接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.api.pose_track = PoseTrack(s.db)
    s.api.pose_track.attach(s.disp)
    yield s
    s.close()


def _登(s, name="alice"):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def test_轨迹接口(站点):
    s = 站点
    tok = _登(s)
    _等(lambda: s.api.pose_track.track("A", 0, 2**62) or None)
    now = int(time.time() * 1000)
    q = f"since={now - 3_600_000}&until={now + 60_000}"
    code, d = s.req("GET", f"/api/robots/A/track?{q}", token=tok)
    assert code == 200 and d["thinned"] is False
    assert d["points"] and d["points"][0]["map_id"]
    assert set(d["points"][0]) == {"t", "map_id", "map_version", "x", "y"}


def test_轨迹接口跨度超过24小时400(站点):
    s = 站点
    tok = _登(s)
    assert s.req("GET", "/api/robots/A/track?since=0&until=86400001", token=tok)[0] == 400
    assert s.req("GET", "/api/robots/A/track?since=x&until=1", token=tok)[0] == 400
    assert s.req("GET", "/api/robots/A/track?since=0&until=1")[0] == 401


def test_多久没巡过接口(站点):
    s = 站点
    tok = _登(s)
    s.api.pose_track.on_telemetry("Z", tel(5000, 0.5))
    code, d = s.req("GET", "/api/coverage?map_id=m&map_version=1&since=0", token=tok)
    assert code == 200 and d["cell_m"] == 2.0 and {"i": 0, "j": 0, "t": 5000} in d["cells"]
    assert s.req("GET", "/api/coverage?map_id=m&since=0", token=tok)[0] == 400
