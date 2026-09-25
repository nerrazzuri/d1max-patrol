"""事件簿(总设计 §3.3):``(boot_id, seq)``,``seq`` 从 1 单调;outbox 落盘;
未确认区间给 reconcile 报。

「确认」在 W00 里是「已发布即视为投递」:运行时在连着的时候发出去就 ``mark_acked``;
断线期间攒下的留在 pending,重连后 reconcile 先报区间、再补发。换 boot_id 后 seq 归 1,
上一个 boot 的未确认事件不再补发(站点按 boot_id 分开算)。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.messages import Event

log = logging.getLogger(__name__)

#: 文件行数过了这个数、而且大半是确认过的,就压实一次(W00c5d,决策 8:狗上的东西不只增不减)。
COMPACT_MIN_LINES = 2000


class EventBook:
    def __init__(self, path: Path, *, boot_id: str, now_ms: Callable[[], int]) -> None:
        self._path = Path(path)
        self.boot_id = boot_id
        self._now = now_ms
        self._seq = 0
        self._pending: dict[int, Event] = {}
        self._lines = 0
        #: 上一个(几个)进程没确认的事件:带到这个 boot 里补发(W00c5d 内部评审,决策 8:断网暂存的
        #: 东西恢复后要传上去,不许因为代理重启就丢了 —— 任务结果、故障都在里面)。
        self._other: dict[str, dict[int, Event]] = {}
        self._other_acked: dict[str, int] = {}
        self._load()
        carried = self._carry()
        # 起来时压实一次:已确认的、带过来了的都不要了,只留这个 boot 没确认的。
        if carried or self._lines > len(self._pending):
            self._compact()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                self._lines += 1
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        raise ValueError("不是对象")
                    if rec.get("op") == "ack":
                        upto = int(rec["seq"])
                        if rec.get("boot_id") == self.boot_id:
                            for s in [s for s in self._pending if s <= upto]:
                                del self._pending[s]
                        else:
                            b = str(rec.get("boot_id"))
                            self._other_acked[b] = max(self._other_acked.get(b, 0), upto)
                        continue
                    ev = Event.from_wire(rec["event"])
                except (ValueError, KeyError, TypeError, ContractError) as exc:
                    log.warning("事件簿第 %d 行坏了,丢掉这一行: %s", n, exc)
                    continue
                if ev.boot_id != self.boot_id:
                    self._other.setdefault(ev.boot_id, {})[ev.seq] = ev
                    continue
                self._seq = max(self._seq, ev.seq)
                self._pending[ev.seq] = ev

    def _carry(self) -> int:
        """把别的 boot 没确认的事件按原来的先后、原来的时刻补发进这个 boot(标上 ``carried_from``)。
        已经被带过一次的(某条事件的 ``carried_from`` 指着它)不再带第二次 —— 带到一半断电的那种。"""
        left = [ev for b, evs in self._other.items() for s, ev in evs.items()
                if s > self._other_acked.get(b, 0)]
        already = {str(ev.data.get("carried_from")) for ev in self._pending.values()
                   if isinstance(ev.data, dict) and ev.data.get("carried_from")}
        already |= {str(ev.data.get("carried_from")) for ev in left
                    if isinstance(ev.data, dict) and ev.data.get("carried_from")}
        n = 0
        for ev in sorted(left, key=lambda e: (e.stamp, e.seq)):
            if ev.event_id in already:
                continue
            root = (ev.data.get("carried_from") if isinstance(ev.data, dict) else None) \
                or ev.event_id
            self.emit(ev.kind, {**(ev.data if isinstance(ev.data, dict) else {}),
                                "carried_from": root}, stamp=ev.stamp)
            n += 1
        if n:
            log.info("上一个进程没确认的事件 %d 条,带到这个 boot 里补发", n)
        self._other.clear()
        return n

    def _append(self, rec: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._lines += 1

    def _compact(self) -> None:
        """重写成「这个 boot 还没确认的事件」。先写临时文件再换名:中途断电老文件还在。"""
        tmp = self._path.with_name(self._path.name + ".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            for s in sorted(self._pending):
                f.write(json.dumps({"op": "event", "event": self._pending[s].to_wire()},
                                   ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        self._lines = len(self._pending)

    def emit(self, kind: str, data: dict[str, Any], *, stamp: int | None = None) -> Event:
        self._seq += 1
        ev = Event(event_id=f"evt-{self.boot_id}-{self._seq:06d}-{uuid.uuid4().hex[:6]}",
                   seq=self._seq, boot_id=self.boot_id,
                   stamp=self._now() if stamp is None else stamp, kind=kind, data=dict(data))
        self._pending[ev.seq] = ev
        self._append({"op": "event", "event": ev.to_wire()})
        return ev

    def mark_acked(self, seq: int) -> None:
        """**累计**确认:到 seq 为止(含)都算送到了。事件按序发,确认也按序。"""
        gone = [s for s in self._pending if s <= seq]
        for s in gone:
            del self._pending[s]
        if gone:
            self._append({"op": "ack", "boot_id": self.boot_id, "seq": seq})
            if self._lines >= COMPACT_MIN_LINES and self._lines > 4 * len(self._pending):
                self._compact()

    def pending(self) -> list[Event]:
        return [self._pending[s] for s in sorted(self._pending)]

    def unacked_range(self) -> tuple[int, int]:
        if not self._pending:
            return (0, 0)
        return (min(self._pending), max(self._pending))

    @property
    def last_seq(self) -> int:
        return self._seq
