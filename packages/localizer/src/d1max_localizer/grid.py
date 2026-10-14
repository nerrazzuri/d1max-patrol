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
- 格子的「被穿过次数」与「障碍点数」按比例判占据 / 可通行(障碍点至少 5 个、占两样加起来的四分之一
  以上判占据;人、玻璃反射这种一会儿有一会儿没的,穿过的次数多就判可通行)。「被穿过」**有逐帧扫描
  就用真射线**(每一帧从雷达到这一帧打到的障碍点;:func:`render` 的 ``scans``):跟着狗走的人,他站
  过的地方在别的时刻被射线穿过,清得掉(W09c1 在 newdog2 上踩到:人连成一道「墙」,走过的路被判成
  墙、另一侧全成了「没扫到」)。真射线停在打到的点前 :data:`RAY_STOP_SHORT_M`,打到的那一格不算
  穿过;按高度判穿过(:data:`RAY_HEIGHT_TOL_M`,桌面底下钻过去的不算)。没有逐帧扫描就退回原型的
  模拟射线(从路线上的点往四周打,碰到墙停 —— 人那道「墙」清不掉);
- 狗身中心走过的路(``body_path``)两边 :data:`FOOTPRINT_M` 以内一律可通行:狗的身子在那儿待过;
- 可通行区做一次开运算(去掉透过没扫到的玻璃漏出去的细扇形;宽的扇形见最后一条);只留贴着可通行区
  的障碍(去掉悬在外面的鬼影);
- **墙只画雷达看得见的那一面**(:func:`_surface`,2026-10-14):从可通行区横竖看过去都得先穿过一层以上
  占据才看得到的格(点云抖出来的厚度、墙背面、家具块里头)改成「不知道」,墙只剩朝空地的**一层**
  (:data:`SURFACE_LAYERS`)。办公室图墙厚 0.20 m(中位数)、p90 0.55 m → 0.05 m、0.05–0.10 m。只把
  占据改成「不知道」,可通行的格一格不动 —— 规划(``d1max_contract.planning.costmap``)、拦截点、站点
  核点位只认可通行,两样都当挡;
- **透过玻璃、开口看到的假空地改成「不知道」**(:func:`_see_through`,2026-10-14,现场 3d.18):雷达
  穿过玻璃看到外面的墙和地面,玻璃本身没有点(或者只有零星几点),外面成了一大片扇形的「可通行」,还跟
  屋里连着 —— 规划会从玻璃穿出去。真射线顺带记下每格是怎么被看到的(:class:`_Seen`):每一帧在近处面
  上找「开口」(:func:`_openings`:两段够长的面之间、里面看到的都比两边远的口子 —— 玻璃两边的墙、门
  框),开口线后面看到的算「透过开口」;还记下从哪些方向看到过。离狗走过的路 :data:`PROTECT_M` 以外,
  ① 八成以上的帧是透过开口看到的(切在开口线上),② 离路 :data:`VIEW_FAR_M` 以外、只从 45° 以内的
  方向看到过的(从窄口、开口认不出来的玻璃远远看过去的扇形),改成「不知道」;再去掉跟路不连着的碎块、
  补回单格的洞。**只把可通行改成「不知道」**:占据一格不动(玻璃外面的墙还画着,悬在灰里),不添可通行。
  代价:狗没进去、只从门口看过的屋子、只从一个方向远远看过的角落也成了「不知道」(往保守那边错);没有
  逐帧扫描(模拟射线)时不做。10-13 办公室图(前雷达)透出去的假空地(离路 3 m 以外、连着路的)64 m² →
  3.7 m²,规划走得到的面积 42.49 → 42.40 m²(离路 2 m 以内一格没少);台式机画图多 16–22% 的时间。
