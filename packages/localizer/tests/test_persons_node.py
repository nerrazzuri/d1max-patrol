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


# ------------------------------------------------------------ W24 外审


def test_外审2_一小时前的点云不拿来量_时间对不上也不量():
    c = 钟()
    n = PersonNode(假检测器([Box(860, 300, 1060, 900, 0.9)]), monotonic=c)
    n.on_cloud(_人(3.0, 0.0), stamp_ns=1_000_000_000)
    c.t += 3600                                           # 雷达停了一小时
    m = _过契约(n.on_image("front", b"x", 3601 * 10**9))
    assert m.people[0].range_m is None, "旧点云不用:距离未知"
    n.on_cloud(_人(3.0, 0.0), stamp_ns=10 * 10**9)
    m = _过契约(n.on_image("front", b"x", 12 * 10**9))
    assert m.people[0].range_m is None, "跟画面差 2 秒:不是同一时刻"
    m = _过契约(n.on_image("front", b"x", 10 * 10**9 + 200_000_000))
    assert m.people[0].range_m == pytest.approx(3.0, abs=0.05)


def test_外审_后相机看到的人用后雷达量():
    c = 钟()
    n = PersonNode(假检测器([Box(860, 300, 1060, 900, 0.9)]), monotonic=c)
    n.on_cloud(_人(9.0, 0.0), source="front")              # 前面 9 m 有东西
    n.on_cloud(_人(4.0, 180.0), source="rear")             # 后面 4 m 有人
    m = _过契约(n.on_image("back", b"x", 0))
    assert m.people[0].bearing_deg == -180.0 or m.people[0].bearing_deg == 180.0
    assert m.people[0].range_m == pytest.approx(4.0, abs=0.05)
    m = _过契约(n.on_image("front", b"x", 0))
    assert m.people[0].range_m == pytest.approx(9.0, abs=0.05)


def test_外审5_截图目录有上限_多了删最旧的(tmp_path):
    import os

    from d1max_localizer.persons import MAX_SNAPSHOTS
    c = 钟()
    n = PersonNode(假检测器([Box(0, 0, 10, 10, 0.9)]), snapshot_dir=tmp_path, monotonic=c,
                   settings=Settings(snapshot_every_s=1))
    for i in range(MAX_SNAPSHOTS + 5):
        c.t += 2
        name = _过契约(n.on_image("front", b"x", i)).snapshot
        os.utime(tmp_path / name, (1000 + i, 1000 + i))
    left = sorted(tmp_path.glob("*.jpg"))
    assert len(left) == MAX_SNAPSHOTS


def test_外审2_点云没带时间戳_也按收到多久判旧():
    c = 钟()
    n = PersonNode(假检测器([Box(860, 300, 1060, 900, 0.9)]), monotonic=c)
    n.on_cloud(_人(3.0, 0.0))                              # 不带时间戳
    c.t += 3600
    assert _过契约(n.on_image("front", b"x", 0)).people[0].range_m is None


# ------------------------------------------------------------ 推理后端(2026-10-08 C40221)

class _假会话:
    def __init__(self, model, sess_options=None, providers=()):
        self.model, self.opts, self.providers_in = model, sess_options, list(providers)

    def get_providers(self):
        return [p[0] if isinstance(p, tuple) else p for p in self.providers_in]

    def get_inputs(self):
        return [type("I", (), {"name": "images"})()]


def _假推理库(monkeypatch, 有的):
    import sys
    import types
    ort = types.ModuleType("onnxruntime")
    ort.get_available_providers = lambda: list(有的)
    ort.SessionOptions = lambda: types.SimpleNamespace(intra_op_num_threads=0,
                                                       inter_op_num_threads=0)
    ort.InferenceSession = _假会话
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))


def test_推理后端_缺省CUDA不用TensorRT_装了也不用(monkeypatch, tmp_path):
    """TensorRT 第一次建引擎要好几分钟(C40221 上超过 5 分钟),每秒几帧的用量下不值:缺省 CUDA。"""
    from d1max_localizer.persons import OnnxDetector
    _假推理库(monkeypatch, ["TensorrtExecutionProvider", "CUDAExecutionProvider",
                        "CPUExecutionProvider"])
    模型 = tmp_path / "person.onnx"
    模型.write_bytes(b"x")
    d = OnnxDetector(模型, trt_cache=tmp_path / "trt")
    assert d.sess.providers_in == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert d.backend == "CUDAExecutionProvider"
    assert not (tmp_path / "trt").exists()


def test_推理后端_开了trt才先用TensorRT_带引擎缓存和半精度_CPU限线程(monkeypatch, tmp_path):
    """CPU 版 onnxruntime 不限线程会占满所有核(C40221 上约 4.5 核);TensorRT 第一次建引擎要几分钟,
    不缓存的话每次重启都要再等。"""
    from d1max_localizer.persons import OnnxDetector
    _假推理库(monkeypatch, ["TensorrtExecutionProvider", "CUDAExecutionProvider",
                        "CPUExecutionProvider"])
    模型 = tmp_path / "person.onnx"
    模型.write_bytes(b"x")
    d = OnnxDetector(模型, trt=True, trt_cache=tmp_path / "trt")
    trt, cuda, cpu = d.sess.providers_in
    assert trt[0] == "TensorrtExecutionProvider"
    assert trt[1] == {"trt_engine_cache_enable": True,
                      "trt_engine_cache_path": str(tmp_path / "trt"), "trt_fp16_enable": True}
    assert (tmp_path / "trt").is_dir()
    assert (cuda, cpu) == ("CUDAExecutionProvider", "CPUExecutionProvider")
    assert d.backend == "TensorrtExecutionProvider"
    assert (d.sess.opts.intra_op_num_threads, d.sess.opts.inter_op_num_threads) == (2, 1)


def test_推理后端_只有CPU就只给CPU_线程数照给的(monkeypatch, tmp_path):
    from d1max_localizer.persons import OnnxDetector
    _假推理库(monkeypatch, ["AzureExecutionProvider", "CPUExecutionProvider"])
    模型 = tmp_path / "person.onnx"
    模型.write_bytes(b"x")
    d = OnnxDetector(模型, threads=3, trt=True, trt_cache=tmp_path / "trt")
    assert d.sess.providers_in == ["CPUExecutionProvider"] and d.backend == "CPUExecutionProvider"
    assert d.sess.opts.intra_op_num_threads == 3
    assert not (tmp_path / "trt").exists(), "没有 TensorRT 就不建缓存目录"


def test_测距只用框中间_框边上更近的遮挡物不算():
    """2026-10-08 C40221:人坐在人形机器人斜后方(约 3 m),框右边缘跟站在前面的机器人(约 2 m)重叠
    一条,整框量到的是机器人。只用框中间一半宽度量,量到的是人。"""
    det = 假检测器([Box(860, 300, 1060, 900, 0.7)])     # 正中、宽 200 像素,左右边约 ±5.8°
    n = PersonNode(det)
    n.on_cloud(np.vstack([_人(3.0, 0.0), _人(2.0, -5.0)]))     # 人在正中 3 m,遮挡物在右边缘 2 m
    人 = _过契约(n.on_image("front", b"x", 1)).people[0]
    assert 人.range_m == pytest.approx(3.0, abs=0.05)
    整框 = PersonNode(det, settings=Settings(range_span=1.0))
    整框.on_cloud(np.vstack([_人(3.0, 0.0), _人(2.0, -5.0)]))
    被挡 = _过契约(整框.on_image("front", b"x", 1)).people[0]
    assert 被挡.range_m == pytest.approx(2.0, abs=0.05)

