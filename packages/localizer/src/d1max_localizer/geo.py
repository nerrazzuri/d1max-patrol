"""建图时的地理配准(W09e 决定 6):MOLA 轨迹(按 ``frames.json`` 投到地图平面、加上天线杆臂)跟同一
时刻的 RTK 固定解配对,二维刚体(旋转 + 平移、不缩放)最小二乘,写 ``geo.json``。

只用固定解;时刻差 ≤ :data:`PAIR_S`;至少 :data:`MIN_PAIRS` 对、走过的范围至少 :data:`MIN_EXTENT_M`;
残差 RMS > :data:`MAX_RMS_M` 不写。配不上不影响这一版能用(``geo.json`` 是可选文件)。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from d1max_contract.geo import GeoRef, llh_to_enu
from d1max_localizer.frames import Frames

PAIR_S = 0.1
MIN_PAIRS = 20
MIN_EXTENT_M = 10.0
MAX_RMS_M = 0.3


def read_rtk(path: Path) -> list[dict[str, Any]]:
    """代理录包时记的 ``rtk.jsonl``(坏行跳过)。"""
    rows = []
    try:
        text = Path(path).read_text("utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        try:
            d = json.loads(line)
            if all(isinstance(d.get(k), (int, float)) and math.isfinite(d[k])
                   for k in ("t", "lat", "lon")):
                rows.append(d)
        except (ValueError, AttributeError):
            continue
    return rows


def fit_rigid2d(src: Sequence[Sequence[float]], dst: Sequence[Sequence[float]]
                ) -> tuple[float, float, float, float]:
    """``dst ≈ R(θ)·src + t`` 的最小二乘(Umeyama,不缩放)→ (θ, tx, ty, RMS)。"""
    n = len(src)
    sx = sum(p[0] for p in src) / n
    sy = sum(p[1] for p in src) / n
    dx = sum(p[0] for p in dst) / n
    dy = sum(p[1] for p in dst) / n
    a = b = 0.0
    for (x, y), (u, v) in zip(src, dst, strict=True):
        x, y, u, v = x - sx, y - sy, u - dx, v - dy
        a += x * u + y * v
        b += x * v - y * u
    th = math.atan2(b, a)
    c, s = math.cos(th), math.sin(th)
    tx, ty = dx - (c * sx - s * sy), dy - (s * sx + c * sy)
    err = [(c * x - s * y + tx - u) ** 2 + (s * x + c * y + ty - v) ** 2
           for (x, y), (u, v) in zip(src, dst, strict=True)]
    return th, tx, ty, math.sqrt(sum(err) / n)


def georeference(traj: Sequence[Any], frames: Frames, rows: Sequence[dict[str, Any]], *,
                 antenna_in_base: tuple[float, float] = (0.0, 0.0), clock_offset_s: float = 0.0
                 ) -> tuple[GeoRef | None, str]:
    """→ (配准, 说明);配不上 (None, 为什么)。

    ``clock_offset_s``:轨迹时间戳(雷达消息头的钟)要加多少才到 ``rtk.jsonl`` 的钟(代理收到的时刻,
    跟录包的接收时刻同一个本机钟)—— 两个钟不是一个钟(W09e 内审应修 1),见
    :func:`build.bag_clock_offset`。"""
    fixed = [r for r in rows if r.get("fix") == "fixed"]
    if not fixed:
        return None, "没有 RTK 固定解"
    stamps = [t + clock_offset_s for t, _, _ in traj]
    lat0, lon0 = float(fixed[0]["lat"]), float(fixed[0]["lon"])
    try:
        alt0 = float(fixed[0].get("alt") or 0.0)
    except (TypeError, ValueError):
        alt0 = 0.0
    alt0 = alt0 if math.isfinite(alt0) else 0.0
    bases = [r["base_ecef"] for r in fixed if isinstance(r.get("base_ecef"), list)
             and len(r["base_ecef"]) == 3]
    src, dst = [], []
    import bisect
    ax, ay = antenna_in_base
    for r in fixed:
        t = float(r["t"])
        k = bisect.bisect_left(stamps, t)
        best = min((i for i in (k - 1, k) if 0 <= i < len(stamps)),
                   key=lambda i: abs(stamps[i] - t), default=None)
        if best is None or abs(stamps[best] - t) > PAIR_S:
            continue
        _, p, q = traj[best]
        x, y, yaw = frames.to_map2d(p, q)
        c, s = math.cos(yaw), math.sin(yaw)
        src.append((x + c * ax - s * ay, y + s * ax + c * ay))
        dst.append(llh_to_enu(float(r["lat"]), float(r["lon"]), lat0, lon0))
    if len(src) < MIN_PAIRS:
        return None, f"跟轨迹对得上时刻的固定解只有 {len(src)} 个(要 {MIN_PAIRS} 个)"
    extent = max(math.dist(a, b) for a in src[:: max(1, len(src) // 50)] for b in src)
    if extent < MIN_EXTENT_M:
        return None, f"走过的范围只有 {extent:.1f} m(要 {MIN_EXTENT_M:g} m):朝向定不准"
    th, tx, ty, rms = fit_rigid2d(src, dst)
    if rms > MAX_RMS_M:
        return None, f"残差 {rms:.2f} m(上限 {MAX_RMS_M:g} m):RTK 跟建图轨迹对不上"
    base = None
    if bases:                                         # 建图时的基站坐标(内审应修 3):取中间那个
        b = sorted(bases)[len(bases) // 2]
        base = (float(b[0]), float(b[1]), float(b[2]))
    g = GeoRef(lat0, lon0, alt0, yaw_deg=math.degrees(th), tx=tx, ty=ty, rms_m=round(rms, 3),
               pairs=len(src), antenna_in_base=antenna_in_base, base_ecef=base)
    return g, f"{len(src)} 对,残差 {rms:.3f} m"
