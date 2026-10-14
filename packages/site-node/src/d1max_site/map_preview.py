"""建好的图的预览(W00c6h):把栅格图(ROS ``map_server`` 那一套:``.yaml`` + ``.pgm``)渲染成灰度 PNG,
给手机看「建出来的图对不对」。站点主机上没有 PIL:纯 Python 解 PGM、编 PNG(``zlib``)。

- 颜色照 ``map_server`` 的三值:``occ = (255 − p) / 255``(``negate: 1`` 时 ``p / 255``),大于
  ``occupied_thresh`` 占据(黑)、小于 ``free_thresh`` 空闲(白)、之间未知(灰)。
- 最大边 ``MAX_SIDE``:大图按整数倍缩,**一格里有占据就算占据**(一像素宽的墙不能缩没了),其次空闲、
  再其次未知。
- 给手机的坐标换算(``meta``):``m_per_px``(预览上一像素几米)、``left_x``(左边缘的地图 x)、``top_y``
  (上边缘的地图 y);地图 (x, y) 在预览上是 ``((x − left_x) / m_per_px, (top_y − y) / m_per_px)``。
  ``origin`` 的转角不认(建图工具出的都是 0);不是 0 时 ``meta`` 里带一句 ``warning``。
- **资源有上限**(W00c6h 内审):图最多 ``MAX_PIXELS`` 像素;P5 文件不许比头里说的大;P2(文本,一个像素
  几个字节、解析吃内存)文件最多 ``MAX_P2_BYTES``;yaml 最多 ``MAX_YAML_BYTES``。调用方先按
  :func:`pgm_limit` 看文件大小,再读。
"""

from __future__ import annotations

import math
import re
import struct
import zlib
from typing import Any

MAX_SIDE = 1024
#: 原图最多这么多像素(5000×5000 的 0.05 m 图是 250 m 见方,庄园约 106 m 见方):再大多半是坏的。
MAX_PIXELS = 25_000_000
#: 文本 PGM(P2)文件最多这么大(字节)。建图工具出的都是 P5。
MAX_P2_BYTES = 8 * 1024 * 1024
MAX_YAML_BYTES = 64 * 1024
#: P5 正文之后容许的尾巴(比如多一个换行)。
_P5_SLACK = 16

_RANK_OCC, _RANK_FREE, _RANK_UNKNOWN = 0, 1, 2
_COLORS = bytes([0, 254, 205]) + bytes(253)   # 名次 → 灰度(取 min 缩图:占据 < 空闲 < 未知)


class PreviewError(ValueError):
    """这张图预览不了(文件坏了)。消息给人看。"""


class NoRaster(PreviewError):
    """这张图没有栅格(比如点云图):不是坏了,是没东西可画。"""


def _num(v: str, key: str) -> float:
    try:
        x = float(v)
    except ValueError:
        raise PreviewError(f"地图 yaml 的 {key} 不是数:{v!r}") from None
    if not math.isfinite(x):
        raise PreviewError(f"地图 yaml 的 {key} 不是有限数")
    return x


def parse_map_yaml(text: str) -> dict[str, Any]:
    """只认 ``map_server`` 用到的那几个键(一行一个 ``键: 值``),不引 YAML 库。"""
    raw: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        raw[k.strip()] = v.strip().strip("'\"")
    if "image" not in raw or "resolution" not in raw or "origin" not in raw:
        raise PreviewError("地图 yaml 里要有 image、resolution、origin")
    res = _num(raw["resolution"], "resolution")
    if res <= 0:
        raise PreviewError("地图 yaml 的 resolution 要大于 0")
    origin = [s.strip() for s in raw["origin"].strip("[]").split(",")]
    if len(origin) < 2:
        raise PreviewError("地图 yaml 的 origin 要是 [x, y, yaw]")
    return {"image": raw["image"], "resolution": res,
            "origin": (_num(origin[0], "origin"), _num(origin[1], "origin")),
            "origin_yaw": _num(origin[2], "origin") if len(origin) > 2 and origin[2] else 0.0,
            "negate": raw.get("negate", "0") not in ("0", "false", "False"),
            "occupied_thresh": _num(raw.get("occupied_thresh", "0.65"), "occupied_thresh"),
            "free_thresh": _num(raw.get("free_thresh", "0.196"), "free_thresh")}


