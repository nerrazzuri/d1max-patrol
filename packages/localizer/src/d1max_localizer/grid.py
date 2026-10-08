"""二维规划栅格(W09c1 决定 3):MOLA 建图的点云 + 建图时雷达走过的路 → ``floor.pgm`` +
``floor.yaml``(ROS map_server 的格式:黑 = 占据、白 = 可通行、灰 = 不知道)。

**按 ``frames.json`` 的平面画**:点云乘 :meth:`Frames.level_matrix`,x、y 就是地图平面坐标(跟定位器
报给代理的位置同一个系),z 是高度 —— 导航去的点跟定位报的位置才对得上。原型
``tools/lio/mola/render_floormap.py`` 自己拿 RANSAC 拟合地面定「上」、跟定位的平面不是一回事,这里
不用它那一套。

画法(照原型,现场验过能出干净的图):

- 地面高度**按走过的地方逐段算**(:class:`_Ground`):狗离地的高度不变,轨迹往下挪这么多就是脚下的
  地面,按 :data:`GROUND_CELL_M` 的粗格子顺着点云往四周推开 :data:`TRAJ_MARGIN_M`(坡地上整张图一个
  地面高度的话,上坡的地面会落进障碍层 —— 内审再议 14);离路线更远的点不要(远处玻璃的反射);
- 障碍 = 离当地地面 :data:`FLOOR_CLEAR_M` 到 :data:`WALL_TOP_M` 那一层。原型从 0.7 m 起(躲开狗自己的
  腿、近处的回波);有了真射线和挖身子,狗自己的回波清得掉,改成 0.4 m —— newdog2 上 0.7 m 起时那几个
  0.75 m 高的圆展台差一点就看不见了(当地地面高一点就没了),0.4 m 起都在、走过的路照样全可通行。比
  0.4 m 矮的东西(路牙、台阶)栅格上看不见;
- **看到过地面**(离当地地面 :data:`FLOOR_BAND_M` 以内有点)、又不是障碍的格子算可通行(开阔地上
  没有齐腰高的东西、射线没处打,光靠射线只有走过的那一条线 —— 内审阻断 1);
- 格子的「被穿过次数」与「障碍点数」按 log-odds 判占据 / 可通行(人、玻璃反射这种一会儿有一会儿没的,
  穿过的次数多就判可通行)。「被穿过」**有逐帧扫描就用真射线**(每一帧从雷达到这一帧打到的障碍点;
  :func:`render` 的 ``scans``):跟着狗走的人,他站过的地方在别的时刻被射线穿过,清得掉(W09c1 在
  newdog2 上踩到:人连成一道「墙」,走过的路被判成墙、另一侧全成了「没扫到」)。没有逐帧扫描就退回
  原型的模拟射线(从路线上的点往四周打,碰到墙停 —— 人那道「墙」清不掉);
- 狗身中心走过的路(``body_path``)两边 :data:`FOOTPRINT_M` 以内一律可通行:狗的身子在那儿待过;
- 可通行区做一次开运算(去掉透过没扫到的玻璃漏出去的细扇形);只留贴着可通行区的障碍(去掉悬在
  外面的鬼影)。

只依赖 numpy(狗上用 ROS 系统 Python 里 apt 装的那份);不要 scipy、PIL。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from d1max_localizer.frames import Frames

RES_M = 0.05
FLOOR_CLEAR_M = 0.4
WALL_TOP_M = 1.8
TRAJ_MARGIN_M = 12.0
GROUND_CELL_M = 1.0
GROUND_WINDOW_M = 0.4
GROUND_BIN_M = 0.05
GROUND_MIN_PTS = 10
FLOOR_BAND_M = (-0.15, 0.25)
MAX_RANGE_M = 12.0
RAYS = 540
POSE_STRIDE_CELLS = 6
#: 真射线:每帧按方位分这么多格,每格只打最近的那个障碍点(它就是这一帧看到的空地的边)。
SCAN_BINS = 1440
#: 真射线在打到的点之前这么远就停(那一格归「打到」,不归「穿过」)。
RAY_STOP_SHORT_M = 0.1
#: 离雷达这么近的点不打射线(狗自己的身子)。
MIN_RANGE_M = 0.3
#: 按三维判「穿过」(W34:2026-10-08 C40221 双雷达地图桌子、椅子大多没了 —— 射线从桌面底下穿过去
#: 打到后面的墙,二维上算穿过了桌子那几格):有障碍点的格子,射线在那一格的高度落在这一格障碍点的高度
#: 范围(上下各放这么多)里才算穿过;从底下钻过去、从上面越过去的不算。没障碍点的格子照旧。
RAY_HEIGHT_TOL_M = 0.1
#: 狗身中心线两边这么宽一律可通行。
FOOTPRINT_M = 0.25
FREE, UNKNOWN, OCCUPIED = 254, 205, 0
#: 栅格最多多少格(站点预览的上限也是 2500 万像素);超了一格放粗一倍,粗到 :data:`MAX_RES_M`
#: 还超就报错。
MAX_CELLS = 25_000_000
MAX_RES_M = 0.2


class GridTooLarge(ValueError):
    """栅格大到放粗了也装不下(多半是轨迹发散了)。"""


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


class _Ground:
    """当地地面高度:狗离地的高度(雷达高度的中位数 − 整体地面)不变,轨迹上每一处往下挪这么多就是脚下的
    地面;按粗格子平均(再按点云修:轨迹的高度会漂),再一圈一圈往外推 ``margin`` 米:新的一格先取已知
    邻格的平均当预估,再按格子里的点修(:meth:`_refine`,坡顺着点云往外走)。推不到的地方没有地面。"""

    def __init__(self, sensor: np.ndarray, floor: float, margin: float,
                 points: np.ndarray) -> None:
        clearance = float(np.median(sensor[:, 2])) - floor
        c = GROUND_CELL_M
        self.lo = sensor[:, :2].min(0) - margin - 2 * c
        size = ((sensor[:, :2].max(0) + margin + 2 * c - self.lo) / c).astype(int) + 1
        W, H = int(size[0]), int(size[1])
        tot, cnt = np.zeros((H, W)), np.zeros((H, W))
        k = ((sensor[:, :2] - self.lo) / c).astype(int)
        np.add.at(tot, (k[:, 1], k[:, 0]), sensor[:, 2] - clearance)
        np.add.at(cnt, (k[:, 1], k[:, 0]), 1)
        known = cnt > 0
        z = np.where(known, tot / np.maximum(cnt, 1), 0.0)
        pk = np.floor((points[:, :2] - self.lo) / c).astype(int)
        inside = (pk[:, 0] >= 0) & (pk[:, 0] < W) & (pk[:, 1] >= 0) & (pk[:, 1] < H)
        self._pz, self._key = points[inside, 2], pk[inside, 1] * W + pk[inside, 0]
        z = np.where(known, self._refine(z, known), z)     # 走过的格子也按点云修(轨迹的高度会漂)
        for _ in range(int(np.ceil(margin / c))):
            zs, ns = np.zeros_like(z), np.zeros_like(z)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx or dy:
                        m = np.roll(np.roll(known, dy, 0), dx, 1)
                        zs += np.where(m, np.roll(np.roll(z, dy, 0), dx, 1), 0.0)
                        ns += m
            new = ~known & (ns > 0)
            new[[0, -1], :] = new[:, [0, -1]] = False       # roll 绕回来的边不算
            prior = np.where(new, zs / np.maximum(ns, 1), 0.0)
            z = np.where(new, self._refine(prior, new), z)
            known |= new
        del self._pz, self._key
        self.z, self.known = z, known

    def _refine(self, prior: np.ndarray, cells: np.ndarray) -> np.ndarray:
        """``cells`` 这些格子:离预估 :data:`GROUND_WINDOW_M` 以内的点按 :data:`GROUND_BIN_M` 分层,
        点够多就取最密的那一层(地面是一大片水平的点;取平均的话,墙从地面起那一截、矮台子的点会把它
        往上拉 —— 内审后突变抓到的)。"""
        H, W = prior.shape
        sel = cells.ravel()[self._key]
        ks, zs = self._key[sel], self._pz[sel]
        base = prior.ravel() - GROUND_WINDOW_M
        nb = int(round(2 * GROUND_WINDOW_M / GROUND_BIN_M))
        b = np.floor((zs - base[ks]) / GROUND_BIN_M).astype(int)
        win = (b >= 0) & (b < nb)
        hist = np.bincount(ks[win] * nb + b[win], minlength=H * W * nb).reshape(H * W, nb)
        top = hist.argmax(1)
        near = win & (np.abs(b - top[ks]) <= 1)             # 最密那层上下各一层里的点取平均
        n = np.bincount(ks[near], minlength=H * W)
        m = np.bincount(ks[near], weights=zs[near], minlength=H * W)
        est = np.where((hist.sum(1) >= GROUND_MIN_PTS) & (n > 0), m / np.maximum(n, 1),
                       prior.ravel())
        return est.reshape(H, W)

    def rel(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """点(地图平面系)离当地地面多高;回(高度, 有没有地面)。"""
        k = np.floor((q[:, :2] - self.lo) / GROUND_CELL_M).astype(int)
        H, W = self.z.shape
        ok = (k[:, 0] >= 0) & (k[:, 0] < W) & (k[:, 1] >= 0) & (k[:, 1] < H)
        ok[ok] = self.known[k[ok, 1], k[ok, 0]]
        h = np.full(len(q), np.nan)
        h[ok] = q[ok, 2] - self.z[k[ok, 1], k[ok, 0]]
        return h, ok


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


def _raytrace_scans(scans: Iterable[tuple[np.ndarray, np.ndarray]], frames: Frames,
                    lo: np.ndarray, shape: tuple[int, int], res: float, band: tuple[float, float],
                    max_range: float, ground: _Ground,
                    occ_h: tuple[np.ndarray, np.ndarray] | None = None
                    ) -> tuple[np.ndarray, int]:
    """真射线:每一帧从雷达到这一帧障碍层里的点(每个方位格最近的那个),路过的格子各记一次。
    ``occ_h``:每格障碍点离当地地面的(最低, 最高)(没有障碍点是 nan);给了就按三维判穿过
    (:data:`RAY_HEIGHT_TOL_M`)。回(每格被穿过的次数, 用了几帧)。"""
    H, W = shape
    passes = np.zeros(H * W, np.int32)
    step = res / 2
    used = 0
    lo_h = hi_h = None
    if occ_h is not None:
        lo_h, hi_h = occ_h[0].ravel(), occ_h[1].ravel()
    for origin, pts in scans:
        o3 = level(np.asarray(origin, float).reshape(1, 3), frames)
        o = o3[0, :2]
        oh, ook = ground.rel(o3)
        q = level(np.asarray(pts, float), frames)
        h, ok = ground.rel(q)
        keep = ok & (h > band[0]) & (h < band[1])
        q, h = q[keep, :2] - o, h[keep]
        r = np.hypot(q[:, 0], q[:, 1])
        ok = (r > MIN_RANGE_M) & (r < max_range)
        q, r, h = q[ok], r[ok], h[ok]
        used += 1
        if not len(r):
            continue
        b = ((np.arctan2(q[:, 1], q[:, 0]) + np.pi) / (2 * np.pi) * SCAN_BINS).astype(int) \
            % SCAN_BINS
        order = np.lexsort((r, b))
        b, r, q, h = b[order], r[order], q[order], h[order]
        first = np.r_[True, b[1:] != b[:-1]]
        q, r, h = q[first], r[first], h[first]
        n = np.maximum(((r - RAY_STOP_SHORT_M) / step).astype(int), 1)
        ray = np.repeat(np.arange(len(r)), n)
        k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        frac = k * step / r[ray]
        xy = o + q[ray] * frac[:, None]
        c = ((xy - lo) / res).astype(int)
        inside = (c[:, 0] >= 0) & (c[:, 0] < W) & (c[:, 1] >= 0) & (c[:, 1] < H)
        if lo_h is not None and bool(ook[0]):
            # 三维:射线在这一格的高度(雷达高度到打到的点的高度之间按距离插)要落在这一格障碍点的
            # 高度范围里;从桌面底下钻过去、从矮东西上面越过去的,不算穿过
            zr = oh[0] + (h[ray] - oh[0]) * frac
            cc = np.where(inside, c[:, 1] * W + c[:, 0], 0)
            lo_c, hi_c = lo_h[cc], hi_h[cc]
            has = np.isfinite(lo_c)
            through = ~has | ((zr >= lo_c - RAY_HEIGHT_TOL_M) & (zr <= hi_c + RAY_HEIGHT_TOL_M))
            inside &= through
        cell = c[inside, 1] * W + c[inside, 0]
        # 一条射线过一格只算一次(步长半格,同一格会落两次)
        key = np.unique(ray[inside].astype(np.int64) * (H * W) + cell)
        np.add.at(passes, key % (H * W), 1)
    return passes.reshape(H, W), used


def render(points: np.ndarray, sensor_traj: np.ndarray, frames: Frames, *, res: float = RES_M,
           floor_clear: float = FLOOR_CLEAR_M, wall_top: float = WALL_TOP_M,
           traj_margin: float = TRAJ_MARGIN_M, max_range: float = MAX_RANGE_M,
           scans: Iterable[tuple[np.ndarray, np.ndarray]] | None = None,
           body_path: np.ndarray | None = None) -> Grid:
    """点云(MOLA 系,(N,3))+ 建图时雷达的位置(MOLA 系,(M,3))→ 栅格(地图平面系)。

    ``scans``:逐帧扫描 ``(雷达位置, 这一帧的点)``(都在 MOLA 系;给了就用真射线,见模块说明);
    ``body_path``:狗身中心走过的路(地图平面,(K,2)),两边 :data:`FOOTPRINT_M` 一律可通行。"""
    q = level(np.asarray(points, float), frames)
    t = level(np.asarray(sensor_traj, float), frames)
    ground = _Ground(t, _floor_height(q[:, 2], t[:, 2]), traj_margin, q)
    h, ok = ground.rel(q)
    wall = ok & (h > floor_clear) & (h < wall_top)
    flr = ok & (h > FLOOR_BAND_M[0]) & (h < FLOOR_BAND_M[1])
    w, fl = q[wall, :2], q[flr, :2]
    both = np.vstack([w, fl, t[:, :2]])
    span = both.max(0) - both.min(0)
    while (span[0] / res + 4) * (span[1] / res + 4) > MAX_CELLS:      # 太大就放粗(内审小 9)
        res *= 2
        if res > MAX_RES_M + 1e-9:
            raise GridTooLarge(f"栅格 {span[0]:.0f} × {span[1]:.0f} m,按 {MAX_RES_M:g} m 一格"
                               f"也超过 {MAX_CELLS} 格:轨迹可能发散了")
    lo = both.min(0) - res
    hi = both.max(0) + res
    W = int((hi[0] - lo[0]) / res) + 2
    H = int((hi[1] - lo[1]) / res) + 2
    occ = np.zeros((H, W), np.int32)
    g = ((w - lo) / res).astype(int)
    np.add.at(occ, (g[:, 1], g[:, 0]), 1)
    # 每格障碍点的高度范围(三维判穿过用,W34)
    occ_lo = np.full(H * W, np.inf)
    occ_hi = np.full(H * W, -np.inf)
    gk = g[:, 1] * W + g[:, 0]
    np.minimum.at(occ_lo, gk, h[wall])
    np.maximum.at(occ_hi, gk, h[wall])
    empty = ~np.isfinite(occ_lo)
    occ_lo[empty], occ_hi[empty] = np.nan, np.nan
    seen = np.zeros((H, W), bool)
    gf = ((fl - lo) / res).astype(int)
    seen[gf[:, 1], gf[:, 0]] = True
    seen = _erode(_dilate(seen, 1), 1)                      # 闭运算:远处点稀,补上小洞
    if scans is not None:
        passes, _ = _raytrace_scans(scans, frames, lo, (H, W), res, (floor_clear, wall_top),
                                    max_range, ground,
                                    occ_h=(occ_lo.reshape(H, W), occ_hi.reshape(H, W)))
    else:
        pc = ((t[:, :2] - lo) / res).astype(int)
        key = (pc[:, 0] // POSE_STRIDE_CELLS) * 100000 + (pc[:, 1] // POSE_STRIDE_CELLS)
        _, idx = np.unique(key, return_index=True)
        poses = pc[np.sort(idx)]
        passes = _raytrace(occ >= 3, poses, int(max_range / res))
    ratio = occ / np.maximum(occ + passes, 1)
    occ_m = (occ >= 5) & (ratio > 0.25)
    free_m = ((passes >= 1) | seen) & ~occ_m
    free_m = _dilate(_erode(free_m, 2), 2)                  # 开运算:去掉细扇形
    if body_path is not None and len(body_path):            # 狗的身子待过的地方
        bc = ((np.asarray(body_path, float)[:, :2] - lo) / res).astype(int)
        bc = bc[(bc[:, 0] >= 0) & (bc[:, 0] < W) & (bc[:, 1] >= 0) & (bc[:, 1] < H)]
        body = np.zeros((H, W), bool)
        body[bc[:, 1], bc[:, 0]] = True
        body = _dilate(body, int(round(FOOTPRINT_M / res)))
        free_m |= body
        occ_m &= ~body
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
