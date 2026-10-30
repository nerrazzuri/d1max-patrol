"""机器人概览(W44 第二期,设计 §5.0「机器人」、§5.1「机器人」):业主看的卡片要的数,一次给全。

每台:名字、在线(在线且新鲜)、电量、今天巡了几趟(站点时区 0 点起派出去、没被拒的巡逻)、
毛病(这台没解决的「机器人需要帮忙 / 掉线」告警 + 设备不在线)、近 30 天的毛病、代理版本和兼容结论、
证书到期。
技术类告警(给业主那一类)不算毛病,在「家里的状况」里说。电池健康机器人还不报,不给。
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Any

from d1max_site.alert_audience import audience
from d1max_site.dispatcher import battery_pct
from d1max_site.sitetime import local_day_start

PROBLEM_AUDIENCES = ("help", "offline")
RECENT_DAYS = 30
RECENT_MAX = 50


def overview(dispatcher: Any, db: Any, *, now_ms: int, tz: tzinfo) -> list[dict[str, Any]]:
    from d1max_site.compat import _robot
    reg = dispatcher.registry
    names = reg.names()
    day0 = local_day_start(now_ms, tz)
    since = now_ms - RECENT_DAYS * 86_400_000
    out = []
    for rec in reg.list():
        rid = rec.robot_id
        c = dispatcher.clients.get(rid)
        fresh = bool(dispatcher._fresh(c))
        [n] = db.query("SELECT COUNT(*) AS n FROM commands WHERE robot_id=? AND kind='patrol' "
                       "AND issued_at>=? AND (ack_result IS NULL OR ack_result NOT IN "
                       "('rejected', 'expired', 'timeout'))", (rid, day0))
        rows = db.query("SELECT key, kind, first_ms, resolved_ms FROM alerts WHERE robot=? AND "
                        "(resolved_ms IS NULL OR first_ms>=?) ORDER BY first_ms DESC LIMIT 1000",
                        (rid, since))
        mine = [r for r in rows if audience(r["kind"]) in PROBLEM_AUDIENCES]
        problems: list[dict[str, Any]] = [{"key": r["key"], "kind": r["kind"],
                                           "first_ms": r["first_ms"]}
                                          for r in mine if r["resolved_ms"] is None]
        dv = dispatcher.devices_view(rid)
        if dv and not dv.get("stale"):
            problems += [{"device": x.get("id"), "state": x.get("state")}
                         for x in dv.get("items", []) if x.get("state") != "online"]
        recent = [{"key": r["key"], "kind": r["kind"], "first_ms": r["first_ms"],
                   "resolved_ms": r["resolved_ms"]}
                  for r in mine if r["first_ms"] >= since][:RECENT_MAX]
        caps = getattr(c, "capabilities", None) if c is not None else None
        out.append({"robot_id": rid, "name": names.get(rid, ""), "revoked": rec.revoked,
                    "expires_at": rec.expires_at, "online": fresh,
                    "battery_pct": battery_pct(c, fresh=fresh),
                    "manual_only": reg.manual_only(rid) is not None,
                    "patrols_today": n["n"], "problems": problems, "recent_problems": recent,
                    "agent": getattr(caps, "agent", None),
                    "compat": _robot(rid, c)["verdict"] if c is not None else "unknown"})
    return out
