"""W73 第五期:外部设备按位置绑定(设计 §8)。设备登记在图上 → 站点算查看点、所在防区、给它专属的
签名回调;设备只说「我被触发了」。老的按防区名报的接口照收:先按圈好的防区找,找不到再按老映射。"""

from __future__ import annotations

import json
import math

import pytest
from test_site_incidents import 站, 签  # noqa: F401  (夹具)

from d1max_site.areas import AreaBook
from d1max_site.devices import DeviceBook, DeviceError

MAP = ("estate-1", "7")
EAST = [[2.0, -3.0], [8.0, -3.0], [8.0, 3.0], [2.0, 3.0]]


class 点位:
    """假的 ``ZonePosts``:离得最近的点就是它要的点(没有栅格的台子)。"""

    def __init__(self):
        self.asked = []

    def pick(self, map_id, version, poly, near, *, radius_m=None, g=None):
        self.asked.append(near)
        return (round(near[0], 2), round(near[1], 2)), ""


@pytest.fixture
def 设备(站):  # noqa: F811
    t = 站
    t.areas = AreaBook(t.db, now_ms=t.clock)
    t.areas.set(*MAP, "东区", EAST, by="alice")
    t.posts = 点位()
    t.book = DeviceBook(t.db, incidents=t.desk, posts=t.posts, areas=t.areas, now_ms=t.clock)
    t.desk.devices = t.book
    return t


async def _设备报(t, dev, secret, body):
    raw = json.dumps(body).encode()
    ts = t.clock()
    src = f"dev-{dev}"
    t.desk.verify(src, str(ts), 签(secret, ts, raw), raw)
    body = t.desk.prepare(src, json.loads(raw))
    t.desk.parse(body)
    return await t.send(t.desk.handle(src, body))


def test_摄像头_查看点取视线中间_防区按点算_给专属密钥(设备):
    t = 设备
    got = t.book.put("cam-1", {"kind": "camera", "map_id": MAP[0], "map_version": MAP[1],
                               "x": 0.0, "y": 0.0, "yaw": 0.0, "range_m": 10.0}, by="alice")
    d = got["device"]
    assert (d["view_x"], d["view_y"]) == (5.0, 0.0), "扇形中线一半距离"
    assert d["zone"] == "东区"
    assert len(got["secret"]) == 64 and got["source"] == "dev-cam-1"
    assert t.desk.intercept("dev-cam-1")["x"] == 5.0
    again = t.book.put("cam-1", {"kind": "camera", "map_id": MAP[0], "map_version": MAP[1],
                                 "x": 0.0, "y": 1.0, "yaw": math.pi / 2, "range_m": 4.0},
                       by="alice")
    assert again["secret"] is None, "改位置不换密钥"
    assert (again["device"]["view_x"], again["device"]["view_y"]) == (0.0, 3.0)
    assert again["device"]["zone"] == "", "不在任何防区里"


def test_门磁_查看点就在门旁(设备):
    t = 设备
    d = t.book.put("door-1", {"kind": "contact", "map_id": MAP[0], "map_version": MAP[1],
                              "x": 3.0, "y": 1.0}, by="alice")["device"]
    assert (d["view_x"], d["view_y"]) == (3.0, 1.0) and d["zone"] == "东区"


def test_写错的拒(设备):
    t = 设备
    for bad in ({"kind": "toaster"}, {"x": "a"}, {"range_m": 0}, {"map_id": ""}):
        with pytest.raises(DeviceError):
            t.book.put("cam-x", {"kind": "camera", "map_id": MAP[0], "map_version": MAP[1],
                                 "x": 0.0, "y": 0.0, **bad}, by="alice")
    with pytest.raises(DeviceError):
        t.book.put("坏 名", {"kind": "camera", "map_id": MAP[0], "map_version": MAP[1],
                            "x": 0.0, "y": 0.0}, by="alice")


async def test_设备只说被触发了_站点按它的位置派(设备):
    t = 设备
    await t.run(12)
    sec = t.book.put("door-1", {"kind": "contact", "map_id": MAP[0], "map_version": MAP[1],
                                "x": 3.0, "y": 1.0}, by="alice")["secret"]
    r = await _设备报(t, "door-1", sec, {"event_id": "d1", "type": "intrusion"})
    assert r["outcome"] == "dispatched" and r["intercept"] == "dev-door-1" and r["zone"] == "东区"


async def test_删了设备_回调验签不过_拦截点也没了(设备):
    from d1max_site.incidents import IncidentAuthError
    t = 设备
    sec = t.book.put("door-1", {"kind": "contact", "map_id": MAP[0], "map_version": MAP[1],
                                "x": 3.0, "y": 1.0}, by="alice")["secret"]
    t.book.remove("door-1")
    assert t.book.list() == [] and t.desk.intercept("dev-door-1") is None
    with pytest.raises(IncidentAuthError):
        await _设备报(t, "door-1", sec, {"event_id": "d1", "type": "intrusion"})


