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

    async def return_to(self, robot_id: str, *, issued_by: str) -> dict[str, Any]:
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

    async def _auto(self, robot_id: str, after: str) -> None:
        try:
            await self.return_to(robot_id, issued_by="standby:auto")
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
