"""一趟归档**还有没有人在写**。发件箱(``outbox``)删一趟之前要问它。

**它不认识引擎**:「现在有没有在跑」这个问题从盘上判断(manifest 里有没有 ``summary``、
目录名上的起始时刻老不老),不问引擎。发件箱另外还会排除引擎**正在写**的那一趟。

W00c5e:老服务按水位清盘、删除预告、导出标记那一套随老服务退役(决策 8:删不删由站点确认
决定,见 ``outbox``),这里只剩「安定」这一条判据。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from d1max_agent.engine.archive import STAMP_FMT, read_manifest

#: 一趟归档多久算"没人再写它了"。
SETTLE_HOURS = 24

#: 目录名在时刻后面可能带一段数字(撞名的序号、发件箱模式的随机后缀)。拆时间戳前得先切掉。
_SUFFIX = re.compile(r"-\d+$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _started_at(name: str) -> datetime | None:
    """从目录名拆时间戳。**不从 manifest 拆** —— manifest 可能缺、可能坏,
    而目录名是 ``archive._make_dir`` 亲手写的,``list_runs`` 排序也靠它。"""
    try:
        stamp = datetime.strptime(_SUFFIX.sub("", name), STAMP_FMT)
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc)


def _manifest(run_dir: Path) -> dict[str, Any] | None:
    """读 manifest,坏了当没有(``archive.read_manifest`` 遇到坏 JSON 是抛)。"""
    try:
        raw = read_manifest(run_dir)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def is_settled(run_dir: Path | str, *, now: datetime | None = None) -> bool:
    """这一趟**还有没有人在写**:manifest 里有 ``summary``(跑完了),或者目录名上的起始时刻
    已经老过 :data:`SETTLE_HOURS`(崩在半路的残骸 —— 也没人在写它了)。两条都不满足的一律叫
    "不安定"。目录名不是时间戳就回 ``False``:不是我们建的目录,一律当"不安定",不碰。
    """
    run = Path(run_dir)
    started = _started_at(run.name)
    if started is None:
        return False
    raw = _manifest(run)
    now = now if now is not None else _utcnow()
    return bool(raw and raw.get("summary")) or started < now - timedelta(hours=SETTLE_HOURS)
