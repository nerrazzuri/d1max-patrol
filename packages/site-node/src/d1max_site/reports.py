"""值班日报(商业化 C-B3b):一段时间里告警、入侵事件、排程巡逻、运行记录各多少,多久有人确认、
谁处理的。

值班台的 Reports 页用:保安交班、给业主看。**只读库**,不改任何东西;数是按 SQL 数出来的(不截),
明细每一样最多带 :data:`ROWS_MAX` 行,截了在回包里写明(``truncated``)。

归到哪一段:告警按**第一次报**的时刻,入侵事件按站点**收到**的时刻,排程按**该跑**的时刻,证据归档按
第一个文件到站点的时刻。都是 ``[since, until)``。

没有的:每台狗在线了多久(站点不记在线历史)、误报率(「记成误报」和「解决了」在库里是同一个动作)。
"""
from __future__ import annotations

import json
from typing import Any

from d1max_site.db import SiteDB

#: 一份报表最长盖这么久(毫秒):31 天。
SPAN_MAX_MS = 31 * 86_400_000
#: 每一样明细最多带这么多行。
ROWS_MAX = 1000


class ReportError(ValueError):
    """时间段不对(API 回 400)。"""


def _median(xs: list[int]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    mid = len(xs) // 2
    return float(xs[mid]) if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def _counts(db: SiteDB, sql: str, args: tuple) -> dict[str, int]:
    return {str(r[0] if r[0] is not None else ""): r[1] for r in db.query(sql, args)}


def daily(db: SiteDB, since_ms: Any, until_ms: Any) -> dict[str, Any]:
    for v in (since_ms, until_ms):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ReportError("since、until 要是非负整数(毫秒)")
    if until_ms <= since_ms:
        raise ReportError("until 要比 since 晚")
    if until_ms - since_ms > SPAN_MAX_MS:
        raise ReportError("一份报表最长 31 天")
    w = (since_ms, until_ms)
    return {"since_ms": since_ms, "until_ms": until_ms, "rows_max": ROWS_MAX,
            "alarms": _alarms(db, w), "incidents": _incidents(db, w),
            "patrols": _patrols(db, w), "runs": _runs(db, w)}


def _alarms(db: SiteDB, w: tuple[int, int]) -> dict[str, Any]:
    where = "FROM alerts WHERE first_ms>=? AND first_ms<?"
    by_level = {"P1": 0, "P2": 0, "P3": 0,
                **_counts(db, f"SELECT level, COUNT(*) {where} GROUP BY level", w)}
    [n] = db.query(
        f"SELECT COUNT(*) AS total, "
        f"SUM(acked_ms IS NOT NULL) AS acked, SUM(resolved_ms IS NOT NULL) AS resolved, "
        f"SUM(acked_ms IS NULL AND resolved_ms IS NULL) AS waiting, "
        f"SUM(acked_ms IS NULL AND resolved_ms IS NOT NULL) AS closed_unacked {where}", w)
    ack: dict[str, Any] = {}
    for level in ("P1", "P2"):
        xs = [r[0] for r in db.query(
            f"SELECT acked_ms - first_ms {where} AND level=? AND acked_ms IS NOT NULL",
            (*w, level))]
        xs = [max(0, x) for x in xs]                    # 钟差一点:不出负数
        med = _median(xs)
        ack[level] = {"n": len(xs), "median_s": None if med is None else med / 1000,
                      "max_s": max(xs) / 1000 if xs else None}
    acked = _counts(db, f"SELECT acked_by, COUNT(*) {where} AND acked_ms IS NOT NULL "
                        "GROUP BY acked_by", w)
    closed = _counts(db, f"SELECT resolved_by, COUNT(*) {where} AND resolved_ms IS NOT NULL "
                         "GROUP BY resolved_by", w)
    handlers = [{"name": name, "acked": acked.get(name, 0), "resolved": closed.get(name, 0)}
                for name in sorted(set(acked) | set(closed))]
    kinds = [{"kind": r["kind"], "level": r["level"], "n": r["n"]} for r in db.query(
        f"SELECT kind, level, COUNT(*) AS n {where} GROUP BY kind, level "
        "ORDER BY level, n DESC, kind", w)]
    rows = [_alarm_row(r) for r in db.query(
        f"SELECT * {where} ORDER BY first_ms, key LIMIT ?", (*w, ROWS_MAX))]
    total = n["total"]
    return {"total": total, "by_level": by_level, "acked": n["acked"] or 0,
            "resolved": n["resolved"] or 0, "waiting": n["waiting"] or 0,
            "closed_unacked": n["closed_unacked"] or 0, "ack": ack, "handlers": handlers,
            "by_kind": kinds, "rows": rows, "truncated": total > len(rows)}


def _alarm_row(r: Any) -> dict[str, Any]:
    try:
        zone = json.loads(r["context"] or "{}").get("zone")
    except (ValueError, AttributeError):
        zone = None
    return {"key": r["key"], "level": r["level"], "kind": r["kind"], "robot": r["robot"],
            "title": r["title"], "first_ms": r["first_ms"], "count": r["count"],
            "acked_by": r["acked_by"], "acked_ms": r["acked_ms"],
            "resolved_by": r["resolved_by"], "resolved_ms": r["resolved_ms"],
            "zone": zone if isinstance(zone, str) else None}


def _incidents(db: SiteDB, w: tuple[int, int]) -> dict[str, Any]:
    where = "FROM incidents WHERE received_at>=? AND received_at<?"
    by = _counts(db, f"SELECT outcome, COUNT(*) {where} GROUP BY outcome", w)
    rows = [{k: r[k] for k in ("id", "received_at", "source", "type", "zone", "intercept",
                               "outcome", "robot_id", "result", "note")}
            for r in db.query(f"SELECT * {where} ORDER BY received_at, id LIMIT ?",
                              (*w, ROWS_MAX))]
    total = sum(by.values())
    return {"total": total, "by_outcome": by, "rows": rows, "truncated": total > len(rows)}


def _patrols(db: SiteDB, w: tuple[int, int]) -> dict[str, Any]:
    """排程:该跑的每一趟最后怎么样了。``started`` 的再看狗报的结果(``result``;还没报 = 还在跑)。"""
    where = "FROM schedule_runs WHERE scheduled_ms>=? AND scheduled_ms<?"
    by = _counts(db, f"SELECT outcome, COUNT(*) {where} GROUP BY outcome", w)
    res = _counts(db, f"SELECT COALESCE(result, 'running'), COUNT(*) {where} "
                      "AND outcome='started' GROUP BY 1", w)
    rows = [{k: r[k] for k in ("id", "entry_id", "scheduled_ms", "outcome", "robot_id",
                               "result", "note")}
            for r in db.query(f"SELECT * {where} ORDER BY scheduled_ms, id LIMIT ?",
                              (*w, ROWS_MAX))]
    total = sum(by.values())
    return {"total": total, "by_outcome": by, "started": by.get("started", 0),
            "by_result": res, "rows": rows, "truncated": total > len(rows)}


def _runs(db: SiteDB, w: tuple[int, int]) -> dict[str, Any]:
    """证据归档:狗传上来的**都算**(巡逻记录、到拦截点拍的、人员截图),不只是巡逻 —— 别拿它当
    巡逻的工作量(C-B3 外审 M1);巡逻跑了几趟看 :func:`_patrols`。"""
    [n] = db.query("SELECT COUNT(*) AS total, SUM(finished) AS finished, SUM(photos) AS photos "
                   "FROM runs WHERE first_ms>=? AND first_ms<?", w)
    by_robot = _counts(db, "SELECT robot_id, COUNT(*) FROM runs WHERE first_ms>=? AND first_ms<? "
                           "GROUP BY robot_id", w)
    return {"total": n["total"], "finished": n["finished"] or 0, "photos": n["photos"] or 0,
            "by_robot": by_robot}
