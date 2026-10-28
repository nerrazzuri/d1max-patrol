"""全局重定位(W41):拿狗此刻看到的「墙那一层」在整张图上套位置。

2026-10-26 C40221 现场:狗转到 (2.4, −2.8) 附近时 MOLA 两次锁到 5 m 外的错解(高度差 1.4 m),
笔记本上用点云套全图的结果是对的(那里的贴合度 0.72,MOLA 那个解只有 0.44)。这里把现场那个脚本
做成功能:

- **扫描**:感知节点每秒发一条 ``wall``(:class:`d1max_contract.obsbridge.Wall`):前雷达(加上标过的
  后雷达)离地 0.4–1.8 m 那一层落在哪些格子里,狗身系,0.1 m 一格。
- **图**:正在用的那张图的 ``floor.pgm`` 里的占用格。每个格子离最近的占用格多远 → 「贴近度」
  ``exp(−(d/σ)²)``;一个位姿的**贴合度** = 扫描点按这个位姿放到图上,贴近度的平均(0–1)。
- **粗找**:图压到 :data:`COARSE_RES` 一格,朝向每 :data:`YAW_STEP_DEG` 度一档,每一档用 FFT 一次算出
  所有位置的贴合度(只要狗身中心落在可通行格子上的),每一档留前几名。
- **细调**:粗找的前 :data:`TOP_K` 名,在原图分辨率上附近 ±0.2 m、±4° 细找。
- **认不认**(:func:`decide`):第一名贴合度不到 :data:`MIN_FIT` 不认(看到的跟图对不上:图旧了、
  被围住了);第二名(离第一名 1 m 以上或朝向差 25° 以上)跟第一名差不到 :data:`MIN_GAP` 也不认
  (长走廊、对称的房间:说不准是哪一处)。不认就把前几名告诉人,让人给位置。

门槛是按现场那一次的数定的(0.72 对 0.44),**待真机标**。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

COARSE_RES = 0.2
YAW_STEP_DEG = 3.0
SIGMA_COARSE = 0.25
SIGMA_FINE = 0.10
#: 只用这么远以内的扫描点(米):远处的点角度差一点就偏很多,也更可能是图外面的东西。
MAX_RANGE_M = 10.0
TOP_K = 6
PER_YAW = 3
NMS_M = 1.0
NMS_DEG = 25.0
MIN_FIT = 0.5
MIN_GAP = 0.1
#: 扫描点太少(被围住、雷达被挡)不找。
MIN_POINTS = 30


class GlobalLocError(Exception):
    """套不了(图读不了、扫描点太少):说清楚原因。"""


@dataclass(frozen=True)
class Candidate:
    x: float
    y: float
    yaw: float
    fit: float

    def to_wire(self) -> dict[str, float]:
        return {"x": round(self.x, 3), "y": round(self.y, 3), "yaw": round(self.yaw, 4),
                "fit": round(self.fit, 3)}


def _near(occ: np.ndarray, res: float, sigma: float) -> np.ndarray:
    """每一格的「贴近度」``exp(−(d/σ)²)``,``d`` 是离最近占用格的距离(八邻、四邻交替扩,近似欧氏;
    扩到 3σ 为止,再远的都是 0)。"""
    steps = max(1, int(math.ceil(3 * sigma / res)))
    dist = np.full(occ.shape, np.inf, dtype=np.float32)
    dist[occ] = 0.0
    cur = occ.copy()
    for k in range(1, steps + 1):
        n = cur.copy()
        n[1:] |= cur[:-1]
        n[:-1] |= cur[1:]
        n[:, 1:] |= cur[:, :-1]
        n[:, :-1] |= cur[:, 1:]
        if k % 2:                                    # 隔一步才扩对角:八边形,比正方形接近圆
            n[1:, 1:] |= cur[:-1, :-1]
            n[:-1, :-1] |= cur[1:, 1:]
            n[1:, :-1] |= cur[:-1, 1:]
            n[:-1, 1:] |= cur[1:, :-1]
        dist[n & ~cur] = k * res
        cur = n
    return np.exp(-(dist / sigma) ** 2).astype(np.float32)


def _pool(a: np.ndarray, k: int, how: str) -> np.ndarray:
    h, w = a.shape
    H, W = -(-h // k), -(-w // k)
    pad = np.zeros((H * k, W * k), dtype=a.dtype)
    pad[:h, :w] = a
    blocks = pad.reshape(H, k, W, k)
    return blocks.any(axis=(1, 3)) if how == "any" else blocks.all(axis=(1, 3))


def _rot(yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s], [s, c]])


def _wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


class Matcher:
    """一张图的套位置器(读图、算贴近度要一两秒:按图缓存,在线程里建)。

    ``occ``、``free``:布尔栅格,**行从下往上**(行号随 y 增大);``origin``:左下角那一格的角。"""

    def __init__(self, occ: np.ndarray, free: np.ndarray, res: float,
                 origin: tuple[float, float]) -> None:
        if occ.shape != free.shape or occ.ndim != 2:
            raise GlobalLocError("占用、可通行两张栅格大小不一样")
        if not occ.any():
            raise GlobalLocError("图里没有占用格(墙、柱子),套不了位置")
        self.res, self.origin = float(res), (float(origin[0]), float(origin[1]))
        self.occ, self.free = occ, free
        self.fine = _near(occ, self.res, SIGMA_FINE)
        self.k = max(1, int(round(COARSE_RES / self.res)))
        self.cres = self.res * self.k
        occ_c = _pool(occ, self.k, "any")
        self.cfree = _pool(free, self.k, "any")
        self.pad = int(math.ceil(MAX_RANGE_M / self.cres)) + 2
        coarse = _near(occ_c, self.cres, SIGMA_COARSE)
        self.coarse = np.pad(coarse, self.pad)
        self._fl = np.fft.rfft2(self.coarse)

    @classmethod
    def from_dir(cls, map_dir: Path) -> Matcher:
        from d1max_contract.planning.costmap import CostmapError, parse_pgm, parse_yaml
        try:
            meta = parse_yaml((map_dir / "floor.yaml").read_text(encoding="utf-8"))
            img = parse_pgm((map_dir / "floor.pgm").read_bytes())
        except (OSError, CostmapError, ValueError) as exc:
            raise GlobalLocError(f"读不了图:{exc}") from None
        p = img.astype(np.float32) / 255.0
        if not meta["negate"]:
            p = 1.0 - p                               # 黑是占用
        occ = np.flipud(p > meta["occupied_thresh"]).copy()
        free = np.flipud(p < meta["free_thresh"]).copy()
        return cls(occ, free, meta["resolution"], meta["origin"])

    # ------------------------------------------------------------ 打分

    def fit(self, pts: Any, pose: tuple[float, float, float]) -> float:
        """扫描点(狗身系)按 ``pose`` 放到图上的贴合度(0–1;落到图外的算 0)。"""
        q = self._clean(pts)
        if not len(q):
            return 0.0
        return float(self._fits(q, np.array([[pose[0], pose[1]]]), pose[2])[0])

    def _fits(self, q: np.ndarray, xy: np.ndarray, yaw: float) -> np.ndarray:
        p = q @ _rot(yaw).T
        h, w = self.fine.shape
        out = np.empty(len(xy), dtype=np.float64)
        for i, (x, y) in enumerate(xy):
            c = np.floor((p[:, 0] + x - self.origin[0]) / self.res).astype(int)
            r = np.floor((p[:, 1] + y - self.origin[1]) / self.res).astype(int)
            ok = (r >= 0) & (r < h) & (c >= 0) & (c < w)
            out[i] = self.fine[r[ok], c[ok]].sum() / len(q)
        return out

    def _clean(self, pts: Any) -> np.ndarray:
        """有限、够近的点,按图的分辨率去重(一格一个点:密的地方不多算)。"""
        q = np.asarray(pts, dtype=float).reshape(-1, 2)
        q = q[np.isfinite(q).all(axis=1) & (np.hypot(q[:, 0], q[:, 1]) <= MAX_RANGE_M)]
        if not len(q):
            return q
        return np.unique(np.round(q / self.res), axis=0) * self.res

    # ------------------------------------------------------------ 找

    def search(self, pts: Any) -> list[Candidate]:
        """→ 候选位姿,贴合度从高到低(彼此离 1 m 以上或朝向差 25° 以上)。"""
        q = self._clean(pts)
        qc = np.unique(np.round(q / self.cres).astype(int), axis=0)
        if len(qc) < MIN_POINTS // 2 or len(q) < MIN_POINTS:
            raise GlobalLocError(f"看到的墙太少({len(q)} 个点):被围住了、雷达被挡,或者四周空旷")
        H, W = self.coarse.shape
        mask = np.pad(self.cfree, self.pad)
        coarse: list[Candidate] = []
        for deg in np.arange(0.0, 360.0, YAW_STEP_DEG):
            yaw = math.radians(deg)
            p = (qc * self.cres) @ _rot(yaw).T
            dc = np.round(p[:, 0] / self.cres).astype(int)
            dr = np.round(p[:, 1] / self.cres).astype(int)
            K = np.zeros((H, W), dtype=np.float32)
            np.add.at(K, (dr % H, dc % W), 1.0)
            S = np.fft.irfft2(self._fl * np.conj(np.fft.rfft2(K)), s=(H, W)) / len(qc)
            S[~mask] = 0.0
            for _ in range(PER_YAW):
                i = int(np.argmax(S))
                r, c = divmod(i, W)
                if S[r, c] <= 0.0:
                    break
                x = self.origin[0] + (c - self.pad + 0.5) * self.cres
                y = self.origin[1] + (r - self.pad + 0.5) * self.cres
                coarse.append(Candidate(x, y, _wrap(yaw), float(S[r, c])))
                n = int(math.ceil(NMS_M / self.cres))
                S[max(0, r - n):r + n + 1, max(0, c - n):c + n + 1] = 0.0
        coarse = _nms(sorted(coarse, key=lambda k: -k.fit))[:TOP_K]
        fine = [self._refine(q, k) for k in coarse]
        return _nms(sorted(fine, key=lambda k: -k.fit))

    def _refine(self, q: np.ndarray, k: Candidate) -> Candidate:
        off = np.arange(-0.2, 0.2001, self.res)
        dx, dy = np.meshgrid(off, off)
        xy = np.stack([k.x + dx.ravel(), k.y + dy.ravel()], axis=1)
        best = k
        for dd in np.arange(-4.0, 4.01, 1.0):
            yaw = _wrap(k.yaw + math.radians(dd))
            f = self._fits(q, xy, yaw)
            i = int(np.argmax(f))
            if f[i] > best.fit or best is k:
                best = Candidate(float(xy[i, 0]), float(xy[i, 1]), yaw, float(f[i]))
        return best


def _nms(cands: list[Candidate]) -> list[Candidate]:
    out: list[Candidate] = []
    for k in cands:
        if any(math.hypot(k.x - o.x, k.y - o.y) < NMS_M
               and abs(_wrap(k.yaw - o.yaw)) < math.radians(NMS_DEG) for o in out):
            continue
        out.append(k)
    return out


def decide(cands: list[Candidate]) -> tuple[Candidate | None, str]:
    """认不认第一名:→ (认的那个, ""),或者 (None, 不认的原因)。"""
    if not cands:
        return None, "图上没有能站机器人的地方跟看到的对得上"
    best = cands[0]
    if best.fit < MIN_FIT:
        return None, (f"最像的 ({best.x:.1f}, {best.y:.1f}) 贴合度只有 {best.fit:.2f}"
                      f"(要 {MIN_FIT:g} 以上):看到的跟图对不上")
    if len(cands) > 1 and best.fit - cands[1].fit < MIN_GAP:
        o = cands[1]
        return None, (f"有两处差不多像:({best.x:.1f}, {best.y:.1f}) 贴合度 {best.fit:.2f}、"
                      f"({o.x:.1f}, {o.y:.1f}) {o.fit:.2f},说不准是哪一处")
    return best, ""


# ------------------------------------------------------------ 核对定位器给的位置

#: 定位器给的位置贴合度到这么多就算对得上(现场对的位置 0.71–0.78;MOLA 锁错的那一次 0.44)。
TRUST_FIT = 0.55
#: 图上最像的那处比定位器给的高出这么多、又离得远(:data:`NMS_M` 以上或朝向差 :data:`NMS_DEG`
#: 以上),才说定位器锁错了。
WRONG_GAP = 0.15
#: 按点云套图的结果请定位器重定位时给的偏差(米)。
GLOBAL_SIGMA_M = 0.3


@dataclass(frozen=True)
class Verdict:
    """核对的结论:``ok`` 对得上(或者说不准、不动它);``reloc`` 给了 = 定位器锁错了、按它重定位;
    ``reason`` 非空 = 定位不可信(写给人看)。"""
    ok: bool
    fit: float
    reloc: Candidate | None = None
    reason: str = ""


def judge(fit_now: float, pose: tuple[float, float, float],
          cands: list[Candidate]) -> Verdict:
    """定位器给的 ``pose`` 贴合度 ``fit_now``,全图找出来的 ``cands``:锁错了没有。"""
    if fit_now >= TRUST_FIT:
        return Verdict(True, fit_now)
    best, why = decide(cands)
    if best is None:
        if cands and cands[0].fit - fit_now >= WRONG_GAP:
            return Verdict(False, fit_now, reason=(
                f"点云套图:定位器给的位置贴合度只有 {fit_now:.2f},图上别处更像但{why};"
                "请重新设位置"))
        return Verdict(True, fit_now)                # 哪儿都不像(图旧了、被围住了):不下结论
    far = math.hypot(best.x - pose[0], best.y - pose[1]) >= NMS_M \
        or abs(_wrap(best.yaw - pose[2])) >= math.radians(NMS_DEG)
    if not far or best.fit - fit_now < WRONG_GAP:
        return Verdict(True, fit_now)
    return Verdict(False, fit_now, reloc=best, reason=(
        f"点云套图:定位器给的 ({pose[0]:.1f}, {pose[1]:.1f}) 贴合度 {fit_now:.2f},"
        f"({best.x:.1f}, {best.y:.1f}) 那里 {best.fit:.2f}:定位器多半锁错了,按那里重定位"))


def resolve_conflict(fit_localizer: float, fit_given: float) -> str:
    """工单 10:定位器稳住的地方跟锚(人给的位置按里程推到现在)对不上时,两处各套一次图的贴合度 →
    ``"localizer"`` 信定位器、``"anchor"`` 信锚(定位器锁错了)、``""`` 说不准(照旧等人)。"""
    if fit_localizer >= TRUST_FIT and fit_localizer - fit_given >= WRONG_GAP:
        return "localizer"
    if fit_given >= TRUST_FIT and fit_given - fit_localizer >= WRONG_GAP:
        return "anchor"
    return ""


class GlobalLoc:
    """代理这头的全局重定位:收感知的 ``wall``(经 :class:`d1max_agent.obstacles.ObstacleView`),按
    正在用的图缓存 :class:`Matcher`(在线程里建、在线程里找)。"""

    def __init__(self, view: Any) -> None:
        self.view = view
        self._key: tuple[str, str] | None = None
        self._dir: Path | None = None
        self._m: tuple[tuple[str, str], Matcher] | None = None

    def set_map(self, key: tuple[str, str] | None, map_dir: Path | None) -> None:
        if (key, map_dir) != (self._key, self._dir):
            self._key, self._dir, self._m = key, map_dir, None

    @property
    def ready(self) -> bool:
        return self._key is not None and self._dir is not None

    async def _matcher(self) -> Matcher:
        if self._key is None or self._dir is None:
            raise GlobalLocError("没有加载地图")
        key, d = self._key, self._dir
        if self._m is not None and self._m[0] == key:
            return self._m[1]
        m = await asyncio.to_thread(Matcher.from_dir, d)
        if self._key == key:
            self._m = (key, m)
        return m

    def scan(self, *, since: float = -math.inf) -> list[tuple[float, float]]:
        """感知最新一份墙那一层(狗身系):要新鲜、攒的时候狗没动、是 ``since`` 之后收到的。"""
        got = self.view.wall_scan(since=since) if self.view is not None else None
        if got is None:
            raise GlobalLocError("没有新的点云(感知没连上、机器人在动,或者刚动过)")
        return got

    async def locate(self, pts: list[tuple[float, float]]) -> list[Candidate]:
        m = await self._matcher()
        return await asyncio.to_thread(m.search, pts)

    async def fits(self, pts: list[tuple[float, float]],
                   poses: list[tuple[float, float, float]]) -> list[float]:
        """几处位置各自的贴合度(工单 10)。"""
        m = await self._matcher()
        return await asyncio.to_thread(lambda: [m.fit(pts, p) for p in poses])

    async def check(self, pts: list[tuple[float, float]],
                    pose: tuple[float, float, float]) -> Verdict:
        m = await self._matcher()
        fit_now = await asyncio.to_thread(m.fit, pts, pose)
        if fit_now >= TRUST_FIT:
            return Verdict(True, fit_now)
        return judge(fit_now, pose, await asyncio.to_thread(m.search, pts))
