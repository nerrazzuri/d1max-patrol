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
- **距离**:前后雷达(各自换到狗身系)**新鲜的**点云里(W24 外审:收到不超过 1 秒、
  跟这帧画面的时间差不超过 0.5 秒;对不上就报距离未知,不拿旧点云量),
  方向在框左右边之间、高度在人身上那一段(相对雷达 ``--z-min``–``--z-max``)、
  离狗 ``--max-range`` 以内的点,取**最近的一簇**(从近往远,头一个 0.5 m 以内凑够 5 个点的
  地方):几个散点不算,人后面的墙、树点再多也不把距离拉远。
  凑不够就是 ``null``(那个方向雷达没打到人)。
- **截图**:一帧里有人、离上一张超过 ``--snapshot-every`` 秒,就把这一帧 JPEG 存进截图目录(最多
  :data:`MAX_SNAPSHOTS` 张,多了删最旧的:代理那头存不下时不许一直攒),文件名随报文报给代理
  (代理存成归档传站点,存完删掉)。
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
#: 点云多旧还能用(收到时刻,单调钟)、跟画面的时间差多大还算同一时刻(各自的话题时间戳)。
CLOUD_MAX_AGE_S = 1.0
CLOUD_MAX_SKEW_NS = 500_000_000
MAX_SNAPSHOTS = 30
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
        raise ValueError(f"检测输出形状错误:{a.shape}")
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


#: CPU 推理的线程上限。onnxruntime 缺省把核占满:C40221 上 CPU 版跑两路相机吃到约 4.5 核,
#: 跟 MOLA、感知抢 CPU(2026-10-08)。GPU 后端不受它影响。
THREADS = 2
#: TensorRT 引擎缓存(``--trt`` 才用)。第一次建引擎要好几分钟(C40221 上超过 5 分钟),不缓存的话
#: 每次重启检测节点都要再等一遍。
TRT_CACHE = Path("/var/lib/d1max/agent/trt-cache")


class OnnxDetector:
    """ONNX 模型(YOLO 系,输入 ``(1, 3, size, size)``、RGB、0–1)。要 ``onnxruntime`` 与 OpenCV:
    **都是真机项**,开发机上没装。装不上、模型读不了抛 :class:`DetectorUnavailable`。

    后端缺省按 CUDA → CPU 的顺序挑装了的(Orin 上要 Jetson 版 ``onnxruntime-gpu``;C40221 实测
    CUDA 单帧 26 ms、CPU 330 ms)。``trt=True`` 才把 TensorRT 放到最前:更快,但第一次建引擎要好几
    分钟、换模型或升级 TensorRT 要重建 —— 每秒几帧的用量下不值(2026-10-08 用户定)。实际用上的记在
    :attr:`backend`。"""

    def __init__(self, model: Path, *, size: int = 640, score: float = 0.5,
                 providers: Sequence[str] = ("CUDAExecutionProvider", "CPUExecutionProvider"),
                 threads: int = THREADS, trt: bool = False,
                 trt_cache: Path | None = TRT_CACHE) -> None:
        try:
            import cv2
            import onnxruntime as ort
        except ImportError as exc:
            raise DetectorUnavailable(f"推理环境未安装:{exc}") from exc
        if not Path(model).is_file():
            raise DetectorUnavailable(f"没有模型文件 {model}")
        # OpenCV 缺省开一个跟核数一样大的线程池:C40221 上 8 个线程各吃 35–48%,把定位器(MOLA)
        # 挤到每秒 6 帧(2026-10-08)。解一张图、缩一张图用不着并行。
        if hasattr(cv2, "setNumThreads"):
            cv2.setNumThreads(1)
        try:
            have = set(ort.get_available_providers())
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, int(threads))
            opts.inter_op_num_threads = 1
            chosen: list[Any] = []
            for p in (("TensorrtExecutionProvider", *providers) if trt else providers):
                if p not in have:
                    continue
                if p == "TensorrtExecutionProvider" and trt_cache is not None:
                    Path(trt_cache).mkdir(parents=True, exist_ok=True)
                    chosen.append((p, {"trt_engine_cache_enable": True,
                                       "trt_engine_cache_path": str(trt_cache),
                                       "trt_fp16_enable": True}))
                else:
                    chosen.append(p)
            self.sess = ort.InferenceSession(str(model), sess_options=opts, providers=chosen)
        except Exception as exc:                         # 模型坏了
            raise DetectorUnavailable(f"模型无法读取:{exc}") from exc
        #: 实际用上的后端(``TensorrtExecutionProvider`` / ``CUDA…`` / ``CPU…``)。
        self.backend = (self.sess.get_providers() or ["?"])[0]
        self.input = self.sess.get_inputs()[0].name
        self.size, self.score = size, score

    def detect(self, jpeg: bytes) -> tuple[list[Box], int, int]:
        import cv2
        import numpy as np
        # 按一半分辨率解(1920×1080 → 960×540):模型输入才 640,全尺寸解码白费 CPU。方向按框在画面里的
        # 比例算,跟分辨率无关。
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
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
    #: 测距只用框中间这一段宽度(比例)。人的身子在框中间,框边上常是挡在前面的别的东西:
    #: 2026-10-08 C40221 上人坐在人形机器人斜后方,框右边缘跟机器人重叠一条,整框量到的是
    #: 2 m 的机器人,不是 3 m 的人。
    range_span: float = 0.5


