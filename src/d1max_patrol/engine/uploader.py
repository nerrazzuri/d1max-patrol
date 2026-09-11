"""上传器。spec §4.1(单位是文件)/§4.2(哈希对上才算传成功)/§4.3(四级优先)。

**这个模块里没有 threading,没有 time.sleep,没有 socket。** ``run_once()``
走一步就返回,钟从参数进来,真的发包由注入的 :class:`UploadSink` 负责。
把它放到后台跑是更上层(线程壳)的事 ——
**"上传线程绝不阻塞巡检"这条保证,靠的是这里一步一停。**
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from d1max_patrol.engine.upload_queue import QueueItem, UploadQueue, backoff_ms, classify

#: 一次发多少。**未验证:1 MiB 一块在客户的 4G 上是不是太大,没量过。**
CHUNK_BYTES = 1_048_576

#: 发一块的超时。**未验证。** 这个数给 sink 的实现用,这里只是把它摆在一处。
HTTP_TIMEOUT_S = 30

_READ = 1_048_576


def sha256_prefix(path: Path | str, n: int) -> str:
    """算文件前 ``n`` 个字节的 sha256。**流式,不整个读进内存** —— 照片可能几 MB,
    事件流跑一整天可能几十 MB。
    """
    digest = hashlib.sha256()
    left = n
    with open(path, "rb") as fh:
        while left > 0:
            chunk = fh.read(min(_READ, left))
            if not chunk:
                break
            digest.update(chunk)
            left -= len(chunk)
    return digest.hexdigest()


class SinkError(Exception):
    """发不出去。**这是唯一允许从 sink.put 漏出来的异常类型。**

    别让 sink 抛别的:``run_once`` 只 catch 这一种,catch Exception 会把编码
    错误、路径错误一起吞成"网络不好",而 ruff 的 BLE 也不许那么写。
    """


@dataclass(frozen=True)
class PutRequest:
    """发出去的一块。``run`` 是 ``<mission>/<timestamp>``,``rel`` 是 run 内相对路径。"""

    sn: str
    run: str
    rel: str
    offset: int
    data: bytes
    total: int


@dataclass(frozen=True)
class PutReceipt:
    """回执。

    ``stored`` 是**服务器那边这个文件现在有多少字节**,``sha256`` 是它对这
    ``stored`` 个字节**自己重新算**出来的哈希。

    **回显我发的哈希不算数**(spec §4.2)—— 那只证明它收到了那个字符串。
    """

    ok: bool
    stored: int
    sha256: str
    message: str = ""


class UploadSink(Protocol):
    def put(self, req: PutRequest) -> PutReceipt: ...


@dataclass(frozen=True)
class Step:
    """走一步的结果。``action`` 只有六种,别加第七种而不改这行注释。

    idle 队列空或都在退避里 / sent 传了一块还没完 / done 这个文件对上了 /
    deferred 发不出去,退避 / rewound 哈希对不上,从头再来 / gone 文件没了
    """

    key: str
    action: str
    detail: str


class Uploader:
    def __init__(
        self,
        runs_root: Path | str,
        queue: UploadQueue,
        sink: UploadSink,
        *,
        sn: str,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.runs_root = Path(runs_root)
        self.queue = queue
        self.sink = sink
        self.sn = sn
        self._rand = rand

    # ---- 扫 ----

    def scan(self) -> int:
        """走一遍 runs 目录,该传的入队。返回**新**入队的条数。

        **不问这一趟结束了没有**(spec §4.1):``RunArchive.finish()`` 有可能
        永远不被调用 —— 断电、进程被杀、任务中途 ABORT —— 而崩溃那一趟恰恰是
        最需要看的一趟。谁在盘上谁就传。
        """
        if not self.runs_root.is_dir():
            return 0
        added = 0
        for path in sorted(self.runs_root.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(self.runs_root).as_posix()
            parts = key.split("/")
            if len(parts) < 3:
                continue
            rel = "/".join(parts[2:])
            priority = classify(rel)
            if priority is None:
                continue
            if self.queue.offer(key, priority, size=path.stat().st_size):
                added += 1
        return added

    def backlog(self) -> int:
        return self.queue.backlog()

    # ---- 传 ----

    def run_once(self, now_ms: int) -> Step:
        ready = self.queue.pending(now_ms)
        if not ready:
            return Step(key="", action="idle", detail="队列空")
        item = ready[0]
        path = self.runs_root / item.key
        parts = item.key.split("/")
        run, rel = "/".join(parts[:2]), "/".join(parts[2:])

        # **不先 is_file() 再 open()** —— 两者之间有个检查-使用窗口,文件恰好在
        # 这中间被删的话,is_file() 判过之后 open() 照样能抛。直接 try open,
        # 拿到 OSError 再去分辨到底是哪一种"读不到"。
        try:
            with open(path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                offset = min(item.offset, size)
                fh.seek(offset)
                data = fh.read(CHUNK_BYTES)
        except OSError:
            return self._missing(item, run, now_ms)

        req = PutRequest(sn=self.sn, run=run, rel=rel, offset=offset, data=data, total=size)
        try:
            receipt = self.sink.put(req)
        except SinkError as exc:
            wait = backoff_ms(item.attempts + 1, rand=self._rand)
            self.queue.defer(item.key, next_ms=now_ms + wait)
            return Step(key=item.key, action="deferred", detail=f"{exc}(等 {wait} ms)")

        if not receipt.ok:
            # 服务器那边比我们以为的少 —— 换过盘、回滚过备份。**以服务器说的为准**,
            # 它才是要长期存的那一份。退回去重发比"假装传过了"便宜得多。
            self.queue.rewind(item.key)
            wait = backoff_ms(item.attempts + 1, rand=self._rand)
            self.queue.defer(item.key, next_ms=now_ms + wait)
            return Step(key=item.key, action="rewound", detail=receipt.message or "服务器拒收")

        # **§4.2 的全部要害在这三行。** 不看 ok,不看它回显了什么,只看它
        # 对自己落盘的那 stored 个字节重新算出来的哈希,跟我们本地同样长度的
        # 前缀哈希对不对得上。
        want = sha256_prefix(path, receipt.stored)
        if receipt.sha256 != want:
            self.queue.rewind(item.key)
            wait = backoff_ms(item.attempts + 1, rand=self._rand)
            self.queue.defer(item.key, next_ms=now_ms + wait)
            return Step(key=item.key, action="rewound", detail="服务器算出来的哈希对不上")

        self.queue.advance(item.key, offset=receipt.stored)
        if receipt.stored >= size:
            self.queue.finish(item.key)
            return Step(key=item.key, action="done", detail=f"{receipt.stored} 字节对上了")
        return Step(key=item.key, action="sent", detail=f"到第 {receipt.stored} 个字节")

    def _missing(self, item: QueueItem, run: str, now_ms: int) -> Step:
        """文件读不到,分三种情况——只有第三种真的允许销账。

        ``UploadQueue.offer()`` 的规矩是"新 size <= 老 size 就不重开",所以这里
        一旦错判成 ``gone`` 销账,而文件其实还在、大小没变,以后每一次 ``scan()``
        都不会把它捡回来——那是真的能永久丢证据的一条路径。spec §4.3 写死的是
        "传不上去就一直排着,不做重试 N 次后放弃"。

        1. ``runs_root`` 自己不是目录——盘没挂上,最现实的触发场景不是杀毒软件,
           是这一卷短暂掉线或者还没 mount 完。**退避,绝不销账。**
        2. ``runs_root`` 在,但这一趟的 run 目录也不在了——``retention.py`` 是
           整趟 ``rmtree`` 删的,这就是水位线真的删掉了它。**销账。**
        3. run 目录还在,单独这一个文件不见了——这不是 retention 的形状,是异常。
           **退避,不销账。** 代价是这一条会一直排着、``backlog()`` 不归零——这正是
           §4.3 要的那一侧:让值守屏上看得见一个不降的积压,好过悄悄丢掉一份证据。
        """
        if not self.runs_root.is_dir():
            wait = backoff_ms(item.attempts + 1, rand=self._rand)
            self.queue.defer(item.key, next_ms=now_ms + wait)
            return Step(
                key=item.key,
                action="deferred",
                detail=f"归档盘不在(runs_root 读不到,等 {wait} ms)",
            )
        if not (self.runs_root / run).is_dir():
            # 水位线把整趟删了(spec §4.4 的删除由水位驱动)。**销账,别永远卡着** ——
            # 一条读不到的文件挡在前面,后面的条目就永远轮不上。
            self.queue.finish(item.key)
            return Step(key=item.key, action="gone", detail="这一趟已经被水位线删掉了")
        wait = backoff_ms(item.attempts + 1, rand=self._rand)
        self.queue.defer(item.key, next_ms=now_ms + wait)
        return Step(
            key=item.key,
            action="deferred",
            detail=f"run 目录还在但这个文件不见了(等 {wait} ms)",
        )
