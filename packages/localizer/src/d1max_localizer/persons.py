"""人员检测节点(W24,决策 39):狗上相机看到人 → 雷达量距离 → 经本机人员桥报给代理。

**只报事实**(这一帧几个人、在哪个方向、多远),判「有没有人、走没走」在代理。

- **取图**:订厂商的压缩图话题(``/front_camera/image_compressed``,JPEG,约 10 Hz;2026-09 勘察量过),
  不解 H.264。每个相机最多 ``--fps`` 帧/秒送去检测(默认 2:够判有没有人,不抢 Orin)。
- **检测**:可换的检测器(:class:`Detector`)。真狗用 ONNX 模型(YOLO 系、COCO 的 person 类,
  ``onnxruntime`` + OpenCV 解图)—— 模型文件、推理环境都是真机项;没装上就报 ``check: no_model``,
  代理据此不判「人走了」。开发机上用假检测器跑通整条链。
- **方向**:框的水平中点 + 相机水平视场角(厂商:111°)→ 狗身系方向(朝前 0°、朝左为正)。不要相机内参:
  广角镜头按等距模型(``--lens equidistant``,默认)或针孔模型算,误差在几度,够挑雷达点。
  后相机朝后:方向加 180°。相机装在雷达正上方 75 mm、前 8 mm(厂商安装尺寸),当成同一点。
- **距离**:前雷达这一帧(换到狗身系)里,方向在框左右边之间、高度在人身上那一段(相对雷达
  ``--z-min``–``--z-max``)、离狗 ``--max-range`` 以内的点,取**最近的一簇**(从近往远,头一个
  0.5 m 以内凑够 5 个点的地方):几个散点不算,人后面的墙、树点再多也不把距离拉远。
  凑不够就是 ``null``(那个方向雷达没打到人)。
- **截图**:一帧里有人、离上一张超过 ``--snapshot-every`` 秒,就把这一帧 JPEG 存进截图目录,
  文件名随报文报给代理(代理存成归档传站点,存完删掉)。
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

HFOV_DEG = 111.0
Z_MIN, Z_MAX = -0.25, 1.6
MAX_RANGE_M = 25.0
MIN_POINTS = 5
CLUSTER_M = 0.5


# ------------------------------------------------------------ 几何

def bearing_deg(cx: float, width: float, *, hfov_deg: float = HFOV_DEG,
                lens: str = "equidistant", camera: str = "front") -> float:
    """像素列 → 狗身系方向(度,朝前 0°、朝左为正,−180–180)。图像往右 = 往狗的右边 = 负。"""
    if width <= 0:
        raise ValueError("图像宽度要是正数")
    u = (cx - width / 2) / (width / 2)                   # −1 … 1,右为正
    half = math.radians(hfov_deg) / 2
    if lens == "pinhole":
        a = math.atan(u * math.tan(half))
    elif lens == "equidistant":
        a = u * half
    else:
        raise ValueError(f"镜头模型只认 pinhole / equidistant:{lens!r}")
    deg = -math.degrees(a)
    if camera == "back":
        deg += 180.0
    return (deg + 180.0) % 360.0 - 180.0


def range_m(pts_base: Any, lo_deg: float, hi_deg: float, *, z_min: float = Z_MIN,
            z_max: float = Z_MAX, max_range: float = MAX_RANGE_M) -> float | None:
    """狗身系点云里方向在 ``[lo_deg, hi_deg]``(可以跨 ±180)、高度在人身上、``max_range`` 以内的点,
    取最近的一簇(:data:`CLUSTER_M` 以内凑够 :data:`MIN_POINTS` 个)的中位距离。凑不够回 ``None``。"""
    import numpy as np
    p = np.asarray(pts_base, dtype=float)
    if p.size == 0:
        return None
    d = np.hypot(p[:, 0], p[:, 1])
    ang = np.degrees(np.arctan2(p[:, 1], p[:, 0]))
    span = (hi_deg - lo_deg) % 360.0
    rel = (ang - lo_deg) % 360.0
    m = (rel <= span) & (p[:, 2] >= z_min) & (p[:, 2] <= z_max) & (d > 0.3) & (d <= max_range)
    ds = np.sort(d[m])
    if ds.size < MIN_POINTS:
        return None
    span_ok = ds[MIN_POINTS - 1:] - ds[:ds.size - MIN_POINTS + 1] <= CLUSTER_M
    hits = np.flatnonzero(span_ok)
    if hits.size == 0:
        return None
    i = int(hits[0])
    return float(np.median(ds[i:i + MIN_POINTS]))


# ------------------------------------------------------------ 检测器

@dataclass(frozen=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float


class Detector(Protocol):
    def detect(self, jpeg: bytes) -> tuple[list[Box], int, int]:
        """→ (人的框, 图宽, 图高)。"""
        ...


class DetectorUnavailable(RuntimeError):
    """检测模型、推理环境没装上(报 ``no_model``)。"""


def yolo_people(out: Any, *, score: float = 0.5, iou: float = 0.45, scale: float = 1.0,
                pad: tuple[float, float] = (0.0, 0.0)) -> list[Box]:
    """YOLOv8 式输出 ``(1, 4 + 类数, N)``(cx, cy, w, h, 各类分数)→ person(第 0 类)的框,
    按 letterbox 的缩放、补边换回原图坐标,做 NMS。"""
    import numpy as np
    a = np.asarray(out, dtype=float)
    if a.ndim == 3:
        a = a[0]
    if a.shape[0] < 5:
        raise ValueError(f"检测输出形状不对:{a.shape}")
    s = a[4]
    keep = s >= score
    if not keep.any():
        return []
    cx, cy, w, h = (a[i][keep] for i in range(4))
    s = s[keep]
    x1 = (cx - w / 2 - pad[0]) / scale
    y1 = (cy - h / 2 - pad[1]) / scale
    x2 = (cx + w / 2 - pad[0]) / scale
    y2 = (cy + h / 2 - pad[1]) / scale
    order = list(np.argsort(-s))
    boxes: list[Box] = []
    while order:
        i = order.pop(0)
        boxes.append(Box(float(x1[i]), float(y1[i]), float(x2[i]), float(y2[i]), float(s[i])))
        rest = []
        for j in order:
            ix = max(0.0, min(x2[i], x2[j]) - max(x1[i], x1[j]))
            iy = max(0.0, min(y2[i], y2[j]) - max(y1[i], y1[j]))
            inter = ix * iy
            union = (x2[i] - x1[i]) * (y2[i] - y1[i]) + (x2[j] - x1[j]) * (y2[j] - y1[j]) - inter
            if union <= 0 or inter / union < iou:
                rest.append(j)
        order = rest
    return boxes


class OnnxDetector:
    """ONNX 模型(YOLO 系,输入 ``(1, 3, size, size)``、RGB、0–1)。要 ``onnxruntime`` 与 OpenCV:
    **都是真机项**,开发机上没装。装不上、模型读不了抛 :class:`DetectorUnavailable`。"""

    def __init__(self, model: Path, *, size: int = 640, score: float = 0.5,
                 providers: Sequence[str] = ("TensorrtExecutionProvider", "CUDAExecutionProvider",
                                             "CPUExecutionProvider")) -> None:
        try:
            import cv2  # noqa: F401
            import onnxruntime as ort
        except ImportError as exc:
            raise DetectorUnavailable(f"推理环境没装:{exc}") from exc
        if not Path(model).is_file():
            raise DetectorUnavailable(f"没有模型文件 {model}")
        try:
            have = set(ort.get_available_providers())
            self.sess = ort.InferenceSession(str(model),
                                             providers=[p for p in providers if p in have])
        except Exception as exc:                         # 模型坏了
            raise DetectorUnavailable(f"模型读不了:{exc}") from exc
        self.input = self.sess.get_inputs()[0].name
        self.size, self.score = size, score

    def detect(self, jpeg: bytes) -> tuple[list[Box], int, int]:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("JPEG 解不开")
        h, w = img.shape[:2]
        scale = self.size / max(h, w)
        nh, nw = int(round(h * scale)), int(round(w * scale))
        canvas = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
        px, py = (self.size - nw) // 2, (self.size - nh) // 2
        canvas[py:py + nh, px:px + nw] = cv2.resize(img, (nw, nh))
        x = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self.sess.run(None, {self.input: x})[0]
        return yolo_people(out, score=self.score, scale=scale, pad=(px, py)), w, h


# ------------------------------------------------------------ 一帧

@dataclass
class Settings:
    hfov_deg: float = HFOV_DEG
    lens: str = "equidistant"
    z_min: float = Z_MIN
    z_max: float = Z_MAX
    max_range: float = MAX_RANGE_M
    snapshot_every_s: float = 10.0


class PersonNode:
    """把一帧图 + 最近的雷达点云 → 人员桥的 ``persons`` 报文。不碰 ROS,测试直接喂。"""

    def __init__(self, detector: Detector | None, *, snapshot_dir: Path | None = None,
                 settings: Settings | None = None, unavailable: str = "",
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_ns: Callable[[], int] = time.time_ns) -> None:
        self.detector = detector
        self.unavailable = unavailable
        self.cfg = settings or Settings()
        self.snapshot_dir = snapshot_dir
        self._mono = monotonic
        self._wall_ns = wall_ns
        self.seq = 0
        self._snap_at = -1e18
        #: 最近一帧前雷达点云(狗身系)。
        self.cloud: Any = None

    def on_cloud(self, pts_base: Any) -> None:
        self.cloud = pts_base

    def on_image(self, camera: str, jpeg: bytes, stamp_ns: int) -> dict[str, Any]:
        self.seq += 1
        base = {"t": "persons", "seq": self.seq, "stamp_ns": max(0, int(stamp_ns)),
                "camera": camera, "people": [], "snapshot": "", "reason": ""}
        if self.detector is None:
            return base | {"check": "no_model", "reason": self.unavailable[:200] or "没检测器"}
        try:
            boxes, w, _h = self.detector.detect(jpeg)
        except Exception as exc:                         # noqa: BLE001 - 一帧坏了不停
            return base | {"check": "no_camera", "reason": f"这一帧处理不了:{exc}"[:200]}
        people = []
        for b in boxes[:16]:
            c = bearing_deg((b.x1 + b.x2) / 2, w, hfov_deg=self.cfg.hfov_deg, lens=self.cfg.lens,
                            camera=camera)
            left = bearing_deg(b.x1, w, hfov_deg=self.cfg.hfov_deg, lens=self.cfg.lens,
                               camera=camera)
            right = bearing_deg(b.x2, w, hfov_deg=self.cfg.hfov_deg, lens=self.cfg.lens,
                                camera=camera)
            r = None
            if self.cloud is not None:
                r = range_m(self.cloud, right, left, z_min=self.cfg.z_min, z_max=self.cfg.z_max,
                            max_range=self.cfg.max_range)
            people.append({"bearing_deg": round(c, 1),
                           "range_m": None if r is None else round(r, 2),
                           "score": round(min(1.0, max(0.0, b.score)), 3)})
        snap = ""
        if people and self.snapshot_dir is not None \
                and self._mono() - self._snap_at >= self.cfg.snapshot_every_s:
            snap = self._save(camera, jpeg)
        return base | {"check": "ok", "people": people, "snapshot": snap}

    def _save(self, camera: str, jpeg: bytes) -> str:
        name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(self._wall_ns() / 1e9))}" \
               f"-{camera}-{self.seq}.jpg"
        try:
            assert self.snapshot_dir is not None
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.snapshot_dir / f".{name}.tmp"
            tmp.write_bytes(jpeg)
            os.replace(tmp, self.snapshot_dir / name)
        except OSError:
            log.exception("截图存不下")
            return ""
        self._snap_at = self._mono()
        return name


# ------------------------------------------------------------ ROS 节点

def main(argv: Sequence[str] | None = None) -> int:
    """ROS 节点(系统 Python;``d1max-persons.service``)。"""
    ap = argparse.ArgumentParser(prog="d1max-persons")
    ap.add_argument("--model", type=Path, default=Path("/etc/d1max/person.onnx"),
                    help="ONNX 检测模型(YOLO 系,COCO person 类);没有就报 no_model")
    ap.add_argument("--socket", type=Path, default=Path("/var/lib/d1max/agent/persons.sock"))
    ap.add_argument("--snapshots", type=Path,
                    default=Path("/var/lib/d1max/agent/person-snapshots"))
    ap.add_argument("--front-topic", default="/front_camera/image_compressed")
    ap.add_argument("--back-topic", default="/rear_camera/image_compressed",
                    help="后相机(空 = 不用)")
    ap.add_argument("--lidar-topic", default="/front_lidar")
    ap.add_argument("--frames", type=Path, default=None,
                    help="前雷达外参(frames.json);不给就跟着代理正在用的图(--maps-dir)走")
    ap.add_argument("--maps-dir", type=Path, default=Path("/var/lib/d1max/agent/maps"))
    ap.add_argument("--fps", type=float, default=2.0, help="每个相机每秒最多检测几帧")
    ap.add_argument("--hfov", type=float, default=HFOV_DEG)
    ap.add_argument("--lens", choices=("equidistant", "pinhole"), default="equidistant")
    ap.add_argument("--z-min", type=float, default=Z_MIN)
    ap.add_argument("--z-max", type=float, default=Z_MAX)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import CompressedImage, PointCloud2

    from d1max_contract.persbridge import PROTO
    from d1max_localizer.build import cloud_xyz
    from d1max_localizer.frames import Frames
    from d1max_localizer.obstacles import LineClient, Mount, active_frames

    detector: Detector | None = None
    why = ""
    try:
        detector = OnnxDetector(a.model)
    except DetectorUnavailable as exc:
        why = str(exc)
        log.error("检测器起不来(报 no_model):%s", why)
    node_logic = PersonNode(detector, snapshot_dir=a.snapshots, unavailable=why,
                            settings=Settings(hfov_deg=a.hfov, lens=a.lens, z_min=a.z_min,
                                              z_max=a.z_max))

    def mount() -> Mount | None:
        try:
            if a.frames is not None:
                return Mount.from_frames(Frames.load(a.frames))
            got = active_frames(a.maps_dir)
            return Mount.from_frames(Frames.load(got[1])) if got else None
        except Exception as exc:                         # noqa: BLE001
            log.error("外参读不了:%s(先不测距)", exc)
            return None

    mnt = mount()
    client = LineClient(lambda: _unix(a.socket), {"t": "hello", "proto": PROTO,
                                                  "name": "d1max-persons"})
    rclpy.init(args=None)
    node = rclpy.create_node("d1max_persons")
    qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                     reliability=ReliabilityPolicy.BEST_EFFORT)
    last: dict[str, float] = {}

    def on_lidar(raw: bytes) -> None:
        if mnt is None:
            return
        try:
            node_logic.on_cloud(mnt.to_base(cloud_xyz(deserialize_message(raw, PointCloud2))))
        except Exception:
            log.exception("雷达这一帧处理不了")

    def on_image(camera: str) -> Callable[[bytes], None]:
        def cb(raw: bytes) -> None:
            now = time.monotonic()
            if now - last.get(camera, -1e9) < 1.0 / max(0.1, a.fps):
                return
            last[camera] = now
            try:
                msg = deserialize_message(raw, CompressedImage)
                st = msg.header.stamp
                client.send(node_logic.on_image(camera, bytes(msg.data),
                                                st.sec * 1_000_000_000 + st.nanosec))
            except Exception:
                log.exception("%s 这一帧处理不了", camera)
        return cb

    node.create_subscription(PointCloud2, a.lidar_topic, on_lidar, qos, raw=True)
    node.create_subscription(CompressedImage, a.front_topic, on_image("front"), qos, raw=True)
    if a.back_topic:
        node.create_subscription(CompressedImage, a.back_topic, on_image("back"), qos, raw=True)
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    log.info("人员检测:订 %s%s + %s → %s", a.front_topic,
             f"、{a.back_topic}" if a.back_topic else "", a.lidar_topic, a.socket)
    hb = time.monotonic()
    try:
        while rclpy.ok():
            ex.spin_once(timeout_sec=0.2)
            client.poll()
            if time.monotonic() - hb >= 1.0:
                hb = time.monotonic()
                client.send({"t": "hb", "seq": max(1, node_logic.seq)})
    finally:
        ex.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


def _unix(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(os.fspath(path))
    return s


if __name__ == "__main__":
    raise SystemExit(main())
