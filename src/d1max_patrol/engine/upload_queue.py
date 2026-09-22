"""盘上的上传队列。spec §4.3。

**为什么在盘上:** 断电重启后队列还得在。内存队列等于"一断电,攒了三天的
东西全没了"。

**为什么是日志结构:** 只追加,同一个 key 后写的覆盖先写的,启动时整个重放。
就地改一行 JSON 的话,断电正好断在中间就留下半行,读不出来等于没有队列 ——
追加写最坏情况只丢**最后**那半行,前面的全在。

**这个模块不碰网络,不碰线程。** 它只知道"有哪些文件要传、传到第几个字节、
下次什么时候可以再试"。真的传是 :mod:`d1max_patrol.engine.uploader` 的事,
把它放到后台跑是更上层(线程壳)的事 —— 那一层依赖这里,反过来不行。
"""

from __future__ import annotations

import json
import os
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: spec §4.3 那张表。数字小的先走。
PRIORITY_ALERT = 1
PRIORITY_EVENTS = 2
PRIORITY_PHOTO = 3
PRIORITY_TELEMETRY = 4

#: 退避。**未验证:这三个数都没在真机上量过。**
#: BACKOFF_BASE_MS —— 第一次失败等 5 秒。CPE 重拨一次可能要 20 秒,这个数可能太急。
#: BACKOFF_CAP_MS —— spec §4.3 定死的 5 分钟封顶,**这个数不许改**;未验证的是
#:   "封到顶之后积压涨多快",那要拿真机的上行带宽量。
#: BACKOFF_JITTER —— 5 只狗同时断网再同时恢复,抖动够不够把它们散开。
BACKOFF_BASE_MS = 5_000
BACKOFF_CAP_MS = 300_000
BACKOFF_JITTER = 0.2

#: 队列文件里的行数涨到活条数的这个倍数就紧凑一次。
#: **未验证:** 真机上一趟巡检产生多少行、重试多密,都没量过。
COMPACT_RATIO = 2.0

#: 文件名 → 优先级。**白名单,不是黑名单** —— 将来加一种文件,必须有人显式
#: 决定它是几级,而不是让它悄悄落进某个缺省档。
_EXACT: dict[str, int] = {
    "alerts.jsonl": PRIORITY_ALERT,
    "events.jsonl": PRIORITY_EVENTS,
    # manifest.json 是这一趟的身份(任务定义快照 + 环境指纹),跟事件流同级:
    # 没有它,服务器上那堆事件不知道是哪一趟、哪台机器、哪个版本产生的。
    "manifest.json": PRIORITY_EVENTS,
    "report.md": PRIORITY_PHOTO,
    "report.html": PRIORITY_PHOTO,
    # 判读结论与人工复核。**没有它们,服务器上那堆照片不知道谁判过、判成什么**——
    # W03 之前这三份只在狗上有一份,保留策略删掉整趟就没了。
    "findings.json": PRIORITY_EVENTS,
    "review.json": PRIORITY_EVENTS,
    "telemetry.jsonl": PRIORITY_TELEMETRY,
}


#: **会被原地改写**的文件。报告每次判读/复核都删掉重生成,findings 判读重跑整个
#: 换掉,review 每次复核回写。照片和 ``*.jsonl`` 追加流从不原地改写。
#: 这张表决定升级上来的老队列(没有 mtime)怎么对待已 ``done`` 的条目:
#: 在这张表上的,``done`` 证明不了"盘上这一份就是传上去的那一份"(升级前最后
#: 一次改写可能没来得及传),第一次见到 mtime 就从 0 重传一次;不在表上的只记
#: mtime,不把整库照片重传一遍。**加一种改写型文件,必须同时进这里和 _EXACT。**
REWRITTEN_IN_PLACE: frozenset[str] = frozenset(
    {"report.md", "report.html", "findings.json", "review.json"})


def rewritten_in_place(key: str) -> bool:
    """这个 key(相对 runs_root 的路径)是不是改写型文件。按文件名判。"""
    return key.rsplit("/", 1)[-1] in REWRITTEN_IN_PLACE


