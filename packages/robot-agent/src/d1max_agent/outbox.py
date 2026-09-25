"""发件箱(W00c5d,决策 8)。

决策 8:业务数据都在站点;狗上只许有运行必需的工作副本,以及**断网暂存的发件箱** —— 恢复后上传、
**站点确认即删**,位置可配置(可以指到临时硬盘)、有上限。

- 引擎的运行记录(照片、事件、清单……)直接写在 ``<发件箱>/runs/<任务>/<时刻>/`` 下,**边跑边传**
  (沿用现成的上传队列与分块上传:站点按自己存下的字节回哈希,对上才算这一块传到)。
- **一趟删不删**:这一趟已经安定(清单里有汇总,或者是过了安定期的残骸 —— 判据只有
  :func:`~d1max_agent.engine.retention.is_settled` 一处),不是引擎正在写的那一趟,并且盘上每一个
  该传的文件都在队列里、已确认、大小和修改时间都对得上 → 整趟删掉,队列里的条目也清掉。
  **没确认的一个字节都不删**;满了就拒绝新的巡检(由调用方看 :meth:`facts`),绝不删没传完的。
- 盘况(:class:`~d1max_contract.storage.StorageFacts`)随遥测上站点。

这个类不起线程::meth:`step` 走一拍(扫盘、传到没得传或走满步数、删、算盘况)就返回。
放到后台线程里跑是 :class:`OutboxPump` 的事 —— 上传会等网络,绝不能卡在代理的事件循环里。
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from d1max_agent.engine.retention import is_settled
from d1max_agent.engine.upload_queue import UploadQueue, classify
from d1max_agent.engine.uploader import Uploader, UploadSink
from d1max_contract.storage import StorageFacts

log = logging.getLogger(__name__)

#: 多久扫一次盘(毫秒)。
SCAN_EVERY_MS = 15_000
#: 一拍最多传几块(每块至多 1 MiB)、最多走多久(秒):一拍不能太长 —— 4G 断了的时候每块都要等满
#: 超时,不限时的话一拍能走半小时,盘况冻住、关不了机(W00c5d 内部评审)。
MAX_PUTS_PER_STEP = 64
STEP_BUDGET_S = 5.0
#: 多久删一次、量一次盘(毫秒):每秒把整个发件箱走一遍太费。
HOUSEKEEP_EVERY_MS = 10_000


class Outbox:
    def __init__(self, root: Path | str, *, cap_bytes: int, sink: UploadSink, sn: str,
                 now_ms: Callable[[], int],
                 disk_usage: Callable[[Path], tuple[int, int, int]] = shutil.disk_usage,
                 sub: str = "runs", run_depth: int = 2,
                 classify: Callable[[str], int | None] = classify,
                 settled: Callable[[Path], bool] = is_settled,
                 delete_lock: threading.Lock | None = None) -> None:
        """``sub``:发件箱里的哪一块(运行记录 ``runs``;W00c5d 第二部分的建图录包 ``bags``、生成的图
        ``maps``),各自一个队列、各自的「一趟」层数、哪些文件要传、什么算安定。"""
        self.root = Path(root)
        self.runs_root = self.root / sub
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.cap_bytes = cap_bytes
        self.run_depth = run_depth
        self._settled = settled
        #: 判「能不能删」和删在这把锁里一起做(建图的包:重建开跑时也拿它,不许删到一半)。
        self._delete_lock = delete_lock
        self.queue = UploadQueue(self.root / ("queue.jsonl" if sub == "runs"
                                              else f"queue-{sub}.jsonl"))
        self.uploader = Uploader(self.runs_root, self.queue, sink, sn=sn, run_depth=run_depth,
                                 classify=classify)
        self._classify = classify
        self._now = now_ms
        self._disk_usage = disk_usage
        #: 引擎正在写的那一趟(运行时接上);这些目录一律不删。
        self.active: Callable[[], set[Path]] = set
        self._next_scan = 0
        self._next_housekeep = 0
        self._facts: StorageFacts | None = None
        self._measure_failed = False
        #: 删掉了几趟(看得见才查得到)。
        self.deleted_runs = 0

    def close(self) -> None:
        self.queue.close()

    # ------------------------------------------------------------ 一拍

    def step(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        now = self._now()
        if now >= self._next_scan:
            self.uploader.scan()
            self._next_scan = now + SCAN_EVERY_MS
        deadline = time.monotonic() + STEP_BUDGET_S
        for _ in range(MAX_PUTS_PER_STEP):
            if should_stop() or time.monotonic() > deadline:
                break
            if self.uploader.run_once(self._now()).action == "idle":
                break
        if now >= self._next_housekeep:
            self._next_housekeep = now + HOUSEKEEP_EVERY_MS
            self._prune()
            self._facts = self._measure_safe()

    def _measure_safe(self) -> StorageFacts:
        """量不了(盘掉了、挂载点没了)就按「满了」报:不接巡检、站点出告警 ——
        不拿上一次的好数字充数。"""
        try:
            f = self._measure()
        except OSError as exc:
            if not self._measure_failed:
                log.error("发件箱量不了(盘掉了?):%s —— 按满了报", exc)
            self._measure_failed = True
            return StorageFacts(disk_used_ratio=1.0, outbox_bytes=self.cap_bytes,
                                outbox_cap_bytes=self.cap_bytes, backlog_files=0,
                                backlog_bytes=0, oldest_backlog_s=None)
        self._measure_failed = False
        return f

    def facts(self) -> StorageFacts:
        return self._facts if self._facts is not None else self._measure_safe()

    # ------------------------------------------------------------ 删

    def _runs(self) -> list[Path]:
        level = [self.runs_root]
        for _ in range(self.run_depth):
            level = [c for d in level for c in _dirs(d)]
        return level

    def _prune(self) -> None:
        active = {p.resolve() for p in self.active()}
        for run in self._runs():
            key = run.relative_to(self.runs_root).as_posix()
            with self._delete_lock or contextlib.nullcontext():
                if run.resolve() in active or not self._settled(run):
                    continue
                if not self._confirmed(run, key):
                    continue
                shutil.rmtree(run, ignore_errors=True)
            if run.exists():
                log.warning("发件箱里这一趟删不干净:%s", run)
                continue
            self.queue.forget(key)
            self.deleted_runs += 1
            log.info("站点已确认,发件箱删掉这一趟:%s", key)
        # 上面几层(任务名)空了也不收:引擎建新的一趟时 mkdir(parents) 跟这里抢,会建不出来。

    def _confirmed(self, run: Path, key: str) -> bool:
        """盘上每个该传的文件都已确认、跟传上去的那份一样大、一样新。"""
        seen = 0
        for path in run.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(run).as_posix()
            if self._classify(rel) is None:
                continue
            item = self.queue.get(f"{key}/{rel}")
            st = path.stat()
            if item is None or not item.done or item.size != st.st_size \
                    or item.offset != st.st_size or item.mtime_ns != st.st_mtime_ns:
                return False
            seen += 1
        return seen > 0

    # ------------------------------------------------------------ 盘况

    def _measure(self) -> StorageFacts:
        total, used, _free = self._disk_usage(self.root)
        outbox = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        # 站点永远不收、隔离了的不算积压(站点那头另有 upload_refused 告警)。
        waiting = [i for i in self.queue.all() if not i.done and not i.refused]
        oldest = None
        now_s = self._now() / 1000.0
        for i in waiting:
            try:
                age = now_s - (self.runs_root / i.key).stat().st_mtime
            except OSError:
                continue
            oldest = age if oldest is None else max(oldest, age)
        return StorageFacts(
            disk_used_ratio=min(1.0, max(0.0, used / total)) if total else 1.0,
            outbox_bytes=outbox, outbox_cap_bytes=self.cap_bytes,
            backlog_files=len(waiting),
            backlog_bytes=sum(max(0, i.size - i.offset) for i in waiting),
            oldest_backlog_s=None if oldest is None else max(0, int(oldest)))


def _dirs(p: Path) -> list[Path]:
    try:
        return sorted(c for c in p.iterdir() if c.is_dir())
    except OSError:
        return []


class OutboxPump:
    """后台线程:每 ``period_s`` 每一块发件箱各走一拍。上传等网络,不许卡在代理的事件循环里。"""

    def __init__(self, box: Outbox, *, period_s: float = 1.0,
                 more: tuple[Outbox, ...] = ()) -> None:
        self.box = box
        self.boxes = (box, *more)
        self.period_s = period_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="outbox")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            for b in self.boxes:
                if self._stop.is_set():
                    break
                try:
                    with self._lock:
                        b.step(self._stop.is_set)
                except Exception:
                    log.exception("发件箱这一拍炸了(%s)", b.runs_root.name)
            self._stop.wait(self.period_s)

    def facts(self) -> StorageFacts | None:
        """几块发件箱合起来的盘况:盘与发件箱总量是同一块盘、同一个目录;积压相加,最老的取最老。"""
        got = [b._facts for b in self.boxes]
        if any(f is None for f in got):
            return None
        first = got[0]
        ages = [f.oldest_backlog_s for f in got if f.oldest_backlog_s is not None]
        return StorageFacts(
            disk_used_ratio=first.disk_used_ratio, outbox_bytes=first.outbox_bytes,
            outbox_cap_bytes=first.outbox_cap_bytes,
            backlog_files=sum(f.backlog_files for f in got),
            backlog_bytes=sum(f.backlog_bytes for f in got),
            oldest_backlog_s=max(ages) if ages else None)

    def stop(self, timeout_s: float = 10.0) -> None:
        """叫停后台线程。**不等手上那一块传完**(一块可能要等满 30 s 的网络超时):线程是
        daemon 的,队列每一步都落盘,进程退了下次接着传。线程停下来了才关队列文件。"""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout_s)
        if not self._thread.is_alive():
            for b in self.boxes:
                b.close()

    def retry_refused(self) -> int:
        """站点改了规矩、管理员让隔离的文件再传一次:全部重新排上。返回排上几个。"""
        with self._lock:
            return sum(b.queue.unrefuse_all() for b in self.boxes)
