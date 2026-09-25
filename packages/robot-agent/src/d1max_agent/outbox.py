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

import logging
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from d1max_agent.engine.retention import is_settled
from d1max_agent.engine.upload_queue import UploadQueue, classify
from d1max_agent.engine.uploader import Uploader, UploadSink
from d1max_contract.storage import StorageFacts

log = logging.getLogger(__name__)

#: 多久扫一次盘(毫秒)。
SCAN_EVERY_MS = 15_000
#: 一拍最多传几块(每块至多 1 MiB):一拍不能太长,关机时要等得起。
MAX_PUTS_PER_STEP = 64


class Outbox:
    def __init__(self, root: Path | str, *, cap_bytes: int, sink: UploadSink, sn: str,
                 now_ms: Callable[[], int],
                 disk_usage: Callable[[Path], tuple[int, int, int]] = shutil.disk_usage
                 ) -> None:
        self.root = Path(root)
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.cap_bytes = cap_bytes
        self.queue = UploadQueue(self.root / "queue.jsonl")
        self.uploader = Uploader(self.runs_root, self.queue, sink, sn=sn)
        self._now = now_ms
        self._disk_usage = disk_usage
        #: 引擎正在写的那一趟(运行时接上);这些目录一律不删。
        self.active: Callable[[], set[Path]] = set
        self._next_scan = 0
        self._facts: StorageFacts | None = None
        #: 删掉了几趟(看得见才查得到)。
        self.deleted_runs = 0

    def close(self) -> None:
        self.queue.close()

    # ------------------------------------------------------------ 一拍

    def step(self) -> None:
        now = self._now()
        if now >= self._next_scan:
            self.uploader.scan()
            self._next_scan = now + SCAN_EVERY_MS
        for _ in range(MAX_PUTS_PER_STEP):
            if self.uploader.run_once(self._now()).action == "idle":
                break
        self._prune()
        self._facts = self._measure()

    def facts(self) -> StorageFacts:
        return self._facts if self._facts is not None else self._measure()

    # ------------------------------------------------------------ 删

    def _prune(self) -> None:
        active = {p.resolve() for p in self.active()}
        for mission in _dirs(self.runs_root):
            for run in _dirs(mission):
                if run.resolve() in active or not is_settled(run):
                    continue
                key = f"{mission.name}/{run.name}"
                if not self._confirmed(run, key):
                    continue
                shutil.rmtree(run, ignore_errors=True)
                if run.exists():
                    log.warning("发件箱里这一趟删不干净:%s", run)
                    continue
                self.queue.forget(key)
                self.deleted_runs += 1
                log.info("站点已确认,发件箱删掉这一趟:%s", key)
            # 任务名那一层空了也不收:引擎建新的一趟时 mkdir(parents) 跟这里抢,会建不出来。

    def _confirmed(self, run: Path, key: str) -> bool:
        """盘上每个该传的文件都已确认、跟传上去的那份一样大、一样新。"""
        seen = 0
        for path in run.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(run).as_posix()
            if classify(rel) is None:
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
        waiting = [i for i in self.queue.all() if not i.done]
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
    """后台线程:每 ``period_s`` 走一拍。上传等网络,不许卡在代理的事件循环里。"""

    def __init__(self, box: Outbox, *, period_s: float = 1.0) -> None:
        self.box = box
        self.period_s = period_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="outbox")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    self.box.step()
            except Exception:
                log.exception("发件箱这一拍炸了")
            self._stop.wait(self.period_s)

    def facts(self) -> StorageFacts | None:
        return self.box._facts

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout_s)
        with self._lock:
            self.box.close()
