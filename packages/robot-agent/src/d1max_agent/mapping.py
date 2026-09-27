"""录包与重建经站点(W00c5d 第二部分,决策 8)。

站点下命令 ``mapping``(开始 / 停止录包,人经站点遥控开着狗走)、``map_build``(拿录好的包在狗上
离线重建一张图)。这里只是把现成的建图编排(``d1max_patrol.app.mapping.MappingOrchestrator``,
录 mcap 包 → MOLA 离线建一个地图版本,W09c1)接到发件箱上:

- **包**写进发件箱 ``bags/<包名>-<UTC 时刻>/``(带时刻:同名的包不会在站点上覆盖上一个);停录时打
  一个 ``.done``。**重建成功之后(``.built``)或录完满 ``BAG_KEEP_DAYS`` 天**,传完、站点确认才删 ——
  包是在狗上重建的原料,传完就删的话「拿这个包重建」几乎永远做不成(内部评审)。正在拿它重建的包不删。
- **重建出来的图**(``GEOMETRY_FILES`` 那几样,缺一样都不算建成)挪进发件箱 ``maps/<地图号>/<版本>/``
  (挪、不拷:先验几百 MB),文件都就位之后**最后写** ``map.json``(每个文件的大小与 sha256):站点
  收齐这一份、核对全部文件才登记;狗上传完就删。
- 包传完删了再想重建:这一张不做(在站点上重建要站点主机有 ROS,列进后续)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract.maps import GEOMETRY_FILES, MANIFEST, MapFile, MapRef

log = logging.getLogger(__name__)

#: 包停录后打的标记;拿它重建成功后打的标记。
DONE = ".done"
BUILT = ".built"
#: 边走边建的包(W09c2):开录时记下建哪一版(``{"map_id", "version"}``),收尾完了删。代理重启时按它
#: 接着收尾(内审应修 1:待打包原来只在内存里,重启就丢,站点一直当它「在建」)。
LIVE = ".live"
#: 没重建过的包,录完之后在狗上最多留几天(传完、站点确认之后才删)。
BAG_KEEP_DAYS = 7
#: **盘紧了就不留**(W00c5d 第三部分内部评审:留着的包把发件箱撑满,巡检就被拒 storage_full):
#: 发件箱过了上限的这一份、或者盘用到这条线,传完了的包一律可以删 —— 站点上有一份。
RETAIN_CAP_SHARE = 0.5
RETAIN_DISK_RATIO = 0.8


def storage_pressure(facts: Any) -> bool:
    """盘况(``StorageFacts``)说盘紧了:留着备重建的包该让位了。"""
    if facts is None:
        return False
    return (facts.outbox_bytes > facts.outbox_cap_bytes * RETAIN_CAP_SHARE
            or facts.disk_used_ratio >= RETAIN_DISK_RATIO)


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
    def __init__(self, orchestrator: Any, *, bags_root: Path, maps_out: Path) -> None:
        self.orch = orchestrator
        self.bags_root = Path(bags_root)
        self.maps_out = Path(maps_out)
        self.recording = False
        self.last_bag = ""
        #: 正在拿来重建的包:不许删。跟发件箱删包用同一把锁(判「能不能删」和删在锁里一起做)。
        self.held: set[str] = set()
        self.lock = threading.Lock()
        #: 盘紧不紧(主程序接到发件箱的盘况上):紧了传完的包不再留着备重建。
        self.pressure: Callable[[], bool] = lambda: False
        #: 本地地图库(``MapKeeper``,主程序接上):建好的版本放进去,激活时不用从站点下回来。
        self.keeper: Any = None
        #: 上一次建图的栅格用的哪种射线(``build.json`` 的 ``grid.rays``;``synthetic:…`` 是退回了)。
        self.last_rays = ""
        #: 正在边走边建的(地图号, 版本);停录之后待打包的(包, 地图号, 版本);上一次在线那份
        #: 打不成、退回从录包建的原因(W09c2)。
        self.live: tuple[str, str] | None = None
        self.pending: tuple[str, str, str] | None = None
        #: 开录那条命令的号(正在录的、待打包的):建好、没建成的事件带它(外审阻断 3)。
        self.live_task = ""
        self.pending_task = ""
        self.last_fallback = ""
        #: 起来时找到的、没法接着收尾的边走边建(包, 地图号, 版本, 命令号):调用方报「没建成」。
        self.lost: list[tuple[str, str, str, str]] = []
        self._cleanup()

    @property
    def log_dir(self) -> Path | None:
        """录包、重建子进程的日志目录(W00c6g:站点经 ``proc_log`` 看尾巴);编排没有就是 None。"""
        d = getattr(self.orch, "log_dir", None)
        return None if d is None else Path(d)

    def _cleanup(self) -> None:
        """起来时收拾:攒到一半的图(``.building``)扔掉;没打 ``.done`` 的包是上次录到一半进程没了,
        补上 ``.done``(它已经录不下去了,该传的照传)。"""
        shutil.rmtree(self.maps_out / ".building", ignore_errors=True)
        if self.bags_root.is_dir():
            for b in sorted(self.bags_root.iterdir()):
                if b.is_dir() and not (b / DONE).exists():
                    (b / DONE).touch()
                # 边走边建的没收尾完(代理重启了):接着收尾。同一时刻只会有一趟;真有几个,前面的
                # 交给调用方报「没建成」(``lost``)。
                try:
                    t = json.loads((b / LIVE).read_text("utf-8"))
                    got = (b.name, str(t["map_id"]), str(t["version"]))
                    task = str(t.get("task_id", ""))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
                if self.pending is not None:
                    self.lost.append((*self.pending, self.pending_task))
                    (self.bags_root / self.pending[0] / LIVE).unlink(missing_ok=True)
                    self.held.discard(self.pending[0])
                self.pending, self.pending_task = got, task
                self.held.add(b.name)

    def bag_settled(self, run: Path) -> bool:
        if not (run / DONE).is_file() or run.name in self.held:
            return False
        if (run / BUILT).is_file() or self.pressure():
            return True
        age_days = (time.time() - (run / DONE).stat().st_mtime) / 86400
        return age_days >= BAG_KEEP_DAYS

    async def start(self, name: str, target: tuple[str, str] | None = None,
                    task_id: str = "") -> None:
        """开录。给了 ``target``(地图号, 版本)就同时在线建这一版(W09c2 边走边建);``task_id`` 是开录
        那条命令的号 —— 这一版建好、没建成的事件都带它(站点按它认是哪一次,外审阻断 3)。"""
        if self.recording:
            raise MappingError(f"正在录 {self.last_bag},先停")
        if self.pending is not None:
            raise MappingError(f"上一趟边走边建的 {self.pending[1]}:{self.pending[2]} 还在打包")
        full = f"{name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        if (self.bags_root / full).exists():
            raise MappingError(f"{full} 这个包已经在了,过一秒再来")
        if target is not None:
            if (self.maps_out / target[0] / target[1]).exists():
                raise MappingError(f"{target[0]}:{target[1]} 狗上已经有一份在传了,换个版本号")
            bag = Path(await self.orch.start_record(full, live_map_id=target[0]))
            try:
                (bag / LIVE).write_text(json.dumps({"map_id": target[0], "version": target[1],
                                                    "task_id": task_id}), encoding="utf-8")
            except BaseException:
                # 记不下恢复标记(盘满、只读重挂):已经起了的录包、在线建图收掉再报(外审阻断 1)
                try:
                    await self.orch.stop_record()
                except Exception:
                    log.exception("记不下恢复标记之后停录也没成")
                raise
        else:
            await self.orch.start_record(full)
        self.recording = True
        self.last_bag = full
        self.live = target
        self.live_task = task_id

    async def stop(self) -> None:
        """停录。边走边建的:记下待打包(:meth:`finish`,调用方丢到后台),包先占住(打包要读它)。"""
        if not self.recording:
            raise MappingError("现在没在录包")
        # 编排停录**先回闲着再报**其中一个停止的错(录包、在线建图都停过了):这边不管报不报错都按
        # 「停了」收 —— 不然上层「在录」、下层「闲着」,之后停不了也开不了(外审复查阻断)。边走边建的
        # 照样记待打包,错再往外报。
        err: BaseException | None = None
        try:
            bag = Path(await self.orch.stop_record())
        except Exception as exc:
            err = exc
            bag = self.bags_root / self.last_bag
        self.recording = False
        live, self.live = self.live, None
        if err is not None and not bag.is_dir():
            raise err
        (bag / DONE).touch()                      # 录完了:之后才算安定,传完即删
        # 先打完成标记再记待打包(内审应修 3:打标记炸了,待打包就永远挂着、之后每次开录都拒)
        if live is not None:
            with self.lock:
                self.held.add(bag.name)
            self.pending = (bag.name, *live)
            self.pending_task = self.live_task
        if err is not None:
            raise err

    async def shutdown(self) -> None:
        """代理收尾:正在录就好好停下(录包写完索引、在线建图 SIGINT 存盘),待打包的重启后接着做。"""
        if self.recording:
            await self.stop()

    async def finish(self) -> tuple[MapRef, str]:
        """把边走边建的这一版收尾:先打包在线建好的(``live``);不成(MOLA 半路没了、存下的不齐……)
        就退回在录包上从头建(``bag``)。回(图, 哪种)。"""
        assert self.pending is not None
        bag, map_id, version = self.pending
        src = self.bags_root / bag
        out = self.maps_out / map_id / version
        try:
            if out.exists():
                raise MappingError(f"{map_id}:{version} 狗上已经有一份在传了,换个版本号")
            try:
                made, mode = Path(await self.orch.package(src, map_id)), "live"
                self.last_fallback = ""
            except Exception as exc:  # noqa: BLE001 - 在线那份打不成:退回从录包建,原因记着
                self.last_fallback = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("边走边建的 %s:%s 打包没成(%s),退回从录包建", map_id, version,
                            self.last_fallback)
                made, mode = Path(await self.orch.rebuild(src, map_id)), "bag"
            ref = await asyncio.to_thread(self._settle, made, map_id, version, out)
            (src / BUILT).touch()
            return ref, mode
        finally:
            self.pending = None
            self.pending_task = ""
            (src / LIVE).unlink(missing_ok=True)
            with self.lock:
                self.held.discard(bag)

    def _settle(self, made: Path, map_id: str, version: str, out: Path) -> MapRef:
        """建出来的文件挪进发件箱、放进本地库(出错只记一笔,激活时从站点下)、最后写清单。"""
        missing = [n for n in GEOMETRY_FILES if not (made / n).is_file()]
        if missing:
            raise MappingError(f"重建跑完了,缺 {', '.join(missing)}")
        # 先在点开头的目录里攒齐(上传器不看点开头的路径),再整个挪过去。
        tmp = self.maps_out / ".building" / f"{map_id}@{version}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        files = []
        for n in GEOMETRY_FILES:
            shutil.move(made / n, tmp / n)                # 同一块盘上就是改名
            files.append(MapFile(name=n, size=(tmp / n).stat().st_size,
                                 sha256=_sha256(tmp / n)))
        ref = MapRef(map_id=map_id, version=version, files=tuple(files))
        try:
            self.last_rays = str(json.loads((tmp / "build.json").read_text("utf-8"))
                                 ["grid"]["rays"])[:200]
        except (OSError, ValueError, KeyError, TypeError):
            self.last_rays = ""
        out.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, out)
        # 在写清单**之前**放进本地库:有了清单发件箱就会传、传完就删。
        if self.keeper is not None:
            try:
                self.keeper.adopt(out, ref)
            except Exception:
                log.warning("建好的 %s:%s 放不进本地库(激活时从站点下)", map_id, version,
                            exc_info=True)
        # 文件都就位了才写清单:站点(和发件箱)见到清单才算这张图完整。
        m = out / (MANIFEST + ".tmp")
        m.write_text(json.dumps(ref.to_wire(), ensure_ascii=False), encoding="utf-8")
        os.replace(m, out / MANIFEST)
        return ref

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
            made = Path(await self.orch.rebuild(src, map_id))   # 编排起建图前清掉上一次的产物
            # 挪文件、算几百 MB 的哈希放线程里:不卡住代理(心跳、命令)。
            ref = await asyncio.to_thread(self._settle, made, map_id, version, out)
            (src / BUILT).touch()                         # 重建过了:这个包传完就可以删
            return ref
        finally:
            with self.lock:
                self.held.discard(bag)
