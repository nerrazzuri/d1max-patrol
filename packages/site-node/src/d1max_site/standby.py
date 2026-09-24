"""待命点(W00c2b 设计决定三 A)。每台狗登记若干个命名待命点(地图、x、y、yaw),其中一个是
**默认**。站点派的任务结束(``task_done/failed/aborted``)后,站点自动给这台狗派一条回默认待命点
的 ``goto``:优先级最低(``STANDBY_RETURN``),task_id 以 ``standby-`` 开头。

- 回待命点本身结束后不再回;被抢占(``task_preempted``)不回 —— 抢占它的那一趟结束后会回。
- 没有默认待命点 → 不回,也不报错。
- 狗加载的地图跟待命点的地图不一致 → 不回,记日志(待命点跟着地图走)。
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Callable
from typing import Any

from d1max_contract.messages import Event, MapPose
from d1max_site.ca import SAFE_ID
from d1max_site.db import SiteDB
from d1max_site.dispatcher import Dispatcher, DispatchRefused
from d1max_site.priorities import STANDBY_PREFIX, STANDBY_RETURN

log = logging.getLogger(__name__)

_RETURN_AFTER = frozenset({"task_done", "task_failed", "task_aborted"})


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

    def set(self, robot_id: str, name: str, *, map_id: str, x: float, y: float, yaw: float,
            default: bool = False) -> None:
        if self.dispatcher.registry.get(robot_id) is None:
            raise StandbyError(f"没有登记过 {robot_id}")
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise StandbyError(f"待命点名只许 ASCII 字母、数字、. _ -: {name!r}")
        if not isinstance(map_id, str) or not map_id:
            raise StandbyError("要 map_id")
        for k, v in (("x", x), ("y", y), ("yaw", yaw)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise StandbyError(f"{k} 要是有限数")
        with self.db.tx() as c:
            if default:
                c.execute("UPDATE standby_points SET is_default=0 WHERE robot_id=?", (robot_id,))
            c.execute("INSERT INTO standby_points(robot_id, name, map_id, x, y, yaw, is_default) "
                      "VALUES (?,?,?,?,?,?,?) ON CONFLICT(robot_id, name) DO UPDATE SET "
                      "map_id=excluded.map_id, x=excluded.x, y=excluded.y, yaw=excluded.yaw, "
                      "is_default=excluded.is_default",
                      (robot_id, name, map_id, float(x), float(y), float(yaw), int(default)))

    @staticmethod
    def _row(r) -> dict[str, Any]:
        return {"name": r["name"], "map_id": r["map_id"], "x": r["x"], "y": r["y"],
                "yaw": r["yaw"], "default": bool(r["is_default"])}

    def list(self, robot_id: str) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.query(
            "SELECT * FROM standby_points WHERE robot_id=? ORDER BY name", (robot_id,))]

    def default(self, robot_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM standby_points WHERE robot_id=? AND is_default=1",
                             (robot_id,))
        return self._row(rows[0]) if rows else None

    # ------------------------------------------------------------ 回

    async def return_to(self, robot_id: str, *, issued_by: str) -> dict[str, Any]:
        p = self.default(robot_id)
        if p is None:
            raise DispatchRefused(f"{robot_id} 没有默认待命点")
        c = self.dispatcher.clients.get(robot_id)
        caps = c.capabilities.tasks.get("patrol", {}) if c and c.capabilities else {}
        if caps.get("map_id") != p["map_id"] or not caps.get("map_version"):
            raise DispatchRefused(f"{robot_id} 加载的地图 {caps.get('map_id')!r} 跟待命点 "
                                  f"{p['name']} 的地图 {p['map_id']!r} 对不上")
        target = MapPose(map_id=p["map_id"], map_version=caps["map_version"], frame_id="map",
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

    async def _auto(self, robot_id: str, after: str) -> None:
        try:
            await self.return_to(robot_id, issued_by="standby:auto")
        except Exception as exc:  # noqa: BLE001 - 回不去只记日志:下一次任务结束再试
            log.warning("%s 在 %s 结束后回待命点没派成: %s", robot_id, after, exc)
