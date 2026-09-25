"""录包与重建经站点(W00c5d 第二部分,决策 8)。

站点下命令 ``mapping``(开始 / 停止录包,人经站点遥控开着狗走)、``map_build``(拿录好的包在狗上
离线重建一张图)。这里只是把现成的建图编排(``d1max_patrol.app.mapping.MappingOrchestrator``,
录 mcap 包 → 隔离域里 slam_toolbox 离线重建 → 存图)接到发件箱上:

- **包**写进发件箱 ``bags/<包名>-<UTC 时刻>/``(带时刻:同名的包不会在站点上覆盖上一个);停录时打
  一个 ``.done``。**重建成功之后(``.built``)或录完满 ``BAG_KEEP_DAYS`` 天**,传完、站点确认才删 ——
  包是在狗上重建的原料,传完就删的话「拿这个包重建」几乎永远做不成(内部评审)。正在拿它重建的包不删。
- **重建出来的图**写进发件箱 ``maps/<地图号>/<版本>/``,文件都写好之后**最后写** ``map.json``
  (每个文件的大小与 sha256):站点收齐这一份、核对全部文件才登记;狗上传完就删。
- 包传完删了再想重建:这一张不做(在站点上重建要站点主机有 ROS,列进后续)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from d1max_contract.maps import MANIFEST, MapFile, MapRef

log = logging.getLogger(__name__)

#: 包停录后打的标记;拿它重建成功后打的标记。
DONE = ".done"
BUILT = ".built"
#: 没重建过的包,录完之后在狗上最多留几天(传完、站点确认之后才删)。
BAG_KEEP_DAYS = 7
#: 重建出来的图的文件(地图号 + 这几个后缀;不拿前缀去 glob,``yard`` 会捡到 ``yard.v2.*``)。
MAP_EXTS = (".pgm", ".yaml", ".posegraph", ".data")


class MappingError(RuntimeError):
    """录包、重建做不了。消息给站点看。"""


def bag_classify(rel: str) -> int | None:
    """包里的文件都传,优先级最低(照片、事件先走);点开头的标记不传。"""
    return None if rel.split("/")[-1].startswith(".") else 5


def map_classify(rel: str) -> int | None:
    """图的文件先走,``map.json`` 最后走(站点收齐它才登记)。"""
    if rel.split("/")[-1].startswith("."):
        return None
    return 5 if rel == MANIFEST else 4


def map_settled(run: Path) -> bool:
    return (run / MANIFEST).is_file()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class MappingService:
    def __init__(self, orchestrator: Any, *, bags_root: Path, maps_out: Path,
                 work_dir: Path) -> None:
        self.orch = orchestrator
        self.bags_root = Path(bags_root)
        self.maps_out = Path(maps_out)
        self.work_dir = Path(work_dir)
        self.recording = False
        self.last_bag = ""
        #: 正在拿来重建的包:不许删。跟发件箱删包用同一把锁(判「能不能删」和删在锁里一起做)。
        self.held: set[str] = set()
        self.lock = threading.Lock()
        self._cleanup()

    def _cleanup(self) -> None:
        """起来时收拾:攒到一半的图(``.building``)扔掉;没打 ``.done`` 的包是上次录到一半进程没了,
        补上 ``.done``(它已经录不下去了,该传的照传)。"""
        shutil.rmtree(self.maps_out / ".building", ignore_errors=True)
        if self.bags_root.is_dir():
            for b in self.bags_root.iterdir():
                if b.is_dir() and not (b / DONE).exists():
                    (b / DONE).touch()

    def bag_settled(self, run: Path) -> bool:
        if not (run / DONE).is_file() or run.name in self.held:
            return False
        if (run / BUILT).is_file():
            return True
        age_days = (time.time() - (run / DONE).stat().st_mtime) / 86400
        return age_days >= BAG_KEEP_DAYS

    async def start(self, name: str) -> None:
        if self.recording:
            raise MappingError(f"正在录 {self.last_bag},先停")
        full = f"{name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        if (self.bags_root / full).exists():
            raise MappingError(f"{full} 这个包已经在了,过一秒再来")
        await self.orch.start_record(full)
        self.recording = True
        self.last_bag = full

    async def stop(self) -> None:
        if not self.recording:
            raise MappingError("现在没在录包")
        bag = Path(await self.orch.stop_record())
        self.recording = False
        (bag / DONE).touch()                      # 录完了:之后才算安定,传完即删

    async def build(self, bag: str, map_id: str, version: str) -> MapRef:
        src = self.bags_root / bag
        with self.lock:                                   # 先占住,再查在不在:发件箱删包也拿这把锁
            if not (src / DONE).is_file():
                raise MappingError(f"包 {bag} 不在狗上了(传完、放够天数就删了)或还没录完")
            self.held.add(bag)
        out = self.maps_out / map_id / version
        try:
            if out.exists():
                raise MappingError(f"{map_id}:{version} 狗上已经有一份在传了,换个版本号")
            for ext in MAP_EXTS:                          # 上一次同名的产物不许混进这一次
                (self.work_dir / f"{map_id}{ext}").unlink(missing_ok=True)
            await self.orch.rebuild(src, map_id)
            made = [self.work_dir / f"{map_id}{ext}" for ext in MAP_EXTS
                    if (self.work_dir / f"{map_id}{ext}").is_file()]
            if not made:
                raise MappingError("重建跑完了,没有出图")
            # 先在点开头的目录里攒齐(上传器不看点开头的路径),再整个挪过去。
            tmp = self.maps_out / ".building" / f"{map_id}@{version}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            files = []
            for p in made:
                shutil.copy2(p, tmp / p.name)
                files.append(MapFile(name=p.name, size=(tmp / p.name).stat().st_size,
                                     sha256=_sha256(tmp / p.name)))
            ref = MapRef(map_id=map_id, version=version, files=tuple(files))
            out.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, out)
            # 文件都就位了才写清单:站点(和发件箱)见到清单才算这张图完整。
            m = out / (MANIFEST + ".tmp")
            m.write_text(json.dumps(ref.to_wire(), ensure_ascii=False), encoding="utf-8")
            os.replace(m, out / MANIFEST)
            (src / BUILT).touch()                         # 重建过了:这个包传完就可以删
            return ref
        finally:
            with self.lock:
                self.held.discard(bag)
