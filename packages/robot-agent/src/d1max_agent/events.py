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
        self._load()
        # 起来时压实一次:上一个 boot 的、已确认的都不要了,只留这个 boot 没确认的。
        if self._lines > len(self._pending):
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
                        if rec.get("boot_id") == self.boot_id:
                            upto = int(rec["seq"])
                            for s in [s for s in self._pending if s <= upto]:
                                del self._pending[s]
                        continue
                    ev = Event.from_wire(rec["event"])
                except (ValueError, KeyError, TypeError, ContractError) as exc:
                    log.warning("事件簿第 %d 行坏了,丢掉这一行: %s", n, exc)
                    continue
                if ev.boot_id != self.boot_id:
                    continue                      # 上一个 boot 的,不接着算
                self._seq = max(self._seq, ev.seq)
                self._pending[ev.seq] = ev

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

    def emit(self, kind: str, data: dict[str, Any]) -> Event:
        self._seq += 1
        ev = Event(event_id=f"evt-{self.boot_id}-{self._seq:06d}-{uuid.uuid4().hex[:6]}",
                   seq=self._seq, boot_id=self.boot_id, stamp=self._now(), kind=kind,
                   data=dict(data))
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
