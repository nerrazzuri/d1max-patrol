"""W44:按时刻从录像抓一帧(报警记录页每行的抓拍,设计 §4.5)。

ffmpeg 换成假的脚本:回固定字节 / 失败。"""

from __future__ import annotations

import os
import urllib.request

import pytest
from test_site_api import PW, 站
from test_site_recordings import T0, _存

from d1max_site.recordings import SEGMENT_S, FrameCache, RecordingStore


def _假ffmpeg(tmp_path, *, ok=True):
    """假 ffmpeg:每调一次往 calls 里记一行;ok 就往 stdout 写 JPEG 字节。"""
    calls = tmp_path / "calls"
    p = tmp_path / ("ffmpeg-ok" if ok else "ffmpeg-bad")
    body = "printf 'JPEGDATA'" if ok else "exit 1"
    p.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n{body}\n")
    os.chmod(p, 0o755)
    return str(p), calls


@pytest.fixture
def 库(tmp_path):
    from d1max_site.db import SiteDB
    return RecordingStore(SiteDB(tmp_path / "s.db"), tmp_path / "rec", now_ms=lambda: T0)


def test_哪一段盖住那一刻_边界(库):
    _存(库, stamp="20261004T010000Z")
    assert 库.covering("A", "front", T0)["stamp"] == "20261004T010000Z", "正好在开头"
    assert 库.covering("A", "front", T0 + SEGMENT_S * 1000 - 1) is not None, "结尾前 1 毫秒"
    assert 库.covering("A", "front", T0 + SEGMENT_S * 1000) is None, "结尾那一刻不算"
    assert 库.covering("A", "front", T0 - 1) is None
    assert 库.covering("A", "back", T0) is None, "另一路"


def test_抓一帧_缓存_失败回None(库, tmp_path):
    _存(库, stamp="20261004T010000Z")
    ok, calls = _假ffmpeg(tmp_path)
    cache = FrameCache(库, ffmpeg=ok)
    assert cache.frame("A", "front", T0 + 12_300) == b"JPEGDATA"
    assert "-ss 12" in calls.read_text(), "从这一段开头算偏移的秒数"
    assert cache.frame("A", "front", T0 + 12_900) == b"JPEGDATA"
    assert len(calls.read_text().splitlines()) == 1, "同一段同一秒不再调 ffmpeg"
    assert cache.frame("A", "front", T0 + 120_000) is None, "那一刻没有录像"
    bad, _ = _假ffmpeg(tmp_path, ok=False)
    assert FrameCache(库, ffmpeg=bad).frame("A", "front", T0) is None


def test_缓存有上限(库, tmp_path):
    _存(库, stamp="20261004T010000Z")
    ok, _ = _假ffmpeg(tmp_path)
    cache = FrameCache(库, ffmpeg=ok, max_items=3)
    for s in range(5):
        cache.frame("A", "front", T0 + s * 1000)
    assert len(cache) == 3


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.rec = RecordingStore(s.db, tmp_path / "rec", now_ms=lambda: T0)
    s.api.recordings = s.rec
    s.ok, _ = _假ffmpeg(tmp_path)
    s.api.frames = FrameCache(s.rec, ffmpeg=s.ok)
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def test_接口_抓拍(站点):
    s = 站点
    _存(s.rec, stamp="20261004T010000Z")
    code, d = s.req("POST", "/api/login", {"name": "olga", "password": PW})
    tok = d["token"]
    host, port = s.api.httpd.server_address[:2]
    url = f"http://{host}:{port}/api/recordings/frame?robot=A&camera=front&t={T0 + 5000}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.headers["Content-Type"] == "image/jpeg" and resp.read() == b"JPEGDATA"
    early = f"/api/recordings/frame?robot=A&camera=front&t={T0 - 5000}"
    assert s.req("GET", early, token=tok)[0] == 404
    assert s.req("GET", "/api/recordings/frame?robot=A&camera=front&t=x", token=tok)[0] == 400
    assert s.req("GET", f"/api/recordings/frame?robot=A&camera=front&t={T0}")[0] == 401