- **这条规则去掉的格子另记一份显示用的掩码**(M9,用户 2026-10-14「只许狗扫过的地方,别的淡淡
  地显示」):``Grid.seen``(跟图一样大、一样的行序),:func:`write_seen` 写成 ``seen.pbm``
  (``d1max_contract.maps`` 的 ``SEEN_FILE``)。**只管显示**:``floor.pgm`` 里那些格照样是「不知道」、
  规划照样当挡;站点画平面图时画成淡淡的地面(「看到过、狗没走过」),客户看得出房子的样子。掩码跟
  可通行、占据一格都不重叠;一格都没有(没去掉、模拟射线)就是 ``None``、不写文件。裁图时掩码的格子
  也算「有内容」(不然玻璃外面的那一片被裁掉,掩码跟图对不上)。

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
#: 真射线最后一个采样点离打到的点至少这么远(采样每半格一个,实际停在 0.075–0.1 m);**打到的那一格
#: 一律不算「穿过」**(那一格归「打到」)。原来写 0.1 m、采样还少算一步,实际停在 0.125–0.15 m:墙面
#: 前两格从来没有射线正着穿过,那里零星的噪点(测距拖尾、多次反射)全判成占据,墙往空地那边胖一格、
#: 边上毛糙(2026-10-14)。停近了:墙面前隔一格的噪点有射线穿过、清得掉。没停到半格 / 一格:试过,
#: 墙一样薄(墙厚是 :data:`SURFACE_LAYERS` 管的),可门框的头、桌腿这种细东西多被旁边打到的射线
#: 「穿过」掉一格(合成的门两边各缺一格;C40221 双雷达图上一处 8 格的桌椅)。打到的那一格单独排除:
#: 点落在格子深处时停下前那一步还在这一格里,墙格会被自己的射线「穿过」(玻璃、细杆这种点少的格
#: 就清掉了)。
RAY_STOP_SHORT_M = 0.075
#: 离雷达这么近的点不打射线(狗自己的身子)。
MIN_RANGE_M = 0.3
#: 按三维判「穿过」(W34:2026-10-08 C40221 双雷达地图桌子、椅子大多没了 —— 射线从桌面底下穿过去
#: 打到后面的墙,二维上算穿过了桌子那几格):有障碍点的格子,射线在那一格的高度落在这一格障碍点的高度
#: 范围(上下各放这么多)里才算穿过;从底下钻过去、从上面越过去的不算。没障碍点的格子照旧。
RAY_HEIGHT_TOL_M = 0.1
#: 狗身中心线两边这么宽一律可通行。
FOOTPRINT_M = 0.25
#: 墙只画雷达看得见的那一面(2026-10-14,:func:`_surface`):从可通行区那边看过去,**横竖哪条直线都要
#: 先穿过超过这么多个占据格**才看得到的格(点云抖出来的厚度、墙的背面、家具块的里头)改成「不知道」
#: —— 雷达只看得到墙朝着它的那一面;办公室图上墙厚大半是这一面后面的点(射线打不进去,比例永远是
#: 1)。规划、拦截点把「不知道」跟占据一样当挡,这一步**只改图、不改哪里能走**。**一层**(用户
#: 2026-10-14 嫌两层(约 10 cm)还厚):五张实测图墙厚中位数 0.05 m、p90 0.05–0.10 m,可通行格跟两层
#: 时逐格一样;约 1% 的墙格旁边缺一格(墙面跨在两格分界上、两格各留一半),只是画法,规划照样当挡。
SURFACE_LAYERS = 1
#: 往里、往外最多看这么远(米):只动跟可通行区连着(只走占据格)、这么远以内的格;这么远以内一条直线
#: 都看不到可通行区的不动。
SURFACE_REACH_M = 2.0
# ---- 透过玻璃、开口看到的假空地(2026-10-14,:func:`_see_through`;现场 3d.18)
#: 一帧里的「开口」(:func:`_openings`):近处面(每个方位格最近的障碍点)上两段面之间的口子,口子里
#: 看到的东西都比两边远。两边各得是一段至少这么长的面(墙、柜子、玻璃框;人、椅子、桌腿、窗棂这种
#: 零星的不算 —— 不算的话屋里一堆家具之间全成了「开口」:C40221 双雷达合并图上走过的拐角丢过 17% 的
#: 可走面积;0.4 m 时 10-08 办公室图大屋右上角(一件家具跟右墙之间)整块成了「不知道」)。
OPEN_FLANK_M = 0.6
#: 近处面上相邻两点离这么近算同一段面。
OPEN_SURFACE_GAP_M = 0.25
#: 口子最宽这么宽(门、一扇玻璃、两根柱子之间;更宽的大玻璃面认不出来,归 :data:`VIEW_FAR_M` 那条)。
OPEN_MAX_M = 3.5
#: 口子另一边要比这一边远这么多倍、且至少远这么多米,才算「透过去看到的」。
OPEN_FAR_RATIO = 1.3
OPEN_FAR_M = 0.5
#: 口子里打到的点比开口线近这么多以上,就是口子前面挡着东西,不算开口(玻璃上的回波、窗框落在线上
#: 前后这么一点的不算挡)。
OPEN_IN_FRONT_M = 0.3
#: 口子占的视角超过这么多度、或者开口线离雷达不到这么远的不算:狗站在门口、两张桌子中间时,看出去的
#: 东西就是从那儿直接看到的。
OPEN_MAX_DEG = 75.0
OPEN_MIN_DIST_M = 1.0
#: 一格被看到的帧里,透过开口看到的占这么多以上 → 「只透过开口看到过」。
THROUGH_SHARE = 0.8
#: 看到一格的方向分这么多份(每份 11.25°)。
VIEW_BINS = 32
#: 离狗走过的路(沿可通行格量)超过这么远、所有看到它的方向挤在这么多份(45°)以内的格 →「不知道」:
#: 从窄口远远看过去的扇形。屋里真的空地要么离路近、要么从路上好些地方都看得到。
VIEW_FAR_M = 3.0
VIEW_CONE_BINS = 4
#: 离狗身中心走过的路(沿可通行格量)这么近的一律不动:规划的致命半径 0.57 m 再加一个规划格,狗走过的
#: 地方规划起来跟原来一样宽。
PROTECT_M = 0.8
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
    #: 显示用的「看到过、狗没走过」(M9,见模块说明):跟 ``image`` 一样大的真假;没有是 ``None``。
    seen: np.ndarray | None = None

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