class PersonNode:
    """把一帧图 + 最近的雷达点云 → 人员桥的 ``persons`` 报文。不碰 ROS,测试直接喂。"""

    def __init__(self, detector: Detector | None, *, snapshot_dir: Path | None = None,
                 settings: Settings | None = None, unavailable: str = "",
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_ns: Callable[[], int] = time.time_ns,
                 remote_snapshots: bool = False) -> None:
        self.detector = detector
        #: 跨板(人员检测在厂家 Orin、代理在 RK3588):截图不写本地盘,攒着交给桥发
        #: (:meth:`take_snapshot`)。
        self.remote_snapshots = remote_snapshots
        self._pending_snap: tuple[str, bytes] | None = None
        self.unavailable = unavailable
        self.cfg = settings or Settings()
        self.snapshot_dir = snapshot_dir
        self._mono = monotonic
        self._wall_ns = wall_ns
        self.seq = 0
        self._snap_at = -1e18
        #: 每台雷达最近一帧点云(狗身系):``雷达 → (点, 话题时间戳 ns, 收到时刻)``。
        self.clouds: dict[str, tuple[Any, int, float]] = {}

    def on_cloud(self, pts_base: Any, stamp_ns: int = 0, source: str = "front") -> None:
        self.clouds[source] = (pts_base, int(stamp_ns), self._mono())

    def on_cloud_lazy(self, convert: Callable[[], Any], stamp_ns: int = 0,
                      source: str = "front") -> None:
        """先只记下「怎么换算」,用到(这帧画面里有人)才换算,换算一次就缓存。两台雷达每秒 20 帧,
        每帧都换算到狗身系曾占掉人员检测约四分之一的 CPU(2026-10-08 C40221)。"""
        self.clouds[source] = (convert, int(stamp_ns), self._mono())

    def _fresh_cloud(self, stamp_ns: int) -> Any:
        """跟这帧画面对得上的点云(前后雷达拼起来);一台都对不上回 ``None``(距离报未知)。"""
        import numpy as np
        now = self._mono()
        got = []
        for source, (pts, st, at) in list(self.clouds.items()):
            if now - at > CLOUD_MAX_AGE_S \
                    or (st and stamp_ns and abs(st - stamp_ns) > CLOUD_MAX_SKEW_NS):
                continue
            if callable(pts):                            # 延后换算的:现在才算,算完记住
                pts = pts()
                self.clouds[source] = (pts, st, at)
            got.append(pts)
        if not got:
            return None
        return got[0] if len(got) == 1 else np.vstack(got)

    def on_image(self, camera: str, jpeg: bytes, stamp_ns: int) -> dict[str, Any]:
        self.seq += 1
        base = {"t": "persons", "seq": self.seq, "stamp_ns": max(0, int(stamp_ns)),
                "camera": camera, "people": [], "snapshot": "", "reason": ""}
        if self.detector is None:
            return base | {"check": "no_model", "reason": self.unavailable[:200] or "未检测器"}
        try:
            boxes, w, _h = self.detector.detect(jpeg)
        except Exception as exc:                         # noqa: BLE001 - 一帧坏了不停
            return base | {"check": "no_camera", "reason": f"此帧处理无法完成:{exc}"[:200]}
        cloud = self._fresh_cloud(stamp_ns) if boxes else None      # 没人就不碰点云
        people = []
        for b in boxes[:16]:
            c = bearing_deg((b.x1 + b.x2) / 2, w, hfov_deg=self.cfg.hfov_deg, lens=self.cfg.lens,
                            camera=camera)
            half = (b.x2 - b.x1) * min(1.0, max(0.05, self.cfg.range_span)) / 2
            mid = (b.x1 + b.x2) / 2
            left = bearing_deg(mid - half, w, hfov_deg=self.cfg.hfov_deg, lens=self.cfg.lens,
                               camera=camera)
            right = bearing_deg(mid + half, w, hfov_deg=self.cfg.hfov_deg, lens=self.cfg.lens,
                                camera=camera)
            r = None
            if cloud is not None:
                r = range_m(cloud, right, left, z_min=self.cfg.z_min, z_max=self.cfg.z_max,
                            max_range=self.cfg.max_range)
            people.append({"bearing_deg": round(c, 1),
                           "range_m": None if r is None else round(r, 2),
                           "score": round(min(1.0, max(0.0, b.score)), 3)})
        snap = ""
        if people and (self.snapshot_dir is not None or self.remote_snapshots) \
                and self._mono() - self._snap_at >= self.cfg.snapshot_every_s:
            snap = self._save_remote(camera, jpeg) if self.remote_snapshots \
                else self._save(camera, jpeg)
        return base | {"check": "ok", "people": people, "snapshot": snap}

    def _name(self, camera: str) -> str:
        return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(self._wall_ns() / 1e9))}" \
               f"-{camera}-{self.seq}.jpg"

    def _save_remote(self, camera: str, jpeg: bytes) -> str:
        from d1max_contract.persbridge import MAX_SNAP_BYTES
        if len(jpeg) > MAX_SNAP_BYTES:
            log.warning("截图 %d 字节,超过桥的上限 %d:这一张不发", len(jpeg), MAX_SNAP_BYTES)
            return ""
        name = self._name(camera)
        self._pending_snap = (name, jpeg)
        self._snap_at = self._mono()
        return name

    def take_snapshot(self) -> tuple[str, bytes] | None:
        """跨板:这一帧要发的截图(名字, JPEG);要在引用它的 ``persons`` 之前发。一张只交一次。"""
        got, self._pending_snap = self._pending_snap, None
        return got

    def _save(self, camera: str, jpeg: bytes) -> str:
        name = self._name(camera)
        try:
            assert self.snapshot_dir is not None
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.snapshot_dir / f".{name}.tmp"
            tmp.write_bytes(jpeg)
            os.replace(tmp, self.snapshot_dir / name)
            old = sorted(self.snapshot_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
            for p in old[:-MAX_SNAPSHOTS]:
                p.unlink(missing_ok=True)                # 代理一直没收走的:删最旧的
        except OSError:
            log.exception("截图存不下")
            return ""
        self._snap_at = self._mono()
        return name


# ------------------------------------------------------------ 连代理

class PersonLink:
    """连代理的人员桥(一行一个 JSON)。本机时连 Unix 套接字、直接发 ``hello``;跨板(给了 ``key``)时连
    TCP,
    先收代理的 ``challenge``、``hello`` 带 HMAC 签名和给代理出的题;**代理回的 hello 签名核对上才算
    连上**(复查 P212-I1:不然谁冒充代理发个挑战就能收截图),之后两个方向每一行都封好。代理推来的外参
    (``frames``)交给 ``on_frames``。发不出去就断开,过 ``reconnect_s`` 再连;都不抛(检测照跑)。"""

    def __init__(self, connect: Callable[[], socket.socket], *, key: bytes | None = None,
                 name: str = "d1max-persons", version: str = "",
                 on_frames: Callable[[str, dict], Any] | None = None,
                 reconnect_s: float = 1.0, timeout_s: float = 2.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._connect = connect
        self.key = key
        self.name, self.version = name, version
        self.on_frames = on_frames
        self.reconnect_s, self.timeout_s = reconnect_s, timeout_s
        self._mono = monotonic
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_try = 0.0
        #: 跨板时这条连接的会话密钥和两个方向的序号。
        self._ks: bytes | None = None
        self._tx = self._rx = 0

    @property
    def connected(self) -> bool:
        return self._sock is not None

    @staticmethod
    def _readline(s: socket.socket, buf: bytes, limit: int) -> tuple[bytes, bytes]:
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("代理握手未完就已断开")
            buf += chunk
            if len(buf) > limit:
                raise ConnectionError("握手的一行太长")
        line, _, rest = buf.partition(b"\n")
        return line, rest

    def _open(self) -> bool:
        import secrets

        from d1max_contract import persbridge as pb
        now = self._mono()
        if now < self._next_try:
            return False
        self._next_try = now + self.reconnect_s
        try:
            s = self._connect()
            s.settimeout(self.timeout_s)
            self._ks, self._tx, self._rx = None, 0, 0
            if self.key is not None:
                first, rest = self._readline(s, b"", pb.MAX_LINE)
                ch = pb.parse(first)
                if not isinstance(ch, pb.Challenge):
                    raise ConnectionError(f"代理未发挑战:{type(ch).__name__}")
                nd = secrets.token_hex(16)
                s.sendall(pb.encode(pb.Hello(proto=pb.PROTO, name=self.name, version=self.version,
                                             mac=pb.hello_mac(self.key, ch.nonce, self.name),
                                             nonce=nd)))
                reply, rest = self._readline(s, rest, pb.MAX_LINE)
                h = pb.parse(reply)
                if not isinstance(h, pb.Hello):
                    why = getattr(h, "reason", type(h).__name__)
                    raise ConnectionError(f"代理未返回 hello:{why}")
                import hmac
                if not h.mac or not hmac.compare_digest(h.mac,
                                                        pb.agent_mac(self.key, nd, ch.nonce)):
                    raise ConnectionError("代理回的 hello 签名错误(不是我们的代理,或者密钥不同)")
                self._ks = pb.session_key(self.key, ch.nonce, nd)
                self._buf = rest
            else:
                self._buf = b""
                s.sendall(pb.encode(pb.Hello(proto=pb.PROTO, name=self.name,
                                             version=self.version)))
        except (OSError, ConnectionError, ValueError) as exc:
            log.warning("连不上代理的人员桥:%s", exc)
            try:
                s.close()
            except (OSError, UnboundLocalError):
                pass
            return False
        self._sock = s
        log.info("连上代理的人员桥了%s", "(跨板,过了挑战)" if self.key else "")
        return True

    def send(self, obj: dict) -> bool:
        if self._sock is None and not self._open():
            return False
        assert self._sock is not None
        from d1max_localizer.obstacles import _line  # 跟障碍桥同一种行格式
        line = _line(obj)
        if self._ks is not None:
            from d1max_contract import persbridge as pb
            self._tx += 1
            line = pb.seal(self._ks, pb.TO_AGENT, self._tx, line)
        try:
            self._sock.sendall(line)
            return True
        except OSError as exc:
            log.warning("人员桥发不出去(断开,稍后再连):%s", exc)
            self.close()
            return False

    def poll(self) -> None:
        """代理发来的(hello、外参、出错)读掉;外参交给 ``on_frames``。"""
        import select

        from d1max_contract import persbridge as pb
        s = self._sock
        if s is None:
            return
        try:
            while select.select([s], [], [], 0)[0]:
                chunk = s.recv(65536)
                if not chunk:
                    self.close()
                    return
                self._buf += chunk
        except OSError:
            self.close()
            return
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            try:
                if self._ks is not None:
                    self._rx, m = pb.unseal(self._ks, pb.TO_DETECTOR, line, self._rx)
                else:
                    m = pb.parse(line)
            except Exception as exc:                     # noqa: BLE001
                log.warning("代理发来一行看不懂(或者签名不对,不认):%s", exc)
                continue
            if isinstance(m, pb.FramesMsg) and self.on_frames is not None:
                try:
                    self.on_frames(m.ref, m.frames)
                except Exception:
                    log.exception("外参用不上(图 %s)", m.ref)
            elif isinstance(m, pb.Error):
                log.warning("代理说:%s", m.reason)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf = b""
        self._ks = None


# ------------------------------------------------------------ ROS 节点

def main(argv: Sequence[str] | None = None) -> int:
    """ROS 节点(系统 Python;``d1max-persons.service``)。"""
    ap = argparse.ArgumentParser(prog="d1max-persons")
    ap.add_argument("--model", type=Path, default=Path("/etc/d1max/person.onnx"),
                    help="ONNX 检测模型(YOLO 系,COCO person 类);没有就报 no_model")
    ap.add_argument("--socket", type=Path, default=Path("/var/lib/d1max/agent/persons.sock"))
    ap.add_argument("--connect", default=None, metavar="HOST:PORT",
                    help="跨板:代理在另一块板(自带的 RK3588)上,经机器人内网 TCP 连它的人员桥"
                         "(要配 --key);"
                         "截图走桥、外参由代理推过来。不给 = 本机套接字 --socket")
    ap.add_argument("--key", type=Path, default=None,
                    help="跨板人员桥的共享密钥文件(跟代理那块板同一把;一行十六进制,chmod 600)")
    ap.add_argument("--snapshots", type=Path,
                    default=Path("/var/lib/d1max/agent/person-snapshots"))
    ap.add_argument("--front-topic", default="/front_camera/image_compressed")
    ap.add_argument("--back-topic", default="/rear_camera/image_compressed",
                    help="后相机(空 = 不用)")
    ap.add_argument("--lidar-topic", default="/front_lidar")
    ap.add_argument("--rear-lidar-topic", default="/rear_lidar",
                    help="后雷达(空 = 不用;后相机看到的人靠它测距,外参见 --lidars)")
    ap.add_argument("--lidars", type=Path, default=None,
                    help="后雷达外参 lidars.json(默认 /etc/d1max/lidars.json;没有 = 几何初值)")
    ap.add_argument("--frames", type=Path, default=None,
                    help="前雷达外参(frames.json);不给就跟着代理正在用的图(--maps-dir)走")
    ap.add_argument("--maps-dir", type=Path, default=Path("/var/lib/d1max/agent/maps"))
    ap.add_argument("--fps", type=float, default=2.0, help="每个相机每秒最多检测几帧")
    ap.add_argument("--threads", type=int, default=THREADS,
                    help="CPU 推理的线程上限(缺省 2;onnxruntime 不限会占满所有核)")
    ap.add_argument("--trt", action="store_true",
                    help="先用 TensorRT(更快;第一次建引擎要好几分钟,换模型、升级要重建;缺省 CUDA)")
    ap.add_argument("--trt-cache", type=Path, default=TRT_CACHE,
                    help="TensorRT 引擎缓存目录(--trt 才用)")
    ap.add_argument("--hfov", type=float, default=HFOV_DEG)
    ap.add_argument("--lens", choices=("equidistant", "pinhole"), default="equidistant")
    ap.add_argument("--z-min", type=float, default=Z_MIN)
    ap.add_argument("--z-max", type=float, default=Z_MAX)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # W55:工程师远程打开「详细日志」时调到 DEBUG,到点自动恢复
    from d1max_contract.debuglog import DebugWatch
    DebugWatch().start()
    key: bytes | None = None
    if a.connect:
        if a.key is None:
            ap.error("--connect 要配 --key(共享密钥文件)")
        from d1max_contract.persbridge import load_key
        try:
            key = load_key(a.key)
        except (OSError, ValueError) as exc:
            ap.error(f"人员桥密钥:{exc}")
        host, _, port = a.connect.rpartition(":")
        if not host or not port.isdigit():
            ap.error("--connect 要写成 HOST:PORT")

    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import CompressedImage, PointCloud2

    from d1max_localizer.build import cloud_xyz
    from d1max_localizer.frames import Frames
    from d1max_localizer.livemap import cdr_stamp
    from d1max_localizer.obstacles import Mount, RearMount, active_frames

    detector: Detector | None = None
    why = ""
    try:
        detector = OnnxDetector(a.model, threads=a.threads, trt=a.trt, trt_cache=a.trt_cache)
        log.info("检测器后端:%s", detector.backend)
    except DetectorUnavailable as exc:
        why = str(exc)
        log.error("检测器起不来(报 no_model):%s", why)
    node_logic = PersonNode(detector, snapshot_dir=None if a.connect else a.snapshots,
                            unavailable=why, remote_snapshots=bool(a.connect),
                            settings=Settings(hfov_deg=a.hfov, lens=a.lens, z_min=a.z_min,
                                              z_max=a.z_max))

    def mounts() -> tuple[Mount | None, RearMount | None]:
        try:
            f = Frames.load(a.frames) if a.frames is not None else None
            if f is None:
                got = active_frames(a.maps_dir)
                f = Frames.load(got[1]) if got else None
            if f is None:
                return None, None
            front = Mount.from_frames(f)
            rear = RearMount.from_lidars(front, f, a.lidars) if a.rear_lidar_topic else None
            return front, rear
        except Exception as exc:                         # noqa: BLE001
            log.error("外参读不了:%s(先不测距)", exc)
            return None, None

    cur: dict[str, Any] = {}
    cur["front"], cur["rear"] = (None, None) if a.connect else mounts()

    def on_frames(ref: str, d: dict) -> None:
        """跨板:代理推来正在用的那张图的外参,换上(本机时自己读代理的地图目录)。"""
        f = Frames.from_json(d)
        front = Mount.from_frames(f)
        cur["front"] = front
        cur["rear"] = RearMount.from_lidars(front, f, a.lidars) if a.rear_lidar_topic else None
        log.info("用上图 %s 的外参", ref)

    if a.connect:
        host, _, port = a.connect.rpartition(":")
        client = PersonLink(lambda: socket.create_connection((host, int(port)), timeout=2.0),
                            key=key, on_frames=on_frames)
    else:
        client = PersonLink(lambda: _unix(a.socket))
    from d1max_localizer import rosinit
    rosinit.init(rclpy)
    stop = rosinit.stop_flag()                           # W41:正常停止退出码 0
    node = rclpy.create_node("d1max_persons")
    qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                     reliability=ReliabilityPolicy.BEST_EFFORT)
    last: dict[str, float] = {}

    def on_lidar(source: str) -> Callable[[bytes], None]:
        def cb(raw: bytes) -> None:
            m = cur.get(source)
            if m is None:
                return
            try:
                # 只读报文头里的时刻,整条点云等用到再反序列化、换算
                node_logic.on_cloud_lazy(
                    lambda raw=raw: m.to_base(cloud_xyz(deserialize_message(raw, PointCloud2))),
                    int(round(cdr_stamp(raw) * 1e9)), source)
            except Exception:
                log.exception("%s 雷达这一帧处理不了", source)
        return cb

    def on_image(camera: str) -> Callable[[bytes], None]:
        def cb(raw: bytes) -> None:
            now = time.monotonic()
            if now - last.get(camera, -1e9) < 1.0 / max(0.1, a.fps):
                return
            last[camera] = now
            try:
                msg = deserialize_message(raw, CompressedImage)
                st = msg.header.stamp
                out = node_logic.on_image(camera, bytes(msg.data),
                                          st.sec * 1_000_000_000 + st.nanosec)
                snap = node_logic.take_snapshot()
                if snap is not None:                     # 跨板:截图先发,再发引用它的 persons
                    from d1max_contract.persbridge import Snap
                    sm = Snap.of(*snap)
                    if not client.send({"t": "snap", "name": sm.name, "jpeg_b64": sm.jpeg_b64}):
                        out["snapshot"] = ""             # 截图没发过去:不引用
                client.send(out)
            except Exception:
                log.exception("%s 这一帧处理不了", camera)
        return cb

    node.create_subscription(PointCloud2, a.lidar_topic, on_lidar("front"), qos, raw=True)
    if a.rear_lidar_topic:
        node.create_subscription(PointCloud2, a.rear_lidar_topic, on_lidar("rear"), qos, raw=True)
    node.create_subscription(CompressedImage, a.front_topic, on_image("front"), qos, raw=True)
    if a.back_topic:
        node.create_subscription(CompressedImage, a.back_topic, on_image("back"), qos, raw=True)
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    log.info("人员检测:订 %s%s + %s → %s", a.front_topic,
             f"、{a.back_topic}" if a.back_topic else "", a.lidar_topic,
             f"代理 {a.connect}(跨板)" if a.connect else a.socket)
    hb = time.monotonic()
    try:
        while rclpy.ok() and not stop.is_set():
            ex.spin_once(timeout_sec=0.2)
            client.poll()
            if time.monotonic() - hb >= 1.0:
                hb = time.monotonic()
                client.send({"t": "hb", "seq": max(1, node_logic.seq)})
    except rosinit.shutdown_errors():
        log.info("ROS 关了:停")
    finally:
        ex.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
    log.info("人员检测停了")
    return 0


def _unix(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(os.fspath(path))
    return s


if __name__ == "__main__":
    raise SystemExit(main())
