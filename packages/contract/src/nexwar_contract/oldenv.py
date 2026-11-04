"""改名(d1max → nexwar,2026-11)兼容一个版本:环境里还有 ``D1MAX_X``、没有 ``NEXWAR_X`` 的,照抄过去。

装机脚本迁移时会把 env 文件的键改掉;这里管的是还没迁移的机器、手敲的老命令。抄过去的记一条警告
(只记键名,不记值:值里可能有 PIN、口令)。进程里只做一次。"""
from __future__ import annotations

import logging
import os

OLD, NEW = "D1MAX_", "NEXWAR_"
_done = False


def adopt(environ: dict[str, str] | None = None) -> list[str]:
    """把老键抄成新键(新键已经有的不动)→ 抄了哪些新键。"""
    global _done
    env = os.environ if environ is None else environ
    got = []
    for k in [k for k in env if k.startswith(OLD)]:
        new = NEW + k[len(OLD):]
        if new not in env:
            env[new] = env[k]
            got.append(new)
    if got and environ is None and not _done:
        logging.getLogger("nexwar.oldenv").warning(
            "环境里还有改名前的 D1MAX_* 变量,先照用(%s);请重跑装机脚本迁移", ", ".join(sorted(got)))
    if environ is None:
        _done = True
    return got
