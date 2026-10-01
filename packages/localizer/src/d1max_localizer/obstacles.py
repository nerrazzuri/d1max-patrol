"""感知节点(W11 设计稿 §1–3、§6,W08 决定 8):雷达点云 → 狗身系局部栅格 + 前方净空许可。

- **外参**来自 ``frames.json``(``sensor_up``、``sensor_forward``、``sensor_in_base``:建图流水线按
  点云判上下、按狗走动的方向标前,不靠装法 —— #64 的「装反 180°」其实是「上」的正负号弄反了,见
  ``docs/真机待验证清单.md`` #64);后雷达没有标定,用前雷达的镜像猜。
- **启动自检**:前 :data:`CHECK_FRAMES` 帧拟合地面,地面法向跟「上」夹角 ≤ 5°、雷达离地 0.2–1.0 m、
  地面内点够多,才 ``ok``;不然 ``extrinsic_bad``(代理据此不宣告能自主走)。离地高度取自检的中位数。
- **每帧**:凸起障碍(离地 0.10–1.30 m)、落差(近处比地面低 0.15 m 以上)→ 挡;打到地面、矮草 → 看见了;
  别的格子 = 未知(代理当挡)。机身自己(腿)的点不算。
- **净空**:机身前沿往前、机身宽 + 每边 0.05 m 的走廊里第一个「挡 / 未知」有多远 → 给旁路进程发许可
  ``clear {ms, dist}``(第二层,只在够刹停的时候发)。
- 栅格**不带记忆**:滚动记忆在代理里做(代理有里程)。

核心纯 numpy(不 import ROS);ROS 只在 :func:`main` 里。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CHECK_FRAMES = 20
GIVE_UP_FRAMES = 100
REFIT_EVERY = 5
MAX_TILT_DEG = 5.0
HEIGHT_RANGE_M = (0.2, 1.0)
MIN_GROUND_FRAC = 0.3
PERMIT_MS = 300
#: 旁路进程从协议 4 起认 ``clear``。
CLEAR_PROTO = 4
#: 地面候选平面跟「上」最多差 45°(对着墙起来时别把墙当地面;外参歪 5–45° 照样查得出来)。
MIN_UP_COS = math.cos(math.radians(45.0))
SIDECAR = ("127.0.0.1", 8090)


@dataclass(frozen=True)
class Config:
    res: float = 0.1
    size: int = 80
    h_lo: float = 0.10                  # 凸起障碍的下沿(离地,米;草尖会不会误报是真机项)
    h_hi: float = 1.30                  # 上沿(狗高约 0.6 m,带上头顶的东西)
    ground_tol: float = 0.08            # 离地这么近算地面
    drop: float = 0.15                  # 比地面低这么多算落差
    near: float = 3.0                   # 落差只在这么近里认(远处地面点少、误报多)
    body_len: float = 0.93
    body_wid: float = 0.48
    margin: float = 0.05
    max_v: float = 0.6                  # 最快(W08 第一版 ≤ 0.6 m/s)
    latency: float = 0.2                # 链路延迟(秒,待 W00d 量)
    decel: float = 0.5                  # 刹车减速度(m/s²,待真机量)
    frame_age: float = 0.2              # 一帧点云从扫到到算完(秒,待真机量)

    def stop_dist(self, v: float) -> float:
        return v * self.latency + v * v / (2.0 * self.decel)


DEFAULT = Config()


@dataclass(frozen=True)
class Mount:
    """雷达系 → 狗身系(x 朝前、y 朝左、z 朝上,原点在狗身中心、**雷达那么高**)。"""

    fwd: tuple[float, float, float]     # 狗身 +x 在雷达系里的方向
    left: tuple[float, float, float]
    up: tuple[float, float, float]
    x: float = 0.0                      # 雷达在狗身上的水平位置
    y: float = 0.0

    @classmethod
    def from_frames(cls, f: Any) -> Mount:
        up = _unit(f.sensor_up)
        fwd = _unit(_reject(f.sensor_forward, up))
        left = _cross(up, fwd)
        sx, sy = (f.sensor_in_base if f.sensor_in_base is not None else (0.0, 0.0))
        return cls(fwd=fwd, left=left, up=up, x=float(sx), y=float(sy))

    def mirrored(self) -> Mount:
        """后雷达的猜测外参:前雷达绕上轴转 π、装在狗身后面(真机录包标定之前只用来看见「空」)。"""
        neg = tuple(-v for v in self.fwd)
        return Mount(fwd=neg, left=tuple(-v for v in self.left), up=self.up,  # type: ignore[arg-type]
                     x=-self.x, y=-self.y)

    def to_base(self, pts: Any) -> Any:
        import numpy as np
        R = np.array([self.fwd, self.left, self.up], dtype=float)
        out = np.asarray(pts, dtype=float) @ R.T
        out[:, 0] += self.x
        out[:, 1] += self.y
        return out


def _unit(v: Sequence[float]) -> tuple[float, float, float]:
    n = math.sqrt(sum(c * c for c in v))
    if not n or not math.isfinite(n):
        raise ValueError(f"外参:向量长度不对 {v!r}")
    return (v[0] / n, v[1] / n, v[2] / n)


def _reject(v: Sequence[float], u: Sequence[float]) -> tuple[float, float, float]:
    d = sum(a * b for a, b in zip(v, u, strict=True))
    return (v[0] - d * u[0], v[1] - d * u[1], v[2] - d * u[2])


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


# ------------------------------------------------------------ 地面与自检

def fit_ground(pts: Any, *, iters: int = 120, tol: float = 0.03, seed: int = 0
               ) -> tuple[tuple[float, float, float], float, float] | None:
    """狗身系的点里找地面:雷达以下、水平 0.5–4 m 的点做 RANSAC → (法向(朝上), 雷达离地高度,
    内点占比)。点太少回 ``None``。"""
    import numpy as np
    p = np.asarray(pts, dtype=float)
    r = np.hypot(p[:, 0], p[:, 1])
    cand = p[(p[:, 2] < -0.1) & (r > 0.5) & (r < 4.0)]
    if len(cand) < 30:
        return None
    rng = np.random.default_rng(seed)
    best, best_n = None, 0
    for _ in range(iters):
        a, b, c = cand[rng.choice(len(cand), 3, replace=False)]
        n = np.cross(b - a, c - a)
        nn = np.linalg.norm(n)
        if nn < 1e-6:
            continue
        n = n / nn
        if n[2] < 0:
            n = -n
        if n[2] < MIN_UP_COS:
            continue                                     # 墙、箱子的侧面不当地面候选
        d = -float(n @ a)
        inl = np.abs(cand @ n + d) < tol
        k = int(inl.sum())
        if k > best_n:
            best, best_n = inl, k
    if best is None:
        return None
    q = cand[best]
    centroid = q.mean(0)
    _, _, vt = np.linalg.svd(q - centroid)
    n = vt[2]
    if n[2] < 0:
        n = -n
    height = float(n @ centroid) * -1.0          # 雷达(原点)到平面的距离
    return (float(n[0]), float(n[1]), float(n[2])), height, best_n / len(cand)


class SelfCheck:
    """启动自检:攒 :data:`CHECK_FRAMES` 帧的地面拟合,取中位数判;:data:`GIVE_UP_FRAMES` 帧还攒不够
    (一直找不到地面 —— 外参的「上」弄反了、对着墙)就判没过(内审应修 6:原来永远「还在自检」)。
    过了之后**接着估离地高度**(每 :data:`REFIT_EVERY` 帧一次,取最近 :data:`CHECK_FRAMES` 次的
    中位数):狗开机是趴着的,站起来高度变了,不跟着变的话近处地面全成了落差。"""

    def __init__(self, frames: int = CHECK_FRAMES) -> None:
        self.need = frames
        self.fits: list[tuple[float, float, float]] = []    # (倾角°, 高度, 内点占比)
        self.tried = 0
        self.check = "initializing"
        self.reason = ""
        self.height: float | None = None
        self._heights: list[float] = []

    def feed(self, pts_base: Any) -> None:
        self.tried += 1
        if self.check == "extrinsic_bad":
            return
        if self.check == "ok":
            if self.tried % REFIT_EVERY:
                return
            got = fit_ground(pts_base, seed=self.tried)
            if got is None:
                return
            n, h, frac = got
            tilt = math.degrees(math.acos(max(-1.0, min(1.0, n[2]))))
            if tilt <= MAX_TILT_DEG and frac >= MIN_GROUND_FRAC \
                    and HEIGHT_RANGE_M[0] <= h <= HEIGHT_RANGE_M[1]:
                self._heights = (self._heights + [h])[-self.need:]
                self.height = sorted(self._heights)[len(self._heights) // 2]
            return
        got = fit_ground(pts_base, seed=len(self.fits))
        if got is not None:
            n, h, frac = got
            tilt = math.degrees(math.acos(max(-1.0, min(1.0, n[2]))))
            self.fits.append((tilt, h, frac))
        if len(self.fits) >= self.need:
            self._judge()
        elif self.tried >= GIVE_UP_FRAMES:
            self.check = "extrinsic_bad"
            self.reason = (f"{self.tried} 帧里只有 {len(self.fits)} 帧找得到地面(外参的「上」反了?"
                           "对着墙?)")

    def _judge(self) -> None:
        def med(i: int) -> float:
            v = sorted(f[i] for f in self.fits)
            return v[len(v) // 2]
        tilt, h, frac = med(0), med(1), med(2)
        why = []
        if tilt > MAX_TILT_DEG:
            why.append(f"地面跟外参的「上」差 {tilt:.1f}°(> {MAX_TILT_DEG:g}°)")
        if not HEIGHT_RANGE_M[0] <= h <= HEIGHT_RANGE_M[1]:
            why.append(f"雷达离地 {h:.2f} m(不在 {HEIGHT_RANGE_M[0]}–{HEIGHT_RANGE_M[1]} m)")
        if frac < MIN_GROUND_FRAC:
            why.append(f"地面点只占 {frac:.0%}(对着墙?)")
        self.height = h
        self._heights = [f[1] for f in self.fits]
        self.check, self.reason = ("extrinsic_bad", ";".join(why)) if why else ("ok", "")


# ------------------------------------------------------------ 每帧

def classify(pts_base: Any, height: float, cfg: Config = DEFAULT) -> tuple[bytes, bytes]:
    """狗身系的点(z 相对雷达)→ (挡, 看见过) 两张 ``size × size`` 的 0 / 1 位图
    (行 = x 朝前,列 = y 朝左)。"""
    import numpy as np
    p = np.asarray(pts_base, dtype=float)
    n = cfg.size
    occ = np.zeros(n * n, dtype=np.uint8)
    known = np.zeros(n * n, dtype=np.uint8)
    if len(p) == 0:
        return occ.tobytes(), known.tobytes()
    h = p[:, 2] + height
    hl, hw = cfg.body_len / 2 + cfg.margin, cfg.body_wid / 2 + cfg.margin
    body = (np.abs(p[:, 0]) <= hl) & (np.abs(p[:, 1]) <= hw) & (h < cfg.h_hi)
    r = np.floor(p[:, 0] / cfg.res + n / 2).astype(int)
    c = np.floor(p[:, 1] / cfg.res + n / 2).astype(int)
    inside = (r >= 0) & (r < n) & (c >= 0) & (c < n) & ~body
    idx = r * n + c
    ground = inside & (np.abs(h) <= cfg.ground_tol)
    low = inside & (h > cfg.ground_tol) & (h < cfg.h_lo)            # 矮草、碎石:看见了、不挡
    bump = inside & (h >= cfg.h_lo) & (h <= cfg.h_hi)
    dist = np.hypot(p[:, 0], p[:, 1])
    drop = inside & (h < -cfg.drop) & (dist <= cfg.near)
    known[idx[ground | low | bump | drop]] = 1
    occ[idx[bump | drop]] = 1
    return occ.tobytes(), known.tobytes()


def clear_distance(occ: bytes, known: bytes, cfg: Config = DEFAULT) -> float:
    """机身前沿往前、机身宽 + 每边余量的走廊里,第一个「挡 / 未知」离机身前沿多远(米)。"""
    n = cfg.size
    front = cfg.body_len / 2
    half = cfg.body_wid / 2 + cfg.margin
    best = (n / 2) * cfg.res - front
    for r in range(n // 2, n):
        x0 = (r - n / 2) * cfg.res                       # 这一行格子的后沿
        if x0 < front - 1e-9:
            continue                                     # 跨着机身前沿的那一格:机身自己的点滤掉了
        for c in range(n):
            y0 = (c - n / 2) * cfg.res
            if y0 + cfg.res <= -half or y0 >= half:
                continue
            i = r * n + c
            if occ[i] or not known[i]:
                return max(0.0, x0 - front)
    return best


def permit(dist: float, cfg: Config = DEFAULT) -> int:
    """许可有效期里最快速度走的 + 这一帧的帧龄里走的 + 刹停 + 0.3 m 都够,才给许可(毫秒);不够 0
    (内审应修 2:原来只算刹停,0.3 m 余量被有效期、帧龄吃光)。"""
    need = cfg.max_v * (PERMIT_MS / 1000.0 + cfg.frame_age) + cfg.stop_dist(cfg.max_v) + 0.3
    return PERMIT_MS if dist >= need else 0


# ------------------------------------------------------------ 发出去

class LineClient:
    """一行一个 JSON 的小客户端(阻塞套接字、发不出去就断开、下次再连)。``hello`` 是连上后的
    第一行。"""

    def __init__(self, connect: Any, hello: dict | None, *, reconnect_s: float = 1.0,
                 on_line: Any = None) -> None:
        self._connect = connect
        #: 对面回的每一行(JSON 解开的)交给它;``None`` = 读掉丢弃。
        self.on_line = on_line
        self._buf = b""
        self._hello = hello
        self._sock: socket.socket | None = None
        self._next_try = 0.0
        self.reconnect_s = reconnect_s
        self.sent = 0

    def send(self, obj: dict) -> bool:
        now = time.monotonic()
        if self._sock is None:
            if now < self._next_try:
                return False
            self._next_try = now + self.reconnect_s
            try:
                self._sock = self._connect()
                self._sock.setblocking(False)
                if self._hello is not None:
                    self._sock.sendall(_line(self._hello))
            except OSError as exc:
                log.debug("连不上:%s", exc)
                self._close()
                return False
        try:
            self._sock.sendall(_line(obj))
            self._drain()
        except OSError as exc:
            log.warning("发不出去,断开重连:%s", exc)
            self._close()
            return False
        self.sent += 1
        return True

    def poll(self) -> None:
        """连着就把对面发来的读空(内审阻断 3:旁路进程每秒给所有客户端广播几十条遥测,不读的话它那头
        的发送阻塞、攥着锁,所有客户端一起卡死)。**每帧都调**,不管这一帧发没发东西。"""
        if self._sock is None:
            return
        try:
            self._drain()
        except OSError as exc:
            log.warning("读不了,断开重连:%s", exc)
            self._close()

    def _drain(self) -> None:
        """对面回的(hello、回执、遥测)读掉;``on_line`` 给了就一行一行交给它。对面关了就断开。"""
        assert self._sock is not None
        try:
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    self._close()                        # 对面关了
                    return
                if self.on_line is None:
                    continue
                self._buf += chunk
                *lines, self._buf = self._buf.split(b"\n")
                self._buf = self._buf[-65536:]
                for ln in lines:
                    try:
                        self.on_line(json.loads(ln))
                    except ValueError:
                        continue
        except (BlockingIOError, InterruptedError):
            return

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def close(self) -> None:
        self._close()


def _line(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


class Perception:
    """一帧一帧喂进来(前雷达;后雷达可选)→ 发栅格、发许可。不碰 ROS,测试直接喂。"""

    def __init__(self, front: Mount, *, rear: Mount | None = None, cfg: Config = DEFAULT,
                 obs: LineClient | None = None, sidecar: LineClient | None = None,
                 rear_max_age_s: float = 0.3, clock: Any = time.monotonic) -> None:
        self.front, self.rear, self.cfg = front, rear, cfg
        self.obs, self.sidecar = obs, sidecar
        self.check = SelfCheck()
        self.seq = 0
        self._cmd = 0
        self._rear_pts: Any = None
        self._rear_at = -math.inf
        self.rear_max_age_s = rear_max_age_s
        self._clock = clock
        self.last_clear = 0.0
        self.sidecar_proto: int | None = None
        self._rej_logged = -math.inf
        if sidecar is not None and sidecar.on_line is None:
            sidecar.on_line = self.on_sidecar

    def set_mount(self, front: Mount, rear: Mount | None) -> None:
        """换了图(外参跟着图走):换外参、重新自检。"""
        self.front, self.rear = front, rear
        self.check = SelfCheck()
        self._rear_pts = None

    def on_rear(self, pts_sensor: Any) -> None:
        if self.rear is not None:
            self._rear_pts = self.rear.to_base(pts_sensor)
            self._rear_at = self._clock()

    def on_sidecar(self, msg: Any) -> None:
        """旁路进程回的:``hello`` 核协议版本(要 ≥ 4 才认 ``clear``)、``clear`` 被拒记日志(限频)。
        内审应修 1:原来不读、不看,版本对不上、许可全被拒也一声不响。"""
        if not isinstance(msg, dict):
            return
        if msg.get("t") == "hello":
            proto = msg.get("proto")
            self.sidecar_proto = proto if isinstance(proto, int) else None
            if not isinstance(proto, int) or proto < CLEAR_PROTO:
                log.error("旁路进程的协议是 %r,不认 clear(要 ≥ %d):净空许可发了也没用,重编 "
                          "motion/patrol_agent", proto, CLEAR_PROTO)
        elif msg.get("t") == "ack" and msg.get("ok") is False:
            now = self._clock()
            if now - self._rej_logged > 60.0:
                self._rej_logged = now
                log.warning("旁路进程拒了净空许可:%s", msg.get("error", ""))

    def on_front(self, pts_sensor: Any, stamp_ns: int) -> dict | None:

        from d1max_contract.obsbridge import pack_bits
        if self.sidecar is not None:
            self.sidecar.poll()                          # 每帧都读空(内审阻断 3)
        p = self.front.to_base(pts_sensor)
        self.check.feed(p)
        n = self.cfg.size
        if self.check.height is None:
            # 还在自检、自检没过:照样发一帧(全是未知)让代理知道是什么状态(内审应修 6)
            empty = pack_bits(bytes(n * n), n)
            self.seq += 1
            grid = {"t": "grid", "seq": self.seq, "stamp_ns": max(0, int(stamp_ns)),
                    "res": self.cfg.res, "size": n, "occ": empty, "known": empty, "rear": False,
                    "check": self.check.check, "reason": self.check.reason[:200]}
            if self.obs is not None:
                self.obs.send(grid)
            return grid
        rear = (self._rear_pts is not None
                and self._clock() - self._rear_at <= self.rear_max_age_s)
        occ, known = classify(p, self.check.height, self.cfg)
        if rear:
            # 后雷达外参是猜的(前雷达的镜像):标定之前只用它的「挡」,不用它的「空」放行(内审再议)
            occ_r, _ = classify(self._rear_pts, self.check.height, self.cfg)
            occ = bytes(a | b for a, b in zip(occ, occ_r, strict=True))
            known = bytes(a | b for a, b in zip(known, occ_r, strict=True))
        self.seq += 1
        grid = {"t": "grid", "seq": self.seq, "stamp_ns": max(0, int(stamp_ns)),
                "res": self.cfg.res, "size": n, "occ": pack_bits(occ, n),
                "known": pack_bits(known, n), "rear": bool(rear), "check": self.check.check,
                "reason": self.check.reason[:200]}
        if self.obs is not None:
            self.obs.send(grid)
        self.last_clear = clear_distance(occ, known, self.cfg)
        ms = permit(self.last_clear, self.cfg) if self.check.check == "ok" else 0
        if self.sidecar is not None and ms:
            self._cmd += 1
            self.sidecar.send({"id": self._cmd, "cmd": "clear", "ms": ms,
                               "dist": round(self.last_clear, 2)})
        return grid


def active_frames(maps_dir: Path) -> tuple[str, Path] | None:
    """狗上正在用的那张图的 ``frames.json``:``<maps>/active.json`` 声明的
    ``<地图号>/<版本>/frames.json``。没有、读不了、名字不规矩都回 ``None``(感知就不出栅格 ——
    代理看见「断了」,不宣告能自主走)。"""
    try:
        d = json.loads((maps_dir / "active.json").read_text("utf-8"))
        mid, ver = d["map_id"], d["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(v, str) and v and "/" not in v and ".." not in v for v in (mid, ver)):
        return None
    path = maps_dir / mid / ver / "frames.json"
    return (f"{mid}:{ver}", path) if path.is_file() else None


# ------------------------------------------------------------ ROS 节点

def main(argv: Sequence[str] | None = None) -> int:
    """ROS 节点(系统 Python;``d1max-obstacles.service``)。"""
    ap = argparse.ArgumentParser(prog="d1max-obstacles")
    ap.add_argument("--frames", type=Path, default=None,
                    help="前雷达外参(frames.json);不给就跟着代理正在用的图(--maps-dir)走")
    ap.add_argument("--maps-dir", type=Path, default=Path("/var/lib/d1max/agent/maps"),
                    help="代理的地图目录(active.json 在这里);换了图外参跟着换、重新自检")
    ap.add_argument("--socket", type=Path, default=Path("/var/lib/d1max/agent/obs.sock"))
    ap.add_argument("--front-topic", default="/front_lidar")
    ap.add_argument("--rear-topic", default="", help="后雷达(没标定:只用来看见「空」;空 = 不用)")
    ap.add_argument("--sidecar", default="", help="旁路进程 host:port(发净空许可;空 = 不发)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2

    from d1max_contract.obsbridge import PROTO
    from d1max_localizer.build import cloud_xyz
    from d1max_localizer.frames import Frames

    def load() -> tuple[str, Mount] | None:
        if a.frames is not None:
            return str(a.frames), Mount.from_frames(Frames.load(a.frames))
        got = active_frames(a.maps_dir)
        if got is None:
            return None
        try:
            return got[0], Mount.from_frames(Frames.load(got[1]))
        except Exception as exc:                         # noqa: BLE001 —— 坏的 frames.json:等换图
            log.error("外参读不了(%s):%s", got[1], exc)
            return None

    cur = load()
    while cur is None:
        log.warning("还没有正在用的图的外参(%s),5 s 后再看", a.maps_dir)
        time.sleep(5.0)
        cur = load()
    front = cur[1]
    obs = LineClient(lambda: _unix(a.socket), {"t": "hello", "proto": PROTO,
                                               "name": "d1max-obstacles"})
    side = None
    if a.sidecar:
        host, port = a.sidecar.rsplit(":", 1)
        side = LineClient(lambda: socket.create_connection((host, int(port)), timeout=1.0), None)
    per = Perception(front, rear=front.mirrored() if a.rear_topic else None, obs=obs, sidecar=side)
    rclpy.init(args=None)
    node = rclpy.create_node("d1max_obstacles")
    qos = QoSProfile(depth=2, history=HistoryPolicy.KEEP_LAST,
                     reliability=ReliabilityPolicy.BEST_EFFORT)

    def on_front(raw: bytes) -> None:
        try:
            msg = deserialize_message(raw, PointCloud2)
            st = msg.header.stamp
            per.on_front(cloud_xyz(msg), st.sec * 1_000_000_000 + st.nanosec)
        except Exception:
            log.exception("这一帧处理不了")

    def on_rear(raw: bytes) -> None:
        try:
            per.on_rear(cloud_xyz(deserialize_message(raw, PointCloud2)))
        except Exception:
            log.exception("后雷达这一帧处理不了")

    node.create_subscription(PointCloud2, a.front_topic, on_front, qos, raw=True)
    if a.rear_topic:
        node.create_subscription(PointCloud2, a.rear_topic, on_rear, qos, raw=True)
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    log.info("感知:订 %s%s → %s%s", a.front_topic, f" + {a.rear_topic}" if a.rear_topic else "",
             a.socket, f",许可 → {a.sidecar}" if a.sidecar else "")
    hb = look = time.monotonic()
    try:
        while rclpy.ok():
            ex.spin_once(timeout_sec=0.2)
            if time.monotonic() - hb >= 1.0:
                hb = time.monotonic()
                obs.send({"t": "hb", "seq": max(1, per.seq)})
            if a.frames is None and time.monotonic() - look >= 5.0:
                look = time.monotonic()
                nxt = load()
                if nxt is not None and nxt[0] != cur[0]:
                    log.info("换图了(%s → %s):换外参、重新自检", cur[0], nxt[0])
                    cur = nxt
                    per.set_mount(nxt[1], nxt[1].mirrored() if a.rear_topic else None)
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
