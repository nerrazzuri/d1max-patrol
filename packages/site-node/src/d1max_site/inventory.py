"""站点清点(RC 加固 H5):换主机、恢复之前记一份「现在有什么」,恢复之后再记一份,两份一比。

灾备验收(``docs/真机验证.md`` §3u)原来靠人抄数、对数;这里让机器做:

- ``d1max-site inventory --out before.json``:老站点上跑(服务开着也行,**只读**,不改库、不升库结构);
- ``d1max-site inventory --compare before.json``:新主机恢复完跑,逐项说一样、少了、多了。

判定:

- **站点身份变了**(站点编号、服务证书指纹)= **不过**:狗和手机钉的是这张证书,变了就全连不上;
- 库结构比原来**老** = **不过**(用老程序开了新库);
- **不备份的东西**(连续录像)少了 = 意料之中,照实写;
- 别的少了 = **要人看**:最后一次备份之后才加的会少(最多一个小时),再多就是备份有毛病。清点不知道
  哪些是备份之后加的,所以不替人下结论,只把数摆出来;
- 多了、一样 = 过。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

KIND = "d1max-site-inventory"

#: 数哪些表(表名 → 给人看的名字)。顺序就是打印的顺序。
TABLES: dict[str, str] = {
    "robots": "登记的机器人", "accounts": "账号", "cameras": "固定摄像头",
    "incident_sources": "事件源", "zones": "防区", "zone_areas": "防区范围",
    "authorizations": "授权时段", "intercepts": "拦截点", "standby_points": "待命点",
    "homes": "原点", "chargers": "充电桩", "nav_zones": "禁行区",
    "maps": "地图", "bundles": "任务包", "routes": "巡检路线",
    "route_schedules": "排程", "releases": "软件版本",
    "alerts": "告警", "incidents": "入侵事件", "runs": "运行记录", "run_photos": "照片",
    "audit": "审计", "recordings": "连续录像",
}
#: 备份里没有的(``docs/备份恢复与灾备演练.md``「丢的东西」):恢复后少了是意料之中。
NOT_BACKED_UP = frozenset({"recordings"})


class InventoryError(RuntimeError):
    pass


def take(home: Path, *, fingerprint: str, site_id: str, now_ms: int) -> dict[str, Any]:
    """清点一个站点目录。库**只读**打开:老站点上服务开着也能跑,不会顺手把库升了。"""
    path = home / "site.db"
    if not path.is_file():
        raise InventoryError(f"没有站点库 {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        counts: dict[str, int | None] = {}
        for table in TABLES:
            try:
                counts[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                counts[table] = None                 # 老库没有这张表
        row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        schema = int(row[0]) if row and str(row[0]).isdigit() else 0
        newest: dict[str, int | None] = {}
        for name, sql in (("alert_ms", "SELECT MAX(last_ms) FROM alerts"),
                          ("audit_at", "SELECT MAX(at) FROM audit")):
            try:
                got = con.execute(sql).fetchone()[0]
                newest[name] = int(got) if got is not None else None
            except sqlite3.Error:
                newest[name] = None
    finally:
        con.close()
    return {"kind": KIND, "site_id": site_id, "fingerprint": fingerprint, "schema": schema,
            "taken_ms": now_ms, "counts": counts, "newest": newest}


def load(path: Path) -> dict[str, Any]:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InventoryError(f"读不了清点记录 {path}: {exc}") from exc
    if not isinstance(got, dict) or got.get("kind") != KIND \
            or not isinstance(got.get("counts"), dict):
        raise InventoryError(f"{path} 不是站点清点记录")
    return got


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """回 ``{"ok", "review", "lines"}``:``ok`` 假 = 不过;``review`` 真 = 有要人看的。"""
    lines: list[dict[str, Any]] = []

    def add(level: str, what: str, detail: str) -> None:
        lines.append({"level": level, "what": what, "detail": detail})

    for key, name in (("site_id", "站点编号"), ("fingerprint", "站点证书指纹")):
        if before.get(key) == after.get(key) and before.get(key):
            add("same", name, str(after.get(key)))
        else:
            add("fail", name, f"原来 {before.get(key)},现在 {after.get(key)}:"
                              "机器人和手机认的是原来那个,都会连不上")
    b_schema, a_schema = int(before.get("schema") or 0), int(after.get("schema") or 0)
    if a_schema < b_schema:
        add("fail", "库结构", f"原来 {b_schema},现在 {a_schema}:新主机上的程序比原来的老")
    else:
        add("same" if a_schema == b_schema else "more", "库结构", f"{b_schema} → {a_schema}")
    for table, name in TABLES.items():
        b, a = before["counts"].get(table), (after.get("counts") or {}).get(table)
        if b is None and a is None:
            continue
        if b is None or a is None:
            add("more" if b is None else "review", name,
                f"原来 {'没有这张表' if b is None else b},现在 {'没有这张表' if a is None else a}")
        elif a == b:
            add("same", name, str(a))
        elif a > b:
            add("more", name, f"{b} → {a}(多了 {a - b})")
        elif table in NOT_BACKED_UP:
            add("expected", name, f"{b} → {a}(少了 {b - a}):这一项不备份,恢复后没有是意料之中")
        else:
            add("review", name, f"{b} → {a}(少了 {b - a}):最后一次备份之后才加的会少,"
                                "再多就是备份有毛病,对一下是哪几条")
    return {"ok": not any(x["level"] == "fail" for x in lines),
            "review": any(x["level"] == "review" for x in lines), "lines": lines,
            "before_ms": before.get("taken_ms"), "after_ms": after.get("taken_ms")}


MARK = {"same": "一样", "more": "多了", "expected": "意料之中", "review": "要人看", "fail": "不过"}


def render(result: dict[str, Any]) -> str:
    out = [f"[{MARK[x['level']]}] {x['what']}: {x['detail']}" for x in result["lines"]]
    if not result["ok"]:
        out.append("结论:不过。")
    elif result["review"]:
        out.append("结论:站点身份没变;有少了的,对一下是不是都在最后一次备份之后。")
    else:
        out.append("结论:过。")
    return "\n".join(out)