def classify(rel: str) -> int | None:
    """``rel`` 是相对 run 目录的路径。返回 ``None`` 表示**不入队**。

    ``state.json`` 故意不入队:它是崩溃恢复本地用的,每次状态迁移都整个换掉,
    入队等于每迁移一次就重传一遍,而服务器拿它没有任何用处 —— 事件流里有同样
    的信息且是增量的。
    """
    rel = rel.replace("\\", "/").lstrip("/")
    if rel in _EXACT:
        return _EXACT[rel]
    if rel.startswith("photos/"):
        return PRIORITY_PHOTO
    return None


def backoff_ms(attempts: int, *, rand: Callable[[], float] | None = None) -> int:
    """第 ``attempts`` 次失败之后要等多久。

    **不做"重试 N 次后放弃"**(spec §4.3)—— 放弃意味着悄悄丢证据。所以这个
    函数没有"返回 None 表示别再试了"这条出口,attempts 再大也只是封到顶。
    """
    if attempts < 1:
        return 0
    raw = BACKOFF_BASE_MS * (2 ** min(attempts - 1, 40))
    capped = min(raw, BACKOFF_CAP_MS)
    r = rand or random.random
    # 抖动只往下拉,不越顶 —— 越顶就违反 spec 写死的 5 分钟。
    return int(capped * (1.0 - BACKOFF_JITTER * r()))


@dataclass(frozen=True)
class QueueItem:
    """一个上传项。**单位是文件,不是任务**(spec §4.1)。"""

    key: str
    priority: int
    offset: int = 0
    size: int = 0
    attempts: int = 0
    next_ms: int = 0
    done: bool = False
    #: 上次扫盘看到的 mtime(纳秒)。0 = 不知道(老队列里的条目)。
    #: 它是"文件被原地改写了"的判据 —— report.md 每次判读/复核都被删掉重生成,
    #: 字节数常常一样甚至更小,只比 size 的话服务器上永远是旧结论(W02)。
    mtime_ns: int = 0

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_wire(d: dict[str, Any]) -> QueueItem:
        return QueueItem(
            key=str(d["key"]),
            priority=int(d["priority"]),
            offset=int(d.get("offset", 0)),
            size=int(d.get("size", 0)),
            attempts=int(d.get("attempts", 0)),
            next_ms=int(d.get("next_ms", 0)),
            done=bool(d.get("done", False)),
            mtime_ns=int(d.get("mtime_ns", 0)),
        )


