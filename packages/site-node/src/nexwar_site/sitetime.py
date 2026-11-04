"""站点用哪个时区(W44 第二期)。

时间表、「今天」、报表按天分都按这个算:路线库设过时区(``route_store.timezone``)就用它,没设、认不出来
用站点主机的本地时区。网页不自己算星期、日子、时区。
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _local() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def site_tz(db: Any) -> tzinfo:
    rows = db.query("SELECT timezone FROM route_store WHERE id=1")
    name = rows[0]["timezone"] if rows else None
    if not name:
        return _local()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return _local()


def local_day_start(ms: int, tz: tzinfo) -> int:
    """``ms`` 那一刻在 ``tz`` 里那一天 0 点的毫秒。"""
    d = datetime.fromtimestamp(ms / 1000, tz)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def day_text(ms: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d")