_TOKEN = re.compile(rb"\s*(?:#[^\n]*\n\s*)*(\S+)")


def _header(data: bytes) -> tuple[bytes, int, int, int, int]:
    """PGM 头 → (类型, 宽, 高, 最大值, 头结束的位置)。"""
    pos, head = 0, []
    for _ in range(4):
        m = _TOKEN.match(data, pos)
        if m is None:
            raise PreviewError("PGM 头不完整")
        head.append(m.group(1))
        pos = m.end()
    magic = head[0]
    if magic not in (b"P5", b"P2"):
        raise PreviewError(f"不是灰度 PGM({magic[:4]!r})")
    try:
        w, h, maxval = (int(x) for x in head[1:])
    except ValueError:
        raise PreviewError("PGM 头里的宽、高、最大值不是整数") from None
    if w < 1 or h < 1 or not 1 <= maxval <= 255:
        raise PreviewError(f"PGM 的宽高或最大值不对({w}×{h},{maxval})")
    if w * h > MAX_PIXELS:
        raise PreviewError(f"PGM 太大({w}×{h})")
    return magic, w, h, maxval, pos


def pgm_limit(head: bytes) -> int:
    """按文件开头(头就在前几百字节)算这个 PGM 文件最多多大:调用方先比文件大小,超了不读。"""
    magic, w, h, _, pos = _header(head)
    return pos + 1 + w * h + _P5_SLACK if magic == b"P5" else MAX_P2_BYTES


