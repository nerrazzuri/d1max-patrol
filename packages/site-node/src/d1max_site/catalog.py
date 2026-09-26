"""站点上的任务与排程(W00c2a 设计决定四 A:导入现有任务包)。

任务包的格式与校验在契约包(``d1max_contract.bundle_format``):目录名、纯数据闸、整包指纹。
导入 = 校验 → 读 ``missions/*.json``(每份过 ``parse_mission``)与 ``schedule.yaml`` → 排程里
提到的任务都得在包里 → 一个事务落库,并成为**当前包**(站点同一时刻只按一个包排程)。
同一个 ``bundle_id`` 的版本号只许往上走。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_contract.bundle_format import (
    MISSIONS_DIR,
    SCHEDULE_NAME,
    BundleError,
    read_bundle_schedule,
    verify_bundle,
)
from d1max_contract.mission import Mission, MissionError, parse_mission
from d1max_contract.schedule import Schedule, parse_schedule
from d1max_site.db import SiteDB


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveBundle:
    bundle_id: str
    version: int
    imported_at: int
    schedule: Schedule
    missions: dict[str, Mission]


def import_bundle(db: SiteDB, bundle_dir: Path, *, imported_by: str,
                  now_ms: int) -> dict[str, Any]:
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
    if (bundle_dir / SCHEDULE_NAME).exists():
        try:
            schedule = read_bundle_schedule(bundle_dir)
        except BundleError as exc:
            raise CatalogError(str(exc)) from exc
    else:
        schedule = parse_schedule({"timezone": "UTC", "entries": []})
    missing = sorted({e.mission for e in schedule.entries} - set(missions))
    if missing:
        raise CatalogError(f"排程里提到的任务包里没有: {', '.join(missing)}")
    with db.tx() as c:
        row = c.execute("SELECT max(version) AS v FROM bundles WHERE bundle_id=?",
                        (m.bundle_id,)).fetchone()
        if row["v"] is not None and row["v"] >= m.version:
            raise CatalogError(f"{m.bundle_id} 已经有 v{row['v']},版本号只许往上走"
                               f"(这份是 v{m.version})")
        c.execute("UPDATE bundles SET active=0")
        c.execute("INSERT INTO bundles(bundle_id, version, content_sha256, timezone, schedule, "
                  "imported_at, imported_by, active, schema) VALUES (?,?,?,?,?,?,?,1,?)",
                  (m.bundle_id, m.version, m.content_sha256, schedule.timezone,
                   json.dumps(schedule.to_wire(), ensure_ascii=False), now_ms, imported_by,
                   m.schema))
        for mid, mission in missions.items():
            c.execute("INSERT INTO missions(bundle_id, version, mission_id, definition) "
                      "VALUES (?,?,?,?)", (m.bundle_id, m.version, mid,
                                           json.dumps(mission.to_wire(), ensure_ascii=False)))
    return {"bundle_id": m.bundle_id, "version": m.version, "missions": sorted(missions),
            "schedule_entries": [e.id for e in schedule.entries],
            "timezone": schedule.timezone}


def active_bundle_schema(db: SiteDB) -> tuple[str, int] | None:
    """当前任务包是哪个、schema 几(W00c6d 升级前检查);没有当前包是 ``None``。"""
    rows = db.query("SELECT bundle_id, version, schema FROM bundles WHERE active=1")
    if not rows:
        return None
    return f"{rows[0]['bundle_id']} v{rows[0]['version']}", int(rows[0]["schema"])


def active_bundle(db: SiteDB) -> ActiveBundle | None:
    rows = db.query("SELECT * FROM bundles WHERE active=1")
    if not rows:
        return None
    b = rows[0]
    missions = {r["mission_id"]: parse_mission(json.loads(r["definition"]))
                for r in db.query("SELECT mission_id, definition FROM missions "
                                  "WHERE bundle_id=? AND version=?",
                                  (b["bundle_id"], b["version"]))}
    return ActiveBundle(bundle_id=b["bundle_id"], version=b["version"],
                        imported_at=b["imported_at"],
                        schedule=parse_schedule(json.loads(b["schedule"])), missions=missions)
