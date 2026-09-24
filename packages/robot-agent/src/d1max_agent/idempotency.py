"""命令幂等(总设计 §3.2):``command_id → 结果`` 落盘,重复投递回原结果、不再执行,
代理重启后仍成立。JSON 行文件追加写;启动时重放,坏行只丢那一行。

**每条回执都记**,不只 accepted:拒绝过的命令再来也该回 duplicate + 原结果,不然
站点会看到同一条命令两种结论。同一 command_id 记两次以第一次为准。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from d1max_contract.errors import ContractError
from d1max_contract.messages import Ack

log = logging.getLogger(__name__)


class IdempotencyStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._acks: dict[str, Ack] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
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

    def __len__(self) -> int:
        return len(self._acks)
