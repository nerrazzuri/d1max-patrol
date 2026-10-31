"""特权执行器的调用端(W55):代理、站点要 root 才能做的事,写一个请求、等执行器回结果。

执行器(``deploy/d1max-ops-runner``)由 systemd 路径单元拉起,以 root 跑;它**只认白名单动作**。
请求方只能往自己的请求目录里写 JSON,结果从结果目录读。两边约好的样子:

- 请求 ``<spool>/req/<id>.json``:``{"id": <十六进制>, "action": <动作>, "args": {...}}``
- 结果 ``<spool>/done/<id>.json``:``{"id", "action", "ok": bool, "error": str, ...}``

测试、演示站点、仿真机器人没有 root,用 :class:`SimOps`:动作记下来、照常回 ``ok``,什么都不真做。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

#: 装机脚本写的执行器配置(在就说明这台机器装了执行器)。
CONFIG = Path("/etc/d1max-ops.json")


class PrivOpsError(Exception):
    """请求发不出去、等不到结果(确定没执行)。"""


class PrivTimeout(PrivOpsError):
    """等不到结果,而且执行器已经把请求拿走了:**结果未知**,可能还在做、可能做完了(外审 W55-2 C3)。
    调用方别当没执行;按 ``rid`` 接着查 :meth:`SpoolOps.result`。"""

    def __init__(self, msg: str, rid: str) -> None:
        super().__init__(msg)
        self.rid = rid


class PrivOps(Protocol):
    def call(self, action: str, args: dict[str, Any] | None = None, *,
             timeout_s: float = 60.0, rid: str | None = None) -> dict[str, Any]: ...

    def submit(self, action: str, args: dict[str, Any] | None = None) -> str: ...

    def result(self, rid: str) -> dict[str, Any] | None: ...

    def cancel(self, rid: str) -> bool: ...


class SpoolOps:
    """真的那一份:写请求、轮询结果。"""

    def __init__(self, spool: Path | str, *, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.spool = Path(spool)
        self._sleep = sleep
        self._mono = monotonic

    def submit(self, action: str, args: dict[str, Any] | None = None) -> str:
        """只写请求、不等(会让请求方自己停掉的动作:站点升级、恢复、重启)。回请求号,之后
        :meth:`result` 查。"""
        return self._write(action, args)

    def result(self, rid: str) -> dict[str, Any] | None:
        try:
            got = json.loads((self.spool / "done" / f"{rid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return got if isinstance(got, dict) else None

    def call(self, action: str, args: dict[str, Any] | None = None, *,
             timeout_s: float = 60.0, rid: str | None = None) -> dict[str, Any]:
        """``rid``:调用方先定好请求号(先把它记下来再发,中途进程没了也查得到)。"""
        rid = self._write(action, args, rid)
        done = self.spool / "done" / f"{rid}.json"
        end = self._mono() + timeout_s
        while self._mono() < end:
            try:
                got = json.loads(done.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._sleep(0.2)
                continue
            except (OSError, ValueError) as exc:
                raise PrivOpsError(f"特权执行器的结果读不了:{exc}") from exc
            return got if isinstance(got, dict) else {"ok": False, "error": "结果格式错误"}
        if self.cancel(rid):
            raise PrivOpsError(f"特权执行器 {timeout_s:g} 秒内未处理,请求已撤回"
                               "(d1max-ops.path 未启用?)")
        raise PrivTimeout(f"特权执行器 {timeout_s:g} 秒内未回结果,请求 {rid} 已在执行,"
                          "结果未知", rid)

    def cancel(self, rid: str) -> bool:
        """撤回还没被执行器拿走的请求。回真 = 撤回了(一定不会执行);回假 = 执行器已经拿走了。
        执行器拿请求是**改名认领**,跟这里的删除只有一个能成。"""
        try:
            os.unlink(self.spool / "req" / f"{rid}.json")
        except FileNotFoundError:
            return False
        return True

    def _write(self, action: str, args: dict[str, Any] | None, rid: str | None = None) -> str:
        if rid is None or not (8 <= len(rid) <= 32 and all(c in "0123456789abcdef" for c in rid)):
            rid = secrets.token_hex(12)
        req = self.spool / "req"
        body = json.dumps({"id": rid, "action": action, "args": args or {}},
                          ensure_ascii=False).encode()
        tmp = req / f".{rid}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, req / f"{rid}.json")      # 路径单元只看 *.json:写完才出现
        except OSError as exc:
            raise PrivOpsError(f"特权执行器的请求写不进去({req}):{exc}") from exc
        return rid


class SimOps:
    """测试、演示用:记下每次调用,回 ``ok``(``fail`` 里的动作回失败)。"""

    def __init__(self, *, fail: dict[str, str] | None = None,
                 results: dict[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = dict(fail or {})
        self.results = dict(results or {})
        self._done: dict[str, dict[str, Any]] = {}

    def submit(self, action: str, args: dict[str, Any] | None = None) -> str:
        rid = f"sim{len(self.calls):06d}"
        self._done[rid] = self.call(action, args)
        return rid

    def result(self, rid: str) -> dict[str, Any] | None:
        return self._done.get(rid)

    def cancel(self, rid: str) -> bool:
        return False

    def call(self, action: str, args: dict[str, Any] | None = None, *,
             timeout_s: float = 60.0, rid: str | None = None) -> dict[str, Any]:
        self.calls.append((action, dict(args or {})))
        if action in self.fail:
            return {"action": action, "ok": False, "error": self.fail[action]}
        return {"action": action, "ok": True, "error": "", "simulated": True,
                **self.results.get(action, {})}


def from_config(path: Path = CONFIG) -> SpoolOps | None:
    """这台机器装了执行器就回真的那一份,没装回 ``None``(调用方据此说「不支持」)。"""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    spool = cfg.get("spool") if isinstance(cfg, dict) else None
    if not isinstance(spool, str) or not (Path(spool) / "req").is_dir():
        return None
    return SpoolOps(spool)
