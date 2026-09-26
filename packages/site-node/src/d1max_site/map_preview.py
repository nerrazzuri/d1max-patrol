"""建好的图的预览(W00c6h):把栅格图(ROS ``map_server`` 那一套:``.yaml`` + ``.pgm``)渲染成灰度 PNG,
给手机看「建出来的图对不对」。站点主机上没有 PIL:纯 Python 解 PGM、编 PNG(``zlib``)。

- 颜色照 ``map_server`` 的三值:``occ = (255 − p) / 255``(``negate: 1`` 时 ``p / 255``),大于
  ``occupied_thresh`` 占据(黑)、小于 ``free_thresh`` 空闲(白)、之间未知(灰)。
- 最大边 ``MAX_SIDE``:大图按整数倍缩,**一格里有占据就算占据**(一像素宽的墙不能缩没了),其次空闲、
  再其次未知。
- 给手机的坐标换算(``meta``):``m_per_px``(预览上一像素几米)、``left_x``(左边缘的地图 x)、``top_y``
  (上边缘的地图 y);地图 (x, y) 在预览上是 ``((x − left_x) / m_per_px, (top_y − y) / m_per_px)``。
  ``origin`` 的转角不认(建图工具出的都是 0)。
"""

from __future__ import annotations

import math
import re
import struct
import zlib
from typing import Any

MAX_SIDE = 1024
#: 原图最多这么多像素(8000×8000 的 0.05 m 图是 400 m 见方,比庄园大得多):再大多半是坏的。
MAX_PIXELS = 64_000_000

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
            "negate": raw.get("negate", "0") not in ("0", "false", "False"),
            "occupied_thresh": _num(raw.get("occupied_thresh", "0.65"), "occupied_thresh"),
            "free_thresh": _num(raw.get("free_thresh", "0.196"), "free_thresh")}


_TOKEN = re.compile(rb"\s*(?:#[^\n]*\n\s*)*(\S+)")


def parse_pgm(data: bytes) -> tuple[int, int, bytes]:
    """→ (宽, 高, 每像素一个字节的栅格,从上往下)。认 P5(二进制)、P2(文本),最大值 ≤ 255。"""
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
    if magic == b"P5":
        body = data[pos + 1:pos + 1 + w * h]          # 头后面正好一个空白
        if len(body) != w * h:
            raise PreviewError(f"PGM 短了:要 {w * h} 字节,只有 {len(body)}")
    else:
        vals = data[pos:].split()
        if len(vals) < w * h:
            raise PreviewError(f"PGM 短了:要 {w * h} 个数,只有 {len(vals)}")
        try:
            body = bytes(int(v) for v in vals[:w * h])
        except ValueError:
            raise PreviewError("PGM 的像素不是 0–255 的整数") from None
    if maxval != 255:
        body = bytes(min(255, v * 255 // maxval) for v in body)
    return w, h, body


def _png(w: int, h: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render(pgm: bytes, meta: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """→ (PNG, 给手机的坐标换算)。"""
    w, h, body = parse_pgm(pgm)
    lut = bytearray(256)
    for p in range(256):
        occ = p / 255.0 if meta["negate"] else (255 - p) / 255.0
        lut[p] = (_RANK_OCC if occ > meta["occupied_thresh"]
                  else _RANK_FREE if occ < meta["free_thresh"] else _RANK_UNKNOWN)
    ranks = body.translate(bytes(lut))
    k = max(1, -(-max(w, h) // MAX_SIDE))
    rows = []
    for top in range(0, h, k):
        band = [ranks[r * w:(r + 1) * w] for r in range(top, min(h, top + k))]
        col = band[0] if len(band) == 1 else bytes(map(min, *band))
        if k > 1:
            col = bytes(min(col[i:i + k]) for i in range(0, w, k))
        rows.append(col.translate(_COLORS))
    res = meta["resolution"]
    ox, oy = meta["origin"]
    info = {"width": len(rows[0]), "height": len(rows), "m_per_px": res * k, "left_x": ox,
            "top_y": oy + h * res}
    return _png(info["width"], info["height"], rows), info
