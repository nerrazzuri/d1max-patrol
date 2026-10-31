"""演示站点的假建图(W54,``--mapping-sim <图>``):仿真机器人没有雷达,录不出能建图的包。

这里顶替真的建图编排(``d1max_patrol.app.mapping.MappingOrchestrator``),接在真的录包、打包服务
(:class:`d1max_agent.mapping.MappingService`)下面 —— 录包目录、发件箱、上传、站点登记都是真的流程:

- **录包**:建一个小的假包;边走边建时每隔一会儿记一下仿真机器人的位置(走过的路)。
- **预览**:给的那张图里,走过的路周围 :data:`SEE_M` 米以内的格子「看见了」,别的是未知;跟真的预览
  一样写 ``preview.json``(PNG + 说明 + 位置 + 走过的路)。
- **打包 / 重建**:照搬给的那张图,``coverage.json`` 写走过的路,``build.json`` 的回环按走过的路算:
  最后回到起点 :data:`CLOSE_M` 米以内、走了 :data:`MIN_LOOP_M` 米以上算闭合了一次(修正量给个小值)。
  走得少、没走回来,站点的结论就是「有问题」,跟真机一样。

坐标就是仿真机器人的地图坐标(仿真定位是准的)。只给演示站点用,狗上不会开。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import shutil
import struct
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

SEE_M = 6.0
CLOSE_M = 1.5
MIN_LOOP_M = 5.0
STEP_M = 0.3
UNKNOWN, FREE, OCC = 205, 254, 0


def _read_pgm(path: Path) -> np.ndarray:
    data = path.read_bytes()
    parts: list[bytes] = []
    i = 0
    while len(parts) < 4:                         # P5、宽、高、最大值(跳过注释)
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            i = data.index(b"\n", i) + 1
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        parts.append(data[i:j])
        i = j
    w, h = int(parts[1]), int(parts[2])
    return np.frombuffer(data[i + 1:i + 1 + w * h], np.uint8).reshape(h, w)


def _yaml_num(text: str, key: str) -> list[float]:
    for line in text.splitlines():
        if line.strip().startswith(key + ":"):
            v = line.split(":", 1)[1].strip().strip("[]")
            return [float(x) for x in v.split(",") if x.strip()]
    raise ValueError(f"floor.yaml 里没有 {key}")


def png(img: np.ndarray) -> bytes:
    """8 位灰度 PNG(不靠 PIL)。"""
    h, w = img.shape
    raw = np.hstack([np.zeros((h, 1), np.uint8), np.asarray(img, np.uint8)]).tobytes()

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _path_len(pts: list[list[float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:], strict=False))


class SimMappingOrchestrator:
    def __init__(self, src: Path, *, bags_dir: Path, work: Path,
                 pose: Callable[[], tuple[float, float, float] | None] = lambda: None,
                 sample_s: float = 0.5, preview_s: float = 2.0, build_s: float = 4.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.src = Path(src)
        self.bags_dir = Path(bags_dir)
        self.work = Path(work)
        self.log_dir = self.work / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.pose = pose
        self.sample_s, self.preview_s, self.build_s = sample_s, preview_s, build_s
        self.clock = clock
        self.preview_error = ""
        self._bag: Path | None = None
        self._live: str | None = None
        self._trail: list[list[float]] = []
        self._task: asyncio.Task | None = None
        self._seq = 0
        self._grid = _read_pgm(self.src / "floor.pgm")
        yml = (self.src / "floor.yaml").read_text()
        self._res = _yaml_num(yml, "resolution")[0]
        self._origin = _yaml_num(yml, "origin")[:2]
        self._seen = np.zeros(self._grid.shape, bool)

    # ---- 录包

    async def start_record(self, name: str, live_map_id: str | None = None) -> str:
        bag = self.bags_dir / name
        bag.mkdir(parents=True)
        (bag / "metadata.yaml").write_text("# 演示站点的假包\n")
        (bag / f"{name}_0.mcap").write_bytes(b"sim")
        self._bag, self._live = bag, live_map_id
        self._trail = []
        self._seen[:] = False
        if live_map_id is not None:
            self._task = asyncio.get_running_loop().create_task(self._follow())
        return str(bag)

    async def stop_record(self) -> str:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._sample()
            self._write_preview()
        assert self._bag is not None
        return str(self._bag)

    def preview_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def preview_dir(self, map_id: str) -> Path:
        return self.work / "preview" / map_id

    async def _follow(self) -> None:
        last = 0.0
        while True:
            self._sample()
            if self.clock() - last >= self.preview_s:
                self._write_preview()
                last = self.clock()
            await asyncio.sleep(self.sample_s)

    def _sample(self) -> None:
        p = self.pose()
        if p is None:
            return
        x, y = round(float(p[0]), 2), round(float(p[1]), 2)
        if self._trail and math.hypot(x - self._trail[-1][0], y - self._trail[-1][1]) < STEP_M:
            self._cur = (x, y, float(p[2]))
            return
        self._trail.append([x, y])
        self._cur = (x, y, float(p[2]))
        self._see(x, y)

    def _see(self, x: float, y: float) -> None:
        h, w = self._grid.shape
        c = (x - self._origin[0]) / self._res
        r = h - 1 - (y - self._origin[1]) / self._res
        rad = SEE_M / self._res
        r0, r1 = max(0, int(r - rad)), min(h, int(r + rad) + 1)
        c0, c1 = max(0, int(c - rad)), min(w, int(c + rad) + 1)
        if r0 >= r1 or c0 >= c1:
            return
        rr, cc = np.ogrid[r0:r1, c0:c1]
        self._seen[r0:r1, c0:c1] |= (rr - r) ** 2 + (cc - c) ** 2 <= rad * rad

    def _write_preview(self) -> None:
        if self._live is None:
            return
        img = np.where(self._seen, self._grid, UNKNOWN).astype(np.uint8)
        self._seq += 1
        cur = getattr(self, "_cur", None)
        doc = {"seq": self._seq, "written_at": self.clock(), "run": self._bag.name if self._bag
               else "", "res": self._res, "origin": list(self._origin),
               "width": int(img.shape[1]), "height": int(img.shape[0]),
               "pose": list(cur) if cur else None, "trail": self._trail[-2000:],
               "frames": len(self._trail), "dropped": 0,
               "png": base64.b64encode(png(img)).decode()}
        d = self.preview_dir(self._live)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "preview.json.tmp"
        tmp.write_text(json.dumps(doc))
        tmp.replace(d / "preview.json")

    # ---- 出图

    async def package(self, bag: str | Path, map_id: str) -> str:
        return await self._make(map_id, "live")

    async def rebuild(self, bag: str | Path, map_id: str) -> str:
        return await self._make(map_id, "bag")

    async def _make(self, map_id: str, how: str) -> str:
        await asyncio.sleep(self.build_s)              # 像真的一样等一会儿
        out = self.work / "made" / map_id
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        for n in ("floor.pgm", "floor.yaml", "frames.json", "prior.mm"):
            if (self.src / n).is_file():
                shutil.copyfile(self.src / n, out / n)
            else:
                (out / n).write_text("{}" if n.endswith(".json") else "sim")
        trail = self._trail
        path = _path_len(trail)
        closed = len(trail) >= 2 and path >= MIN_LOOP_M and math.hypot(
            trail[-1][0] - trail[0][0], trail[-1][1] - trail[0][1]) <= CLOSE_M
        lc: dict[str, Any] = {"ran": True, "used": closed, "closures": 1 if closed else 0,
                              "path_m": round(path, 2), "keyframes": len(trail)}
        if closed:
            lc["max_shift_m"] = 0.06
        else:
            lc["why"] = "未找到回环"
        h, w = self._grid.shape
        (out / "coverage.json").write_text(json.dumps({"version": 1, "step_m": STEP_M,
                                                       "path": trail}) + "\n")
        (out / "build.json").write_text(json.dumps({
            "version": 1, "source": f"sim:{how}", "builder": "sim", "frames_count": len(trail),
            "grid": {"res": self._res, "size": [w, h], "origin": list(self._origin),
                     "rays": "sim"},
            "loop_closure": lc, "handedness": {"verdict": "skipped", "why": "演示站点"}},
            ensure_ascii=False, indent=2) + "\n")
        return str(out)
