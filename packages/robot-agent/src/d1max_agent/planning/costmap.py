"""规划代价图(W10 设计稿 §3):``floor.pgm/.yaml`` + 禁行区 → 0.1 m 栅格上的
「硬挡」「致命」「软代价」「限速」。

- **硬挡** ``hard``:占用、未知、禁行区。起点在硬挡里不许规划(狗在禁行区里 → 停,W08 决定 5)。
- **致命** ``lethal``:离硬挡的格心距离 ≤ 机体外接圆半径(W08 决定 6:要原地转)。
- **软代价** ``cost``:致命区外 0.3 m 的带,越近越贵(0–100),让路径走中间;只影响选路,不影响能不能走。
- **限速** ``speed``:每格的限速(m/s),不限是 ``inf``。

栅格行号从下往上(第 0 行 = 最小 y),跟地图系同向;``floor.pgm`` 的行是从上往下,读进来先上下翻。
降采样只往保守方向:一个粗格里有一个细格不是「可通行」就算硬挡。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from d1max_contract.zones import Zone

PLAN_RES_M = 0.1
ROBOT_RADIUS_M = 0.52
#: 膨胀的余量(W08 决定 6「外接圆半径 + 余量」,内审应修 5):致命区 = 半径 + 余量。
INFLATE_MARGIN_M = 0.05
SOFT_BAND_M = 0.3
SOFT_MAX = 100
#: 规划栅格最多多少格(0.1 m 下 400 万格 = 4 万 m²,庄园约 1.1 万 m²)。
MAX_CELLS = 4_000_000
LETHAL = 255


class CostmapError(Exception):
    pass


# ------------------------------------------------------------ 读 floor.*

def _num(v: str, key: str) -> float:
    try:
        x = float(v)
    except ValueError:
        raise CostmapError(f"floor.yaml 的 {key} 不是数:{v!r}") from None
    if not math.isfinite(x):
        raise CostmapError(f"floor.yaml 的 {key} 不是有限数")
    return x


def parse_yaml(text: str) -> dict:
    """只认 ``map_server`` 的那几个键(一行一个 ``键: 值``),不引 YAML 库(跟站点预览同一套认法)。"""
    raw: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" in line:
            k, v = line.split(":", 1)
            raw[k.strip()] = v.strip().strip("'\"")
    if "resolution" not in raw or "origin" not in raw:
        raise CostmapError("floor.yaml 里要有 resolution、origin")
    res = _num(raw["resolution"], "resolution")
    if not 0.01 <= res <= 1.0:
        raise CostmapError(f"floor.yaml 的 resolution 不合理:{res}")
    o = [s.strip() for s in raw["origin"].strip("[]").split(",")]
    if len(o) < 2:
        raise CostmapError("floor.yaml 的 origin 要是 [x, y, yaw]")
    yaw = _num(o[2], "origin") if len(o) > 2 and o[2] else 0.0
    if abs(yaw) > 1e-9:
        raise CostmapError("floor.yaml 的 origin 带旋转,规划不认")
    return {"resolution": res, "origin": (_num(o[0], "origin"), _num(o[1], "origin")),
            "negate": raw.get("negate", "0") not in ("0", "false", "False"),
            "occupied_thresh": _num(raw.get("occupied_thresh", "0.65"), "occupied_thresh"),
            "free_thresh": _num(raw.get("free_thresh", "0.196"), "free_thresh")}


def parse_pgm(data: bytes) -> np.ndarray:
    """P5 二进制灰度(建图流水线只写这种)→ ``(高, 宽)`` uint8,从上往下。"""
    toks: list[bytes] = []
    pos = 0
    while len(toks) < 4:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            nl = data.find(b"\n", pos)
            pos = len(data) if nl < 0 else nl + 1
            continue
        end = pos
        while end < len(data) and not data[end:end + 1].isspace():
            end += 1
        if end == pos:
            raise CostmapError("floor.pgm 头不完整")
        toks.append(data[pos:end])
        pos = end
    if toks[0] != b"P5":
        raise CostmapError(f"floor.pgm 不是 P5({toks[0][:4]!r})")
    try:
        w, h, maxval = (int(t) for t in toks[1:])
    except ValueError:
        raise CostmapError("floor.pgm 头里的宽、高、最大值不是整数") from None
    if w < 1 or h < 1 or not 1 <= maxval <= 255:
        raise CostmapError(f"floor.pgm 的宽高或最大值不对({w}×{h},{maxval})")
    body = data[pos + 1:pos + 1 + w * h]
    if len(body) != w * h:
        raise CostmapError(f"floor.pgm 短了:要 {w * h} 字节,只有 {len(body)}")
    img = np.frombuffer(body, dtype=np.uint8).reshape(h, w)
    if maxval != 255:
        img = (img.astype(np.uint32) * 255 // maxval).astype(np.uint8)
    return img


def load_floor(map_dir: Path) -> tuple[np.ndarray, float, tuple[float, float]]:
    """→ (``free`` 布尔栅格,行从下往上;分辨率;左下角那一格的角在地图系的 (x, y))。
    按 ``map_server`` 的三值规则:占用概率 < ``free_thresh`` 才算可通行,未知、占用都不算。"""
    meta = parse_yaml((map_dir / "floor.yaml").read_text(encoding="utf-8"))
    img = parse_pgm((map_dir / "floor.pgm").read_bytes())
    occ = img.astype(np.float32) / 255.0
    if not meta["negate"]:
        occ = 1.0 - occ
    free = occ < meta["free_thresh"]
    return np.flipud(free).copy(), meta["resolution"], meta["origin"]


# ------------------------------------------------------------ 几何

def _disk(r_cells: float) -> list[tuple[int, int]]:
    n = int(math.floor(r_cells))
    return [(dy, dx) for dy in range(-n, n + 1) for dx in range(-n, n + 1)
            if dy * dy + dx * dx <= r_cells * r_cells + 1e-9]


def dilate(mask: np.ndarray, r_cells: float) -> np.ndarray:
    """格心距离 ≤ ``r_cells`` 格的都算上(圆盘,精确到格心)。"""
    out = mask.copy()
    h, w = mask.shape
    for dy, dx in _disk(r_cells):
        if dy == 0 and dx == 0:
            continue
        ys, yd = (slice(0, h - dy), slice(dy, h)) if dy >= 0 else (slice(-dy, h), slice(0, h + dy))
        xs, xd = (slice(0, w - dx), slice(dx, w)) if dx >= 0 else (slice(-dx, w), slice(0, w + dx))
        out[yd, xd] |= mask[ys, xs]
    return out


def _chamfer(seed: np.ndarray, limit: float) -> np.ndarray:
    """到 ``seed`` 的近似距离(格,8 邻接路径长,≥ 欧氏距离),超过 ``limit`` 的记 ``limit``。
    只用来算软代价,不管能不能走。"""
    d = np.where(seed, 0.0, limit).astype(np.float32)
    r2 = math.sqrt(2.0)
    h, w = d.shape
    for _ in range(2):
        for y in range(1, h):
            row = d[y]
            np.minimum(row, d[y - 1] + 1.0, out=row)
            np.minimum(row[1:], d[y - 1, :-1] + r2, out=row[1:])
            np.minimum(row[:-1], d[y - 1, 1:] + r2, out=row[:-1])
        for y in range(h - 2, -1, -1):
            row = d[y]
            np.minimum(row, d[y + 1] + 1.0, out=row)
            np.minimum(row[1:], d[y + 1, :-1] + r2, out=row[1:])
            np.minimum(row[:-1], d[y + 1, 1:] + r2, out=row[:-1])
        for x in range(1, w):
            np.minimum(d[:, x], d[:, x - 1] + 1.0, out=d[:, x])
        for x in range(w - 2, -1, -1):
            np.minimum(d[:, x], d[:, x + 1] + 1.0, out=d[:, x])
    return d


def polygon_mask(poly: tuple[tuple[float, float], ...], shape: tuple[int, int], res: float,
                 origin: tuple[float, float], margin_m: float = 0.0) -> np.ndarray:
    """多边形压到的格:格心在多边形里,或离多边形 ≤ 半格对角线 + ``margin_m``(往保守方向)。"""
    h, w = shape
    pad = res * math.sqrt(0.5) + margin_m
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    c0 = max(0, int(math.floor((min(xs) - pad - origin[0]) / res)))
    c1 = min(w - 1, int(math.floor((max(xs) + pad - origin[0]) / res)))
    r0 = max(0, int(math.floor((min(ys) - pad - origin[1]) / res)))
    r1 = min(h - 1, int(math.floor((max(ys) + pad - origin[1]) / res)))
    out = np.zeros(shape, dtype=bool)
    if c0 > c1 or r0 > r1:
        return out
    cx = origin[0] + (np.arange(c0, c1 + 1) + 0.5) * res
    cy = origin[1] + (np.arange(r0, r1 + 1) + 0.5) * res
    X, Y = np.meshgrid(cx, cy)
    inside = np.zeros(X.shape, dtype=bool)
    dist2 = np.full(X.shape, np.inf)
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        crosses = (ay > Y) != (by > Y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xc = ax + (Y - ay) * (bx - ax) / (by - ay)
        inside ^= crosses & (X < xc)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0.0, 1.0) if L2 else 0.0
        dist2 = np.minimum(dist2, (X - (ax + t * dx)) ** 2 + (Y - (ay + t * dy)) ** 2)
    out[r0:r1 + 1, c0:c1 + 1] = inside | (dist2 <= pad * pad)
    return out


# ------------------------------------------------------------ 代价图

@dataclass
class Costmap:
    res: float
    origin: tuple[float, float]
    hard: np.ndarray        # bool (h, w)
    lethal: np.ndarray      # bool
    cost: np.ndarray        # uint8:LETHAL 或 0–SOFT_MAX
    speed: np.ndarray       # float32,m/s,不限是 inf

    @property
    def shape(self) -> tuple[int, int]:
        return self.hard.shape

    def cell_of(self, x: float, y: float) -> tuple[int, int] | None:
        c = int(math.floor((x - self.origin[0]) / self.res))
        r = int(math.floor((y - self.origin[1]) / self.res))
        h, w = self.shape
        return (r, c) if 0 <= r < h and 0 <= c < w else None

    def center(self, r: int, c: int) -> tuple[float, float]:
        return (self.origin[0] + (c + 0.5) * self.res, self.origin[1] + (r + 0.5) * self.res)

    def speed_at(self, x: float, y: float) -> float:
        rc = self.cell_of(x, y)
        return math.inf if rc is None else float(self.speed[rc])


def downsample(free: np.ndarray, res: float,
               plan_res: float = PLAN_RES_M) -> tuple[np.ndarray, float]:
    """细栅格 → 规划分辨率的「硬挡」:一个粗格里有一个细格不可通行就算挡(只往保守方向)。"""
    k = max(1, int(round(plan_res / res)))
    h, w = free.shape
    H, W = -(-h // k), -(-w // k)
    if H * W > MAX_CELLS:
        raise CostmapError(f"规划栅格太大({W}×{H} 格)")
    pad = np.zeros((H * k, W * k), dtype=bool)          # 补出来的边当不可通行
    pad[:h, :w] = free
    blocked = ~pad.reshape(H, k, W, k).all(axis=(1, 3))
    return blocked, res * k


def build(blocked: np.ndarray, res: float, origin: tuple[float, float],
          zones: tuple[Zone, ...] = (), *, robot_radius_m: float = ROBOT_RADIUS_M,
          nogo_margin_m: float = 0.0) -> Costmap:
    """静态硬挡 + 区域 → 代价图。``nogo_margin_m``:禁行区按定位 σ 额外外扩(W08 决定 4)。"""
    hard = blocked.copy()
    speed = np.full(blocked.shape, np.inf, dtype=np.float32)
    for z in zones:
        if z.kind == "nogo":
            hard |= polygon_mask(z.polygon, blocked.shape, res, origin, nogo_margin_m)
        else:
            m = polygon_mask(z.polygon, blocked.shape, res, origin)
            speed[m] = np.minimum(speed[m], np.float32(z.max_speed_mps))
    lethal = dilate(hard, robot_radius_m / res)
    band = SOFT_BAND_M / res
    d = _chamfer(lethal, band + 1.0)
    soft = np.clip((band + 1.0 - d) / (band + 1.0) * SOFT_MAX, 0, SOFT_MAX).astype(np.uint8)
    cost = np.where(lethal, np.uint8(LETHAL), soft).astype(np.uint8)
    return Costmap(res=res, origin=origin, hard=hard, lethal=lethal, cost=cost, speed=speed)


def from_map_dir(map_dir: Path, zones: tuple[Zone, ...] = (), **kw) -> Costmap:
    free, res, origin = load_floor(map_dir)
    blocked, pres = downsample(free, res)
    return build(blocked, pres, origin, zones, **kw)
