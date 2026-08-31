"""仿真设备上的地图与路径。

载荷形状照抄 refs/nav-api §1.5(所有 PGM 地图)与 §2.1(某地图下的所有路径),
这样客户端的 parse_map_ids / parse_paths_payload 能原样读回。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d1max_patrol.protocol.nav_types import Waypoint

# 假设(待真机验证): 新建空白地图的 id 编号规则为 map_1, map_2, ...
# 供应商文档 §1.5 的示例使用 map_id_1/map_id_2，且无"新建空白地图"的接口，
# 实际设备保存完成的 SLAM 会话后分配什么 id 未知。须在真机上运行建图会话、
# 保存后调用 get_all_map，记录设备分配的实际 id。
_AUTO_NAME = re.compile(r"^map_(\d+)$")


class StoreError(Exception):
    """地图或路径不存在、或 id 冲突。"""


@dataclass
class MapRecord:
    """一张占用栅格地图。

    宽高默认值为仿真便利设定，不代表实际设备能力。实际设备的地图尺寸由 SLAM 结果决定。
    """

    map_id: str
    # 假设(待真机验证): 默认网格尺寸为 20×20，数据全零。供应商文档 §1.4 的示例为
    # 1000×1000 地图(约百万整数、数 MB JSON)，实际 get_pgm_map 响应可能远大于仿真器。
    # 须在真机验证：缓冲区大小、响应超时、网络开销是否能应对真实负载。所有零数据是
    # 仿真器便利(设备无障碍物，路径规划用运动学直线逼近)，与 SLAM 产出的混合值不同。
    width: int = 20
    height: int = 20
    resolution: float = 0.05
    origin_x: float = 0.0
    origin_y: float = 0.0
    #: §1.5 载荷第二项的标签列表,形如 [[0, [x, y, z]], ...]
    tags: list[Any] = field(default_factory=list)

    def to_grid(self) -> dict[str, Any]:
        return {
            "header": {"frame_id": "map", "stamp": {"sec": 0, "nanosec": 0}},
            "info": {
                "map_load_time": {"sec": 0, "nanosec": 0},
                "resolution": self.resolution,
                "width": self.width,
                "height": self.height,
                "origin": {
                    "position": {"x": self.origin_x, "y": self.origin_y, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
            },
            # 全 0 表示全空闲。仿真器不做障碍物,路径规划由运动学模型直线趋近代替。
            "data": [0] * (self.width * self.height),
        }


class MapStore:
    """地图与路径的内存存储,可选存盘以复现场景。"""

    def __init__(self) -> None:
        self.maps: dict[str, MapRecord] = {}
        self.paths: dict[str, dict[str, list[Waypoint]]] = {}

    # ------------------------------------------------------------ 地图

    def map_ids(self) -> list[str]:
        return sorted(self.maps)

    def _next_auto_id(self) -> str:
        used = {
            int(m.group(1))
            for key in self.maps
            if (m := _AUTO_NAME.match(key)) is not None
        }
        n = 1
        while n in used:
            n += 1
        return f"map_{n}"

    def create_map(self, map_id: str | None = None) -> str:
        if map_id is None:
            map_id = self._next_auto_id()
        if map_id in self.maps:
            raise StoreError(f"地图 {map_id!r} 已存在")
        self.maps[map_id] = MapRecord(map_id=map_id)
        self.paths.setdefault(map_id, {})
        return map_id

    def remove_maps(self, map_ids: Sequence[str]) -> int:
        """删除若干地图,返回实际删掉的个数。不存在的静默跳过。"""
        removed = 0
        for map_id in map_ids:
            if self.maps.pop(map_id, None) is not None:
                removed += 1
            self.paths.pop(map_id, None)
        return removed

    def rename_map(self, old_id: str, new_id: str) -> None:
        if old_id not in self.maps:
            raise StoreError(f"地图 {old_id!r} 不存在")
        if new_id in self.maps:
            raise StoreError(f"地图 {new_id!r} 已存在")
        record = self.maps.pop(old_id)
        record.map_id = new_id
        self.maps[new_id] = record
        self.paths[new_id] = self.paths.pop(old_id, {})

    def _require(self, map_id: str) -> MapRecord:
        record = self.maps.get(map_id)
        if record is None:
            raise StoreError(f"地图 {map_id!r} 不存在")
        return record

    def occupancy_grid(self, map_id: str) -> dict[str, Any]:
        return self._require(map_id).to_grid()

    def all_pgm_payload(self) -> dict[str, Any]:
        """§1.5 的 data: {map_id: [OccupancyGrid, 标签列表]}。"""
        return {
            map_id: [record.to_grid(), list(record.tags)]
            for map_id, record in self.maps.items()
        }

    # ------------------------------------------------------------ 路径

    def set_path(self, map_id: str, path_id: str, waypoints: Sequence[Waypoint]) -> None:
        self._require(map_id)
        self.paths.setdefault(map_id, {})[path_id] = list(waypoints)

    def get_paths(self, map_id: str) -> dict[str, list[Waypoint]]:
        self._require(map_id)
        # 返回映射的浅拷贝:调用方增删键不会影响存储。值(list[Waypoint])仍共享,
        # 与公开的 paths 属性同级别 —— Waypoint 本身不可变。
        return dict(self.paths.get(map_id, {}))

    def remove_path(self, map_id: str, path_id: str) -> None:
        """删不存在的静默返回 —— 与厂商批量删除的宽松语义一致。"""
        self.paths.get(map_id, {}).pop(path_id, None)

    def paths_payload(self, map_id: str) -> list[Any]:
        """§2.1 的 data: [map_id, [path_ids], {path_id: [[点名, pose], ...]}]。"""
        paths = self.get_paths(map_id)
        return [
            map_id,
            sorted(paths),
            {pid: [wp.to_wire() for wp in wps] for pid, wps in paths.items()},
        ]

    # ------------------------------------------------------------ 存盘

    def save(self, path: str | Path) -> None:
        payload = {
            "maps": [
                {
                    "map_id": r.map_id,
                    "width": r.width,
                    "height": r.height,
                    "resolution": r.resolution,
                    "origin_x": r.origin_x,
                    "origin_y": r.origin_y,
                    "tags": r.tags,
                }
                for r in self.maps.values()
            ],
            "paths": {
                map_id: {pid: [wp.to_wire() for wp in wps] for pid, wps in paths.items()}
                for map_id, paths in self.paths.items()
            },
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self, path: str | Path) -> None:
        """从存档恢复。文件不存在则保持空,便于首次启动。"""
        p = Path(path)
        if not p.is_file():
            return
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            self.maps = {
                entry["map_id"]: MapRecord(**entry) for entry in payload.get("maps", [])
            }
            self.paths = {
                map_id: {
                    pid: [Waypoint.from_wire(w) for w in wire] for pid, wire in paths.items()
                }
                for map_id, paths in payload.get("paths", {}).items()
            }
        # MIN-2: KeyError 必须一起收 —— 存档里缺 `map_id` 键时上面那句
        # `entry["map_id"]` 抛的是 KeyError,它不是 ValueError 的子类,
        # 会直接穿透 StoreError 这道边界,把"存档坏了"暴露成一个裸 KeyError。
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            raise StoreError(f"读档失败({p})：{e}") from e
        for map_id in self.maps:
            self.paths.setdefault(map_id, {})