class UploadQueue:
    """``queue.jsonl`` 的读写。**每行 flush + fsync。**"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, QueueItem] = {}
        self._order: list[str] = []
        self._lines = 0
        self._replay()
        self._fh = open(self._path, "a", encoding="utf-8")

    # ---- 盘 ----

    def _replay(self) -> None:
        if not self._path.exists():
            return
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                self._lines += 1
                try:
                    item = QueueItem.from_wire(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    # 半行。**只可能是最后一行** —— 追加写不会在中间留残缺。
                    # 丢掉它,前面的全留着。这正是日志结构换来的那半条命。
                    continue
                self._remember(item)

    def _remember(self, item: QueueItem) -> None:
        if item.key not in self._items:
            self._order.append(item.key)
        self._items[item.key] = item

    def _write(self, item: QueueItem) -> None:
        self._remember(item)
        self._fh.write(json.dumps(item.to_wire(), ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._lines += 1
        if self._items and self._lines > len(self._items) * COMPACT_RATIO:
            self._compact()

    def _compact(self) -> None:
        """死行太多就重写一遍。**先写临时文件再 rename** —— 中途断电的话,
        老的 queue.jsonl 原封不动还在,重放照样能用。
        """
        tmp = self._path.with_name(self._path.name + ".tmp")
        # 注意:不按 done 过滤。self._items 本来就是按 key 去重之后的最新状态,
        # "死行"指的是同一个 key 之前写过的旧行,不是"已经传完的 key"——已完成
        # 的条目还要留着 offset/size,供追加流(events.jsonl)变长之后 offer()
        # 重新打开时接着传,不然这里一压缩,offset 就跟着没了。
        alive = [self._items[k] for k in self._order]
        with open(tmp, "w", encoding="utf-8") as fh:
            for item in alive:
                fh.write(json.dumps(item.to_wire(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._fh.close()
        os.replace(tmp, self._path)
        self._fh = open(self._path, "a", encoding="utf-8")
        self._items = {i.key: i for i in alive}
        self._order = [i.key for i in alive]
        self._lines = len(alive)

    def close(self) -> None:
        self._fh.close()

    # ---- 队 ----

    def offer(self, key: str, priority: int, *, size: int, mtime_ns: int = 0) -> bool:
        """入队。返回 ``True`` 表示这是新的一条。

        同一个 key 再来,分三种:

        * **长了**:只把 size 改大,不动 offset。追加流(events.jsonl)一直在长,
          已经传到第几个字节是不能忘的;已经 ``done`` 的也重新打开 —— 那是新证据。
        * **没长但 mtime 变了**:文件被原地改写了(report.md 判读后重生成、
          findings.json 复核后回写)。**从 0 重传**,``done`` 清掉。只比 size 的话
          同样大小或更小的改动永远传不上去(W02)。
        * **都没变**:什么都不做。每轮 scan 都会走到这儿,不能每轮都重传。

        ``mtime_ns`` 为 0 表示调用方不知道(老代码、老队列)。老条目第一次见到
        真 mtime 分两种(:data:`REWRITTEN_IN_PLACE`):改写型文件且已 ``done`` 的
        **从 0 重开一次** —— 老队列里的 ``done`` 证明不了盘上这份就是传上去的
        那份,升级前最后一次判读可能已经把它换掉了,而没进 pending 的条目服务器
        哈希也救不了;其余(照片、追加流)只把 mtime 记下来,不把已传完的历史
        全部重传。
        """
        old = self._items.get(key)
        if old is None:
            self._write(QueueItem(key=key, priority=priority, size=size, mtime_ns=mtime_ns))
            return True
        if size > old.size:
            # **长了就重新打开,哪怕它已经 done。** offset 原样留着接着传。
            self._write(QueueItem(**{**old.to_wire(), "size": size, "done": False,
                                     "mtime_ns": mtime_ns or old.mtime_ns}))
            return False
        if mtime_ns and old.mtime_ns and mtime_ns != old.mtime_ns:
            # **原地改写:从头来。** 之前传的那些字节对应的是旧内容,一个都不算数。
            self._write(QueueItem(**{**old.to_wire(), "size": size, "offset": 0,
                                     "done": False, "mtime_ns": mtime_ns}))
            return False
        if mtime_ns and not old.mtime_ns:
            if old.done and rewritten_in_place(key):
                # 老条目、改写型、已 done:不信这个 done,重传一次。见上。
                self._write(QueueItem(**{**old.to_wire(), "size": size, "offset": 0,
                                         "done": False, "mtime_ns": mtime_ns}))
            else:
                # 老条目补上 mtime(size 顺手刷新,别让它跟盘上对不上),别的不动。
                self._write(QueueItem(**{**old.to_wire(), "size": size, "mtime_ns": mtime_ns}))
        return False

    def advance(self, key: str, *, offset: int) -> None:
        """确认传到了第 ``offset`` 个字节。**attempts 归零** —— 有进展就不该
        继续退避:传了一半断了,恢复之后不能还罚站五分钟。
        """
        old = self._items[key]
        self._write(QueueItem(**{**old.to_wire(), "offset": offset, "attempts": 0, "next_ms": 0}))

    def rewind(self, key: str) -> None:
        """哈希对不上。**从头再来。** 这不是重试,是"之前传的那些全不算数"。"""
        old = self._items[key]
        self._write(QueueItem(**{**old.to_wire(), "offset": 0}))

    def defer(self, key: str, *, next_ms: int) -> None:
        old = self._items[key]
        self._write(
            QueueItem(**{**old.to_wire(), "attempts": old.attempts + 1, "next_ms": next_ms})
        )

    def finish(self, key: str) -> None:
        old = self._items[key]
        self._write(QueueItem(**{**old.to_wire(), "offset": old.size, "done": True}))

    # ---- 查 ----

    def get(self, key: str) -> QueueItem | None:
        return self._items.get(key)

    def all(self) -> list[QueueItem]:
        return [self._items[k] for k in self._order]

    def pending(self, now_ms: int) -> list[QueueItem]:
        """能传的,按 spec §4.3 的四级排;同级按入队先后。"""
        rank = {k: n for n, k in enumerate(self._order)}
        ready = [
            self._items[k]
            for k in self._order
            if not self._items[k].done and self._items[k].next_ms <= now_ms
        ]
        return sorted(ready, key=lambda i: (i.priority, rank[i.key]))

    def backlog(self) -> int:
        """还没传完的条数。**这个数要报进心跳**(spec §4.3)。"""
        return sum(1 for i in self._items.values() if not i.done)
