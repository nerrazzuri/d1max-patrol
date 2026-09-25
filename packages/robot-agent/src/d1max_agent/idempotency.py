"""命令幂等(总设计 §3.2):``command_id → 结果`` 落盘,重复投递回原结果、不再执行,
代理重启后仍成立。JSON 行文件追加写;启动时重放,坏行只丢那一行。

**每条回执都记**,不只 accepted:拒绝过的命令再来也该回 duplicate + 原结果,不然
站点会看到同一条命令两种结论。同一 command_id 记两次以第一次为准。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from d1max_contract.errors import ContractError
from d1max_contract.messages import Ack

log = logging.getLogger(__name__)

#: 留最近多少条(W00c5d,决策 8:不只增不减)。命令有效期以分钟计,几千条够几周;更老的命令
#: 就算被重投,也会先因为过期被拒,不会再执行一次。
KEEP = 5000


class IdempotencyStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._acks: dict[str, Ack] = {}
        self._lines = 0
        self._load()
        if self._lines > 2 * KEEP:
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
                    ack = Ack.from_wire(json.loads(line))
                except (ValueError, ContractError) as exc:
                    log.warning("幂等记录第 %d 行坏了,丢掉这一行: %s", n, exc)
                    continue
                self._acks.setdefault(ack.command_id, ack)

    def lookup(self, command_id: str) -> Ack | None:
        return self._acks.get(command_id)

    def remember(self, ack: Ack) -> None:
        if ack.command_id in self._acks:
            return
        self._acks[ack.command_id] = ack
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ack.to_wire(), ensure_ascii=False) + "\n")
            f.flush()
        self._lines += 1
        if self._lines > 2 * KEEP:
            self._compact()

    def _compact(self) -> None:
        """只留最近 ``KEEP`` 条。先写临时文件再换名:中途断电老文件还在。"""
        keep = list(self._acks.values())[-KEEP:]
        tmp = self._path.with_name(self._path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for a in keep:
                f.write(json.dumps(a.to_wire(), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        self._acks = {a.command_id: a for a in keep}
        self._lines = len(keep)

    def __len__(self) -> int:
        return len(self._acks)
