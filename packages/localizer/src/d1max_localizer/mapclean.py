"""三维定位地图去噪(W70,用户 2026-11-03 现场:「需要先做地图噪点清理」)。

给 Super-LIO 定位用的 ``superlio_map.pcd`` 原来只降采样:建图时的行人、临时箱子、透过玻璃看到的外面
都原样进了定位地图。打包时(:func:`d1max_localizer.build.package`)按体素(跟地图一样
:data:`~d1max_localizer.slioloc.MAP_VOXEL_M`)做三步,各自独立判,最后一起删:

1. **射线清除**(:class:`RayClear`):用建图时的逐帧扫描(跟画栅格同一批,不多读一遍录包),雷达到
   每个点的射线穿过的地图体素记一次「穿过」,射线打到的记一次「打中」。穿过的帧数够多、又远多于打中
   的帧数 = 当时有东西、后来空了(人走了、箱子挪了),删掉。离地 :data:`FLOOR_KEEP_M` 以内的不按
   这一步删(远处的射线贴着地面擦过去,会把地面当成「穿过」)。
2. **零星孤点**(:func:`isolated`):周围 26 个体素里有点的不到 :data:`ISOLATED_MIN` 个。
3. **透过开口看到的**:二维栅格判出来「只透过玻璃、门口看到过、机器人没进去过」的格子(``seen.pbm``
   那些,M9)上方的点 —— **只数、不删**(``see_through_seen``):这些格子分不清玻璃外面和从门口看进去的
   房间,office1 上照删会删掉 14% 不动的桌椅隔断,定位要靠它们。玻璃外面的变化交给套图打分兜底。

每一步要删的超过 :data:`MAX_DROP` 就那一步不做(多半是轨迹或参数有问题,宁可不删),写进统计的
``skipped``。统计写进 ``build.json`` 的 ``superlio_map.denoise``,现场拿来核。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from d1max_localizer.slioloc import _NEIGH, _keys

#: 射线采样步长(米;半个体素,斜着穿过也不漏格)。
RAY_STEP_M = 0.125
#: 射线只算这么远以内的一段(远处斜着擦过墙面,会把墙当「穿过」啃掉;office1 上 20 m 时
#: 沿墙删出几道线)。
CLEAR_RANGE_M = 10.0
#: 离射线终点这么近的不算「穿过」(终点附近是打中的那面东西本身,测距有噪声)。
END_MARGIN_M = 0.5
#: 雷达近处的点不要(机身、雨罩)。
MIN_RANGE_M = 0.5
#: 每帧最多取这么多条射线(均匀抽)。
RAYS_PER_SCAN = 2000
#: 至少这么多帧的射线穿过,才可能删。
PASS_MIN = 5
#: 穿过的帧数至少是打中帧数的这么多倍。
PASS_PER_HIT = 5.0
#: 离地这么高以内的点不按射线删(贴地射线擦过地面)。
FLOOR_KEEP_M = 0.2
#: 周围 26 个体素里有点的少于这么多个 = 零星孤点。
ISOLATED_MIN = 1
#: 一步最多删这么大比例,超过就那一步不做。
MAX_DROP = 0.25


def voxel(xyz: Any, res: float) -> np.ndarray:
    """每个体素留一个点(跟 :func:`d1max_localizer.slioloc.voxel_down` 一样)。"""
    from d1max_localizer.slioloc import voxel_down
    return voxel_down(xyz, res)


class RayClear:
    """累计逐帧扫描在地图体素上的「穿过」「打中」帧数(:meth:`add`),最后 :meth:`dynamic` 判。"""

    def __init__(self, points: Any, res: float) -> None:
        self.res = float(res)
        k = _keys(np.floor(np.asarray(points, float) / self.res).astype(np.int64))
        self._order = np.argsort(k, kind="stable")
        self._sorted = k[self._order]
        n = len(k)
        self.passes = np.zeros(n, np.int32)
        self.hits = np.zeros(n, np.int32)
        self.scans = 0
        self._ts = np.arange(RAY_STEP_M / 2, CLEAR_RANGE_M, RAY_STEP_M)

    def _index(self, keys: np.ndarray) -> np.ndarray:
        """体素键 → 地图点的下标(不在地图里的丢掉);同一帧里同一个体素只算一次。"""
        keys = np.unique(keys)
        if not len(self._sorted) or not len(keys):
            return np.zeros(0, np.int64)
        i = np.clip(np.searchsorted(self._sorted, keys), 0, len(self._sorted) - 1)
        ok = self._sorted[i] == keys
        return self._order[i[ok]]

    def add(self, sensor: Any, pts: Any) -> None:
        """一帧:雷达位置、这一帧的点(都在地图系)。"""
        s = np.asarray(sensor, float).reshape(3)
        p = np.asarray(pts, float).reshape(-1, 3)
        d = p - s
        r = np.linalg.norm(d, axis=1)
        p, d, r = p[r > MIN_RANGE_M], d[r > MIN_RANGE_M], r[r > MIN_RANGE_M]
        if not len(p):
            return
        self.scans += 1
        hit = self._index(_keys(np.floor(p / self.res).astype(np.int64)))
        np.add.at(self.hits, hit, 1)
        if len(p) > RAYS_PER_SCAN:
            sel = np.linspace(0, len(p) - 1, RAYS_PER_SCAN).astype(int)
            d, r = d[sel], r[sel]
        u = d / r[:, None]
        reach = np.minimum(r - END_MARGIN_M, CLEAR_RANGE_M)
        ts = self._ts[self._ts < reach.max()] if (reach > 0).any() else self._ts[:0]
        if not len(ts):
            return
        inside = ts[None, :] < reach[:, None]
        ri, ti = np.nonzero(inside)
        q = s + u[ri] * ts[ti, None]
        through = self._index(_keys(np.floor(q / self.res).astype(np.int64)))
        np.add.at(self.passes, through, 1)

    def dynamic(self, *, height: Any = None) -> np.ndarray:
        """哪些地图点是「后来空了」的(真假,跟构造时的点一一对应)。``height``:每个点离当地地面
        多高(``nan`` = 不知道,当作不在地面附近);离地 :data:`FLOOR_KEEP_M` 以内的不删。"""
        dyn = (self.passes >= PASS_MIN) & (self.passes >= PASS_PER_HIT * self.hits)
        if height is not None:
            h = np.asarray(height, float)
            dyn &= ~(np.isfinite(h) & (h < FLOOR_KEEP_M))
        return dyn


def isolated(points: Any, res: float) -> np.ndarray:
    """零星孤点:周围 26 个体素里有点的少于 :data:`ISOLATED_MIN` 个。"""
    v = np.floor(np.asarray(points, float) / res).astype(np.int64)
    k = _keys(v)
    s = np.unique(k)
    near = np.zeros(len(k), np.int32)
    for d in _NEIGH:
        if not d.any():
            continue
        nk = _keys(v + d)
        i = np.clip(np.searchsorted(s, nk), 0, max(len(s) - 1, 0))
        near += (s[i] == nk) if len(s) else 0
    return near < ISOLATED_MIN


def apply(points: Any, masks: Mapping[str, Any], *, max_drop: float = MAX_DROP
          ) -> tuple[np.ndarray, dict[str, Any]]:
    """按顺序把各步要删的合起来删。回(留下的点, 统计)。统计里每一步是「这一步新删的」(前面几步已经
    删了的不重复算);一步要删的(按它自己判的,不管重不重)超过 ``max_drop`` 就不做、记进
    ``skipped``。"""
    pts = np.asarray(points, float)
    n = len(pts)
    drop = np.zeros(n, bool)
    stats: dict[str, Any] = {"before": n}
    skipped: list[str] = []
    for name, m in masks.items():
        m = np.asarray(m, bool)
        if n and m.sum() > max_drop * n:
            skipped.append(f"{name}:要删 {m.sum()}/{n}({m.mean():.0%})超过 {max_drop:.0%},这步不做")
            stats[name] = 0
            continue
        stats[name] = int((m & ~drop).sum())
        drop |= m
    stats["kept"] = int(n - drop.sum())
    stats["skipped"] = skipped
    return pts[~drop], stats


def through_mask(points: Any, plane: Any, seen: Any, origin: tuple[float, float], res: float
                 ) -> np.ndarray:
    """点(``plane``:地图平面上的 (x, y),跟 ``points`` 一一对应)落在「只透过开口看到」的格子上。
    ``seen``:跟 ``floor.pgm`` 一样的掩码(行 0 = y 最大的那行),``None`` = 没有这种格子。"""
    n = len(np.asarray(points))
    if seen is None:
        return np.zeros(n, bool)
    m = np.asarray(seen, bool)
    H, W = m.shape
    xy = np.asarray(plane, float)[:, :2]
    c = np.floor((xy[:, 0] - origin[0]) / res).astype(int)
    r = H - 1 - np.floor((xy[:, 1] - origin[1]) / res).astype(int)
    ok = (c >= 0) & (c < W) & (r >= 0) & (r < H)
    out = np.zeros(n, bool)
    out[ok] = m[r[ok], c[ok]]
    return out
