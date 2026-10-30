"""今天的巡逻(W53):日程的每一轮 + 临时巡逻,按时间排,一行一趟、人话的状态。

App 的值守页、日程页,网页的值守页都用这一份(``GET /api/patrols/today``)。按站点时区算「今天」。

状态:
- ``done`` 走完、``failed`` 没走完(原因在 ``reason``)、``aborted`` 被叫停、``running`` 正在走;
- ``waiting`` 到点了还没起跑(机器人忙、离线……还在窗口里,一会儿可能就跑了);
- ``missed`` 这一轮没跑(窗口过了、跳过);
- ``next`` 下一趟、``later`` 再往后;
- 临时巡逻另有 ``queued`` 排队中、``cancelled`` 取消了、``expired`` 排太久作废了。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from d1max_contract.schedule import occurrences_between
from d1max_site.catalog import active_bundle
from d1max_site.sitetime import site_tz

_RESULT = {"done": "done", "failed": "failed", "aborted": "aborted", "preempted": "failed"}
_TERMINAL = {"task_done": "done", "task_failed": "failed", "task_aborted": "aborted",
             "task_preempted": "failed"}
#: 排程账里「这一轮到点了还没起跑」的几种去向(还在窗口里就是 waiting)。
_PENDING = ("busy", "no_robot", "displaced", "supervised", "weather", "skew", "ambiguous")


def _terminal(db: Any, robot: str, task_id: str) -> tuple[str, int, str] | None:
    """这一趟的结束事件 → (状态, 时刻, 原因)。"""
    if not task_id:
        return None
    rows = db.query(
        "SELECT kind, data, received_at FROM events WHERE robot_id=? AND kind IN "
        "('task_done','task_failed','task_aborted','task_preempted') "
        "AND json_extract(data, '$.task_id')=? ORDER BY rowid DESC LIMIT 1", (robot, task_id))
    if not rows:
        return None
    try:
        data = json.loads(rows[0]["data"])
    except ValueError:
        data = {}
    why = ""
    if isinstance(data, dict):
        why = str(data.get("reason") or data.get("error") or data.get("detail") or "")[:200]
    if rows[0]["kind"] == "task_preempted":
        why = why or "被更急的任务打断了"
    return _TERMINAL[rows[0]["kind"]], int(rows[0]["received_at"]), why


def today_view(db: Any, *, now_ms: int, adhoc: Any = None) -> dict[str, Any]:
    act = active_bundle(db)
    tz = act.schedule.tz() if act is not None else site_tz(db)
    now = datetime.fromtimestamp(now_ms / 1000, tz=tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    a_ms, b_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    items: list[dict[str, Any]] = []
    if act is not None:
        for e in act.schedule.entries:
            if act.since(e.id) > b_ms:
                continue
            for occ in occurrences_between(e, start, end):
                occ_ms = int(occ.timestamp() * 1000)
                if occ_ms < act.since(e.id):
                    continue                            # 这条日程加进来之前的轮次:那时还没有它
                items.append(_schedule_item(db, e, occ_ms, now_ms))
    if adhoc is not None:
        for r in adhoc.between(a_ms, b_ms):
            items.append(_adhoc_item(db, r, now_ms))
    items.sort(key=lambda x: (x["at_ms"], x["label"]))
    first = next((it for it in items if it["state"] == "later"), None)
    if first is not None:
        first["state"] = "next"                         # 还没到点的第一趟
    return {"date": start.strftime("%Y-%m-%d"), "timezone": str(tz), "items": items}


def _schedule_item(db: Any, e: Any, occ_ms: int, now_ms: int) -> dict[str, Any]:
    rows = [dict(r) for r in db.query(
        "SELECT outcome, robot_id, task_id, result, note, decided_at FROM schedule_runs "
        "WHERE entry_id=? AND scheduled_ms=? ORDER BY id", (e.id, occ_ms))]
    started = next((r for r in rows if r["outcome"] == "started"), None)
    item: dict[str, Any] = {"at_ms": occ_ms, "kind": "schedule", "label": e.mission,
                            "robot": e.robot, "by": "", "state": "later", "reason": "",
                            "end_ms": None, "schedule_id": e.id, "adhoc_id": "", "task_id": ""}
    if started is not None:
        rid = started["robot_id"] or e.robot
        item.update(robot=rid, task_id=started["task_id"] or "")
        fin = _terminal(db, rid, item["task_id"])
        if fin is not None:
            item.update(state=fin[0], end_ms=fin[1], reason=fin[2])
        elif started["result"] in _RESULT:
            item["state"] = _RESULT[started["result"]]
        else:
            item["state"] = "running"
        return item
    if occ_ms > now_ms:
        return item
    settled = next((r for r in rows if r["outcome"] in ("skip", "alarm", "dispatch_failed",
                                                           "lost")), None)
    last = rows[-1] if rows else None
    window_over = now_ms > occ_ms + int(e.window_min) * 60_000
    if settled is not None or window_over:
        item["state"] = "missed"
        item["reason"] = ((settled or last or {}).get("note") or "到点没有能派的机器人")[:200]
    else:
        item["state"] = "waiting"
        item["reason"] = ((last or {}).get("note") or "")[:200] if last and \
            last["outcome"] in _PENDING else ""
    return item


def _adhoc_item(db: Any, r: dict[str, Any], now_ms: int) -> dict[str, Any]:
    at = r["started_ms"] or r["not_before_ms"]
    item: dict[str, Any] = {"at_ms": at, "kind": "adhoc", "label": r["label"], "robot": r["robot"],
                            "by": r["by"], "state": r["state"], "reason": r["note"] or "",
                            "end_ms": None, "schedule_id": "", "adhoc_id": r["id"],
                            "task_id": r["task_id"] or ""}
    if r["state"] == "queued":
        item["end_ms"] = r["not_before_ms"] + int(r["estimate_s"]) * 1000
    elif r["state"] == "started":
        fin = _terminal(db, r["robot"], r["task_id"])
        if fin is not None:
            item.update(state=fin[0], end_ms=fin[1], reason=fin[2])
        else:
            item.update(state="running",
                        end_ms=(r["started_ms"] or now_ms) + int(r["estimate_s"]) * 1000,
                        reason="")
    return item