class _Seen:
    """逐帧记下每格是怎么被看到的(:func:`_see_through` 用):直接看到的帧数、透过开口看到的帧数
    (:func:`_openings`;一帧里一格只记一次),和从哪些方向看到过(:data:`VIEW_BINS` 份,一位一份)。
    「看到」= 射线穿过(跟 ``passes`` 同一批采样)或者看到了这一格的地面。"""

    def __init__(self, cells: int) -> None:
        self.direct = np.zeros(cells, np.int32)
        self.through = np.zeros(cells, np.int32)
        self.dirs = np.zeros(cells, np.uint32)

    def vote(self, cell: np.ndarray, thr: np.ndarray) -> None:
        """一帧看到的格(可以重复),``thr``:是不是透过开口看到的。"""
        self.direct[cell[~thr]] += 1                        # 重复的下标只加一次:一帧一票
        self.through[cell[thr]] += 1

    def look(self, cell: np.ndarray, sector: np.ndarray) -> None:
        """一帧看到的格和从哪一份方向看到的(``sector`` 从小到大排好)。"""
        cuts = np.searchsorted(sector, np.arange(VIEW_BINS + 1))
        for s in range(VIEW_BINS):
            if cuts[s + 1] > cuts[s]:
                self.dirs[cell[cuts[s]:cuts[s + 1]]] |= np.uint32(1 << s)

    def scan(self, o: np.ndarray, floor: np.ndarray, s_open: np.ndarray, lo: np.ndarray,
             shape: tuple[int, int], res: float, max_range: float,
             ray_cell: np.ndarray, ray_thr: np.ndarray, ray_sector: np.ndarray) -> None:
        """一帧:射线穿过的格(``ray_*``,方向份从小到大)+ 地面点 ``floor``(相对雷达),地面点远过
        这个方位的开口线(``s_open``)就是透过开口看到的。"""
        H, W = shape
        fr = np.hypot(floor[:, 0], floor[:, 1])
        near = (fr > MIN_RANGE_M) & (fr < max_range)
        floor, fr = floor[near], fr[near]
        fb = ((np.arctan2(floor[:, 1], floor[:, 0]) + np.pi) / (2 * np.pi) * SCAN_BINS
              ).astype(int) % SCAN_BINS
        c = ((floor + o - lo) / res).astype(int)
        ins = (c[:, 0] >= 0) & (c[:, 0] < W) & (c[:, 1] >= 0) & (c[:, 1] < H)
        fcell, fb, fthr = c[ins, 1] * W + c[ins, 0], fb[ins], (fr > s_open[fb])[ins]
        self.vote(np.r_[ray_cell, fcell], np.r_[ray_thr, fthr])
        self.look(ray_cell, ray_sector)
        order = np.argsort(fb, kind="stable")
        self.look(fcell[order], fb[order] * VIEW_BINS // SCAN_BINS)


#: 找开口时「回到近处」的距离分档(米):一档一档往外,每档多 15%。
_LADDER = 0.3 * 1.15 ** np.arange(28)


def _openings(b: np.ndarray, r: np.ndarray, q: np.ndarray) -> np.ndarray:
    """一帧的近处面(每个方位格最近的障碍点:格号 ``b`` 从小到大、距离 ``r``、相对雷达的平面坐标
    ``q``)上的开口 → 每个方位格从雷达到开口线的距离(没有开口是 inf)。

    开口:一段面的头(尾)a,往后(往前)第一个「回到近处」的点 c(距离不超过 a 的
    :data:`OPEN_FAR_RATIO` 倍、也不超过 a 远 :data:`OPEN_FAR_M`);a、c 都得在一段至少
    :data:`OPEN_FLANK_M` 长的面上,a、c 之间不超过 :data:`OPEN_MAX_M`、视角不超过
    :data:`OPEN_MAX_DEG`、开口线离雷达至少 :data:`OPEN_MIN_DIST_M`;夹在中间的方位格里打到的点不许比
    线段 ac 近 :data:`OPEN_IN_FRONT_M` 以上(口子前面挡着东西的不是开口,比如远处两段墙中间一根近处的
    柱子)。夹在中间的方位格到线段 ac 的距离就是开口线。玻璃没有回波(或者只有零星几点、窗框)时,
    玻璃两边的墙就是 a、c。"""
    nb = SCAN_BINS
    out = np.full(nb, np.inf)
    n = len(r)
    if n < 3:
        return out
    gap = np.hypot(*(np.roll(q, -1, 0) - q).T)              # i → i+1(首尾相接)
    cont = gap <= OPEN_SURFACE_GAP_M
    if cont.all():
        return out
    s0 = int(np.argmin(cont)) + 1                           # 转一下,让 0 号是一段面的头
    rot = (np.arange(n) + s0) % n
    seg = np.r_[0, np.cumsum(~cont[rot][:-1])]
    length = np.bincount(seg, weights=np.where(cont[rot], gap[rot], 0.0))
    flank = np.zeros(n, bool)
    flank[rot] = length[seg] >= OPEN_FLANK_M
    near = np.where(flank, r, np.inf)
    near2 = np.r_[near, near]
    idx = np.arange(2 * n)
    big = 4 * n
    fwd = np.array([np.minimum.accumulate(np.where(near2 <= t, idx, big)[::-1])[::-1]
                    for t in _LADDER])                      # 第 i 个起往后第一个 ≤ t 的
    bwd = np.array([np.maximum.accumulate(np.where(near2 <= t, idx, -1)) for t in _LADDER])
    lvl = np.clip(np.searchsorted(_LADDER, np.maximum(r * OPEN_FAR_RATIO, r + OPEN_FAR_M),
                                  side="right") - 1, 0, len(_LADDER) - 1)
    ends = np.flatnonzero(flank & ~cont)                    # 一段面的尾,往后找
    heads = np.flatnonzero(flank & np.roll(~cont, 1))       # 一段面的头,往前找
    a = np.r_[ends, bwd[lvl[heads], heads + n - 1]]
    c = np.r_[fwd[lvl[ends], ends + 1], heads + n]
    ok = (a >= 0) & (c < 2 * n)
    a, c = a[ok], c[ok]
    b2, q2 = np.r_[b, b + nb], np.r_[q, q]
    span = b2[c] - b2[a]
    qa, d = q2[a], q2[c] - q2[a]
    t0 = np.clip(-(qa * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    ok = (span >= 2) & (span <= OPEN_MAX_DEG / 360.0 * nb) \
        & (np.hypot(d[:, 0], d[:, 1]) <= OPEN_MAX_M) \
        & (np.hypot(*(qa + d * t0[:, None]).T) >= OPEN_MIN_DIST_M)
    if not ok.any():
        return out
    a, qa, d, span = a[ok], qa[ok], d[ok], span[ok]
    m = span - 1                                            # 夹在中间的方位格
    p = np.repeat(np.arange(len(a)), m)
    bins = b2[a][p] + 1 + np.arange(m.sum()) - np.repeat(np.cumsum(m) - m, m)
    ang = (bins + 0.5) / nb * 2 * np.pi - np.pi
    ux, uy = np.cos(ang), np.sin(ang)
    A, D = qa[p], d[p]
    den = ux * D[:, 1] - uy * D[:, 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (A[:, 0] * D[:, 1] - A[:, 1] * D[:, 0]) / den   # 射线 s·u 跟线段 A + t·D 的交点
        t = (A[:, 0] * uy - A[:, 1] * ux) / den
    hit = np.isfinite(s) & (s > 0) & (t >= -0.01) & (t <= 1.01)
    bins, s, p = bins[hit] % nb, s[hit], p[hit]
    rb = np.full(nb, np.inf)
    rb[b] = r
    blocked = np.bincount(p[rb[bins] < s - OPEN_IN_FRONT_M], minlength=len(a)) > 0
    keep = ~blocked[p]                                      # 口子里有比开口线近的东西:不是开口
    bins, s = bins[keep], s[keep]
    if not len(bins):
        return out
    order = np.lexsort((s, bins))                           # 一格有几条开口线取最近的
    bins, s = bins[order], s[order]
    first = np.r_[True, bins[1:] != bins[:-1]]
    out[bins[first]] = s[first]
    return out


def _raytrace_scans(scans: Iterable[tuple[np.ndarray, np.ndarray]], frames: Frames,
                    lo: np.ndarray, shape: tuple[int, int], res: float, band: tuple[float, float],
                    max_range: float, ground: _Ground,
                    occ_h: tuple[np.ndarray, np.ndarray] | None = None,
                    seen: _Seen | None = None) -> tuple[np.ndarray, int]:
    """真射线:每一帧从雷达到这一帧障碍层里的点(每个方位格最近的那个),路过的格子各记一次。
    ``occ_h``:每格障碍点离当地地面的(最低, 最高)(没有障碍点是 nan);给了就按三维判穿过
    (:data:`RAY_HEIGHT_TOL_M`)。``seen``:给了就顺带记下每格是直接看到还是透过开口看到、从哪些方向
    看到(:class:`_Seen`)。回(每格被穿过的次数, 用了几帧)。"""
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
        q_all = level(np.asarray(pts, float), frames)
        h_all, ok_all = ground.rel(q_all)
        keep = ok_all & (h_all > band[0]) & (h_all < band[1])
        q, h = q_all[keep, :2] - o, h_all[keep]
        r = np.hypot(q[:, 0], q[:, 1])
        ok = (r > MIN_RANGE_M) & (r < max_range)
        q, r, h = q[ok], r[ok], h[ok]
        used += 1
        floor = None
        if seen is not None:
            fl = ok_all & (h_all > FLOOR_BAND_M[0]) & (h_all < FLOOR_BAND_M[1])
            floor = q_all[fl, :2] - o
        if not len(r):
            if seen is not None:
                none = np.zeros(0, int)
                seen.scan(o, floor, np.full(SCAN_BINS, np.inf), lo, (H, W), res, max_range,
                          none, none.astype(bool), none)
            continue
        b = ((np.arctan2(q[:, 1], q[:, 0]) + np.pi) / (2 * np.pi) * SCAN_BINS).astype(int) \
            % SCAN_BINS
        order = np.lexsort((r, b))
        b, r, q, h = b[order], r[order], q[order], h[order]
        first = np.r_[True, b[1:] != b[:-1]]
        b, q, r, h = b[first], q[first], r[first], h[first]
        # 采样点 0, step, 2·step …,最后一个离打到的点不近于 RAY_STOP_SHORT_M
        n = np.maximum(np.floor((r - RAY_STOP_SHORT_M) / step).astype(int) + 1, 1)
        ray = np.repeat(np.arange(len(r)), n)
        k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        frac = k * step / r[ray]
        xy = o + q[ray] * frac[:, None]
        c = ((xy - lo) / res).astype(int)
        end = ((o + q - lo) / res).astype(int)              # 打到的点那一格:归「打到」
        inside = (c[:, 0] >= 0) & (c[:, 0] < W) & (c[:, 1] >= 0) & (c[:, 1] < H) \
            & ((c[:, 0] != end[ray, 0]) | (c[:, 1] != end[ray, 1]))
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
        if seen is not None:
            s_open = _openings(b, r, q)
            ri = ray[inside]
            seen.scan(o, floor, s_open, lo, (H, W), res, max_range, cell,
                      k[inside] * step > s_open[b[ri]], b[ri] * VIEW_BINS // SCAN_BINS)
    return passes.reshape(H, W), used


def _shifted(m: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """``out[y, x] = m[y - dy, x - dx]``,出界的是 False。"""
    h, w = m.shape
    out = np.zeros_like(m)
    if abs(dy) < h and abs(dx) < w:
        out[max(dy, 0):h + min(dy, 0), max(dx, 0):w + min(dx, 0)] = \
            m[max(-dy, 0):h + min(-dy, 0), max(-dx, 0):w + min(-dx, 0)]
    return out


def _surface(occ: np.ndarray, free: np.ndarray, res: float) -> np.ndarray:
    """墙只画看得见的那一面(:data:`SURFACE_LAYERS`):回该改成「不知道」的占据格 —— 两样都满足:

    - 跟可通行区连着:从可通行区只走占据格(四邻接),超过 :data:`SURFACE_LAYERS` 步才到,
      :data:`SURFACE_REACH_M` 以内;
    - 挡在后面:从它沿上下左右四条直线往外看(「不知道」的格看得穿),够得着可通行区的那几条,每条
      都先穿过超过 :data:`SURFACE_LAYERS` 个占据格(算它自己)。不看斜线:墙面毛糙,斜线从相邻两列
      的凹凸之间钻出去,直墙上会留下一列列三格厚的(斜墙按横竖数本来就更厚,剥完也还是连着的)。

    隔着几格「不知道」看得到可通行区的(擦着看过去的走廊墙朝走廊的那面 —— 2026-10-14 只留贴着可通行区
    那一层的试法丢过一整段这样的墙)、只走占据格够不着的(另一堵墙、隔着一道缝的重影)、哪条直线都看
    不到可通行区的,都不动。"""
    n = max(int(round(SURFACE_REACH_M / res)), SURFACE_LAYERS + 1)
    reached = free
    deep = np.zeros(occ.shape, bool)
    for i in range(1, n + 1):                               # 只走占据格,第 i 步够着的
        new = _dilate(reached, 1) & occ & ~reached
        if not new.any():
            break
        if i > SURFACE_LAYERS:
            deep |= new
        reached = reached | new
    if not deep.any():
        return deep
    seen = np.zeros(occ.shape, bool)                        # 有条直线穿过不多于那么多层就到可通行区
    reach = np.zeros(occ.shape, bool)                       # 有条直线够得着可通行区
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        found = np.zeros(occ.shape, bool)
        crossed = np.zeros(occ.shape, np.int32)
        for k in range(n + 1):                              # 看 c − k·方向 那一格
            hit = _shifted(free, k * dy, k * dx) & ~found
            seen |= hit & (crossed <= SURFACE_LAYERS)
            reach |= hit
            found |= hit
            crossed += _shifted(occ, k * dy, k * dx) & ~found
    return deep & reach & ~seen


def _geodesic(seed: np.ndarray, within: np.ndarray, steps: int) -> np.ndarray:
    """从 ``seed`` 出发、只走 ``within`` 的格、走 ``steps`` 步以内够得着的格(四邻接、八邻接隔一步
    换着走,近似圆:正方向 ``steps`` 格、斜方向多约 6%)。"""
    out = seed & within
    for i in range(steps):
        g = _dilate(out, 1)
        if i % 2:
            for dy, dx in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                g |= _shifted(out, dy, dx)
        g &= within
        if (g == out).all():
            break
        out = g
    return out


def _connected(mask: np.ndarray, seed: np.ndarray) -> np.ndarray:
    """``mask`` 里跟 ``seed`` 四邻接连着的格。横着、竖着一整段一整段地传(一段里有一格连上了整段都连
    上),来回扫到不变为止。"""
    keep = seed & mask
    while True:
        before = int(keep.sum())
        keep = _runs(mask, keep)
        keep = _runs(mask.T, keep.T).T
        if int(keep.sum()) == before:
            return keep


def _runs(m: np.ndarray, k: np.ndarray) -> np.ndarray:
    """``m`` 每一行里连着的一段,只要有一格在 ``k`` 里,整段都算上。"""
    m, k = np.ascontiguousarray(m), np.ascontiguousarray(k)
    head = m.copy()
    head[:, 1:] &= ~m[:, :-1]                               # 每行里一段的头
    run = np.cumsum(head.ravel())
    hit = np.zeros(int(run[-1]) + 1, bool)
    hit[run[k.ravel()]] = True
    return m & hit[run].reshape(m.shape)


def _in_cone(dirs: np.ndarray, width: int) -> np.ndarray:
    """方向位(:data:`VIEW_BINS` 份)都挤在连着的 ``width`` 份以内的(一位都没有的不算)。"""
    out = np.zeros(dirs.shape, bool)
    full = (1 << VIEW_BINS) - 1
    win = (1 << width) - 1
    for s in range(VIEW_BINS):
        w = ((win << s) | (win >> (VIEW_BINS - s))) & full
        out |= (dirs & np.uint32(full ^ w)) == 0
    return out & (dirs != 0)


def _see_through(free: np.ndarray, seen: _Seen, seed: np.ndarray, res: float) -> np.ndarray:
    """透过玻璃、开口看到的假空地 → 「不知道」(:data:`THROUGH_SHARE` 起那几个常量;回剩下的可通行):

    - 离狗走过的路(``seed``,沿可通行格量)超过 :data:`PROTECT_M`,并且
    - 看到它的帧里至少 :data:`THROUGH_SHARE` 是透过开口看到的(:func:`_openings`:玻璃、狗没进去的门
      —— 切在开口线上),或者
    - 离路超过 :data:`VIEW_FAR_M`、看到它的方向都在 :data:`VIEW_CONE_BINS` 份(45°)以内(从窄口、
      零星有回波的玻璃远远看过去的扇形,开口认不出来的也算);
    - 跟狗走过的路已经不连着(只走可通行格)的可通行格也改成「不知道」(扇形剩下的碎块;狗本来也走
      不过去);
    - 最后,留下的可通行区里夹着的单格、一格宽的缝补回去(闭运算,只补原来就是可通行的格;放在连通
      之后,不会把扇形的碎块接回来):规划把一格「不知道」膨胀成半径 0.57 m 的挡,10-13 办公室图大屋
      靠墙那条零星去掉的格让规划少了 0.7 m²。

    只把可通行改成「不知道」:占据不动,不添可通行。"""
    H, W = free.shape
    direct, through = seen.direct.reshape(H, W), seen.through.reshape(H, W)
    protect = _geodesic(seed, free, int(round(PROTECT_M / res)))
    far = free & ~_geodesic(seed, free, int(round(VIEW_FAR_M / res)))
    via = (through > 0) & (through >= THROUGH_SHARE * (direct + through))
    narrow = np.zeros((H, W), bool)
    narrow[far] = _in_cone(seen.dirs.reshape(H, W)[far], VIEW_CONE_BINS)
    keep = _connected(free & (protect | ~(via | narrow)), seed)
    return keep | (free & _erode(_dilate(keep, 1), 1))      # 留下的里头零星的一格、一格宽的缝补回去


def render(points: np.ndarray, sensor_traj: np.ndarray, frames: Frames, *, res: float = RES_M,
           floor_clear: float = FLOOR_CLEAR_M, wall_top: float = WALL_TOP_M,
           traj_margin: float = TRAJ_MARGIN_M, max_range: float = MAX_RANGE_M,
           scans: Iterable[tuple[np.ndarray, np.ndarray]] | None = None,
           body_path: np.ndarray | None = None) -> Grid:
    """点云(MOLA 系,(N,3))+ 建图时雷达的位置(MOLA 系,(M,3))→ 栅格(地图平面系)。

    ``scans``:逐帧扫描 ``(雷达位置, 这一帧的点)``(都在 MOLA 系;给了就用真射线,并去掉透过玻璃、
    开口看到的假空地,见模块说明);``body_path``:狗身中心走过的路(地图平面,(K,2)),两边
    :data:`FOOTPRINT_M` 一律可通行(没给的话「走过的路」用雷达的轨迹)。"""
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
    views, used = None, 0
    if scans is not None:
        views = _Seen(H * W)
        passes, used = _raytrace_scans(scans, frames, lo, (H, W), res, (floor_clear, wall_top),
                                       max_range, ground,
                                       occ_h=(occ_lo.reshape(H, W), occ_hi.reshape(H, W)),
                                       seen=views)
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
    walked = np.zeros((H, W), bool)                         # 狗走过的路(身子中心;没给用雷达的)
    path = body_path if body_path is not None and len(body_path) else t[:, :2]
    pc = ((np.asarray(path, float)[:, :2] - lo) / res).astype(int)
    pc = pc[(pc[:, 0] >= 0) & (pc[:, 0] < W) & (pc[:, 1] >= 0) & (pc[:, 1] < H)]
    walked[pc[:, 1], pc[:, 0]] = True
    if body_path is not None and len(body_path):            # 狗的身子待过的地方
        body = _dilate(walked, int(round(FOOTPRINT_M / res)))
        free_m |= body
        occ_m &= ~body
    occ_m &= _dilate(free_m, int(0.5 / res))                # 只留贴着可通行区的障碍
    occ_m &= ~_surface(occ_m, free_m, res)                  # 墙只画看得见的那一面
    seen_only = np.zeros((H, W), bool)
    if views is not None and used and (walked & free_m).any():
        kept = _see_through(free_m, views, walked, res)     # 透过玻璃、开口看到的假空地
        seen_only = free_m & ~kept                          # M9:去掉的那些,显示时画淡色
        free_m = kept
    img = np.full((H, W), UNKNOWN, np.uint8)
    img[free_m] = FREE
    img[occ_m] = OCCUPIED
    known = (img != UNKNOWN) | seen_only
    if known.any():                                         # 裁到有内容的范围,四周留 5 格
        ys, xs = np.where(known)
        y0, x0 = max(int(ys.min()) - 5, 0), max(int(xs.min()) - 5, 0)
        y1, x1 = min(int(ys.max()) + 6, H), min(int(xs.max()) + 6, W)
        img = img[y0:y1, x0:x1]
        seen_only = seen_only[y0:y1, x0:x1]
        lo = lo + np.array([x0, y0]) * res
    return Grid(image=np.flipud(img).copy(), origin=(float(lo[0]), float(lo[1])), res=res,
                seen=np.flipud(seen_only).copy() if seen_only.any() else None)


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


def write_seen(grid: Grid, path: Path) -> Path | None:
    """显示用的掩码(M9)写成 PBM 二进制(``P4``:一格一位、每行补满一个字节、高位在前,1 = 是),宽高、
    行序跟 ``floor.pgm`` 一样。没有掩码、一格都没有 → 不写,回 ``None``。"""
    if grid.seen is None or not grid.seen.any():
        return None
    path = Path(path)
    h, w = grid.seen.shape
    bits = np.packbits(grid.seen.astype(bool), axis=1)
    path.write_bytes(f"P4\n{w} {h}\n".encode() + bits.tobytes())
    return path
