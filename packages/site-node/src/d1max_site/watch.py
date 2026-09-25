"""值守汇总(W00c5a;原先是狗上老服务的 ``/api/watch/summary``,§5.1):**把静默失败摆到玻璃上。**

每台狗一行:在线没有、最后一次见到、电量与取值时刻、站点量的钟偏、未解决告警按级别计数;
再加站点自己一行(排程这一拍办成了没有、站点自己的备份)。

**缺的事实一律 ``None``,不写 0。** ``0`` 是「查过了,没有」,``None`` 是「不知道」—— 两句话在
屏上长得一样,在现场差着一次事故。每个 ``None`` 在 ``why`` 里附一句人话。

狗的存储(W00c5d,决策 8):盘水位、证据积压(文件数、最老一条等了多久)来自狗每 10 s 随遥测带的
盘况;还没收到过就是「不知道」。任务包落差在代理模式下没有意义(任务随命令下发,狗上没有任务包);
备份在站点做,看站点那一行。

**只读。**
"""

from __future__ import annotations

from typing import Any

from d1max_site.alert_sources import SITE
from d1max_site.alert_store import AlertDesk
from d1max_site.dispatcher import Dispatcher

#: 狗上存储那几项为什么是 ``None``。印给值班的人看,不许有排期黑话。
NO_DOG_STORAGE = "狗上的存储情况还没接到站点,这一档是「不知道」,不是「没问题」"
NO_BATTERY = "还没收到过电量遥测 —— 这一档是「不知道」,不是「电量为 0」"
NO_SKEW = "还没收到过遥测,量不了钟偏 —— 这一档是「不知道」,不是「钟是准的」"
NO_SCHEDULER = "这个站点没开排程"
NO_BUNDLE = "任务随命令下发,狗上没有任务包,没有落差可言"
BACKUP_AT_SITE = "备份在站点做(决策 8:数据都在站点),看站点那一行"
NO_BACKUP = "站点没配备份目录:站点的库和证据只有这一份"


def watch_summary(dispatcher: Dispatcher, desk: AlertDesk, *, now_ms: int,
                  scheduler: Any = None, backup: Any = None) -> dict[str, Any]:
    counts = desk.open_counts()
    zero = {"P1": 0, "P2": 0, "P3": 0}
    robots = []
    for rid in sorted(dispatcher.clients):
        c = dispatcher.clients[rid]
        why: dict[str, str] = {"bundle_lag": NO_BUNDLE, "backup": BACKUP_AT_SITE}
        st = dispatcher.storage.get(rid)
        if st is None:
            for k in ("disk_used_ratio", "upload_backlog", "oldest_backlog_s"):
                why[k] = NO_DOG_STORAGE
        t = c.telemetry
        t_at = dispatcher.telemetry_at.get(rid)
        battery = skew = None
        if t is None or t_at is None:
            why["battery_pct"] = NO_BATTERY
            why["clock_skew_s"] = NO_SKEW
        else:
            battery = t.battery_pct
            skew = round((t.stamp - t_at) / 1000.0, 1)
        robots.append({
            "robot_id": rid,
            "online": bool(c.status and c.status.online),
            "fresh": not dispatcher.is_stale(rid) and c.status_live_at is not None,
            "last_seen_ms": c.status_live_at,
            "battery_pct": battery,
            "battery_as_of_ms": t_at if battery is not None else None,
            "clock_skew_s": skew,
            "alerts": dict(counts.get(rid, zero)),
            "disk_used_ratio": st[0].disk_used_ratio if st else None,
            "upload_backlog": st[0].backlog_files if st else None,
            "oldest_backlog_s": st[0].oldest_backlog_s if st else None,
            "storage_as_of_ms": st[1] if st else None,
            "bundle_lag": None,
            "backup": None,
            "why": why,
        })
    if scheduler is None:
        site = {"schedule_ok": None, "schedule_error": "", "why": {"schedule_ok": NO_SCHEDULER}}
    else:
        err = getattr(scheduler, "last_error", "") or ""
        site = {"schedule_ok": not err, "schedule_error": err, "why": {}}
    site["alerts"] = dict(counts.get(SITE, zero))
    if backup is None:
        site["backup"] = None
        site["why"]["backup"] = NO_BACKUP
    else:
        site["backup"] = backup.status()
        if not site["backup"]["configured"]:
            site["why"]["backup"] = NO_BACKUP
    return {"now_ms": now_ms, "robots": robots, "site": site}
