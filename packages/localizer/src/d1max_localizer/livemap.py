"""建图时的实时预览(W09f):边走边建时,狗上跟在线建图(``mola-cli``)一起跑的一个小进程,订实时雷达与
MOLA 的位姿,逐帧累加一张俯视粗栅格,每几秒写一张快照(PNG + 说明)到这张图的中间目录;代理的
``mapping_preview`` 读它给手机。

- 在线建图的地图在 ``mola-cli`` 的内存里,停的时候才存盘、运行中取不出来(往 ROS 上发整张三维地图
  我们关了,W09c2),所以预览自己攒一张。它只是给人看的:**尽力而为**,丢帧、起不来都不碍建图。
- 坐标是 MOLA 系的俯视(「上」按雷达装法定号、平面两轴按装法上的朝前),跟建好的图(按 ``frames.json``
  另转)**不通用**。
- 离地高度按**这一帧**雷达的高度算(狗的腿把雷达架在地面上方差不多同样高):上坡不会把地面当障碍。
- 要 ROS 的只有 :func:`main`(系统 Python);栅格、配对、写盘是纯 numpy,开发机的 venv 里测。
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import logging
import math
import os
import signal
import struct
import time
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from d1max_localizer.frames import quat_to_mat

log = logging.getLogger("d1max.livemap")

#: 装法上的上、朝前(雷达系;RS-Airy X 朝上、Z 朝前 —— 跟建图脚本 ``build.SENSOR_*_HINT`` 一样)
SENSOR_UP_HINT = (1.0, 0.0, 0.0)
SENSOR_FORWARD_HINT = (0.0, 0.0, 1.0)
RES_M = 0.1
MAX_SIDE = 1024                 # 长边超过这么多格就整体粗一倍
UP_FRAMES = 5                   # 前几帧看地面在雷达哪边,定「上」的正负号
FLOOR_BAND = (-0.15, 0.25)      # 离地(m):地面
OBST_BAND = (0.4, 1.8)          # 离地(m):障碍(跟规划栅格 grid.FLOOR_CLEAR_M 一样从 0.4 起)
OBST_HITS = 3                   # 一格累计这么多个障碍点才画黑(零星的杂点不画)
RANGE_M = (0.6, 25.0)           # 近处是狗自己;远处稀疏,只会把画幅撑大
TRAIL_STEP_M = 0.3
TRAIL_MAX = 1000                # 满了抽一半、间距翻倍(回执里每次给整条,抽稀不碍事)
POINT_STRIDE = 8                # 每帧的点隔几个取一个
MAX_RATE_HZ = 2.0               # 每秒最多累加几帧
#: 位姿配哪帧点云:位姿的时刻减点云报文头的时刻在这个范围里(含)。MOLA 的位姿时刻可能是扫描的中间
#: (离线轨迹比报文头晚 50 ms,10 Hz 下正好半帧),所以只往晚的一边放宽;窗口比一帧(100 ms)窄,
#: 配上的只会有一帧 —— 有两帧取不晚于位姿的最近那帧。
PAIR_WINDOW_S = (-0.01, 0.08)
KEEP_SCANS = 10                 # 等位姿的点云最多留几帧
KEEP_POSES = 50
PERIOD_S = 3.0                  # 多久写一张快照(有新帧才写)
#: PNG 超过这么大就先把图缩一半再发(障碍优先),直到放得下 —— 回执有上限(代理那边 240 KiB),
#: 不能因为图大就一直不发(内审再议 1)
PNG_MAX_BYTES = 150 * 1024
SNAPSHOT = "preview.json"
UNKNOWN, FREE, OCC = 205, 254, 0   # 跟 floor.pgm 一个配色


def _unit(v: Any) -> Any:
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


class LiveGrid:
    """逐帧累加的俯视栅格。:meth:`add` 吃 MOLA 系里的一帧(雷达位置、雷达的转动、点),:meth:`render`
    出图。"""

    def __init__(self, *, sensor_up: Sequence[float] = SENSOR_UP_HINT,
                 sensor_forward: Sequence[float] = SENSOR_FORWARD_HINT, res: float = RES_M,
                 max_side: int = MAX_SIDE, trail_max: int = TRAIL_MAX) -> None:
        self._su = _unit(sensor_up)
        self._sf = _unit(sensor_forward)
        self.res = float(res)
        self._max_side = int(max_side)
        self._trail_max = int(trail_max)
        self._trail_step = TRAIL_STEP_M
        self.up: Any = None
        self._e1: Any = None
        self._e2: Any = None
        self._clear = 0.0                       # 雷达离地多高
        self._early: list[tuple[Any, Any, Any]] = []
        self._floor: Any = None                 # [i − i0, j − j0] 的计数
        self._obst: Any = None
        self._i0 = self._j0 = 0
        self._lo: tuple[int, int] | None = None     # 画过的格子的范围(含)
        self._hi: tuple[int, int] | None = None
        self.frames = 0
        self.trail: list[tuple[float, float]] = []
        self.pose: tuple[float, float, float] | None = None

    # ------------------------------------------------------------------ 吃帧

    def add(self, origin: Any, R: Any, pts: Any) -> None:
        origin, R, pts = np.asarray(origin, float), np.asarray(R, float), np.asarray(pts, float)
        if self.up is None:
            self._early.append((origin, R, pts))
            if len(self._early) < UP_FRAMES or not self._decide_up():
                del self._early[:-UP_FRAMES]
                return
            early, self._early = self._early, []
            for o, r, p in early:
                self._integrate(o, r, p)
            return
        self._integrate(origin, R, pts)

    def _decide_up(self) -> bool:
        """「上」= 装法上的上转到 MOLA 系(几帧平均),正负号按地面:以雷达为准的相对高度里最密的那层
        (5 cm 一层)要在雷达下面。那层有多低就是雷达离地多高。"""
        u = _unit(sum(R @ self._su for _, R, _ in self._early))
        rel = []
        for o, _, p in self._early:
            d = p - o
            n = np.linalg.norm(d, axis=1)
            rel.append((d @ u)[(n >= RANGE_M[0]) & (n <= RANGE_M[1])])
        h = np.concatenate(rel) if rel else np.zeros(0)
        # 跟建图脚本 ``build.orient`` 一样只看雷达上下 1.5 m(内审应修 4:放到 2 m,室内 2.4 m 的天花板
        # 落进来、又比地面密,「上」就翻了、整张预览镜像)。范围外的直方图自己不数。
        hist, edges = np.histogram(h, bins=60, range=(-1.5, 1.5))
        if hist.max(initial=0) == 0:
            return False
        k = int(np.argmax(hist))
        peak = (edges[k] + edges[k + 1]) / 2
        if peak > 0:
            u = -u
        f = sum(R @ self._sf for _, R, _ in self._early)
        f = _unit(f - (f @ u) * u)
        self.up, self._e1, self._e2 = u, f, np.cross(u, f)
        self._clear = abs(float(peak))
        return True

    def _integrate(self, origin: Any, R: Any, pts: Any) -> None:
        rel = pts - origin
        n = np.linalg.norm(rel, axis=1) if len(rel) else np.zeros(0)
        keep = (n >= RANGE_M[0]) & (n <= RANGE_M[1])
        rel, pts = rel[keep], pts[keep]
        h = rel @ self.up + self._clear
        fl = (h >= FLOOR_BAND[0]) & (h <= FLOOR_BAND[1])
        ob = (h >= OBST_BAND[0]) & (h <= OBST_BAND[1])
        sel = fl | ob
        if sel.any():
            xy = np.stack([pts[sel] @ self._e1, pts[sel] @ self._e2], 1)
            self._accumulate(xy, fl[sel], ob[sel])
        self.frames += 1
        x, y = float(origin @ self._e1), float(origin @ self._e2)
        fwd = R @ self._sf
        self.pose = (x, y, math.atan2(float(fwd @ self._e2), float(fwd @ self._e1)))
        self._add_trail(x, y)

    def _add_trail(self, x: float, y: float) -> None:
        if self.trail and math.dist(self.trail[-1], (x, y)) < self._trail_step:
            return
        self.trail.append((x, y))
        if len(self.trail) > self._trail_max:
            self.trail = self.trail[::2]
            self._trail_step *= 2

    def _cells(self, xy: Any) -> Any:
        return np.floor(xy / self.res).astype(np.int64)

    def _accumulate(self, xy: Any, fl: Any, ob: Any) -> None:
        c = self._cells(xy)
        while True:
            lo = c.min(0) if self._lo is None else np.minimum(c.min(0), self._lo)
            hi = c.max(0) if self._hi is None else np.maximum(c.max(0), self._hi)
            if int((hi - lo).max()) + 1 <= self._max_side:
                break
            self._coarsen()
            c = self._cells(xy)
        self._lo, self._hi = (int(lo[0]), int(lo[1])), (int(hi[0]), int(hi[1]))
        self._fit()
        i, j = c[:, 0] - self._i0, c[:, 1] - self._j0
        np.add.at(self._floor, (i[fl], j[fl]), 1)
        np.add.at(self._obst, (i[ob], j[ob]), 1)

    def _fit(self) -> None:
        """数组盖住 ``_lo``–``_hi``(多留 32 格,免得每帧都挪)。"""
        assert self._lo is not None and self._hi is not None
        if self._floor is not None:
            nx, ny = self._floor.shape
            if (self._i0 <= self._lo[0] and self._j0 <= self._lo[1]
                    and self._hi[0] < self._i0 + nx and self._hi[1] < self._j0 + ny):
                return
        pad = 32
        i0, j0 = self._lo[0] - pad, self._lo[1] - pad
        shape = (self._hi[0] - i0 + 1 + pad, self._hi[1] - j0 + 1 + pad)
        floor, obst = np.zeros(shape, np.uint32), np.zeros(shape, np.uint32)
        if self._floor is not None:
            nx, ny = self._floor.shape
            a, b = self._i0 - i0, self._j0 - j0
            floor[a:a + nx, b:b + ny] = self._floor
            obst[a:a + nx, b:b + ny] = self._obst
        self._floor, self._obst, self._i0, self._j0 = floor, obst, i0, j0

    def _coarsen(self) -> None:
        """整体粗一倍:格 (i, j) 并进 (i // 2, j // 2),计数相加。"""
        self.res *= 2
        if self._floor is None:
            return
        ii, jj = np.nonzero((self._floor > 0) | (self._obst > 0))
        fv, ov = self._floor[ii, jj], self._obst[ii, jj]
        ii, jj = (ii + self._i0) // 2, (jj + self._j0) // 2
        self._floor = self._obst = None
        if len(ii) == 0:
            self._lo = self._hi = None
            return
        self._lo = (int(ii.min()), int(jj.min()))
        self._hi = (int(ii.max()), int(jj.max()))
        self._fit()
        np.add.at(self._floor, (ii - self._i0, jj - self._j0), fv)
        np.add.at(self._obst, (ii - self._i0, jj - self._j0), ov)

    # ------------------------------------------------------------------ 出图

    def render(self) -> tuple[Any, dict[str, Any]] | None:
        """→ (灰度图:第 0 行在最上、y 朝上, 说明)。还没画过东西回 None。"""
        if self._floor is None or self._lo is None or self._hi is None:
            return None
        a, b = self._lo[0] - self._i0, self._lo[1] - self._j0
        c, d = self._hi[0] - self._i0 + 1, self._hi[1] - self._j0 + 1
        fl, ob = self._floor[a:c, b:d], self._obst[a:c, b:d]
        img = np.full(fl.shape, UNKNOWN, np.uint8)
        img[fl > 0] = FREE
        img[ob >= OBST_HITS] = OCC
        img = np.ascontiguousarray(img.T[::-1])
        meta = {"res": self.res, "origin": [self._lo[0] * self.res, self._lo[1] * self.res],
                "width": int(img.shape[1]), "height": int(img.shape[0])}
        return img, meta


def png(img: Any) -> bytes:
    """8 位灰度 PNG(狗上不一定有 PIL)。"""
    h, w = img.shape
    raw = np.hstack([np.zeros((h, 1), np.uint8), np.asarray(img, np.uint8)]).tobytes()

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def halve(img: Any, meta: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """图缩一半:2 × 2 并一格,有障碍算障碍、否则有地面算地面。左下角不动(往上、往右补未知)。"""
    h, w = img.shape
    ph, pw = h % 2, w % 2
    a = np.pad(img, ((ph, 0), (0, pw)), constant_values=UNKNOWN)      # 第 0 行在最上:补在上面
    b = a.reshape(a.shape[0] // 2, 2, a.shape[1] // 2, 2)
    out = np.full((b.shape[0], b.shape[2]), UNKNOWN, np.uint8)
    out[(b == FREE).any(axis=(1, 3))] = FREE
    out[(b == OCC).any(axis=(1, 3))] = OCC
    return out, meta | {"res": meta["res"] * 2, "width": int(out.shape[1]),
                        "height": int(out.shape[0])}


class Snapshots:
    """把栅格写成快照(一个 JSON:说明 + base64 的 PNG,先写临时文件再改名 —— 读的人不会读到半张)。"""

    def __init__(self, out: Path, *, run: str, now: Callable[[], float] = time.time) -> None:
        self.out = Path(out)
        self.run = run
        self._now = now
        self.seq = 0
        self._frames = 0

    def write(self, grid: LiveGrid, *, dropped: int = 0) -> bool:
        """有新帧就写一张,回写没写成。写盘失败记一行、下次再试。"""
        if grid.frames == self._frames:
            return False
        r = grid.render()
        if r is None:
            return False
        img, meta = r
        data = png(img)
        while len(data) > PNG_MAX_BYTES and max(img.shape) > 16:
            img, meta = halve(img, meta)
            data = png(img)
        doc = meta | {"seq": self.seq + 1, "run": self.run, "frames": grid.frames,
                      "dropped": dropped, "written_at": self._now(),
                      "pose": [round(v, 3) for v in grid.pose] if grid.pose else None,
                      "trail": [[round(x, 2), round(y, 2)] for x, y in grid.trail],
                      "png": base64.b64encode(data).decode("ascii")}
        tmp = self.out / f".{SNAPSHOT}.tmp"
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            # 不许写 NaN:手机上的 JSON 解不开(内审应修 3)
            text = json.dumps(doc, separators=(",", ":"), allow_nan=False)
            tmp.write_text(text)
            os.replace(tmp, self.out / SNAPSHOT)
        except (OSError, ValueError) as exc:
            log.warning("预览快照写不了(下次再试):%s", exc)
            return False
        self.seq += 1
        self._frames = grid.frames
        return True


class Feeder:
    """点云配位姿:按时间戳配(见 :data:`PAIR_WINDOW_S`;谁先到都行)。每秒最多 ``rate_hz`` 帧,
    多的在反序列化之前就丢(:meth:`want`)。"""

    def __init__(self, grid: Any, *, rate_hz: float = MAX_RATE_HZ) -> None:
        self.grid = grid
        self._gap = 1.0 / rate_hz
        self._last: float | None = None
        self.dropped = 0
        self._scans: collections.deque[tuple[float, Any]] = collections.deque(maxlen=KEEP_SCANS)
        self._poses: collections.deque[tuple[float, Any, Any]] = collections.deque(
            maxlen=KEEP_POSES)

    def want(self, stamp: float) -> bool:
        # 时间戳往回跳了(钟被校回去):从这一帧重新算,不然要等时间追上旧值才再收(内审应修 2)
        if self._last is not None and 0 <= stamp - self._last < self._gap - 1e-9:
            self.dropped += 1
            return False
        self._last = stamp
        return True

    def on_scan(self, stamp: float, xyz: Any) -> None:
        xyz = np.asarray(xyz, float)[::POINT_STRIDE]
        for k, (t, p, q) in enumerate(self._poses):
            if _paired(stamp, t):
                del self._poses[k]
                self._use(p, q, xyz)
                return
        self._scans.append((stamp, xyz))

    def on_pose(self, stamp: float, p: Sequence[float], q: Sequence[float]) -> None:
        hit = [k for k, (t, _) in enumerate(self._scans) if _paired(t, stamp)]
        if hit:
            k = max(hit, key=lambda i: self._scans[i][0])
            xyz = self._scans[k][1]
            for _ in range(k + 1):                  # 更早的等不到位姿了
                self._scans.popleft()
            self._use(p, q, xyz)
            return
        self._poses.append((stamp, p, q))

    def _use(self, p: Sequence[float], q: Sequence[float], xyz: Any) -> None:
        R = np.array(quat_to_mat(q))
        o = np.asarray(p, float)
        self.grid.add(o, R, xyz @ R.T + o)


class Handlers:
    """ROS 回调(:func:`main` 接上):一条坏消息只丢它自己,不让预览进程退出(内审应修 3:短报文、
    缺字段的点云、全零或 NaN 的位姿原来会一路抛出 ``spin_once``,进程退了也没人重启)。"""

    def __init__(self, feeder: Feeder, *, decode_cloud: Callable[[bytes], Any]) -> None:
        self.feeder = feeder
        self._decode = decode_cloud
        self.bad = 0

    def _bad(self, what: str, exc: BaseException | None = None) -> None:
        self.bad += 1
        if self.bad == 1 or self.bad % 100 == 0:
            log.warning("预览:丢了一条坏的%s(累计 %d):%s", what, self.bad, exc or "不是有限数")

    def on_scan(self, raw: bytes) -> None:
        try:
            st = cdr_stamp(raw)
            if self.feeder.want(st):
                self.feeder.on_scan(st, self._decode(raw))
        except Exception as exc:  # noqa: BLE001 —— 尽力而为,坏一帧丢一帧
            self._bad("点云", exc)

    def on_pose(self, m: Any) -> None:
        try:
            s, p, o = m.header.stamp, m.pose.pose.position, m.pose.pose.orientation
            v = (p.x, p.y, p.z, o.x, o.y, o.z, o.w)
            norm = o.x ** 2 + o.y ** 2 + o.z ** 2 + o.w ** 2
            if not all(math.isfinite(c) for c in v) or norm < 1e-9:
                self._bad("位姿")
                return
            self.feeder.on_pose(s.sec + s.nanosec * 1e-9, (p.x, p.y, p.z), (o.x, o.y, o.z, o.w))
        except Exception as exc:  # noqa: BLE001
            self._bad("位姿", exc)


def _paired(scan_t: float, pose_t: float) -> bool:
    return PAIR_WINDOW_S[0] - 1e-9 <= pose_t - scan_t <= PAIR_WINDOW_S[1] + 1e-9


def cdr_stamp(raw: bytes) -> float:
    """序列化的 ROS 消息(CDR)开头的 ``header.stamp`` → 秒,不用反序列化整条点云。"""
    order = "<" if raw[1] == 1 else ">"
    sec, nsec = struct.unpack_from(order + "iI", raw, 4)
    return sec + nsec * 1e-9


def main(argv: Sequence[str] | None = None) -> int:
    """ROS 节点(系统 Python)。编排经 ``deploy/d1max-live-preview`` 起,SIGTERM 停(停前再写一张)。"""
    ap = argparse.ArgumentParser(prog="d1max-live-preview")
    ap.add_argument("--out", required=True, type=Path, help="快照写到哪个目录")
    ap.add_argument("--run", required=True, help="这一趟的号(录包名)")
    ap.add_argument("--lidar-topic", default="/front_lidar")
    ap.add_argument("--pose-topic", default="/d1max_mapping/lidar_odometry/pose")
    ap.add_argument("--period", type=float, default=PERIOD_S)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2

    from d1max_localizer.build import cloud_xyz

    grid = LiveGrid()
    feeder = Feeder(grid)
    snaps = Snapshots(a.out, run=a.run)
    stop = False

    def _stop(*_: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    rclpy.init(args=None)
    node = rclpy.create_node("d1max_mapview")
    # best_effort:预览丢帧无所谓,不能让慢的 Python 订阅者拖住雷达(录包、MOLA 要全帧);也跟
    # reliable 的发送方配得上。
    qos = QoSProfile(depth=2, history=HistoryPolicy.KEEP_LAST,
                     reliability=ReliabilityPolicy.BEST_EFFORT)

    h = Handlers(feeder, decode_cloud=lambda raw: cloud_xyz(deserialize_message(raw, PointCloud2)))
    node.create_subscription(PointCloud2, a.lidar_topic, h.on_scan, qos, raw=True)
    pose_qos = QoSProfile(depth=KEEP_POSES, reliability=ReliabilityPolicy.BEST_EFFORT)
    node.create_subscription(Odometry, a.pose_topic, h.on_pose, pose_qos)
    # 一个执行器反复用(全局的那个每次 spin_once 都要加、摘节点;内审小 4)
    ex = SingleThreadedExecutor()
    ex.add_node(node)
    log.info("预览:订 %s + %s,写到 %s", a.lidar_topic, a.pose_topic, a.out)
    last = time.monotonic()
    try:
        while not stop and rclpy.ok():
            ex.spin_once(timeout_sec=0.2)
            if time.monotonic() - last >= a.period:
                last = time.monotonic()
                if snaps.write(grid, dropped=feeder.dropped):
                    log.info("快照 %d:%d 帧、限速丢 %d、坏的 %d", snaps.seq, grid.frames,
                             feeder.dropped, h.bad)
    finally:
        snaps.write(grid, dropped=feeder.dropped)
        ex.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
