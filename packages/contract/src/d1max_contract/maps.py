"""地图经站点的契约(W00c5d 第二部分,决策 8:站点是地图的唯一权威,狗上只有正在用的那一张)。

- **一张图 = 一个地图号 + 版本 + 一组文件**(狗上建的图::data:`GEOMETRY_FILES`,W09c1 起;之前的版本是
  slam_toolbox 的 ``.pgm``/``.yaml``/``.posegraph``/``.data``;可选 ``home.json`` 原点)。每个文件
  带大小与 sha256,狗下载后逐个核对,对不上不载入。
- **下发**:命令 ``map_activate``(不是任务;有任务在跑回 busy)。狗收下就回 accepted,后台下载、核对、
  交给适配器载入,完了发事件 ``map_activated`` 或 ``map_activate_failed``;载入成功后能力里的
  ``loaded_map`` 跟着变,站点按新版本派单。
- **建图**:命令 ``mapping``(开始 / 停止录包,不动狗,由人经站点遥控开着走);命令 ``map_build``
  (拿录好的包在狗上离线重建一张图,完了发 ``map_built`` / ``map_build_failed``)。录包和生成的图都写进
  发件箱、传到站点,站点收齐登记。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from d1max_contract.errors import ContractError

#: 地图号、版本、包名、文件名:只许这些字符(会成为站点与狗上的目录名)。
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 一张图最多几个文件、单个文件最大多少字节。
MAX_FILES = 16
MAX_FILE_BYTES = 2 * 1024 ** 3
#: 图的清单文件名(狗建完图最后写它;站点收齐这一份才登记)。
MANIFEST = "map.json"
#: 狗上建的地图版本里的几何文件(W08 决定 5、W09c1):定位先验(MOLA 的局部地图)、坐标换算、按同一平面
#: 画的规划栅格、「哪里有图」(建图时走过的路)、怎么建的。站点、手机的预览照旧用 ``floor.*``。
GEOMETRY_FILES = ("prior.mm", "frames.json", "floor.pgm", "floor.yaml", "coverage.json",
                  "build.json")
COVERAGE = "coverage.json"
#: 定位器要的那两样(W09b):配了定位器的狗只收带着它们的版本。
PRIOR_FILES = ("prior.mm", "frames.json")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def check_name(v: Any, what: str) -> str:
    if not isinstance(v, str) or not NAME_RE.match(v) or ".." in v:
        raise ContractError(f"{what} 只许字母、数字、. _ -,1–64 位,不许 ..:{v!r}")
    return v


@dataclass(frozen=True)
class MapFile:
    name: str
    size: int
    sha256: str

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_wire(cls, d: Any) -> MapFile:
        if not isinstance(d, dict):
            raise ContractError("map file: 要是对象")
        size, sha = d.get("size"), d.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            raise ContractError(f"map file: size 要是 0–{MAX_FILE_BYTES} 的整数")
        if not isinstance(sha, str) or not _HEX64.match(sha):
            raise ContractError("map file: sha256 要是 64 位小写十六进制")
        name = check_name(d.get("name"), "文件名")
        if name == MANIFEST:
            raise ContractError(f"map file: {MANIFEST} 是清单本身,不列在文件里")
        return cls(name=name, size=size, sha256=sha)


@dataclass(frozen=True)
class MapRef:
    """一张图:地图号 + 版本 + 文件清单。``map_activate`` 的载荷,也是 ``map.json`` 的内容。"""

    map_id: str
    version: str
    files: tuple[MapFile, ...]

    def to_wire(self) -> dict[str, Any]:
        return {"map_id": self.map_id, "version": self.version,
                "files": [f.to_wire() for f in self.files]}

    @classmethod
    def from_wire(cls, d: Any) -> MapRef:
        if not isinstance(d, dict):
            raise ContractError("map: 要是对象")
        files = d.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise ContractError(f"map: files 要是 1–{MAX_FILES} 个文件")
        parsed = tuple(MapFile.from_wire(f) for f in files)
        if len({f.name for f in parsed}) != len(parsed):
            raise ContractError("map: 文件名重复")
        return cls(map_id=check_name(d.get("map_id"), "地图号"),
                    version=check_name(d.get("version"), "版本"), files=parsed)


def parse_mapping(p: Any) -> tuple[str, str]:
    """``mapping`` 命令的载荷:``{action: start|stop, name}`` → (action, name)。停止不要名字。"""
    if not isinstance(p, dict):
        raise ContractError("mapping: 载荷要是对象")
    action = p.get("action")
    if action == "stop":
        return "stop", ""
    if action != "start":
        raise ContractError("mapping: action 只能是 start / stop")
    return "start", check_name(p.get("name"), "包名")


def parse_map_build(p: Any) -> tuple[str, str, str]:
    """``map_build`` 的载荷:``{bag, map_id, version}``。"""
    if not isinstance(p, dict):
        raise ContractError("map_build: 载荷要是对象")
    return (check_name(p.get("bag"), "包名"), check_name(p.get("map_id"), "地图号"),
            check_name(p.get("version"), "版本"))


#: 巡检点、goto 的目标离建图时走过的路超过这么远就拒(W09c 决定 5:那里的先验是远处扫到的,
#: 定位不可靠)。
COVERAGE_RADIUS_M = 5.0
#: ``coverage.json`` 最多几个点(每 0.5 m 一点,100 km 的路)。
MAX_COVERAGE_POINTS = 200_000


class Coverage:
    """「哪里有图」:建图时走过的路(地图平面上的点)。按 :data:`COVERAGE_RADIUS_M` 见方分桶,近处查桶,
    查不到再全扫(只在要拒的时候,提示里要说离多远)。"""

    def __init__(self, points: tuple[tuple[float, float], ...]) -> None:
        self.points = points
        self._cell = COVERAGE_RADIUS_M
        self._buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for p in points:
            self._buckets.setdefault(self._key(*p), []).append(p)

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return math.floor(x / self._cell), math.floor(y / self._cell)

    def gap(self, x: float, y: float) -> float:
        """``(x, y)`` 离走过的路多远(米);一个点都没有是无穷大。"""
        kx, ky = self._key(x, y)
        near = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for p in self._buckets.get((kx + dx, ky + dy), ())]
        best = min((math.hypot(px - x, py - y) for px, py in near), default=math.inf)
        if best <= self._cell:
            return best
        return min((math.hypot(px - x, py - y) for px, py in self.points), default=math.inf)


def parse_coverage(d: Any) -> Coverage:
    """``coverage.json``:``{"version": 1, "step_m": 0.5, "path": [[x, y], ...]}``。"""
    if not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get("path"), list):
        raise ContractError("coverage.json 要是 {version: 1, path: [[x, y], ...]}")
    path = d["path"]
    if len(path) > MAX_COVERAGE_POINTS:
        raise ContractError(f"coverage.json 点太多({len(path)})")
    out = []
    for p in path:
        if not isinstance(p, list) or len(p) != 2 or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in p):
            raise ContractError(f"coverage.json 的点要是两个有限数:{p!r}")
        out.append((float(p[0]), float(p[1])))
    return Coverage(tuple(out))


def mapping_target(p: Any) -> tuple[str, str] | None:
    """``mapping start`` 带的地图号与版本(W09c2:录包的同时在线建这一版);没带是 None(只录包)。
    两个要么都给、要么都不给;停止不带。"""
    if not isinstance(p, dict):
        raise ContractError("mapping: 载荷要是对象")
    has = ("map_id" in p, "version" in p)
    if not any(has):
        return None
    if p.get("action") != "start":
        raise ContractError("mapping: 只有开始录包才带地图号与版本")
    if not all(has):
        raise ContractError("mapping: 地图号与版本要一起给")
    return check_name(p.get("map_id"), "地图号"), check_name(p.get("version"), "版本")
