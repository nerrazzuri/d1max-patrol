"""待命点(W00c2b 设计决定三 A)。每台狗登记若干个命名待命点(地图、x、y、yaw),其中一个是
**默认**。站点派的任务**正常完成**(``task_done``)后,站点自动给这台狗派一条回默认待命点的
``goto``:优先级最低(``STANDBY_RETURN``),task_id 以 ``standby-`` 开头。

- **只在 ``task_done`` 之后回**(内部评审):``task_aborted`` 是人按了停止键,要狗停在原地;
  ``task_failed`` 可能是急停、丢定位、安全裁定 —— 这时让狗自己再动起来是错的。这两种停在原地等人,
  人可以用「回待命点」手动叫它回。
- 回待命点本身结束后不再回;被抢占不回 —— 抢占它的那一趟结束后会回。
- 没有默认待命点 → 不回,也不报错。
- 待命点记着登记时的地图与**版本**;狗加载的地图或版本对不上 → 不回(地图重建之后旧坐标不可信),
  推一条 ``standby_failed`` 给值守的人看。
- 手动「回待命点」也是最低优先级:狗在跑别的任务时它回 ``busy``;排程到点照样能抢它。
- **巡检跑完之后,直线的狗沿来路回**(W00c6b):狗在能力里报 ``goto.path``(直线桥是
  ``straight``,规划器上线后是 ``planned``;读不到按 ``straight``)。直线的狗跑完一趟巡检,
  不再派直线 ``goto`` —— 从最后一个巡检点直线走回待命点会穿墙 —— 而是派一趟回程巡检:
  那一趟的航点倒序(只要位姿,不带动作)+ 最后一个点是待命点,点位失败就跳过。取不到那一趟
  的任务定义就不回(推 ``standby_failed``),不退回直线。``goto`` 跑完、手动「回待命点」照旧
  直线 ``goto``(过渡期受 W00c6i 的监护租约约束)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections.abc import Callable
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.geometry import Pose
from d1max_contract.messages import Event, MapPose
from d1max_contract.mission import Mission, MissionError, MissionWaypoint, Policy, parse_mission
from d1max_site.ca import SAFE_ID
from d1max_site.db import SiteDB
from d1max_site.dispatcher import Dispatcher, DispatchRefused
from d1max_site.priorities import STANDBY_PREFIX, STANDBY_RETURN

log = logging.getLogger(__name__)

_RETURN_AFTER = frozenset({"task_done"})


class StandbyError(RuntimeError):
    pass


class StandbyManager:
    def __init__(self, db: SiteDB, dispatcher: Dispatcher, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self._tasks: set[asyncio.Task] = set()
        dispatcher.on_event(self._on_event)

    # ------------------------------------------------------------ 登记

    def set(self, robot_id: str, name: str, *, map_id: str, map_version: str, x: float,
            y: float, yaw: float, default: bool | None = None) -> None:
        """登记或改一个待命点。``default=None`` = 不动这个点原来的默认标记。"""
        if self.dispatcher.registry.get(robot_id) is None:
            raise StandbyError(f"没有登记过 {robot_id}")
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise StandbyError(f"待命点名只许 ASCII 字母、数字、. _ -: {name!r}")
        for k, v in (("map_id", map_id), ("map_version", map_version)):
            if not isinstance(v, str) or not v:
                raise StandbyError(f"要 {k}")
        if default is not None and not isinstance(default, bool):
            raise StandbyError("default 要是 true/false")
        xs = []
        for k, v in (("x", x), ("y", y), ("yaw", yaw)):
            try:
                ok = not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
            except OverflowError:                # 400 位的整数
                ok = False
            if not ok:
                raise StandbyError(f"{k} 要是有限数")
            xs.append(float(v))
        with self.db.tx() as c:
            old = c.execute("SELECT is_default FROM standby_points WHERE robot_id=? AND name=?",
                            (robot_id, name)).fetchone()
            flag = bool(old["is_default"]) if (default is None and old) else bool(default)
            if flag:
                c.execute("UPDATE standby_points SET is_default=0 WHERE robot_id=?", (robot_id,))
            c.execute("INSERT INTO standby_points(robot_id, name, map_id, map_version, x, y, yaw, "
                      "is_default) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(robot_id, name) DO UPDATE "
                      "SET map_id=excluded.map_id, map_version=excluded.map_version, "
                      "x=excluded.x, y=excluded.y, yaw=excluded.yaw, "
                      "is_default=excluded.is_default",
                      (robot_id, name, map_id, map_version, *xs, int(flag)))

    def remove(self, robot_id: str, name: str) -> None:
        with self.db.tx() as c:
            cur = c.execute("DELETE FROM standby_points WHERE robot_id=? AND name=?",
                            (robot_id, name))
            if cur.rowcount == 0:
                raise StandbyError(f"{robot_id} 没有待命点 {name}")

    @staticmethod
    def _row(r) -> dict[str, Any]:
        return {"name": r["name"], "map_id": r["map_id"], "map_version": r["map_version"],
                "x": r["x"], "y": r["y"],
                "yaw": r["yaw"], "default": bool(r["is_default"])}

    def list(self, robot_id: str) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.query(
            "SELECT * FROM standby_points WHERE robot_id=? ORDER BY name", (robot_id,))]

    def default(self, robot_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM standby_points WHERE robot_id=? AND is_default=1",
                             (robot_id,))
        return self._row(rows[0]) if rows else None

    # ------------------------------------------------------------ 回

    def _checked_default(self, robot_id: str) -> dict[str, Any]:
        """默认待命点,且狗加载的地图与版本对得上。对不上抛 ``DispatchRefused``。"""
        p = self.default(robot_id)
        if p is None:
            raise DispatchRefused(f"{robot_id} 没有默认待命点")
        c = self.dispatcher.clients.get(robot_id)
        caps = c.capabilities.tasks.get("patrol", {}) if c and c.capabilities else {}
        loaded = (caps.get("map_id"), caps.get("map_version"))
        if loaded[0] is None:
            raise DispatchRefused(f"{robot_id} 没报已加载的地图")
        if loaded != (p["map_id"], p["map_version"]):
            raise DispatchRefused(f"{robot_id} 加载的地图是 {loaded[0]}:{loaded[1]},待命点 "
                                  f"{p['name']} 登记在 {p['map_id']}:{p['map_version']}"
                                  f"(地图或版本对不上)")
        return p

    async def return_to(self, robot_id: str, *, issued_by: str) -> dict[str, Any]:
        p = self._checked_default(robot_id)
        target = MapPose(map_id=p["map_id"], map_version=p["map_version"], frame_id="map",
                         x=p["x"], y=p["y"], yaw=p["yaw"]).to_wire()
        return await self.dispatcher.goto(robot_id, target, None, issued_by=issued_by,
                                          priority=STANDBY_RETURN,
                                          task_id=f"{STANDBY_PREFIX}{uuid.uuid4().hex[:12]}")

    def _on_event(self, robot_id: str, e: Event) -> None:
        task_id = e.data.get("task_id") if isinstance(e.data, dict) else None
        if e.kind not in _RETURN_AFTER or not task_id or task_id.startswith(STANDBY_PREFIX):
            return
        if self.default(robot_id) is None:
            return
        # 事件回调在事件循环里、同步地跑;派单要 await 回执,另起一个协程。
        t = asyncio.get_running_loop().create_task(self._auto(robot_id, task_id))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _path_kind(self, robot_id: str) -> str:
        c = self.dispatcher.clients.get(robot_id)
        caps = c.capabilities.tasks.get("goto", {}) if c and c.capabilities else {}
        return str(caps.get("path", "straight"))

    def _route_back(self, robot_id: str, task_id: str) -> tuple[MissionWaypoint, ...] | None:
        """跑完的那一趟要不要沿来路回、来路是什么。``None`` = 照旧直线 ``goto``(不是巡检,或者
        狗会规划)。直线的狗跑完的巡检、却取不到那一趟的任务定义 → 抛 ``StandbyError``(不回,
        不退回直线)。"""
        rows = self.db.query("SELECT kind, payload FROM commands WHERE task_id=? AND robot_id=? "
                             "ORDER BY issued_at DESC LIMIT 1", (task_id, robot_id))
        if not rows or rows[0]["kind"] != "patrol" or self._path_kind(robot_id) == "planned":
            return None
        try:
            m = parse_mission(json.loads(rows[0]["payload"])["mission"])
        except (ValueError, KeyError, TypeError, MissionError, ContractError) as exc:
            raise StandbyError(f"直线的狗要沿来路回待命点,取不到 {task_id} 的来路: {exc}") from exc
        return m.waypoints

    async def _return_along(self, robot_id: str, came: tuple[MissionWaypoint, ...]) -> None:
        """回程巡检:来路倒序(只要位姿)+ 待命点;点位失败跳过、不重试。"""
        from d1max_site.dispatcher import MAX_PATROL_WAYPOINTS
        p = self._checked_default(robot_id)
        if len(came) + 1 > MAX_PATROL_WAYPOINTS:
            raise StandbyError(f"来路 {len(came)} 个航点,回程放不下")
        home = Pose.from_xy_yaw(p["x"], p["y"], p["yaw"])
        wps = tuple(MissionWaypoint(name=w.name, pose=w.pose) for w in reversed(came))
        wps += (MissionWaypoint(name=f"待命点·{p['name']}", pose=home),)
        mission = Mission(mission=f"回待命点·{p['name']}", map_id=p["map_id"], waypoints=wps,
                          policy=Policy(on_waypoint_failed="skip", waypoint_retry=0))
        await self.dispatcher.patrol(robot_id, mission.to_wire(), issued_by="standby:auto",
                                     priority=STANDBY_RETURN,
                                     task_id=f"{STANDBY_PREFIX}{uuid.uuid4().hex[:12]}")

    async def _auto(self, robot_id: str, after: str) -> None:
        try:
            came = self._route_back(robot_id, after)
            if came is None:
                await self.return_to(robot_id, issued_by="standby:auto")
            else:
                await self._return_along(robot_id, came)
        except Exception as exc:  # noqa: BLE001 - 回不去:记日志、推给值守的人,下次任务完成再试
            log.warning("%s 在 %s 结束后回待命点没派成: %s", robot_id, after, exc)
            self.dispatcher.feed.publish({"kind": "standby_failed", "robot_id": robot_id,
                                          "after": after, "reason": str(exc)})

    async def close(self) -> None:
        """收掉还在等回执的自动回程。在关派遣器之前调。"""
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except BaseException:  # noqa: BLE001 - 取消与其他异常都只是收尾
                pass
