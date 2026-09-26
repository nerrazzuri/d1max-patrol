"""这台狗在每张图上的原点,按(地图号, 版本)落盘在 ``homes.json``(W00c6f 内审阻断 1、应修 6)。

- 两处写:在当前位置标原点(``mark_home``)、站点下发地图(``map_activate``:带了原点就记它,没带就
  删掉这张图上记着的 —— 站点是权威,那时按图里的 ``home.json``)。
- **先落盘再改内存**:写临时文件、fsync、换名、fsync 目录;落不了盘抛 ``OSError``,调用方拒收,
  原点不变。
- 狗起来、换图时先看这里,再看 ``MapKeeper.home_of``(``active.json``、图里的 ``home.json``)。按
  ``--map`` 起来的狗(没有站点下发的 ``active.json``,也可以没配地图保管)也照用 —— 以前标的原点只
  记在 ``active.json`` 里,这种狗重启就回到 ``--home``。
- 最多记 ``MAX_ENTRIES`` 张图(最近写的留下);文件坏了当没有。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

MAX_ENTRIES = 32


def _key(map_id: str, version: str) -> str:
    return f"{map_id}@{version}"


class HomeBook:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def get(self, map_id: str, version: str) -> tuple[float, float, float] | None:
        rec = self._load().get(_key(map_id, version))
        try:
            x, y, yaw = (float(rec[k]) for k in ("x", "y", "yaw"))
        except (KeyError, TypeError, ValueError):
            return None
        return (x, y, yaw) if all(map(math.isfinite, (x, y, yaw))) else None

    def put(self, map_id: str, version: str, home: tuple[float, float, float] | None, *,
            now_ms: int) -> None:
        """记下这张图上的原点(``None`` = 删掉)。出错抛 ``OSError``,原来的文件不动。"""
        d = self._load()
        key = _key(map_id, version)
        if home is None:
            if key not in d:
                return
            d.pop(key)
        else:
            d.pop(key, None)                      # 挪到最后:最近写的
            d[key] = {"map_id": map_id, "version": version, "x": home[0], "y": home[1],
                      "yaw": home[2], "at_ms": now_ms}
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
