"""两个地图版本的对比图(地图管理页):站点把两版的规划栅格按**世界坐标**对齐,画一张差异图。

- 对齐只看每版 ``floor.yaml`` 的 ``origin``、``resolution``(``map_server`` 约定:origin = 左下角
  格子的世界坐标)。分辨率不同的,按较细的那个重采(最近邻)。**不做任何配准**:不平移、不旋转、不缩放。
- **提醒(必须让人看到)**:分开建的两个版本不一定在同一个坐标系里 —— 每次建图以那一趟的起点为原点,
  回环、重建都会让整张图整体挪一点、转一点。对比图上「大面积都不一样」多半是坐标系不同,不是地方变了;
  只有两版确实同一个坐标系(同一张图改出来的,或 ``frames.json`` 一样且起点对得上)差异才可信。
  所以结果里总带 ``caveat``;再用一个粗判(A 的墙附近有多少在 B 里也有墙)给 ``alignment``,太低就
  ``likely_misaligned`` = True,界面要红字说。
- 颜色:两版一样的画灰(墙深灰、空地浅灰、都没扫到不画);只有 A 有墙 = 红;只有 B 有墙 = 蓝;
  A 空地 B 没扫到 = 橙;A 没扫到 B 空地 = 青(新扫到的空地)。

纯 numpy + zlib,跟 ``map_preview`` 一样用到才引 numpy。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Any

#: 对比画布最多多少格(并集范围按较细分辨率);再大拒绝(吃内存)。
MAX_CELLS = 25_000_000
#: 输出图最长边(像素);更大的按整数倍缩(优先保留差异)。
MAX_SIDE = 1600
#: 粗判:A 的墙有这个比例在 B 的墙附近(3×3)才算对得上;低于它认为坐标系可能不同。
ALIGN_OK_SHARE = 0.4
#: 两边都得有这么多墙格才有资格粗判(太少不下结论)。
ALIGN_MIN_WALLS = 30

CAVEAT = ("分开建的版本不一定在同一个坐标系里(每次建图以那一趟的起点为原点,回环和重建会让整张图"
          "挪一点、转一点)。此地图只按两版各自的原点和分辨率对齐,没有做配准:大面积的差异很可能是"
          "坐标系不同,不是地方变了。")

# 类别编码(同时是并集里的优先级:缩图时取大的 —— 差异盖过一样)
_SAME_NONE, _SAME_FREE, _SAME_OCC, _FREE_TO_UNK, _UNK_TO_FREE, _OCC_B, _OCC_A = range(7)
COLORS = {
    "same_wall": (70, 70, 70), "same_free": (225, 225, 225),
    "wall_only_a": (214, 39, 40), "wall_only_b": (31, 119, 180),
    "free_only_a": (255, 160, 60), "free_only_b": (60, 190, 190),
}
_PALETTE = {
    _SAME_NONE: (255, 255, 255), _SAME_FREE: COLORS["same_free"], _SAME_OCC: COLORS["same_wall"],
    _FREE_TO_UNK: COLORS["free_only_a"], _UNK_TO_FREE: COLORS["free_only_b"],
    _OCC_B: COLORS["wall_only_b"], _OCC_A: COLORS["wall_only_a"],
}


class DiffError(ValueError):
    """这两版比不了(范围太大等)。消息给人看。"""


@dataclass(frozen=True)
class Diff:
    png: bytes
    stats: dict[str, Any]


def _png_rgb(w: int, h: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + raw[r * w * 3:(r + 1) * w * 3] for r in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _sample(ranks, ox: float, oy: float, res: float, xs, ys):
    """世界坐标 ``(xs, ys)``(行向量、列向量,都是格子中心)在这一版栅格里的名次;范围外 = 未知(2)。
    ``ranks`` 从上往下(第 0 行是最北),origin 是左下角。"""
    import numpy as np
    h, w = ranks.shape
    cx = np.floor((xs - ox) / res).astype(np.int64)
    cy = np.floor((ys - oy) / res).astype(np.int64)
    row = h - 1 - cy
    inside = ((cx >= 0) & (cx < w))[None, :] & ((row >= 0) & (row < h))[:, None]
    out = np.full((len(ys), len(xs)), 2, dtype=np.uint8)
    rr, cc = np.nonzero(inside)
    out[rr, cc] = ranks[row[rr], cx[cc]]
    return out


def _near(mask):
    """3×3 邻域里有 True 的格子。"""
    import numpy as np
    p = np.pad(mask, 1)
    out = np.zeros_like(mask)
    h, w = mask.shape
    for dy in range(3):
        for dx in range(3):
            out |= p[dy:dy + h, dx:dx + w]
    return out


def diff_grids(ranks_a, meta_a: dict[str, Any], ranks_b, meta_b: dict[str, Any]) -> Diff:
    """``ranks_*``:``map_preview.grid_ranks`` 的结果(0 占据 / 1 空闲 / 2 未知);``meta_*``:
    ``map_preview.parse_map_yaml`` 的结果。"""
    import numpy as np
    ha, wa = ranks_a.shape
    hb, wb = ranks_b.shape
    res = min(meta_a["resolution"], meta_b["resolution"])
    ax0, ay0 = meta_a["origin"]
    bx0, by0 = meta_b["origin"]
    x0 = min(ax0, bx0)
    y0 = min(ay0, by0)
    x1 = max(ax0 + wa * meta_a["resolution"], bx0 + wb * meta_b["resolution"])
    y1 = max(ay0 + ha * meta_a["resolution"], by0 + hb * meta_b["resolution"])
    w = max(1, int(np.ceil((x1 - x0) / res - 1e-9)))
    h = max(1, int(np.ceil((y1 - y0) / res - 1e-9)))
    if w * h > MAX_CELLS:
        raise DiffError(f"两版并起来的范围太大({w}×{h} 格):这两版的原点相差太远,"
                        "多半不在同一个坐标系里")
    xs = x0 + (np.arange(w) + 0.5) * res
    ys = y0 + (np.arange(h) + 0.5) * res            # 自下而上
    a = _sample(ranks_a, ax0, ay0, meta_a["resolution"], xs, ys)
    b = _sample(ranks_b, bx0, by0, meta_b["resolution"], xs, ys)
    occ_a, occ_b = a == 0, b == 0
    free_a, free_b = a == 1, b == 1
    cls = np.full(a.shape, _SAME_NONE, dtype=np.uint8)
    cls[free_a & free_b] = _SAME_FREE
    cls[occ_a & occ_b] = _SAME_OCC
    cls[free_a & (b == 2)] = _FREE_TO_UNK
    cls[(a == 2) & free_b] = _UNK_TO_FREE
    cls[occ_b & ~occ_a] = _OCC_B
    cls[occ_a & ~occ_b] = _OCC_A              # 墙的差异盖过一切
    cell_m2 = res * res
    counts = {k: int((cls == v).sum()) for k, v in (
        ("same_wall", _SAME_OCC), ("same_free", _SAME_FREE), ("wall_only_a", _OCC_A),
        ("wall_only_b", _OCC_B), ("free_only_a", _FREE_TO_UNK), ("free_only_b", _UNK_TO_FREE))}
    area = {k: round(v * cell_m2, 2) for k, v in counts.items()}
    # 粗判:A 的墙有多少在 B 的墙附近
    n_a, n_b = int(occ_a.sum()), int(occ_b.sum())
    share = None
    if n_a >= ALIGN_MIN_WALLS and n_b >= ALIGN_MIN_WALLS:
        share = round(float((occ_a & _near(occ_b)).sum()) / n_a, 3)
    misaligned = share is not None and share < ALIGN_OK_SHARE
    # 画:自上而下;太大按整数倍缩(取优先级大的)
    img = cls[::-1]
    k = max(1, -(-max(w, h) // MAX_SIDE))
    if k > 1:
        ph, pw = -(-h // k) * k, -(-w // k) * k
        pad = np.full((ph, pw), _SAME_NONE, dtype=np.uint8)
        pad[:h, :w] = img
        img = pad.reshape(ph // k, k, pw // k, k).max(axis=(1, 3))
    lut = np.array([_PALETTE[i] for i in range(7)], dtype=np.uint8)
    rgb = lut[img]
    oh, ow = rgb.shape[:2]
    stats = {"width": ow, "height": oh, "m_per_px": res * k, "left_x": x0, "top_y": y0 + h * res,
             "resolution_m": res, "counts": counts, "area_m2": area,
             "colors": {k2: "#{:02x}{:02x}{:02x}".format(*v) for k2, v in COLORS.items()},
             "alignment": share, "likely_misaligned": misaligned,
             "same_resolution": abs(meta_a["resolution"] - meta_b["resolution"]) < 1e-9,
             "caveat": CAVEAT}
    if meta_a.get("origin_yaw") or meta_b.get("origin_yaw"):
        stats["warning"] = "地图 origin 带转角,对比图未按转角转"
    return Diff(png=_png_rgb(ow, oh, rgb.tobytes()), stats=stats)