def parse_pgm(data: bytes) -> tuple[int, int, bytes]:
    """→ (宽, 高, 每像素一个字节的栅格,从上往下)。认 P5(二进制)、P2(文本),最大值 ≤ 255。"""
    magic, w, h, maxval, pos = _header(data)
    if magic == b"P5":
        body = data[pos + 1:pos + 1 + w * h]          # 头后面正好一个空白
        if len(body) != w * h:
            raise PreviewError(f"PGM 短了:要 {w * h} 字节,只有 {len(body)}")
        if len(data) > pos + 1 + w * h + _P5_SLACK:
            raise PreviewError(f"PGM 比头里说的大({len(data)} 字节,头里说 {w}×{h})")
    else:
        if len(data) > MAX_P2_BYTES:
            raise PreviewError(f"文本 PGM 太大({len(data)} 字节,最多 {MAX_P2_BYTES})")
        vals = data[pos:].split()
        if len(vals) < w * h:
            raise PreviewError(f"PGM 短了:要 {w * h} 个数,只有 {len(vals)}")
        try:
            body = bytes(int(v) for v in vals[:w * h])
        except ValueError:
            raise PreviewError("PGM 的像素不是 0–255 的整数") from None
    if maxval != 255:
        body = body.translate(bytes(min(255, v * 255 // maxval) for v in range(256)))
    return w, h, body


def _png(w: int, h: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _rank_lut(meta: dict[str, Any]) -> bytes:
    """灰度 → 名次(占据 / 空闲 / 未知),照 ``map_server`` 的三值。"""
    lut = bytearray(256)
    for p in range(256):
        occ = p / 255.0 if meta["negate"] else (255 - p) / 255.0
        lut[p] = (_RANK_OCC if occ > meta["occupied_thresh"]
                  else _RANK_FREE if occ < meta["free_thresh"] else _RANK_UNKNOWN)
    return bytes(lut)


def _shrink(w: int, h: int) -> int:
    """预览比原图缩几倍(整数,最大边不超过 ``MAX_SIDE``)。"""
    return max(1, -(-max(w, h) // MAX_SIDE))


def render(pgm: bytes, meta: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """→ (PNG, 给手机的坐标换算)。"""
    w, h, body = parse_pgm(pgm)
    ranks = body.translate(_rank_lut(meta))
    k = _shrink(w, h)
    rows = []
    for top in range(0, h, k):
        band = [ranks[r * w:(r + 1) * w] for r in range(top, min(h, top + k))]
        col = band[0] if len(band) == 1 else bytes(map(min, *band))
        if k > 1:
            col = bytes(min(col[i:i + k]) for i in range(0, w, k))
        rows.append(col.translate(_COLORS))
    res = meta["resolution"]
    ox, oy = meta["origin"]
    info: dict[str, Any] = {"width": len(rows[0]), "height": len(rows), "m_per_px": res * k,
                            "left_x": ox, "top_y": oy + h * res}
    if meta.get("origin_yaw"):
        info["warning"] = (f"地图 origin 带转角 {meta['origin_yaw']:.3f} rad,预览没按转角转:"
                           "待命点画的位置可能不准")
    return _png(info["width"], info["height"], rows), info


# ------------------------------------------------------------ 给客户看的图(地图显示改进)
#
# 下面两样只管「显示」:导航栅格、狗用的坐标一样都不动。numpy 在站点上有
# (``d1max-contract[planning]``),用到才引。
#
# - :func:`wall_rotation`:建筑在图上常是斜的。按墙的走向算一个**建议的显示转角** ``rotation_deg``:
#   把预览图绕中心**在屏幕上顺时针**转这么多度(屏幕 y 朝下;跟 CSS ``rotate(θdeg)``、Flutter
#   ``Transform.rotate(angle: θ·π/180)`` 一个方向),主要的墙就横平竖直。落在 [-45, 45);
#   算不出来给 0。办法:占据格的「投影尖不尖」(扫描件纠斜的老办法)。把占据格投到方向 φ 和它的
#   垂直方向上做核密度,墙顺着 φ 的时候投影挤在一条线上、平方和最大;两个方向一起算,φ 以 90° 为周期。
#   杂物、噪点各个方向都摊开,只抬底、不挪峰;大块的起伏(杂物成片、几栋楼排成一行)先减掉滑动平均
#   再算。格子是方的:核取得够宽(σ ≥ 0.6 格),格点的周期性就不会让 0°、45° 平白占便宜。先 0.5°
#   一档粗扫(σ 1 格),再在最好的那档 ±0.75° 里 0.05° 一档细扫(σ 0.6 格),抛物线插值。峰不比
#   中位数高出 ``ROT_MIN_CONTRAST`` 倍(圆形场地、只有噪点)算不出来,给 0;不到 ``ROT_SNAP_DEG``
#   的也给 0(本来就正,别让客户端为零点几度把整张图转糊)。
# - :func:`render_plan`:「平面图」样子的 ``plan.png``,跟 ``preview.png`` 盖**完全一样**的范围
#   (同样的 ``left_x``、``top_y``、``width × m_per_px`` × ``height × m_per_px`` 米),像素是预览的
#   :func:`plan_scale` 倍(2–4)。灰度 + 透明(PNG 色彩类型 4):未知全透明(露出页面底色),
#   空闲白、不透明(地板),墙黑。墙是「画出来的线」:墙格子先做高斯模糊(单个噪点被抹掉、台阶状的
#   斜墙被抹圆),插值到输出分辨率,再按等值线取阈值、用梯度把边缘抗锯齿到大约一个像素宽;一格宽的
#   直墙画出来约 1.7 格宽,8 连通的斜墙不断。地板边(空闲 / 未知交界)同样处理,零星没扫到的
#   一两格被补上。纯 numpy + zlib。图大的时候先按整数倍合并(墙:一格里有占据就算;地板:取比例),
#   工作栅格不超过 ``PLAN_WORK_CELLS`` 格。

#: 平面图最大边、最多像素;倍数从 4 往下挑能放下的(预览最大边 1024,两倍总放得下)。
PLAN_MAX_SIDE = 2048
PLAN_MAX_PIXELS = PLAN_MAX_SIDE * PLAN_MAX_SIDE
_PLAN_SCALES = (4, 3, 2)
#: 渲染平面图时工作栅格最多这么多格(比这大先按整数倍合并):管住内存(几个 float32 数组)。
PLAN_WORK_CELLS = 4_500_000
#: 墙:模糊的 σ(工作格)、等值线阈值。一格宽的直墙模糊后峰约 0.47、斜墙约 0.33,单个噪点约 0.22。
_WALL_SIGMA, _WALL_LEVEL = 0.8, 0.25
#: 地板(看得见的地方 = 空闲 + 占据):模糊的 σ(工作格)、阈值。
_FLOOR_SIGMA, _FLOOR_LEVEL = 1.2, 0.5

#: 转角:最多拿多少个占据格来算(多了随机抽,种子固定)、少于多少个不算。
ROT_MAX_POINTS = 40_000
ROT_MIN_POINTS = 50
#: 峰要比中位数高这么多倍才算「看得出墙的走向」(实测:只有噪点、圆形场地 1.2–1.4;带一堆杂物的楼
#: 3.5 以上;办公室两张实图 4.8、13.6)。
ROT_MIN_CONTRAST = 2.0
#: 小于这么多度当 0。
ROT_SNAP_DEG = 0.5
#: 投影直方图减掉多宽(格)的滑动平均。
_ROT_HIGHPASS = 12.0


def grid_ranks(pgm: bytes, meta: dict[str, Any]):
    """→ numpy ``uint8`` 数组 (高, 宽),从上往下(跟预览一个方向),每格是名次
    (占据 0 / 空闲 1 / 未知 2)。"""
    import numpy as np
    w, h, body = parse_pgm(pgm)
    return np.frombuffer(body.translate(_rank_lut(meta)), dtype=np.uint8).reshape(h, w)


def _peakiness(p, sigma: float) -> float:
    """投影值 ``p``(格)的核密度(高斯,``sigma`` 格)减掉自己的滑动平均(``_ROT_HIGHPASS`` 格宽)
    再求平方和 —— 墙(很多点挤在一条线上)是尖峰、分高;杂物成片、几栋楼凑巧排成一行这种大块起伏
    被减掉,不算。σ 取到 0.6 格以上,格点(间距 1 格、45° 时 0.71 格)那点周期性被抹得一干二净。"""
    import numpy as np
    step = sigma / 2                                  # 先线性分到细桶里,再用高斯核卷
    m = int(round(_ROT_HIGHPASS / step)) | 1
    lead = m + 8                                      # 两头留够空:两次卷积都不碰边
    t = (p - p.min()) / step + lead
    i = np.floor(t).astype(np.int64)
    f = t - i
    n = int(i.max()) + lead + 2
    hist = np.bincount(i, 1.0 - f, minlength=n) + np.bincount(i + 1, f, minlength=n)
    x = np.arange(-6, 7, dtype=np.float64)            # ±3σ
    hist = np.convolve(hist, np.exp(-x * x / 8.0), mode="same")
    hp = hist - np.convolve(hist, np.full(m, 1.0 / m), mode="same")
    return float(np.dot(hp, hp))


def wall_rotation(ranks) -> float:
    """建议的显示转角(度,[-45, 45)):预览图在屏幕上**顺时针**转这么多,主要的墙横平竖直;算不出来 0。
    ``ranks`` 是 :func:`grid_ranks` 出的(行朝下)。"""
    import numpy as np
    rows, cols = np.nonzero(ranks == _RANK_OCC)
    if rows.size < ROT_MIN_POINTS:
        return 0.0
    if rows.size > ROT_MAX_POINTS:                    # 太多随机抽(种子固定,结果可复现)
        pick = np.random.default_rng(0).choice(rows.size, ROT_MAX_POINTS, replace=False)
        rows, cols = rows[pick], cols[pick]
    u = cols - cols.mean()                            # 屏幕坐标:u 朝右、v 朝下
    v = rows - rows.mean()

    def score(phi: float, sigma: float) -> float:
        c, s = math.cos(math.radians(phi)), math.sin(math.radians(phi))
        return _peakiness(u * c + v * s, sigma) + _peakiness(v * c - u * s, sigma)

    coarse = np.arange(-45.0, 45.0, 0.5)
    sc = np.array([score(a, 1.0) for a in coarse])
    best = int(np.argmax(sc))
    if sc[best] < ROT_MIN_CONTRAST * float(np.median(sc)):
        return 0.0
    fine = coarse[best] + np.arange(-15, 16) * 0.05
    sf = np.array([score(a, 0.6) for a in fine])
    j = int(np.argmax(sf))
    phi = float(fine[j])
    if 0 < j < len(sf) - 1:
        den = sf[j - 1] - 2 * sf[j] + sf[j + 1]
        if den < 0:
            phi += 0.05 * 0.5 * float(sf[j - 1] - sf[j + 1]) / float(den)
    # 墙在屏幕上的方向角是 φ(屏幕 y 朝下,从 +u 往 +v 量);顺时针转 θ 后是 φ + θ,要它是 90° 的整数倍
    theta = round(-phi, 1)
    theta = (theta + 45.0) % 90.0 - 45.0
    return 0.0 if abs(theta) < ROT_SNAP_DEG else round(theta, 1)


def plan_scale(width: int, height: int) -> int:
    """平面图是预览的几倍(2–4):最大边不超过 ``PLAN_MAX_SIDE``、像素不超过 ``PLAN_MAX_PIXELS``。"""
    for s in _PLAN_SCALES:
        if max(width, height) * s <= PLAN_MAX_SIDE and width * height * s * s <= PLAN_MAX_PIXELS:
            return s
    return _PLAN_SCALES[-1]


def _kernel(sigma: float):
    import numpy as np
    r = int(math.ceil(3 * sigma))
    x = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-x * x / np.float32(2 * sigma * sigma))
    return k / k.sum()


#: 模糊时一次处理多少行(临时数组只有这么大)。
_BLUR_ROWS = 256


def _blur(a, sigma: float) -> None:
    """可分离的高斯模糊,**就地**改 ``a``(``float32``)。``a`` 四周已经垫了至少两倍核半径的 0:只算
    里面,最外一圈留 0(离内容超过三个 σ,本来也是 0)。按行分块,临时数组小。"""
    import numpy as np
    k = _kernel(sigma)
    r = len(k) // 2
    hh, ww = a.shape
    tmp = np.zeros_like(a)
    for r0 in range(0, hh, _BLUR_ROWS):               # 横着
        r1 = min(hh, r0 + _BLUR_ROWS)
        dst = tmp[r0:r1, r:ww - r]
        for i, kv in enumerate(k):
            dst += kv * a[r0:r1, i:i + ww - 2 * r]
    a[...] = 0
    for r0 in range(r, hh - r, _BLUR_ROWS):           # 竖着
        r1 = min(hh - r, r0 + _BLUR_ROWS)
        dst = a[r0:r1]
        for i, kv in enumerate(k):
            dst += kv * tmp[r0 - r + i:r1 - r + i]


def _taps(n_out: int, step: float, pad: int):
    """输出第 i 个像素的中心落在工作格的哪儿(格子中心在 j + 0.5;工作格前面垫了 ``pad`` 格)→
    双线性插值的左格、权重。"""
    import numpy as np
    x = (np.arange(n_out, dtype=np.float64) + 0.5) * step - 0.5 + pad
    i0 = np.floor(x).astype(np.int64)
    return i0, (x - i0).astype(np.float32)


def _resample(a, xs, ys):
    """可分离的双线性插值:先横后竖(就地算,输出大小的数组同时最多两个)。"""
    x0, wx = xs
    y0, wy = ys
    tmp = a[:, x0]
    tmp *= 1 - wx
    nxt = a[:, x0 + 1]
    nxt *= wx
    tmp += nxt
    del nxt
    out = tmp[y0, :]
    out *= (1 - wy)[:, None]
    nxt = tmp[y0 + 1, :]
    nxt *= wy[:, None]
    out += nxt
    return out


def _coverage(field, level: float):
    """等值线 ``level`` 以上的覆盖率:按梯度把边缘抗锯齿到大约一个像素宽。就地改 ``field``。"""
    import numpy as np
    gy, gx = np.gradient(field)
    np.hypot(gx, gy, out=gx)
    del gy
    np.maximum(gx, np.float32(1e-6), out=gx)
    field -= np.float32(level)
    field /= gx
    field += np.float32(0.5)
    return np.clip(field, 0.0, 1.0, out=field)


def _plan_pixels(ranks, k: int, width: int, height: int, s: int):
    """→ ``uint8`` 数组 (height·s, width·s, 2):灰度、透明度。``k``:预览比原图缩的倍数;预览盖
    ``height·k`` × ``width·k`` 个原图格(可能比原图多出一点,多出来的当未知)。"""
    import numpy as np
    h, w = ranks.shape
    ext_h, ext_w = height * k, width * k
    f = max(1, k // s)                                # 输出一像素不止一格:先按整数倍合并
    while -(-ext_h // f) * -(-ext_w // f) > PLAN_WORK_CELLS:
        f += 1
    gh, gw = -(-ext_h // f), -(-ext_w // f)           # 工作格
    pad = 2 * (len(_kernel(max(_WALL_SIGMA, _FLOOR_SIGMA))) // 2) + 2
    step = k / (s * f)                                # 工作格 / 输出像素
    out_h, out_w = height * s, width * s
    xs, ys = _taps(out_w, step, pad), _taps(out_h, step, pad)

    def layer(mask, any_cell: bool, sigma: float, level: float):
        """原图格上的真假 → 合并成工作格(``any_cell``:有一格算一格,否则取比例)→ 垫边 → 模糊 →
        插值到输出 → 覆盖率。一层一层做,原图大小的只有真假数组,省内存。"""
        work = np.zeros((gh + 2 * pad, gw + 2 * pad), dtype=np.float32)
        inner = work[pad:pad + gh, pad:pad + gw]
        if f == 1:
            inner[:h, :w] = mask
        else:
            full = np.zeros((gh * f, gw * f), dtype=np.uint8)
            full[:h, :w] = mask
            blocks = full.reshape(gh, f, gw, f)
            inner[...] = (blocks.max(axis=(1, 3)) if any_cell
                          else blocks.sum(axis=(1, 3), dtype=np.uint32) / np.float32(f * f))
            del full, blocks
        _blur(work, sigma)
        return _coverage(_resample(work, xs, ys), level)

    # 墙:一格里有占据就算(细墙合并后不丢);地板(看得见的 = 空闲 + 占据):取比例
    wall_a = layer(ranks == _RANK_OCC, True, _WALL_SIGMA, _WALL_LEVEL)
    floor_a = layer(ranks != _RANK_UNKNOWN, False, _FLOOR_SIGMA, _FLOOR_LEVEL)
    # 墙盖在地板上:透明度 = 墙 + 地板 × (1 − 墙);灰度 = 地板 × (1 − 墙) / 透明度(墙黑、地板白)
    floor_a *= 1 - wall_a
    alpha = wall_a
    alpha += floor_a
    px = np.empty((out_h, out_w, 2), dtype=np.uint8)
    px[..., 1] = np.rint(alpha * 255)
    # 全透明的地方灰度给白(地板色):不预乘透明度的缩放也不会在边上渗出黑边
    gray = np.divide(floor_a, alpha, out=np.ones_like(alpha), where=px[..., 1] > 0)
    px[..., 0] = np.rint(gray * 255)
    return px


def _png_ga(px) -> bytes:
    """灰度 + 透明(色彩类型 4)、8 位的 PNG;每行用 Up 滤波(跟上一行相减,大片一样的颜色压得更小)。"""
    import numpy as np
    out_h, out_w = px.shape[:2]
    flat = px.reshape(out_h, out_w * 2)
    raw = np.empty((out_h, out_w * 2 + 1), dtype=np.uint8)
    raw[:, 0] = 2
    raw[0, 1:] = flat[0]
    np.subtract(flat[1:], flat[:-1], out=raw[1:, 1:])   # uint8:按 256 取模,正是 PNG 要的

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", out_w, out_h, 8, 4, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


def render_plan(ranks) -> tuple[bytes, int]:
    """``ranks``(:func:`grid_ranks` 出的)→ (平面图 PNG, 倍数)。范围跟 :func:`render` 出的预览
    一模一样,像素是它的倍数倍。"""
    h, w = ranks.shape
    k = _shrink(w, h)
    width, height = -(-w // k), -(-h // k)
    s = plan_scale(width, height)
    return _png_ga(_plan_pixels(ranks, k, width, height, s)), s
