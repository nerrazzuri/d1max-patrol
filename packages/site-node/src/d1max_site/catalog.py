"""站点上的任务与排程(W00c2a 设计决定四 A:导入现有任务包)。

**2026-10-14 起站点的路线库(``routes.py``)是唯一真理源**:任务包导入落进路线库(按名字替换,摘要见
:func:`import_bundle`),派遣器、排程执行器读的 :func:`active_bundle`
也读路线库;``bundles``/``missions`` 两张表只留导入记录(版本号只许往上走、升级前检查要的
schema)。以下是导入那一头原来的规矩,照旧:

任务包的格式与校验在契约包(``d1max_contract.bundle_format``):目录名、纯数据闸、整包指纹。
导入 = 校验 → 读 ``missions/*.json``(每份过 ``parse_mission``)与 ``schedule.yaml`` → 排程里
提到的任务都得在包里或库里 → 一个事务并进路线库。
同一个 ``bundle_id`` 的版本号只许往上走。

**每个任务都得绑地图版本**(R1,``map_version``):没绑的老包不导入,报错列出站点上这张图有哪几版。
站点升级之前就导入了的老包照样读得出来(没绑的任务 ``map_version`` 是空串),派遣器、排程都不派它。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract.bundle_format import (
    BUNDLE_SCHEMA,
    MISSIONS_DIR,
    SCHEDULE_NAME,
    BundleError,
    read_bundle_schedule,
    unbound_mission_text,
    verify_bundle,
)
from d1max_contract.mission import Mission, MissionError, parse_mission
from d1max_contract.schedule import Schedule, parse_schedule
from d1max_site.db import SiteDB


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveBundle:
    """派遣器、排程执行器眼里的「现在的任务与排程」= 路线库此刻的样子。``bundle_id``/``version`` 是
    给视图用的名字(库的修订号);``active_from``:每条排程从什么时候起算数(毫秒,见 ``routes.py``)。"""
    bundle_id: str
    version: int
    schedule: Schedule
    missions: dict[str, Mission]
    active_from: dict[str, int]

    def since(self, entry_id: str) -> int:
        return self.active_from.get(entry_id, 0)


def import_bundle(db: SiteDB, bundle_dir: Path, *, imported_by: str,
                  now_ms: int) -> dict[str, Any]:
    """任务包 → 路线库(按名字替换;摘要见 ``routes.import_into_store``)。成功回
    ``{"bundle_id","version","missions","schedule_entries","timezone", "routes":{added,replaced,
    unchanged},"schedules":{…},"kept_routes","kept_schedules","timezone_change","changed"}``。"""
    from d1max_site.routes import import_into_store
    bundle_dir = Path(bundle_dir)
    try:
        m = verify_bundle(bundle_dir)
    except (BundleError, OSError) as exc:          # OSError:读不了(权限等),也是 409 不是 500
        raise CatalogError(f"任务包校验没过: {exc}") from exc
    missions: dict[str, Mission] = {}
    mdir = bundle_dir / MISSIONS_DIR
    for p in sorted(mdir.glob("*.json")) if mdir.is_dir() else ():
        try:
            missions[p.stem] = parse_mission(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, MissionError) as exc:
            raise CatalogError(f"任务 {p.name} 不合规: {exc}") from exc
    unbound = [unbound_mission_text(f"{mid}.json", m.map_id, _map_versions(db, m.map_id))
               for mid, m in missions.items() if not m.map_version]
    if unbound:
        # R1:老包(schema 1)里的任务没绑地图版本。不导入 —— 也**不**替人填狗现在用的那一版:哪一版
        # 是这些点量的,只有画点的人知道。报错列出站点上这张图有哪几版,给人核
        raise CatalogError(";".join(unbound))
    schedule: Schedule | None = None
    if (bundle_dir / SCHEDULE_NAME).exists():
        try:
            schedule = read_bundle_schedule(bundle_dir)
        except BundleError as exc:
            raise CatalogError(str(exc)) from exc
    sched_wire = schedule.to_wire() if schedule is not None else {"timezone": "UTC", "entries": []}
    with db.tx() as c:
        row = c.execute("SELECT max(version) AS v FROM bundles WHERE bundle_id=?",
                        (m.bundle_id,)).fetchone()
        if row["v"] is not None and row["v"] >= m.version:
            raise CatalogError(f"{m.bundle_id} 已经有 v{row['v']},版本号只许往上走"
                               f"(这份是 v{m.version})")
        rep = import_into_store(c, missions, schedule, by=imported_by, now=now_ms,
                                source=f"bundle {m.bundle_id} v{m.version}",
                                bundle=(m.bundle_id, m.version))
        # 导入记录(版本号只许往上走;升级前检查看最近一份导入的 schema)
        c.execute("UPDATE bundles SET active=0")
        c.execute("INSERT INTO bundles(bundle_id, version, content_sha256, timezone, schedule, "
                  "imported_at, imported_by, active, schema) VALUES (?,?,?,?,?,?,?,1,?)",
                  (m.bundle_id, m.version, m.content_sha256,
                   schedule.timezone if schedule is not None else "UTC",
                   json.dumps(sched_wire, ensure_ascii=False), now_ms, imported_by, m.schema))
        for mid, mission in missions.items():
            c.execute("INSERT INTO missions(bundle_id, version, mission_id, definition) "
                      "VALUES (?,?,?,?)", (m.bundle_id, m.version, mid,
                                           json.dumps(mission.to_wire(), ensure_ascii=False)))
    return {"bundle_id": m.bundle_id, "version": m.version, "missions": sorted(missions),
            "schedule_entries": [e.id for e in schedule.entries] if schedule is not None else [],
            "timezone": schedule.timezone if schedule is not None else None,
            "routes": rep["routes"], "schedules": rep["schedules"],
            "kept_routes": rep["kept_routes"], "kept_schedules": rep["kept_schedules"],
            "timezone_change": rep["timezone"], "changed": rep["changed"]}


def import_summary(got: dict[str, Any]) -> str:
    """导入结果给人看的一段话(命令行、接口都用)。"""
    def part(label: str, d: dict[str, list[str]]) -> str:
        bits = [f"{k} {', '.join(v)}" for k, v in
                (("新加", d["added"]), ("替换", d["replaced"]), ("没变", d["unchanged"])) if v]
        return f"{label} " + (";".join(bits) if bits else "无")
    out = [f"导入了 {got['bundle_id']} v{got['version']}", part("路线", got["routes"]),
           part("排程", got["schedules"])]
    if got["kept_routes"]:
        out.append("站点上原有、包里没有而留着的路线 " + ", ".join(got["kept_routes"]))
    if got["kept_schedules"]:
        out.append("站点上原有、包里没有而留着的排程 " + ", ".join(got["kept_schedules"]))
    if got["timezone_change"]:
        tc = got["timezone_change"]
        out.append(f"排程时区 {tc['from'] or '未设'} → {tc['to']}")
    return ";".join(out)


def _map_versions(db: SiteDB, map_id: str) -> list[str]:
    """站点地图目录里这张图有哪几版,新的在前(只用来写报错)。"""
    return [r["version"] for r in db.query(
        "SELECT version FROM maps WHERE map_id=? ORDER BY created_ms DESC", (map_id,))]


def active_bundle_schema(db: SiteDB) -> tuple[str, int] | None:
    """当前任务的 schema 几(W00c6d 升级前检查):最近一份导入的任务包的;没导入过包、但路线库里有路线
    (全是编辑器存的,每条都绑了地图版本)按当前格式算;都没有是 ``None``。"""
    rows = db.query("SELECT bundle_id, version, schema FROM bundles WHERE active=1")
    if rows:
        return f"{rows[0]['bundle_id']} v{rows[0]['version']}", int(rows[0]["schema"])
    if db.query("SELECT 1 FROM routes LIMIT 1"):
        return "站点路线库", BUNDLE_SCHEMA
    return None


def active_bundle(db: SiteDB) -> ActiveBundle | None:
    """路线库此刻的任务与排程;库里什么都没有是 ``None``。"""
    routes = db.query("SELECT route_id, definition FROM routes")
    entries = db.query("SELECT entry_id, definition, active_from_ms FROM route_schedules "
                       "ORDER BY entry_id")
    s = db.query("SELECT revision, timezone FROM route_store WHERE id=1")
    if not routes and not entries:
        return None
    tz = (s[0]["timezone"] if s else None) or "UTC"
    return ActiveBundle(
        bundle_id="site-routes", version=int(s[0]["revision"]) if s else 0,
        schedule=parse_schedule({"timezone": tz,
                                 "entries": [json.loads(e["definition"]) for e in entries]}),
        missions={r["route_id"]: parse_mission(json.loads(r["definition"])) for r in routes},
        active_from={e["entry_id"]: int(e["active_from_ms"]) for e in entries})
