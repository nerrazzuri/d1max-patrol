"""人员检测节点(W24)的纯逻辑:方向(等距、针孔、后相机)、雷达测距(背景点多也取近处、跨 ±180°)、
YOLO 输出 → 人框(letterbox 换回原图、NMS)、一帧 → 报文(没检测器、坏帧、截图限频);报文过契约校验。"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from d1max_localizer.persons import Box, PersonNode, Settings, bearing_deg, range_m, yolo_people

from d1max_contract.persbridge import Persons, parse


def test_方向_正中是0_右边是负_左边是正_边上是半个视场():
    assert bearing_deg(960, 1920) == 0.0
    assert bearing_deg(1920, 1920) == pytest.approx(-55.5)
    assert bearing_deg(0, 1920) == pytest.approx(55.5)
    assert bearing_deg(1920, 1920, lens="pinhole") == pytest.approx(-55.5)
    # 边上都是半个视场时,半路上针孔算出来的角度比等距大(广角镜头两种模型能差 8° 上下)
    assert bearing_deg(1440, 1920) == pytest.approx(-27.75)
    assert bearing_deg(1440, 1920, lens="pinhole") == pytest.approx(-36.04, abs=0.01)


def test_后相机朝后_加180():
    assert bearing_deg(960, 1920, camera="back") == pytest.approx(-180.0)
    assert bearing_deg(0, 1920, camera="back") == pytest.approx(-124.5), "后相机画面左边 = 狗的右后"
    assert bearing_deg(1920, 1920, camera="back") == pytest.approx(124.5)
    with pytest.raises(ValueError):
        bearing_deg(1, 0)
    with pytest.raises(ValueError):
        bearing_deg(1, 10, lens="fisheye")


def _人(dist, bearing, n=40, z=(0.0, 1.2)):
    a = math.radians(bearing)
    zs = np.linspace(*z, n)
    return np.column_stack([np.full(n, dist * math.cos(a)), np.full(n, dist * math.sin(a)), zs])


def test_测距_几个散点不算():
    stray = np.array([[2.0, 0.2, 0.5], [2.6, 0.2, 0.5], [3.3, 0.2, 0.5]])   # 隔得远的三个散点
    assert range_m(np.vstack([stray, _人(7.0, 5.0)]), 0.0, 10.0) == pytest.approx(7.0, abs=0.01)


def test_测距_取近处_背景墙不把它拉远_高度不对的不算_点少就没有():
    cloud = np.vstack([_人(4.0, 10.0), _人(12.0, 10.0, n=400)])        # 人前面 4 m,后面一堵墙
    r = range_m(cloud, 5.0, 15.0)
    assert r == pytest.approx(4.0, abs=0.01)
    assert range_m(cloud, 30.0, 40.0) is None, "那个方向没点"
    ground = _人(4.0, 10.0, z=(-1.0, -0.6))
    assert range_m(ground, 5.0, 15.0) is None, "地面上的点不算人"
    assert range_m(_人(4.0, 10.0, n=3), 5.0, 15.0) is None
    assert range_m(np.zeros((0, 3)), 0, 10) is None
    assert range_m(_人(30.0, 10.0), 5.0, 15.0) is None, "超出量程"


def test_测距_扇区跨正负180():
    cloud = _人(3.0, 178.0)
    assert range_m(cloud, 170.0, -170.0) == pytest.approx(3.0, abs=0.01)
    assert range_m(cloud, -170.0, 170.0) is None, "反过来是另外那 340°的扇区(不含 178°)"


def test_YOLO输出_只要人_分数过线_letterbox换回原图_NMS去重():
    # (1, 4+2 类, 4 个候选):第 0 类是人
    cand = np.array([
        [320, 330, 100, 500],   # cx
        [320, 322, 300, 300],   # cy
        [100, 100, 50, 50],     # w
        [200, 200, 50, 50],     # h
        [0.9, 0.8, 0.3, 0.7],   # 人
        [0.1, 0.1, 0.9, 0.1],   # 别的类
    ], dtype=float)[None]
    boxes = yolo_people(cand, score=0.5, scale=0.5, pad=(0.0, 80.0))
    assert len(boxes) == 2, "前两个重叠去掉一个;第三个分数不够"
    b = boxes[0]
    assert (b.x1, b.y1, b.x2, b.y2) == pytest.approx((540.0, 280.0, 740.0, 680.0))
    assert b.score == pytest.approx(0.9)
    assert yolo_people(cand, score=0.95) == []
    with pytest.raises(ValueError):
        yolo_people(np.zeros((1, 3, 2)))


class 假检测器:
    def __init__(self, boxes=(), fail=False):
        self.boxes, self.fail = list(boxes), fail

    def detect(self, jpeg):
        if self.fail:
            raise ValueError("JPEG 解不开")
        return self.boxes, 1920, 1080


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _过契约(d):
    m = parse(json.dumps(d).encode())
    assert isinstance(m, Persons)
    return m


def test_一帧_有人带方向和距离_截图限频_都过契约(tmp_path):
    c = 钟()
    det = 假检测器([Box(860, 300, 1060, 900, 0.88)])     # 正中、宽 200 像素
    n = PersonNode(det, snapshot_dir=tmp_path, monotonic=c, wall_ns=lambda: 1_791_000_000 * 10**9,
                   settings=Settings(snapshot_every_s=10))
    n.on_cloud(np.vstack([_人(6.0, 1.0), _人(15.0, 0.0, n=300)]))
    m = _过契约(n.on_image("front", b"\xff\xd8x\xff\xd9", 5))
    assert m.check == "ok" and m.camera == "front" and len(m.people) == 1
    p = m.people[0]
    assert p.bearing_deg == 0.0 and p.range_m == pytest.approx(6.0, abs=0.05) and p.score == 0.88
    assert m.snapshot and (tmp_path / m.snapshot).read_bytes() == b"\xff\xd8x\xff\xd9"
    c.t += 5
    assert _过契约(n.on_image("front", b"x", 6)).snapshot == "", "10 秒内不再存"
    c.t += 6
    assert _过契约(n.on_image("front", b"x", 7)).snapshot != ""
    assert not list(tmp_path.glob(".*.tmp")), "不留临时文件"


def test_一帧_没人不存截图_没雷达距离是空():
    n = PersonNode(假检测器([]))
    m = _过契约(n.on_image("back", b"x", 1))
    assert m.check == "ok" and m.people == () and m.snapshot == ""
    n2 = PersonNode(假检测器([Box(0, 0, 100, 100, 0.6)]))
    assert _过契约(n2.on_image("front", b"x", 1)).people[0].range_m is None


def test_没检测器报no_model_坏帧报no_camera_序号递增():
    n = PersonNode(None, unavailable="推理环境没装:No module named 'onnxruntime'")
    m = _过契约(n.on_image("front", b"x", 1))
    assert m.check == "no_model" and "onnxruntime" in m.reason and m.people == ()
    n2 = PersonNode(假检测器(fail=True))
    m2 = _过契约(n2.on_image("front", b"x", 1))
    assert m2.check == "no_camera" and "解不开" in m2.reason
    assert _过契约(n2.on_image("front", b"x", 2)).seq == 2
