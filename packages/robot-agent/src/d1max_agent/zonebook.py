"""这台狗在每个几何版本上的区域集(W10),按(地图号, 版本)落盘在 ``zones.json``。

站点是权威、经 ``zones_set`` 整份下发;狗起来、换图时先读这里(断网重启也照样守着禁行区)。
**先落盘再改内存**:写临时文件、fsync、换名、fsync 目录;落不了盘抛 ``OSError``,调用方拒收。
最多记 ``MAX_ENTRIES`` 个版本(最近写的留下);文件坏了、某一份解析不过都当没有。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.zones import ZoneSet

MAX_ENTRIES = 16


def _key(map_id: str, version: str) -> str:
    return f"{map_id}@{version}"


class ZoneBook:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def get(self, map_id: str, version: str) -> ZoneSet | None:
        raw = self._load().get(_key(map_id, version))
        try:
            zs = ZoneSet.from_wire(raw)
        except ContractError:
            return None
        return zs if (zs.map_id, zs.map_version) == (map_id, version) else None

    def put(self, zs: ZoneSet) -> None:
        d = self._load()
        key = _key(zs.map_id, zs.map_version)
        d.pop(key, None)
        d[key] = zs.to_wire()
        while len(d) > MAX_ENTRIES:
            d.pop(next(iter(d)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(d, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
