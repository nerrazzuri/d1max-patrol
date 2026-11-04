"""要报、还没报成的告警(W24 复查起;W29 复查:天气也用)。

告警的**意图**先跟引起它的那一步在同一个事务里落库(``pending_alerts``,连标题、内容、现场),之后每拍
:func:`flush` 报出去,报成才删 —— 告警写不进去、站点重启都不丢。

**按意图去重**(W24 复查二):报的时候把意图号(``pending_alerts:<id>``,自增、不复用)写进告警的现场;
报成了、删待报那一下没成,下一拍在告警表里查到这个意图号(已解决的也算),就只删待报、不再报一次。
告警簿里这一位这一种还挂着的,当报过了(不重报)。
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def queue(tx: Any, *, kind: str, robot: str, title: str, detail: str,
          context: dict[str, Any], now_ms: int) -> None:
    """在调用方的事务里记一条要报的告警。"""
    tx.execute("INSERT INTO pending_alerts(kind, robot, title, detail, context, created_ms) "
               "VALUES (?,?,?,?,?,?)",
               (kind, robot, title, detail, json.dumps(context, ensure_ascii=False), now_ms))


def flush(db: Any, alerts: Any) -> int:
    """把待报的报出去,报成才删。回报成(或确认报过)了几条。没接告警台就不动。"""
    if alerts is None:
        return 0
    n = 0
    has_open = getattr(alerts, "has_open", None)
    for r in db.query("SELECT * FROM pending_alerts ORDER BY id"):
        intent = f"pending_alerts:{r['id']}"
        try:
            done = bool(db.query(
                "SELECT 1 FROM alerts WHERE robot=? AND kind=? "
                "AND json_extract(context, '$.intent')=? LIMIT 1",
                (r["robot"], r["kind"], intent)))
            if not done and not (callable(has_open) and has_open(r["robot"], r["kind"])):
                alerts.raise_alert(kind=r["kind"], robot=r["robot"], title=r["title"],
                                   detail=r["detail"],
                                   context=json.loads(r["context"]) | {"intent": intent})
            with db.tx() as c:
                c.execute("DELETE FROM pending_alerts WHERE id=?", (r["id"],))
            n += 1
        except Exception:
            log.exception("%s 的 %s 告警还没报成(下一拍再报)", r["robot"], r["kind"])
    return n
