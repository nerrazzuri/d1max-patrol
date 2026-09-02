"""把一张**存好的**地图读成页面能画的占据栅格。

建图跑完之后 ``map_saver_cli`` 在 ``maps/`` 下留一对文件:``<图名>.pgm``
(像素)和 ``<图名>.yaml``(分辨率、原点、阈值)。这是 ROS 那边的标准格式,
Nav2 也是读它来定位的。页面要画底图,读的就是同一对文件 —— 人在页面上看
见的图,和机器人正在用来定位的图,是同一张。

**为什么不直接把 pgm 发给浏览器。** 浏览器不认 pgm,而且像素值和占据概率
不是一回事(要过 ``occupied_thresh`` / ``free_thresh`` 两个阈值,还要分出
"未知")。转成占据栅格再用和地图桥同一套 RLE 编码,页面就只有一份解码和
一份画图逻辑,不管图是从桥上来的还是从盘上读的。

**行序要翻。** pgm 的第一行是图像**最上面**那行,而 ``OccupancyGrid`` 的
第一行是**最下面**那行(y 最小)。不翻的话整张图上下颠倒,而且颠倒的图看
起来仍然"像一张图",人不一定看得出来 —— 直到标的点全错。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from d1max_patrol.protocol.map_frames import MapFrame, encode_rle

#: 读地图 yaml 时这几个键必须有。缺了就说清楚缺哪个,别给一个默认值 ——
#: 分辨率蒙错一倍,标出来的点位就偏一倍。
_REQUIRED = ("image", "resolution", "origin")


class GridError(RuntimeError):
    """地图读不出来。消息是给人看的,会显示在页面上。"""


@dataclass(frozen=True, slots=True)
class Grid:
    """一张占据栅格。字段和 ``MapFrame`` 对齐,只是多了个来源。"""

    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    cells: tuple[int, ...]
    #: ``saved`` 是盘上存好的图,``live`` 是建图桥正在推的。页面靠它决定
    #: 要不要标"这张图还在长"。
    source: str = "saved"

    def to_wire(self) -> dict[str, Any]:
        return {
            "width": self.width, "height": self.height,
            "resolution": self.resolution,
            "origin_x": self.origin_x, "origin_y": self.origin_y,
            "origin_yaw": self.origin_yaw,
            "rle": encode_rle(self.cells),
            "source": self.source,
        }


def _header_tokens(raw: bytes) -> tuple[list[bytes], int]:
    """读 pgm 头:magic、宽、高、最大值。返回 (四个词, 像素起始位置)。

    pgm 的头是"空白分隔的词",换行和空格等价,``#`` 到行尾是注释 ——
    按行切会在某些工具生成的文件上读错。
    """
    tokens: list[bytes] = []
    pos = 0
    while len(tokens) < 4:
        while pos < len(raw) and raw[pos:pos + 1].isspace():
            pos += 1
        if pos < len(raw) and raw[pos:pos + 1] == b"#":
            while pos < len(raw) and raw[pos:pos + 1] != b"\n":
                pos += 1
            continue
        start = pos
        while pos < len(raw) and not raw[pos:pos + 1].isspace():
            pos += 1
        if start == pos:
            raise GridError("pgm 头读到一半就没了")
        tokens.append(raw[start:pos])
    return tokens, pos + 1        # 头之后**恰好一个**空白字节,再往后是像素


def read_pgm(path: Path) -> tuple[int, int, bytes]:
    """读一个二进制 pgm(P5)。返回 (宽, 高, 逐像素的字节)。

    只认 P5(二进制)和 maxval 255:``map_saver_cli`` 出的就是这一种。别的
    变体宁可明确报错,也不要猜着解 —— 解错的地图会让人在错的位置标点。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GridError(f"{path.name} 读不了: {exc}") from exc
    tokens, offset = _header_tokens(raw)
    magic, width_raw, height_raw, maxval_raw = tokens
    if magic != b"P5":
        raise GridError(f"{path.name} 不是二进制 pgm(头是 {magic!r})")
    try:
        width, height, maxval = (int(width_raw), int(height_raw),
                                 int(maxval_raw))
    except ValueError as exc:
        raise GridError(f"{path.name} 的头里有读不成数的东西: {exc}") from exc
    if maxval != 255:
        raise GridError(f"{path.name} 的最大灰度是 {maxval},只认 255")
    pixels = raw[offset:offset + width * height]
    if len(pixels) != width * height:
        raise GridError(
            f"{path.name} 说自己是 {width}x{height},实际只有 {len(pixels)} 个像素")
    return width, height, pixels


def _cells(width: int, height: int, pixels: bytes, *, negate: bool,
           occupied: float, free: float) -> tuple[int, ...]:
    """像素 -> 占据值,顺便把行序翻过来。

    换算用的是 ROS ``map_server`` 那套:亮的是空地、暗的是障碍,中间那段
    是"不知道"。**中间段必须是 -1 而不是 50**:未知区域画成半占据,页面上
    看起来就像整张图都被墙围住了。
    """
    out: list[int] = []
    for row in range(height - 1, -1, -1):
        base = row * width
        for col in range(width):
            value = pixels[base + col]
            shade = value / 255.0 if negate else (255 - value) / 255.0
            if shade > occupied:
                out.append(100)
            elif shade < free:
                out.append(0)
            else:
                out.append(-1)
    return tuple(out)


def load_saved(maps_dir: Path, map_id: str) -> Grid:
    """读 ``<maps_dir>/<map_id>.yaml`` + 它指的那张 pgm。"""
    meta_path = Path(maps_dir) / f"{map_id}.yaml"
    if not meta_path.is_file():
        raise GridError(f"没有存好的图:{map_id}",)
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GridError(f"{meta_path.name} 读不了: {exc}") from exc
    if not isinstance(meta, dict):
        raise GridError(f"{meta_path.name} 里不是一个映射")
    missing = [key for key in _REQUIRED if key not in meta]
    if missing:
        raise GridError(f"{meta_path.name} 缺了 {'、'.join(missing)}")
    origin = meta["origin"]
    if not isinstance(origin, (list, tuple)) or len(origin) < 3:
        raise GridError(f"{meta_path.name} 的 origin 应为 [x, y, yaw]")
    # image 是相对 yaml 自己的路径 —— 地图目录整个搬到别的机器上还得能读。
    image = (meta_path.parent / str(meta["image"])).resolve()
    width, height, pixels = read_pgm(image)
    try:
        resolution = float(meta["resolution"])
    except (TypeError, ValueError) as exc:
        raise GridError(f"{meta_path.name} 的 resolution 不是数") from exc
    cells = _cells(
        width, height, pixels,
        negate=bool(meta.get("negate", 0)),
        occupied=float(meta.get("occupied_thresh", 0.65)),
        free=float(meta.get("free_thresh", 0.196)))
    return Grid(width=width, height=height, resolution=resolution,
                origin_x=float(origin[0]), origin_y=float(origin[1]),
                origin_yaw=float(origin[2]), cells=cells, source="saved")


def from_frame(frame: MapFrame) -> Grid:
    """建图桥推来的那一帧。**图还在长的时候只有它**。"""
    return Grid(width=frame.width, height=frame.height,
                resolution=frame.resolution, origin_x=frame.origin_x,
                origin_y=frame.origin_y, origin_yaw=frame.origin_yaw,
                cells=tuple(frame.data), source="live")


__all__ = ["Grid", "GridError", "from_frame", "load_saved", "read_pgm"]
