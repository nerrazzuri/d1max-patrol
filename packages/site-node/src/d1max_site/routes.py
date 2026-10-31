"""巡检路线库(2026-10-14,用户定:**站点存的路线、排程是唯一真理源**;网页、手机都能改)。

一条路线 = 一份任务定义(契约 ``d1max_contract.mission`` 的线格式):名字、地图号 + **地图版本**
(必填,R1)、有序的点(名字、x、y、朝向 + 已有的动作 dwell/photo/light + ``check`` 文字)、策略。
一条排程 = 契约 ``schedule`` 的一条 entry(语义不变)。时区整个库一个。

接口里的点是**平的**:``{"name", "x", "y", "yaw", "check"?, "actions"?}``(库里和任务包里存的是契约的
四元数线格式,进出这里转一次)。

- **改**:整条换,带 ``base_revision``(改之前看到的这一条的修订号;新建是 0),对不上抛
  :class:`RouteConflict`(两个人同时改,后到的不许悄悄盖掉先到的 —— 同禁行区)。
- **存之前逐点校验**:点在这一版图的可通行格子上、离建图走过的路不超过 ``COVERAGE_RADIUS_M``
  (复用 ``maps.reach_problem``)、地图版本站点里有、不超过派遣器的点数与字节上限。错的点逐个报
  (``RouteError.errors``:``[{"index", "name", "problem"}]``),**不是第一个错了就停**。
- **删路线**:还有排程在用的不许删(不悄悄丢排程)。
- **排程每条自己记 ``active_from_ms``**(新加、改了的那一刻):那之前到点的轮次不跑、不补记
  (原来按「任务包导入的那一刻」整包算,同一个意思)。
- **任务包**:导进来落进同一个库(:func:`import_into_store`,按名字替换,摘要说清楚加了什么、换了什么、
  留着什么),也能把库导成任务包(:meth:`RouteStore.export_bundle`,``import-bundle`` 收得下)。
  派遣器、排程执行器读的是 :func:`d1max_site.catalog.active_bundle`,它读这里的表。
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from d1max_contract.bundle_format import MISSIONS_DIR, SCHEDULE_NAME, BundleError, build_bundle
from d1max_contract.geometry import Pose
from d1max_contract.mission import Mission, MissionError, MissionWaypoint, parse_mission
from d1max_contract.schedule import Schedule, ScheduleError, parse_schedule

#: 路线名、排程 id:ASCII 字母数字 . _ -(进 URL、照片文件名、任务包文件名)。
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 同派遣器的上限(``dispatcher.MAX_PATROL_*``;这里复制一份是为了不倒着依赖派遣器,有测试对着)。
MAX_WAYPOINTS = 500
MAX_BYTES = 200_000
#: 逐段查走不走得通最多查这么多段(每段一次规划;再多的不查,派单时狗自己会报)。
MAX_SEGMENT_CHECKS = 100
#: 编辑器能写的键;别的(``route`` 的 vendor_path 之类)不收。
_EDITABLE = frozenset({"map_id", "map_version", "waypoints", "policy", "base_revision"})
EXPORT_BUNDLE_ID = "site-routes"


class RouteError(Exception):
    """存不下:``errors`` 是逐点的问题。"""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class RouteConflict(RouteError):
    pass


class RouteNotFound(RouteError):
    pass


def _json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


def flat_waypoint(w: MissionWaypoint) -> dict[str, Any]:
    out: dict[str, Any] = {"name": w.name, "x": w.pose.position.x, "y": w.pose.position.y,
                           "yaw": w.pose.yaw}
    if w.check:
        out["check"] = w.check
    if w.actions:
        out["actions"] = [a.to_wire() for a in w.actions]
    return out


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _wire_waypoints(raw: Any) -> list[dict[str, Any]]:
    """接口里平的点 → 契约线格式。数不对的逐点报。"""
    if not isinstance(raw, list) or not raw:
        raise RouteError("waypoints 要是非空列表")
    out, errs = [], []
    for i, w in enumerate(raw):
        name = w.get("name", "") if isinstance(w, dict) else ""
        if not isinstance(w, dict) or not all(_num(w.get(k)) for k in ("x", "y")) \
                or not _num(w.get("yaw", 0.0)):
            errs.append({"index": i, "name": name if isinstance(name, str) else "",
                         "problem": "点要有数字的 x、y(和可选的 yaw)"})
            continue
        item: dict[str, Any] = {"name": name,
                                "pose": Pose.from_xy_yaw(float(w["x"]), float(w["y"]),
                                                         float(w.get("yaw", 0.0))).to_wire()}
        for k in ("check", "actions"):
            if k in w:
                item[k] = w[k]
        out.append(item)
    if errs:
        raise RouteError(f"{len(errs)} 个点不成形", errs)
    return out


class RouteStore:
    def __init__(self, db: Any, *, maps: Any = None, now_ms: Callable[[], int]) -> None:
        self.db = db
        self.maps = maps
        self._now = now_ms
        #: W44 第三期:规划(``intercept_reach.InterceptReach``),存之前逐段查相邻两点走不走得通;
        #: 没接(测试台子、没地图目录)不查。
        self.reach: Any = None

    # ------------------------------------------------------------ 读

    def _store_row(self) -> Any:
        rows = self.db.query("SELECT * FROM route_store WHERE id=1")
        return rows[0] if rows else None

    @staticmethod
    def _route_view(r: Any) -> dict[str, Any]:
        m = parse_mission(json.loads(r["definition"]))
        return {"id": r["route_id"], "map_id": r["map_id"], "map_version": r["map_version"],
                "revision": r["revision"], "updated_ms": r["updated_ms"],
                "updated_by": r["updated_by"], "source": r["source"],
                "bound": bool(r["map_version"]),
                "waypoints": [flat_waypoint(w) for w in m.waypoints],
                "policy": m.policy.to_wire()}

    @staticmethod
    def _schedule_view(r: Any) -> dict[str, Any]:
        return {**json.loads(r["definition"]), "revision": r["revision"],
                "active_from_ms": r["active_from_ms"], "updated_ms": r["updated_ms"],
                "updated_by": r["updated_by"], "source": r["source"]}

    def view(self, map_id: str | None = None, map_version: str | None = None) -> dict[str, Any]:
        """整个库(可按地图号、版本筛路线)。排程不筛:它们按路线名跟着。"""
        where, args = [], []
        if map_id:
            where.append("map_id=?")
            args.append(map_id)
        if map_version:
            where.append("map_version=?")
            args.append(map_version)
        sql = "SELECT * FROM routes" + (" WHERE " + " AND ".join(where) if where else "") \
            + " ORDER BY route_id"
        s = self._store_row()
        last = s["last_import"] if s is not None and s["last_import"] else ""
        return {
            "revision": s["revision"] if s is not None else 0,
            "timezone": s["timezone"] if s is not None else None,
            "updated_ms": s["updated_ms"] if s is not None else 0,
            "updated_by": s["updated_by"] if s is not None else "",
            "last_import": json.loads(last) if last else None,
            "routes": [self._route_view(r) for r in self.db.query(sql, tuple(args))],
            "schedules": [self._schedule_view(r) for r in self.db.query(
                "SELECT * FROM route_schedules ORDER BY entry_id")],
        }

    def get(self, route_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM routes WHERE route_id=?", (route_id,))
        if not rows:
            raise RouteNotFound(f"没有路线 {route_id!r}")
        out = self._route_view(rows[0])
        m = parse_mission(json.loads(rows[0]["definition"]))
        out["schedules"] = [self._schedule_view(r) for r in self.db.query(
            "SELECT * FROM route_schedules ORDER BY entry_id")
            if json.loads(r["definition"]).get("mission") == route_id]
        out["problems"] = self.problems(m.map_id, m.map_version, m.waypoints)
        return out

    # ------------------------------------------------------------ 校验

    def problems(self, map_id: str, version: str, waypoints: Any) -> list[dict[str, Any]]:
        """逐点校验:每个点的问题都列出来(``index``:第几个,从 0 数;整条的问题 ``index`` 是 None)。"""
        if not version:
            return [{"index": None, "name": "", "problem":
                     "未绑地图版本:核过这些点是在哪一版图上量的再选版本(R1)"}]
        if self.maps is None:
            return []
        from d1max_site.maps import MapError
        try:
            self.maps.get(map_id, version)
        except Exception as exc:        # noqa: BLE001 —— 名字不合规矩也是「没有这一版」
            return [{"index": None, "name": "",
                     "problem": f"站点没有地图 {map_id}:{version}({exc})"}]
        errors: list[dict[str, Any]] = []
        for i, w in enumerate(waypoints):
            try:
                why = self.maps.reach_problem(
                    map_id, version, [(w.name, w.pose.position.x, w.pose.position.y)])
            except MapError as exc:
                why = str(exc)
            if why:
                errors.append({"index": i, "name": w.name, "problem": why})
        errors += self._segments(map_id, version, waypoints, {e["index"] for e in errors})
        return errors

    def _segments(self, map_id: str, version: str, waypoints: Any,
                  bad: set[int]) -> list[dict[str, Any]]:
        """相邻两点之间走不走得通(被墙、禁行区隔开):``{"index": None, "segment": [i, i+1]}``。
        点本身有错的那一段不再查(一个错不报两遍);最多查 :data:`MAX_SEGMENT_CHECKS` 段。"""
        if self.reach is None or len(waypoints) < 2:
            return []
        out: list[dict[str, Any]] = []
        for i in range(min(len(waypoints) - 1, MAX_SEGMENT_CHECKS)):
            if i in bad or i + 1 in bad:
                continue
            a, b = waypoints[i], waypoints[i + 1]
            ok = self.reach.reachable(map_id, version, (a.pose.position.x, a.pose.position.y),
                                      (b.pose.position.x, b.pose.position.y))
            if ok is False:
                out.append({"index": None, "name": f"{a.name}→{b.name}", "segment": [i, i + 1],
                            "problem": f"{a.name} 到 {b.name} 走不通(被墙或禁行区隔开)"})
        return out

    def _validate_route(self, route_id: str, body: dict[str, Any]) -> Mission:
        return self.build_mission(route_id, body)

    def build_mission(self, route_id: str, body: dict[str, Any]) -> Mission:
        """按存路线的同一套规矩把接口里的一条路线变成任务定义(W44:保安的临时路线也走这里,不存)。"""
        if not NAME_RE.match(route_id):
            raise RouteError(f"路线名只许 ASCII 字母、数字、. _ -(1–64 个字符):{route_id!r}")
        unknown = sorted(set(body) - _EDITABLE)
        if unknown:
            raise RouteError(f"不认识的字段:{', '.join(unknown)}")
        version = body.get("map_version")
        if not isinstance(version, str) or not version:
            raise RouteError("map_version 必填(字符串):这些点是在此地图的哪一版上量的")
        raw: dict[str, Any] = {"mission": route_id, "map_id": body.get("map_id"),
                               "map_version": version,
                               "waypoints": _wire_waypoints(body.get("waypoints"))}
        if body.get("policy") is not None:
            raw["policy"] = body["policy"]
        try:
            m = parse_mission(raw)
        except (MissionError, ValueError) as exc:
            raise RouteError(str(exc)) from exc
        if len(m.waypoints) > MAX_WAYPOINTS:
            raise RouteError(f"一条路线最多 {MAX_WAYPOINTS} 个点,此条 {len(m.waypoints)} 个")
        if len(_json(m.to_wire()).encode()) > MAX_BYTES:
            raise RouteError(f"路线定义超过 {MAX_BYTES} 字节")
        errs = self.problems(m.map_id, m.map_version, m.waypoints)
        if errs:
            raise RouteError(f"{len(errs)} 处不能用", errs)
        return m

    # ------------------------------------------------------------ 写

    @staticmethod
    def _touch(tx: Any, by: str, now: int) -> int:
        tx.execute("INSERT OR IGNORE INTO route_store(id, revision, updated_ms, updated_by) "
                   "VALUES (1, 0, 0, '')")
        tx.execute("UPDATE route_store SET revision=revision+1, updated_ms=?, updated_by=? "
                   "WHERE id=1", (now, by))
        return int(tx.execute("SELECT revision FROM route_store WHERE id=1").fetchone()[0])

    @staticmethod
    def _base(base_revision: Any) -> int:
        if isinstance(base_revision, bool) or not isinstance(base_revision, int) \
                or base_revision < 0:
            raise RouteError("base_revision 要是整数(改之前看到的修订号;新建是 0)")
        return base_revision

    def put_route(self, route_id: str, body: dict[str, Any], *, by: str) -> dict[str, Any]:
        base = self._base(body.get("base_revision"))
        m = self._validate_route(route_id, body)
        definition = _json(m.to_wire())
        now = self._now()
        with self.db.tx() as tx:
            row = tx.execute("SELECT revision FROM routes WHERE route_id=?",
                             (route_id,)).fetchone()
            cur = row["revision"] if row is not None else 0
            if base != cur:
                raise RouteConflict(
                    f"路线 {route_id} 已被修改(现在是第 {cur} 版,你看到的是第 {base} 版)"
                    if cur else f"路线 {route_id} 已被删除:请刷新后再改")
            if row is None:
                tx.execute("INSERT INTO routes(route_id, map_id, map_version, definition, "
                           "revision, updated_ms, updated_by, source) VALUES (?,?,?,?,1,?,?,?)",
                           (route_id, m.map_id, m.map_version, definition, now, by, "editor"))
            else:
                tx.execute("UPDATE routes SET map_id=?, map_version=?, definition=?, "
                           "revision=revision+1, updated_ms=?, updated_by=?, source='editor' "
                           "WHERE route_id=?",
                           (m.map_id, m.map_version, definition, now, by, route_id))
            self._touch(tx, by, now)
        return self.get(route_id)

    def delete_route(self, route_id: str, *, base_revision: Any, by: str) -> None:
        base = self._base(base_revision)
        now = self._now()
        with self.db.tx() as tx:
            row = tx.execute("SELECT revision FROM routes WHERE route_id=?",
                             (route_id,)).fetchone()
            if row is None:
                raise RouteNotFound(f"没有路线 {route_id!r}")
            if row["revision"] != base:
                raise RouteConflict(f"路线 {route_id} 已被修改(现在是第 {row['revision']} 版,"
                                    f"你看到的是第 {base} 版):请刷新后再删")
            users = [r["entry_id"] for r in tx.execute(
                "SELECT entry_id, definition FROM route_schedules").fetchall()
                if json.loads(r["definition"]).get("mission") == route_id]
            if users:
                raise RouteError(f"排程 {', '.join(sorted(users))} 还在用路线 {route_id}:"
                                 "先改排程或删排程")
            tx.execute("DELETE FROM routes WHERE route_id=?", (route_id,))
            self._touch(tx, by, now)

    # ---- 排程

    def set_timezone(self, tz: Any, *, by: str) -> dict[str, Any]:
        if not isinstance(tz, str) or not tz.strip():
            raise RouteError("timezone 必填(IANA 时区名,如 Asia/Kuala_Lumpur)")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RouteError(f"timezone 无法识别:{tz!r}") from exc
        now = self._now()
        with self.db.tx() as tx:
            self._touch(tx, by, now)
            cur = tx.execute("SELECT timezone FROM route_store WHERE id=1").fetchone()[0]
            tx.execute("UPDATE route_store SET timezone=? WHERE id=1", (tz,))
            if cur != tz:
                # 同一个钟点在别的时区是另一个时刻:每条排程从现在起算数,之前到点的不补跑
                tx.execute("UPDATE route_schedules SET active_from_ms=?", (now,))
        return self.view()

    def put_schedule(self, entry_id: str, body: dict[str, Any], *, by: str) -> dict[str, Any]:
        base = self._base(body.get("base_revision"))
        if not NAME_RE.match(entry_id):
            raise RouteError(f"排程 id 只许 ASCII 字母、数字、. _ -(1–64 个字符):{entry_id!r}")
        raw = {k: v for k, v in body.items() if k not in ("base_revision", "revision", "id")}
        s = self._store_row()
        tz = s["timezone"] if s is not None else None
        if not tz:
            raise RouteError("先设排程的时区(timezone):不读系统时区")
        try:
            sch = parse_schedule({"timezone": tz, "entries": [{"id": entry_id, **raw}]})
        except ScheduleError as exc:
            raise RouteError(str(exc)) from exc
        entry = sch.entries[0]
        definition = _json(entry.to_wire())
        now = self._now()
        with self.db.tx() as tx:
            if tx.execute("SELECT 1 FROM routes WHERE route_id=?",
                          (entry.mission,)).fetchone() is None:
                raise RouteError(f"排程要跑的路线 {entry.mission!r} 不存在")
            row = tx.execute("SELECT * FROM route_schedules WHERE entry_id=?",
                             (entry_id,)).fetchone()
            cur = row["revision"] if row is not None else 0
            if base != cur:
                raise RouteConflict(f"排程 {entry_id} 已被修改(现在是第 {cur} 版,"
                                    f"你看到的是第 {base} 版)" if cur else
                                    f"排程 {entry_id} 已被删除:请刷新后再改")
            if row is None:
                tx.execute("INSERT INTO route_schedules(entry_id, definition, revision, "
                           "active_from_ms, updated_ms, updated_by, source) "
                           "VALUES (?,?,1,?,?,?,'editor')", (entry_id, definition, now, now, by))
                self._touch(tx, by, now)
            elif _json(json.loads(row["definition"])) != definition:
                # 改了:从现在起算(改钟点不该把今天早些时候就过了的那一轮补跑出来)
                tx.execute("UPDATE route_schedules SET definition=?, revision=revision+1, "
                           "active_from_ms=?, updated_ms=?, updated_by=?, source='editor' "
                           "WHERE entry_id=?", (definition, now, now, by, entry_id))
                self._touch(tx, by, now)
        return self.view()

    def delete_schedule(self, entry_id: str, *, base_revision: Any, by: str) -> None:
        base = self._base(base_revision)
        now = self._now()
        with self.db.tx() as tx:
            row = tx.execute("SELECT revision FROM route_schedules WHERE entry_id=?",
                             (entry_id,)).fetchone()
            if row is None:
                raise RouteNotFound(f"没有排程 {entry_id!r}")
            if row["revision"] != base:
                raise RouteConflict(f"排程 {entry_id} 已被修改(现在是第 {row['revision']} 版,"
                                    f"你看到的是第 {base} 版):请刷新后再删")
            tx.execute("DELETE FROM route_schedules WHERE entry_id=?", (entry_id,))
            self._touch(tx, by, now)

    # ------------------------------------------------------------ 导出成任务包

    def export_bundle(self, dest_root: Path, *, by: str, bundle_id: str = EXPORT_BUNDLE_ID,
                      version: int | None = None) -> dict[str, Any]:
        """把库打成一个 ``import-bundle`` 收得下的任务包(备份、搬到别的站点)。版本号默认取
        「库修订号、这个包号已导入过的最大版本号」里大的再加一:包版本只许往上走。"""
        v = self.view()
        unbound = [r["id"] for r in v["routes"] if not r["bound"]]
        if unbound:
            raise RouteError(f"路线 {', '.join(unbound)} 未绑地图版本,无法打包:先在路线页里选版本、"
                             "核过点再存")
        if not v["routes"] and not v["schedules"]:
            raise RouteError("库里没有路线,没有什么可导出的")
        if version is None:
            got = self.db.query("SELECT max(version) AS v FROM bundles WHERE bundle_id=?",
                                (bundle_id,))
            version = max(int(v["revision"]), int(got[0]["v"] or 0) if got else 0) + 1
        tmp = Path(tempfile.mkdtemp(prefix="d1max-route-export-"))
        try:
            mdir = tmp / MISSIONS_DIR
            mdir.mkdir()
            for r in self.db.query("SELECT route_id, definition FROM routes"):
                (mdir / f"{r['route_id']}.json").write_text(
                    json.dumps(json.loads(r["definition"]), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            if v["timezone"]:
                sch = {"timezone": v["timezone"], "entries": [
                    json.loads(r["definition"]) for r in self.db.query(
                        "SELECT definition FROM route_schedules ORDER BY entry_id")]}
                parse_schedule(sch)                          # 导出的东西自己得合规
                (tmp / SCHEDULE_NAME).write_text(
                    yaml.safe_dump(sch, allow_unicode=True, sort_keys=False), encoding="utf-8")
            built_at = datetime.fromtimestamp(self._now() / 1000, tz=timezone.utc).isoformat()
            try:
                out = build_bundle(tmp, dest_root, bundle_id=bundle_id, version=version,
                                   built_at=built_at, built_by=by)
            except (BundleError, ScheduleError) as exc:
                raise RouteError(f"无法打包:{exc}") from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return {"path": str(out), "bundle_id": bundle_id, "version": version,
                "routes": [r["id"] for r in v["routes"]],
                "schedules": [e["id"] for e in v["schedules"]], "timezone": v["timezone"]}


# ---------------------------------------------------------------- 任务包导进库


def import_into_store(tx: Any, missions: dict[str, Mission], schedule: Schedule | None, *,
                      by: str, now: int, source: str,
                      bundle: tuple[str, int] | None = None) -> dict[str, Any]:
    """在调用方的事务里,把任务包的任务、排程按名字并进库。

    - 同名路线:内容一样 → ``unchanged``(不动修订号);不一样 → ``replaced``(修订 +1)。库里有、包里
      没有的路线**留着**(``kept_routes``)。
    - 排程按 id 同理;库里有、包里没有的排程**留着**(``kept_schedules``),**绝不悄悄丢**。
    - 包有排程文件时它的时区算数;跟库里的不同、而库里有留着的排程时拒收(它们的钟点会悄悄换了意思)。
      包没带排程文件 = 排程、时区都不动。
    - 排程提到的路线,包里或库里有一个就行。
    """
    from d1max_site.catalog import CatalogError
    stored_routes = {r["route_id"]: r for r in tx.execute("SELECT * FROM routes").fetchall()}
    stored_sched = {r["entry_id"]: r for r in tx.execute("SELECT * FROM route_schedules"
                                                         ).fetchall()}
    srow = tx.execute("SELECT * FROM route_store WHERE id=1").fetchone()
    cur_tz = srow["timezone"] if srow is not None else None
    for rid in missions:
        if not NAME_RE.match(rid):
            raise CatalogError(f"任务文件名 {rid!r} 不合规矩(只许 ASCII 字母、数字、. _ -,"
                               "1–64 个字符):改名再打包")
    entries = list(schedule.entries) if schedule is not None else []
    for e in entries:
        if not NAME_RE.match(e.id):
            raise CatalogError(f"排程 id {e.id!r} 不合规矩(只许 ASCII 字母、数字、. _ -,"
                               "1–64 个字符)")
    missing = sorted({e.mission for e in entries} - set(missions) - set(stored_routes))
    if missing:
        raise CatalogError(f"排程里提到的任务包里没有、站点里也没有: {', '.join(missing)}")
    kept_sched = sorted(set(stored_sched) - {e.id for e in entries})
    tz_change = None
    if schedule is not None:
        new_tz = schedule.timezone
        if cur_tz != new_tz:
            if cur_tz and kept_sched:
                raise CatalogError(
                    f"包里排程的时区是 {new_tz},站点现在是 {cur_tz},而站点上还有排程 "
                    f"{', '.join(kept_sched)} 包里没有:换时区它们的钟点会悄悄换了意思。"
                    "先在站点上改好或删掉它们,或者让包带全")
            tz_change = {"from": cur_tz, "to": new_tz}

    rep: dict[str, Any] = {"routes": {"added": [], "replaced": [], "unchanged": []},
                           "schedules": {"added": [], "replaced": [], "unchanged": []}}
    changed = False
    for rid, m in sorted(missions.items()):
        d = _json(m.to_wire())
        old = stored_routes.get(rid)
        if old is None:
            tx.execute("INSERT INTO routes(route_id, map_id, map_version, definition, revision, "
                       "updated_ms, updated_by, source) VALUES (?,?,?,?,1,?,?,?)",
                       (rid, m.map_id, m.map_version, d, now, by, source))
            rep["routes"]["added"].append(rid)
        elif _json(json.loads(old["definition"])) == d:
            rep["routes"]["unchanged"].append(rid)
            continue
        else:
            tx.execute("UPDATE routes SET map_id=?, map_version=?, definition=?, "
                       "revision=revision+1, updated_ms=?, updated_by=?, source=? "
                       "WHERE route_id=?", (m.map_id, m.map_version, d, now, by, source, rid))
            rep["routes"]["replaced"].append(rid)
        changed = True
    for e in entries:
        d = _json(e.to_wire())
        old = stored_sched.get(e.id)
        if old is None:
            tx.execute("INSERT INTO route_schedules(entry_id, definition, revision, "
                       "active_from_ms, updated_ms, updated_by, source) "
                       "VALUES (?,?,1,?,?,?,?)", (e.id, d, now, now, by, source))
            rep["schedules"]["added"].append(e.id)
        elif _json(json.loads(old["definition"])) == d and tz_change is None:
            rep["schedules"]["unchanged"].append(e.id)
            continue
        else:
            tx.execute("UPDATE route_schedules SET definition=?, revision=revision+1, "
                       "active_from_ms=?, updated_ms=?, updated_by=?, source=? WHERE entry_id=?",
                       (d, now, now, by, source, e.id))
            rep["schedules"]["replaced"].append(e.id)
        changed = True
    if tz_change is not None:
        tx.execute("INSERT OR IGNORE INTO route_store(id, revision, updated_ms, updated_by) "
                   "VALUES (1, 0, 0, '')")
        tx.execute("UPDATE route_store SET timezone=? WHERE id=1", (tz_change["to"],))
        changed = True
    rep["kept_routes"] = sorted(set(stored_routes) - set(missions))
    rep["kept_schedules"] = kept_sched
    rep["timezone"] = tz_change
    rep["changed"] = changed
    if changed:
        RouteStore._touch(tx, by, now)
    if bundle is not None:
        tx.execute("INSERT OR IGNORE INTO route_store(id, revision, updated_ms, updated_by) "
                   "VALUES (1, 0, 0, '')")
        tx.execute("UPDATE route_store SET last_import=? WHERE id=1", (json.dumps(
            {"bundle_id": bundle[0], "version": bundle[1], "at_ms": now, "by": by,
             "routes": rep["routes"], "schedules": rep["schedules"],
             "kept_routes": rep["kept_routes"], "kept_schedules": rep["kept_schedules"],
             "timezone": rep["timezone"]}, ensure_ascii=False),))
    return rep
