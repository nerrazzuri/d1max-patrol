"""W40(上装 V1.2,决策 70):热像当第三路相机。``cameras.json`` 校验、各路从哪拉、口令不外漏、
``--record auto`` 录不录、狗没有的那一路推流直接拒。不要 ffmpeg。"""

from __future__ import annotations

import argparse
import json

import pytest

from d1max_agent.main import record_cameras
from d1max_agent.video_push import (
    CameraConfigError,
    VideoPusher,
    lavfi_source,
    load_cameras,
    rtsp_source,
)
from d1max_contract.video import VideoRequest, scrub

URL = "rtsp://admin:S3cret!pw@192.168.10.64:554/Streaming/Channels/101"


def _写(tmp_path, d) -> object:
    p = tmp_path / "cameras.json"
    p.write_text(json.dumps(d) if not isinstance(d, str) else d)
    return p


def test_相机配置_没有文件就是只有自带两路(tmp_path):
    assert load_cameras(tmp_path / "nope.json") == {}
    src = rtsp_source("192.168.168.168", {})
    assert src.cameras == ("front", "back")
    assert src("back")[-1] == "rtsp://192.168.168.168:8554/back"


def test_相机配置_热像按自己的地址拉_能改自带那两路(tmp_path):
    got = load_cameras(_写(tmp_path, {"thermal": {"url": URL, "record": True},
                                       "front": {"url": "rtsp://10.0.0.5/live"}}))
    assert got == {"thermal": {"url": URL, "record": True},
                   "front": {"url": "rtsp://10.0.0.5/live", "record": False}}
    src = rtsp_source("192.168.168.168", got)
    assert src.cameras == ("front", "back", "thermal")
    assert src("thermal") == ["-rtsp_transport", "tcp", "-i", URL]
    assert src("front")[-1] == "rtsp://10.0.0.5/live"


@pytest.mark.parametrize("d", [
    {"side": {"url": URL}},
    {"thermal": {"url": "http://192.168.10.64/"}},
    {"thermal": {}},
    {"thermal": {"url": URL, "record": "yes"}},
    [1],
    "{坏的",
])
def test_相机配置_写错就拒(tmp_path, d):
    with pytest.raises(CameraConfigError):
        load_cameras(_写(tmp_path, d))


def test_RTSP口令打码_SRT口令照旧():
    t = f"[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized {URL} srt://h:1?passphrase=abc&x=1"
    out = scrub(t)
    assert "S3cret" not in out and "rtsp://admin:***@192.168.10.64" in out
    assert "passphrase=***" in out
    assert scrub("rtsp://192.168.168.168:8554/front") == "rtsp://192.168.168.168:8554/front"


def test_狗没有的那一路_推流直接拒_不起进程():
    v = VideoPusher(source=lavfi_source, ffmpeg="/nonexistent/ffmpeg")
    assert v.cameras() == ("front", "back")
    why = v.request(VideoRequest(camera="thermal", url="srt://127.0.0.1:9000",
                                 passphrase="Q7kP2mX9vL4nR8tW", ttl_ms=5000))
    assert why.startswith("unsupported") and "thermal" in why
    assert v.starts == 0


def test_record_auto_真狗录写了record的热像_没写的不录(tmp_path):
    ns = argparse.Namespace
    on = _写(tmp_path, {"thermal": {"url": URL, "record": True}})
    assert record_cameras(ns(record="auto", hal="d1max", cameras=on)) == \
        ["front", "back", "thermal"]
    off = tmp_path / "off.json"
    off.write_text(json.dumps({"thermal": {"url": URL}}))
    assert record_cameras(ns(record="auto", hal="d1max", cameras=off)) == ["front", "back"]
    assert record_cameras(ns(record="front,thermal", hal="d1max", cameras=off)) == \
        ["front", "thermal"]
    with pytest.raises(SystemExit, match="thermal"):
        record_cameras(ns(record="thermal", hal="d1max", cameras=tmp_path / "nope.json"))
    bad = _写(tmp_path, {"thermal": {"url": "ftp://x"}})
    with pytest.raises(SystemExit, match="rtsp"):
        record_cameras(ns(record="auto", hal="d1max", cameras=bad))