async def test_老接口_按防区名_先找圈好的防区_找不到按老映射(设备):
    from test_site_incidents import _报
    t = 设备
    await t.run(12)
    t.desk.zone_point = lambda z: ({"name": f"zone:{z}", "map_id": MAP[0], "map_version": MAP[1],
                                    "x": 4.0, "y": 0.0, "yaw": 0.0, "reach": ""}
                                   if z == "东区" else None)
    r = await _报(t, event_id="e1", zone="东区")
    assert r["outcome"] == "dispatched" and r["intercept"] == "zone:东区"
    r = await _报(t, event_id="e2", zone="front-yard")
    assert r["intercept"] == "gate", "没圈的防区按老的拦截点映射"


async def test_放到图上的摄像头_ONVIF报来的按它的位置派_没放的照老映射(设备):
    from d1max_site.cctv import Camera, OnvifEvent, add_camera, incident_body, set_placement
    t = 设备
    await t.run(12)
    add_camera(t.db, Camera(name="cam-9", onvif_url="http://x", username="u", password="p",
                            zone="front-yard"), now_ms=0)
    ev = OnvifEvent(topic="tns1:RuleEngine/FieldDetector", operation="", source={}, data={})
    body = incident_body(Camera(name="cam-9", onvif_url="", username="", password="",
                                zone="front-yard"), ev, t.clock())
    r = await t.send(t.desk.handle("cctv-cam-9", body))
    assert r["intercept"] == "gate", "没放到图上:老映射"
    set_placement(t.db, "cam-9", {"map_id": MAP[0], "map_version": MAP[1], "x": 0.0, "y": 0.0,
                                  "yaw": 0.0, "fov_deg": 90.0})
    body = {**body, "event_id": "cam-9-2"}
    r = await t.send(t.desk.handle("cctv-cam-9", body))
    assert r["intercept"] == "cam:cam-9" and r["zone"] == "东区", r
    p = t.desk.intercept("cam:cam-9")
    assert (p["x"], p["y"]) == (5.0, 0.0), "视线中线一半(默认看 10 m)"



async def test_外审_重新圈防区以后_设备和摄像头按新防区判(设备):
    from d1max_site.cctv import Camera, add_camera, set_placement
    t = 设备
    t.book.put("door-1", {"kind": "contact", "map_id": MAP[0], "map_version": MAP[1],
                          "x": 3.0, "y": 1.0}, by="alice")
    add_camera(t.db, Camera(name="cam-9", onvif_url="http://x", username="u", password="p",
                            zone="front-yard"), now_ms=0)
    set_placement(t.db, "cam-9", {"map_id": MAP[0], "map_version": MAP[1], "x": 0.0, "y": 0.0,
                                  "yaw": 0.0, "fov_deg": 90.0})
    assert t.book.get("door-1")["zone"] == "东区"
    assert t.book.by_source("cctv-cam-9")["zone"] == "东区"
    t.areas.remove(*MAP, "东区")
    t.areas.set(*MAP, "西区", EAST, by="alice")             # 同一块地方改叫西区
    assert t.book.get("door-1")["zone"] == "西区"
    assert t.book.by_source("dev-door-1")["zone"] == "西区"
    assert t.book.by_source("cctv-cam-9")["zone"] == "西区", "摄像头缓存不带旧防区"
    t.areas.remove(*MAP, "西区")
    assert t.book.get("door-1")["zone"] == "", "不在任何防区:按全站"


def test_外审r2_10_摄像头移出所有防区_按全站算_不退回旧防区(设备):
    from d1max_site.cctv import Camera, add_camera, set_placement
    t = 设备
    add_camera(t.db, Camera(name="cam-9", onvif_url="http://x", username="u", password="p",
                            zone="东区"), now_ms=0)
    set_placement(t.db, "cam-9", {"map_id": MAP[0], "map_version": MAP[1], "x": 0.0, "y": 0.0,
                                  "yaw": 0.0, "fov_deg": 90.0})
    assert t.book.by_source("cctv-cam-9")["zone"] == "东区"
    t.areas.remove(*MAP, "东区")
    assert t.book.by_source("cctv-cam-9")["zone"] == ""


async def test_外审r3_5_摄像头移出所有防区_报来的事件也按全站_不用事件里带的旧防区(设备):
    from d1max_site.cctv import Camera, OnvifEvent, add_camera, incident_body, set_placement
    from d1max_site.modes import ArmingDesk
    t = 设备
    await t.run(12)
    cam = Camera(name="cam-9", onvif_url="http://x", username="u", password="p", zone="东区")
    add_camera(t.db, cam, now_ms=0)
    set_placement(t.db, "cam-9", {"map_id": MAP[0], "map_version": MAP[1], "x": 0.0, "y": 0.0,
                                  "yaw": 0.0, "fov_deg": 90.0})
    t.areas.remove(*MAP, "东区")
    assert t.book.by_source("cctv-cam-9")["zone"] == ""
    t.desk.arming = ArmingDesk(t.db, now_ms=t.clock)
    t.desk.arming.set_zone_home("东区", False)
    t.desk.arming.set_mode("home", by="olga")                    # 东区撤着,别的(全站)布防
    ev = OnvifEvent(topic="tns1:RuleEngine/FieldDetector", operation="", source={}, data={})
    r = await t.send(t.desk.handle("cctv-cam-9", incident_body(cam, ev, t.clock())))
    assert r["zone"] == "cctv-cam-9" and r["outcome"] == "dispatched", r
