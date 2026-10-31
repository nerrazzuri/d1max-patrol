"""值班日报(商业化 C-B3b):一段时间里告警、入侵事件、排程巡逻、运行记录各多少,多久有人确认、
谁处理的。

值班台的 Reports 页用:保安交班、给管理员看。**只读库**,不改任何东西;数是按 SQL 数出来的(不截),
明细每一样最多带 :data:`ROWS_MAX` 行,截了在回包里写明(``truncated``)。

归到哪一段:告警按**第一次报**的时刻,入侵事件按站点**收到**的时刻,排程按**该跑**的时刻,证据归档按
第一个文件到站点的时刻。都是 ``[since, until)``。

没有的:每台机器人在线了多久(站点不记在线历史)。误报从 W44 起按告警结论(``verdict``)数,
见 :func:`summary`。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from d1max_site.alert_audience import GUARD_AUDIENCES, audience
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


def _check(since_ms: Any, until_ms: Any) -> None:
    for v in (since_ms, until_ms):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ReportError("since、until 要是非负整数(毫秒)")
    if until_ms <= since_ms:
        raise ReportError("until 要比 since 晚")
    if until_ms - since_ms > SPAN_MAX_MS:
        raise ReportError("一份报表最长 31 天")


def daily(db: SiteDB, since_ms: Any, until_ms: Any) -> dict[str, Any]:
    _check(since_ms, until_ms)
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


_ZERO = {"patrols": 0, "incidents": 0, "false_alarms": 0, "robot_problems": 0}
_PATROL_OK = "(ack_result IS NULL OR ack_result NOT IN ('rejected', 'expired', 'timeout'))"


def summary(db: SiteDB, since_ms: Any, until_ms: Any, *, tz: tzinfo) -> dict[str, Any]:
    """管理员的报表页(W44 第二期,设计 §5.1「报表」):``[since, until)`` 里

    - ``totals``:巡了多少趟(派出去、没被拒的巡逻)、出了几次事(有人闯入、起火过热)、其中误报几次
      (结论 ``false_alarm``)、机器人出问题几次(需要帮忙、掉线);
    - ``days``:按站点时区的日子,每天这四个数,没事的日子也列(柱状图连续);
    - ``events``:给保安的那四类告警,按时间,最多 :data:`ROWS_MAX` 条(截了 ``truncated``)。
    """
    _check(since_ms, until_ms)
    w = (since_ms, until_ms)

    def day_of(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d")

    first = datetime.fromtimestamp(since_ms / 1000, tz).date()
    last = datetime.fromtimestamp((until_ms - 1) / 1000, tz).date()
    days: dict[str, dict[str, int]] = {}
    d: date = first
    while d <= last:
        days[d.isoformat()] = dict(_ZERO)
        d += timedelta(days=1)
    for r in db.query(f"SELECT issued_at FROM commands WHERE kind='patrol' AND issued_at>=? AND "
                      f"issued_at<? AND {_PATROL_OK}", w):
        days[day_of(r["issued_at"])]["patrols"] += 1
    events: list[dict[str, Any]] = []
    total_events = 0
    for r in db.query("SELECT key, kind, level, robot, title, first_ms, verdict, resolved_by, "
                      "context FROM alerts WHERE first_ms>=? AND first_ms<? "
                      "ORDER BY first_ms, key", w):
        aud = audience(r["kind"])
        if aud not in GUARD_AUDIENCES:
            continue
        bucket = days[day_of(r["first_ms"])]
        if aud in ("intrusion", "fire"):
            bucket["incidents"] += 1
            if r["verdict"] == "false_alarm":
                bucket["false_alarms"] += 1
        else:
            bucket["robot_problems"] += 1
        total_events += 1
        if len(events) < ROWS_MAX:
            ev = {k: r[k] for k in ("key", "kind", "level", "robot", "title",
                                    "first_ms", "verdict", "resolved_by")} | {"audience": aud}
            if r["robot"] == "site":                   # 摄像头报的:带上防区(报表上写哪个防区)
                try:
                    zone = json.loads(r["context"] or "{}").get("zone")
                except (ValueError, AttributeError):
                    zone = None
                if isinstance(zone, str) and zone:
                    ev["zone"] = zone
            events.append(ev)
    totals = dict(_ZERO)
    for b in days.values():
        for k in totals:
            totals[k] += b[k]
    span = until_ms - since_ms
    return {"since_ms": since_ms, "until_ms": until_ms, "totals": totals,
            "days": [{"day": k, **v} for k, v in days.items()], "events": events,
            "truncated": total_events > len(events),
            "kpis": kpis(db, since_ms, until_ms), "kpis_prev": kpis(db, since_ms - span, since_ms)}


#: 一趟巡逻没收到结束事件时,按这么长算它在跑(人工干预率用)。
RUN_MAX_MS = 3 * 3_600_000
_TERMINAL = ("task_done", "task_failed", "task_aborted", "task_preempted")


def _down_ms(db: SiteDB, names: list[str], kinds: tuple[str, ...], since_ms: int,
             until_ms: int) -> int:
    """这几台在 ``[since, until)`` 里掉线一共多久(掉线告警从出现到解决;没解决算到 until;
    同一台重叠的合并)。"""
    if not names:
        return 0
    marks = ",".join("?" * len(kinds))
    per: dict[str, list[tuple[int, int]]] = {}
    for r in db.query(f"SELECT robot, first_ms, resolved_ms FROM alerts WHERE kind IN ({marks}) "
                      "AND first_ms<? AND (resolved_ms IS NULL OR resolved_ms>?)",
                      (*kinds, until_ms, since_ms)):
        if r["robot"] in names:
            a = max(r["first_ms"], since_ms)
            b = min(r["resolved_ms"] if r["resolved_ms"] is not None else until_ms, until_ms)
            if b > a:
                per.setdefault(r["robot"], []).append((a, b))
    total = 0
    for spans in per.values():
        spans.sort()
        cur_a, cur_b = spans[0]
        for a, b in spans[1:]:
            if a <= cur_b:
                cur_b = max(cur_b, b)
            else:
                total += cur_b - cur_a
                cur_a, cur_b = a, b
        total += cur_b - cur_a
    return total


def kpis(db: SiteDB, since_ms: int, until_ms: int) -> dict[str, Any]:
    """报表的关键指标(W60):``[since, until)`` 里

    安防运营 —— ``patrol_completion`` 派出的巡逻(没被拒的)有几成走完(``task_done``);
    ``response_s`` 给保安的告警从出现到有人确认平均几秒;``ack_rate`` 给保安的告警有几成有人确认。
    系统可靠性 —— ``robot_availability`` / ``camera_availability`` 在册的机器人 / 摄像头
    不在掉线状态的时间占比
    (按掉线告警算);``intervention_rate`` 派出的巡逻里中途出过「要人帮忙」告警的占几成。
    没有可算的(没派过、没告警、没机器人)给 ``None``,不给 0(0% 可用率吓人又不对)。
    """
    w = (since_ms, until_ms)
    sent = db.query("SELECT task_id, robot_id, issued_at FROM commands WHERE kind='patrol' "
                    f"AND issued_at>=? AND issued_at<? AND {_PATROL_OK}", w)
    marks = ",".join("?" * len(_TERMINAL))
    done = helped = 0
    for r in sent:
        ev = db.query("SELECT kind, received_at FROM events WHERE robot_id=? "
                      f"AND kind IN ({marks}) "
                      "AND json_extract(data, '$.task_id')=? ORDER BY received_at LIMIT 1",
                      (r["robot_id"], *_TERMINAL, r["task_id"]))
        end = ev[0]["received_at"] if ev else r["issued_at"] + RUN_MAX_MS
        if ev and ev[0]["kind"] == "task_done":
            done += 1
        if any(audience(h["kind"]) == "help" for h in db.query(
                "SELECT kind FROM alerts WHERE robot=? AND first_ms>=? AND first_ms<=?",
                (r["robot_id"], r["issued_at"], end))):
            helped += 1
    guard = [r for r in db.query("SELECT kind, first_ms, acked_ms FROM alerts "
                                 "WHERE first_ms>=? AND first_ms<?", w)
             if audience(r["kind"]) in GUARD_AUDIENCES]
    acked = [r for r in guard if r["acked_ms"] is not None and r["acked_ms"] >= r["first_ms"]]
    span = until_ms - since_ms
    robots = [r["robot_id"] for r in db.query(
        "SELECT robot_id FROM robots WHERE revoked=0 AND issued_at<?", (until_ms,))]
    cams = [r["name"] for r in db.query("SELECT name FROM cameras WHERE added_ms<?", (until_ms,))]
    from d1max_site.cctv import alert_robot

    def avail(names: list[str], kinds: tuple[str, ...]) -> float | None:
        if not names or span <= 0:
            return None
        return 1 - _down_ms(db, names, kinds, since_ms, until_ms) / (len(names) * span)

    n = len(sent)
    return {
        "patrols_sent": n, "patrols_done": done,
        "patrol_completion": done / n if n else None,
        "response_s": (sum((r["acked_ms"] - r["first_ms"]) / 1000 for r in acked) / len(acked)
                       if acked else None),
        "ack_rate": len(acked) / len(guard) if guard else None,
        "robot_availability": avail(robots, ("robot_offline", "robot_offline_idle")),
        "camera_availability": avail([alert_robot(c) for c in cams], ("cctv_offline",)),
        "intervention_rate": helped / n if n else None,
    }
