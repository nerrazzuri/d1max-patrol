"""临时详细日志(W55):工程师远程打开「详细日志 N 分钟」,到点自动恢复。

一个到点文件(JSON ``{"until_ms": ...}``)由代理写;我们自己的 Python 服务各起一个看门的线程,
每 10 秒看一次,
没过点就把根日志级别调到 DEBUG,过点或文件没了就调回原来的级别。文件写坏了当没有。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

#: 缺省的到点文件(代理、各服务都按这一个)。
DEFAULT_FLAG = Path("/var/lib/d1max/debug-until.json")


def until_ms(flag: Path) -> int:
    try:
        v = json.loads(Path(flag).read_text(encoding="utf-8")).get("until_ms")
    except (OSError, ValueError, AttributeError):
        return 0
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else 0


def set_until(flag: Path, until: int) -> None:
    """写到点时刻(``0`` = 关)。先写临时文件再换名:看门的线程不会读到半个文件。"""
    flag = Path(flag)
    flag.parent.mkdir(parents=True, exist_ok=True)
    tmp = flag.with_name(flag.name + ".tmp")
    tmp.write_text(json.dumps({"until_ms": int(until)}), encoding="utf-8")
    tmp.replace(flag)


class DebugWatch:
    """看门的:``tick()`` 一次看一回(测试直接调);``start()`` 起后台线程每 ``every_s`` 秒看一回。"""

    def __init__(self, flag: Path = DEFAULT_FLAG, *, logger: logging.Logger | None = None,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 every_s: float = 10.0) -> None:
        self.flag = Path(flag)
        self.logger = logger or logging.getLogger()
        self.base = self.logger.level
        self._now = now_ms
        self.every_s = every_s
        self.on = False

    def tick(self) -> bool:
        want = until_ms(self.flag) > self._now()
        if want and not self.on:
            self.base = self.logger.level
            self.logger.setLevel(logging.DEBUG)
            self.logger.info("详细日志已打开(远程运维)")
        elif not want and self.on:
            self.logger.setLevel(self.base)
            self.logger.info("详细日志已关闭")
        self.on = want
        return want

    def start(self) -> threading.Thread:
        def loop() -> None:
            while True:
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 —— 看门的线程不许死
                    pass
                time.sleep(self.every_s)
        t = threading.Thread(target=loop, name="d1max-debuglog", daemon=True)
        t.start()
        return t
