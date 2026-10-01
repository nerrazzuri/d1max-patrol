"""后雷达外参标定(W09i 设计稿 §2):前雷达建好的图当参照,后雷达所有帧一起对上去,解同一个
``T_front_rear``。

- 参照:前雷达的帧按建图轨迹放进世界系,体素取质心(:data:`LEVELS` 由粗到细,每层一份)。
- 参照点带法向(体素协方差最小特征向量;不够平的体素(墙角、枝叶)不用)。
- 一轮:后雷达的点按「那一刻的前雷达位姿 × T」放进世界系 → 体素哈希找最近的参照点(只看相邻 27 格,
  不引 scipy)→ 对上的点、法向退回那一刻的前雷达系 → 所有帧一起解**点到面**的小增量(6×6 最小二乘,
  去掉最远的 20%)。点到点对体素质心有偏(仿真 1.3 cm / 1.1°),点到面没有。
- 守门(:func:`calibrate` 回的 ``ok``):对上的点占比 ≥ :data:`MIN_INLIER`、残差中位数 ≤ :data:
  `MAX_MEDIAN_M`、
  离初值 ≤ :data:`MAX_SHIFT_M` / :data:`MAX_TURN_DEG`。不过就不写文件(说原因)——
  坏的标定比没标定危险。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

#: (体素米, 这一层迭代几轮):由粗到细,初值差 10 cm / 5° 也收得回来。
LEVELS = ((0.4, 5), (0.2, 5), (0.1, 5), (0.05, 5))
MIN_INLIER = 0.5
MAX_MEDIAN_M = 0.05
MAX_SHIFT_M = 0.3
MAX_TURN_DEG = 20.0
TRIM = 0.8
REAR_POINTS_PER_FRAME = 1000


@dataclass
class Result:
    T: Any                       # 4×4,后雷达系 → 前雷达系
    ok: bool
    why: str
    inlier: float                # 最细一层对上的点占比
    median_m: float              # 最细一层对上的点到面距离的中位数
    shift_m: float               # 离初值的平移
    turn_deg: float              # 离初值的转角
    frames: int


class VoxelMap:
    """体素质心 + 法向 + 相邻 27 格最近邻(纯 numpy:键排好序,每个偏移一次 ``searchsorted``)。

    法向按 ``max(2·体素, 0.2 m)`` 的大格算(小格里点太少,法向不准),不够平(最小 / 中间特征值
    > :data:`FLAT`)或点少于 5 个的格不要。"""

    _OFF = np.array([(i, j, k) for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)])
    FLAT = 0.1
    _OFF_KEY = (_OFF[:, 0] << 42) + (_OFF[:, 1] << 21) + _OFF[:, 2]

    def __init__(self, pts: Any, voxel: float) -> None:
        pts = np.asarray(pts, float)
        self.v = voxel
        uniq, inv = np.unique(self._enc(np.floor(pts / voxel).astype(np.int64)),
                              return_inverse=True)                 # 排好序的
        c = self._mean(pts, inv.reshape(-1), len(uniq))
        nrm, good = self._normals(pts, max(2 * voxel, 0.2), c)
        self.k, self.c, self.n = uniq[good], c[good], nrm[good]

    @staticmethod
    def _mean(pts: Any, inv: Any, n: int) -> Any:
        cnt = np.bincount(inv, minlength=n).astype(float)
        return np.stack([np.bincount(inv, weights=pts[:, d], minlength=n) for d in range(3)], 1) \
            / cnt[:, None]

    def _normals(self, pts: Any, big: float, at: Any) -> tuple[Any, Any]:
        key, inv = np.unique(self._enc(np.floor(pts / big).astype(np.int64)), return_inverse=True)
        inv = inv.reshape(-1)
        n = len(key)
        cnt = np.bincount(inv, minlength=n).astype(float)
        mu = self._mean(pts, inv, n)
        d = pts - mu[inv]
        cov = np.zeros((n, 3, 3))
        for i in range(3):
            for j in range(i, 3):
                cov[:, i, j] = cov[:, j, i] = np.bincount(inv, weights=d[:, i] * d[:, j],
                                                          minlength=n) / cnt
        w, v = np.linalg.eigh(cov)
        flat = (cnt >= 5) & (w[:, 0] <= self.FLAT * np.maximum(w[:, 1], 1e-12))
        nrm = v[:, :, 0]
        q = self._enc(np.floor(at / big).astype(np.int64))
        i = np.clip(np.searchsorted(key, q), 0, len(key) - 1)
        return nrm[i], (key[i] == q) & flat[i]

    @staticmethod
    def _enc(keys: Any) -> Any:
        k = keys + (1 << 20)
        return (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]

    def nearest(self, q: Any) -> tuple[Any, Any, Any]:
        """→ (最近的质心, 它的法向, 距离);相邻 27 格里没有的距离是 inf。"""
        kb = self._enc(np.floor(q / self.v).astype(np.int64))
        order = np.argsort(kb)                  # 排好序再查快得多;键是线性编码,加偏移次序不变
        kb, qs = kb[order], q[order]
        best_d = np.full(len(q), np.inf)
        idx = np.zeros(len(q), dtype=np.int64)
        for off in self._OFF_KEY:
            key = kb + off
            i = np.clip(np.searchsorted(self.k, key), 0, len(self.k) - 1)
            hit = self.k[i] == key
            dv = self.c[i] - qs
            d = np.where(hit, np.sqrt(np.einsum("ij,ij->i", dv, dv)), np.inf)
            better = d < best_d
            best_d = np.where(better, d, best_d)
            idx = np.where(better, i, idx)
        back = np.empty_like(order)
        back[order] = np.arange(len(order))
        return self.c[idx][back], self.n[idx][back], best_d[back]


def kabsch(src: Any, dst: Any) -> Any:
    """最小二乘刚体变换 ``dst ≈ R·src + t`` → 4×4。"""
    ms, md = src.mean(0), dst.mean(0)
    H = (src - ms).T @ (dst - md)
    U, _, Vt = np.linalg.svd(H)
    D = np.diag([1.0, 1.0, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, md - R @ ms
    return T


def _apply(T: Any, p: Any) -> Any:
    return p @ T[:3, :3].T + T[:3, 3]


def delta(T0: Any, T1: Any) -> tuple[float, float]:
    """两个变换差多少:(平移米, 转角度)。"""
    D = np.linalg.inv(T0) @ T1
    ang = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(D[:3, :3]) - 1) / 2))))
    return float(np.linalg.norm(D[:3, 3])), ang


def _skew_solve(p: Any, m: Any, n: Any) -> Any:
    """点到面一小步:``min Σ (n·(p + ω×p + v − m))²`` → 4×4 增量(左乘)。"""
    J = np.hstack([np.cross(p, n), n])
    r = np.einsum("ij,ij->i", n, p - m)
    x = np.linalg.lstsq(J.T @ J + 1e-9 * np.eye(6), -J.T @ r, rcond=None)[0]
    w, v = x[:3], x[3:]
    th = float(np.linalg.norm(w))
    K = np.array([[0, -w[2], w[1]], [w[2], 0, -w[0]], [-w[1], w[0], 0]])
    R = np.eye(3) + (math.sin(th) / th * K + (1 - math.cos(th)) / th**2 * K @ K if th > 1e-12
                     else K)
    D = np.eye(4)
    D[:3, :3], D[:3, 3] = R, v
    return D


def calibrate(frames: Sequence[tuple[Any, ...]], T0: Any, *, levels=LEVELS,
              seed: int = 0) -> Result:
    """``frames``:``(前雷达在世界系的位姿 4×4, 前雷达一帧, 后雷达那一刻的前雷达位姿 4×4,
    后雷达一帧)``
    —— 或者三元组 ``(位姿, 前雷达一帧, 后雷达一帧)``(同一时刻)。``T0``:初值。"""
    rng = np.random.default_rng(seed)
    T0 = np.asarray(T0, float)
    fr = [f if len(f) == 4 else (f[0], f[1], f[0], f[2]) for f in frames]
    if not fr:
        return Result(T0, False, "没有帧", 0.0, math.inf, 0.0, 0.0, 0)
    ref = np.vstack([_apply(np.asarray(Twf, float), np.asarray(p, float)) for Twf, p, _, _ in fr])
    ps, Rs, ts = [], [], []
    for _, _, Twr, p in fr:
        p = np.asarray(p, float)
        if len(p) > REAR_POINTS_PER_FRAME:
            p = p[rng.choice(len(p), REAR_POINTS_PER_FRAME, replace=False)]
        Twr = np.asarray(Twr, float)
        ps.append(p)
        Rs.append(np.broadcast_to(Twr[:3, :3], (len(p), 3, 3)))
        ts.append(np.broadcast_to(Twr[:3, 3], (len(p), 3)))
    P, Rw, tw = np.vstack(ps), np.concatenate(Rs), np.concatenate(ts)   # 每个点带它那一刻的位姿
    T = T0.copy()
    inlier, med = 0.0, math.inf
    for voxel, iters in levels:
        vm = VoxelMap(ref, voxel)
        gate = 2.0 * voxel
        for _ in range(iters):
            pf = _apply(T, P)                                     # 前雷达系
            m, nw, d = vm.nearest(np.einsum("nij,nj->ni", Rw, pf) + tw)
            ok = d <= gate
            inlier = float(ok.mean())
            if ok.sum() < 50:
                return Result(T, False, f"对不上(体素 {voxel} m 时只有 {int(ok.sum())} 个点挨得着)",
                              inlier, math.inf, *delta(T0, T), len(fr))
            Rt = np.transpose(Rw[ok], (0, 2, 1))                  # 退回那一刻的前雷达系
            mf = np.einsum("nij,nj->ni", Rt, m[ok] - tw[ok])
            nf = np.einsum("nij,nj->ni", Rt, nw[ok])
            dist = np.abs(np.einsum("ij,ij->i", nf, pf[ok] - mf))
            keep = dist <= np.quantile(dist, TRIM)
            T = _skew_solve(pf[ok][keep], mf[keep], nf[keep]) @ T
            med = float(np.median(dist))
    shift, turn = delta(T0, T)
    why = []
    if inlier < MIN_INLIER:
        why.append(f"对上的点只占 {inlier:.0%}(要 ≥ {MIN_INLIER:.0%})")
    if med > MAX_MEDIAN_M:
        why.append(f"残差中位数 {med * 100:.1f} cm(要 ≤ {MAX_MEDIAN_M * 100:.0f} cm)")
    if shift > MAX_SHIFT_M or turn > MAX_TURN_DEG:
        why.append(f"离初值太远({shift:.2f} m、{turn:.1f}°;"
                   f"最多 {MAX_SHIFT_M} m、{MAX_TURN_DEG:.0f}°)"
                   ":不像头尾对称装的,先核装法")
    return Result(T, not why, ";".join(why) or "过了", inlier, med, shift, turn, len(fr))
