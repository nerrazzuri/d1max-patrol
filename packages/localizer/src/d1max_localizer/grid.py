"""二维规划栅格(W09c1 决定 3):MOLA 建图的点云 + 建图时雷达走过的路 → ``floor.pgm`` +
``floor.yaml``(ROS map_server 的格式:黑 = 占据、白 = 可通行、灰 = 不知道)。

**按 ``frames.json`` 的平面画**:点云乘 :meth:`Frames.level_matrix`,x、y 就是地图平面坐标(跟定位器
报给代理的位置同一个系),z 是高度 —— 导航去的点跟定位报的位置才对得上。原型
``tools/lio/mola/render_floormap.py`` 自己拿 RANSAC 拟合地面定「上」、跟定位的平面不是一回事,这里
不用它那一套。

画法(照原型,现场验过能出干净的图):

- 地面高度 = 雷达以下那部分点的高度直方图的峰;障碍 = 离地 :data:`FLOOR_CLEAR_M` 到
  :data:`WALL_TOP_M` 那一层(从狗身子以上起,狗自己的腿、近处的回波不进来);
- 离建图路线 :data:`TRAJ_MARGIN_M` 以外的障碍点不要(远处玻璃的反射);
- 从路线上的点往四周打射线,碰到墙停;格子的「被穿过次数」与「障碍点数」按 log-odds 判占据 /
  可通行(人、玻璃反射这种一会儿有一会儿没的,穿过的次数多就判可通行);
- 可通行区做一次开运算(去掉透过没扫到的玻璃漏出去的细扇形);只留贴着可通行区的障碍(去掉悬在
  外面的鬼影)。

只依赖 numpy(狗上用 ROS 系统 Python 里 apt 装的那份);不要 scipy、PIL。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from d1max_localizer.frames import Frames

RES_M = 0.05
FLOOR_CLEAR_M = 0.7
WALL_TOP_M = 1.8
TRAJ_MARGIN_M = 12.0
MAX_RANGE_M = 12.0
RAYS = 540
POSE_STRIDE_CELLS = 6
FREE, UNKNOWN, OCCUPIED = 254, 205, 0


@dataclass
class Grid:
    image: np.ndarray                                # 行 0 = 地图上 y 最大的那一行(PGM 的约定)
    origin: tuple[float, float]                      # 左下角那一格在地图平面上的 (x, y)
    res: float

    def cell_of(self, x: float, y: float) -> tuple[int, int] | None:
        """地图平面上的点在图像里的 (行, 列);出了图是 None。"""
        c = int((x - self.origin[0]) / self.res)
        r_from_bottom = int((y - self.origin[1]) / self.res)
        r = self.image.shape[0] - 1 - r_from_bottom
        if 0 <= r < self.image.shape[0] and 0 <= c < self.image.shape[1]:
            return r, c
        return None


def level(points: np.ndarray, frames: Frames) -> np.ndarray:
    """MOLA 系的 (N,3) 点 → 地图平面系(x、y 平面,z 高度)。"""
    return points @ np.asarray(frames.level_matrix(), dtype=float).T


def _dilate(m: np.ndarray, n: int) -> np.ndarray:
    out = m.copy()
    for _ in range(n):
        g = out.copy()
        g[1:, :] |= out[:-1, :]
        g[:-1, :] |= out[1:, :]
        g[:, 1:] |= out[:, :-1]
        g[:, :-1] |= out[:, 1:]
        out = g
    return out


def _erode(m: np.ndarray, n: int) -> np.ndarray:
    return ~_dilate(~m, n)


def _floor_height(h: np.ndarray, sensor_h: np.ndarray) -> float:
    """雷达以下那些点的高度直方图的峰(天花板、上层不算进来)。"""
    below = h[h < float(np.median(sensor_h))]
    if len(below) < 100:
        below = h
    hist, edges = np.histogram(below, bins=300)
    k = int(np.argmax(hist))
    return float((edges[k] + edges[k + 1]) / 2)


def _near_traj(xy: np.ndarray, traj_xy: np.ndarray, margin: float) -> np.ndarray:
    """每个点离路线是否在 ``margin`` 以内(按 1 m 的粗格子近似:路线所在格子往外扩 ``margin`` 格)。"""
    if margin <= 0 or len(traj_xy) == 0:
        return np.ones(len(xy), bool)
    cell = 1.0
    lo = np.minimum(xy.min(0), traj_xy.min(0)) - margin - 1
    size = (np.maximum(xy.max(0), traj_xy.max(0)) + margin + 1 - lo) / cell
    W, H = int(size[0]) + 1, int(size[1]) + 1
    m = np.zeros((H, W), bool)
    t = ((traj_xy - lo) / cell).astype(int)
    m[t[:, 1], t[:, 0]] = True
    m = _dilate(m, int(np.ceil(margin / cell)))
    p = ((xy - lo) / cell).astype(int)
    return m[p[:, 1], p[:, 0]]


def _raytrace(walls: np.ndarray, poses: np.ndarray, max_cells: int) -> np.ndarray:
    """从每个位姿格子往四周打射线,碰到墙停;回每格被穿过的次数。"""
    H, W = walls.shape
    passes = np.zeros((H, W), np.int32)
    ang = np.linspace(0, 2 * np.pi, RAYS, endpoint=False)
    ca, sa = np.cos(ang), np.sin(ang)
    steps = np.arange(1, max_cells)
    for px, py in poses:
        xs = (px + np.outer(ca, steps)).astype(int)
        ys = (py + np.outer(sa, steps)).astype(int)
        inside = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        for r in range(RAYS):
            xr, yr = xs[r][inside[r]], ys[r][inside[r]]
            if len(xr) == 0:
                continue
            hit = walls[yr, xr]
            stop = int(np.argmax(hit)) if hit.any() else len(xr)
            passes[yr[:stop], xr[:stop]] += 1
        if 0 <= py < H and 0 <= px < W:
            passes[py, px] += 1
    return passes


def render(points: np.ndarray, sensor_traj: np.ndarray, frames: Frames, *, res: float = RES_M,
           floor_clear: float = FLOOR_CLEAR_M, wall_top: float = WALL_TOP_M,
           traj_margin: float = TRAJ_MARGIN_M, max_range: float = MAX_RANGE_M) -> Grid:
    """点云(MOLA 系,(N,3))+ 建图时雷达的位置(MOLA 系,(M,3))→ 栅格(地图平面系)。"""
    q = level(np.asarray(points, float), frames)
    t = level(np.asarray(sensor_traj, float), frames)
    floor = _floor_height(q[:, 2], t[:, 2])
    wall = (q[:, 2] > floor + floor_clear) & (q[:, 2] < floor + wall_top)
    wall &= _near_traj(q[:, :2], t[:, :2], traj_margin)
    w = q[wall, :2]
    both = np.vstack([w, t[:, :2]]) if len(w) else t[:, :2]
    lo = both.min(0) - res
    hi = both.max(0) + res
    W = int((hi[0] - lo[0]) / res) + 2
    H = int((hi[1] - lo[1]) / res) + 2
    occ = np.zeros((H, W), np.int32)
    g = ((w - lo) / res).astype(int)
    np.add.at(occ, (g[:, 1], g[:, 0]), 1)
    pc = ((t[:, :2] - lo) / res).astype(int)
    key = (pc[:, 0] // POSE_STRIDE_CELLS) * 100000 + (pc[:, 1] // POSE_STRIDE_CELLS)
    _, idx = np.unique(key, return_index=True)
    poses = pc[np.sort(idx)]
    passes = _raytrace(occ >= 3, poses, int(max_range / res))
    ratio = occ / np.maximum(occ + passes, 1)
    occ_m = (occ >= 5) & (ratio > 0.25)
    free_m = (passes >= 1) & ~occ_m
    free_m = _dilate(_erode(free_m, 2), 2)                  # 开运算:去掉细扇形
    occ_m &= _dilate(free_m, int(0.5 / res))                # 只留贴着可通行区的障碍
    img = np.full((H, W), UNKNOWN, np.uint8)
    img[free_m] = FREE
    img[occ_m] = OCCUPIED
    known = img != UNKNOWN
    if known.any():                                         # 裁到有内容的范围,四周留 5 格
        ys, xs = np.where(known)
        y0, x0 = max(int(ys.min()) - 5, 0), max(int(xs.min()) - 5, 0)
        y1, x1 = min(int(ys.max()) + 6, H), min(int(xs.max()) + 6, W)
        img = img[y0:y1, x0:x1]
        lo = lo + np.array([x0, y0]) * res
    return Grid(image=np.flipud(img).copy(), origin=(float(lo[0]), float(lo[1])), res=res)


def write(grid: Grid, base: Path) -> tuple[Path, Path]:
    """写 ``<base>.pgm`` 与 ``<base>.yaml``(map_server 的格式,``mode: trinary``)。"""
    base = Path(base)
    pgm, yml = base.with_suffix(".pgm"), base.with_suffix(".yaml")
    h, w = grid.image.shape
    pgm.write_bytes(f"P5\n{w} {h}\n255\n".encode() + grid.image.tobytes())
    yml.write_text(f"image: {pgm.name}\nresolution: {grid.res}\n"
                   f"origin: [{grid.origin[0]:.3f}, {grid.origin[1]:.3f}, 0.0]\nnegate: 0\n"
                   f"occupied_thresh: 0.65\nfree_thresh: 0.196\nmode: trinary\n")
    return pgm, yml
