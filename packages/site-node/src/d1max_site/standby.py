"""待命点(W00c2b 设计决定三 A)与原点(W13a,决策 16)。

**原点与待命点是两样东西**(决策 16):原点是安全返航、回充的语义,每台狗每张图的每个版本一个(表
``homes``),下发地图时发给狗;待命点是运营调度的语义,可以多个、可调(表 ``standby_points``)。
「在这儿标原点」只改原点 —— 这张图上这台狗还没有待命点时,顺手用它建一个默认待命点(老库迁移时也是
拿默认待命点抄成原点,两边起步一样);「在这儿设待命点」只改待命点、狗上的原点不动。

每台狗登记若干个命名待命点(地图、x、y、yaw),其中一个是**默认**。站点派的任务**正常完成**(``task_done``)后,站点自动给这台狗派一条回默认待命点的
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
  那一趟的航点倒序(只要位姿,不带动作)+ 最后一个点是待命点。回程巡检**继承原任务的 policy**
  (超时、电量线、丢定位/丢控制权的处置),只改三样(W00c6b 内审):点位失败就中止(原地停 ——
  跳过一点就是一条没走过的直线)、只跑一圈、电量到返航线接着往前走(``on_battery_low:
  continue`` —— 剩下的路就是回家的路,掉头是往远端走)。
- 直线的狗**只在知道狗停在最后一个巡检点时才回**:那一趟的命令记录在(只看 ``patrol``/``goto``
  两类 —— ``abort`` 用的也是这个 task_id)、原巡检的地图与版本跟待命点对得上、每个点都报了到
  (条数 = 点数 × 圈数)。有一样不成就不回、推 ``standby_failed``,不退回直线。半路电量返航的
  那一趟代理报的是 ``task_failed``,本来就不回。
- **回哪一个**(W14):排程派的那一趟(``sched-`` 开头),排程条目写了 ``standby`` 就回那一个 ——
  **按派单时记在运行记录里的**(``schedule_runs.standby_name``),跑着的时候换了任务包、改了或删了那条
  排程都不影响这一趟(W14 外审)。这台狗**没有那个名字的点**(删了、写错)就退回默认的并记日志 —— 停在
  最后一个巡检点过夜比回默认的那一个糟;**有这个点但登记在别的地图或版本上**:不退回默认的,跟默认点
  对不上地图时一样不回、推 ``standby_failed``(坐标不可信)。别的都回默认的。
- ``goto`` 跑完、手动「回待命点」照旧直线 ``goto``(过渡期受 W00c6i 的监护租约约束)。遥控放租之后
  (``teleop-`` 那一趟 ``task_done``),直线的狗**不自动回**:起点是人刚开到的任意位置;人在场、
  知道狗在哪,要回就手动叫。会规划的狗照旧回。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.geometry import Pose
from d1max_contract.messages import Event, MapPose
from d1max_contract.mission import Mission, MissionError, MissionWaypoint, parse_mission
from d1max_site.ca import SAFE_ID
from d1max_site.db import SiteDB
from d1max_site.dispatcher import TELEOP_TASK_PREFIX, Dispatcher, DispatchRefused
from d1max_site.priorities import STANDBY_PREFIX, STANDBY_RETURN

log = logging.getLogger(__name__)

#: 自动回程碰上这些暂时性的拒绝(W09h 钟差还不知道;代理重连时状态还不新鲜 / 还没报在线)限时再试:
#: 每 2 s 一次、最多 7 次(约 15 s,够攒齐钟差样本)。别的拒绝(没人监护、忙、地图对不上)不再试。
_TRANSIENT_REFUSALS = ("钟差还不知道", "不新鲜", "不在线")
STANDBY_TRANSIENT_RETRIES = 7
STANDBY_RETRY_EVERY_S = 2.0

_RETURN_AFTER = frozenset({"task_done"})


class StandbyError(RuntimeError):
    pass


class StandbyManager:
    def __init__(self, db: SiteDB, dispatcher: Dispatcher, *, now_ms: Callable[[], int],
                 refusal: Callable[[str], str] | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        #: 自动回程碰上暂时性的拒绝(钟差还不知道、状态不新鲜)时隔多久再试(W09h 内审再议 1)。
        self._sleep = sleep
        #: 站点这一道关(W00c6i):要人监护的狗没人监护时回理由,自动回待命点不派。
        self.refusal = refusal
        self.db = db
        #: 标原点的回执(接口线程)与晚到的 ``home_marked`` 事件(事件循环)会同时调 ``mark``:
        #: 「这张图上还没有
        #: 待命点 → 建一个默认的」得一把锁做完(W16 时查出的竞态:两边都建,
        #: 后建的那次看见前一次已经设了默认,
        #: 把同一个点又写成「不是默认」)。
        self._mark_lock = threading.Lock()
        self.dispatcher = dispatcher
        self._now = now_ms
        self._tasks: set[asyncio.Task] = set()
        #: 这一趟跑完先别自动回(W22:到了拦截点要驱离,驱离结束时派回程)。
        #: ``(狗, 任务号) → 真 = 先别回``。
        self.hold: Callable[[str, str], bool] | None = None
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

    def mark(self, robot_id: str, name: str, data: Any) -> None:
        """狗在当前位置标了原点(W00c6f、W13a):登记成这台狗在这张图这个版本上的**原点**;这台狗在这张图
        这个版本上还没有待命点,顺手用它建一个(这台狗没有默认待命点、或者默认的在别的图上,就设成默认)。
        回执、``home_marked`` 事件都走这里(同样的值登记两遍没事)。"""
        if not isinstance(data, dict):
            raise StandbyError("狗没报位置")
        mid, ver = data["map_id"], data["map_version"]
        with self._mark_lock:
            self.set_home(robot_id, name, map_id=mid, map_version=ver, x=data["x"], y=data["y"],
                          yaw=data["yaw"])
            here = [p for p in self.list(robot_id)
                    if (p["map_id"], p["map_version"]) == (mid, ver)]
            if not here:
                d = self.default(robot_id)
                self.set(robot_id, name, map_id=mid, map_version=ver, x=data["x"], y=data["y"],
                         yaw=data["yaw"],
                         default=d is None or (d["map_id"], d["map_version"]) != (mid, ver))

    def set_home(self, robot_id: str, name: str, *, map_id: str, map_version: str, x: float,
                 y: float, yaw: float) -> None:
        """登记或改这台狗在这张图这个版本上的原点(W13a)。"""
        if self.dispatcher.registry.get(robot_id) is None:
            raise StandbyError(f"没有登记过 {robot_id}")
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise StandbyError(f"原点名只许 ASCII 字母、数字、. _ -: {name!r}")
        for k, v in (("map_id", map_id), ("map_version", map_version)):
            if not isinstance(v, str) or not v:
                raise StandbyError(f"要 {k}")
        xs = []
        for k, v in (("x", x), ("y", y), ("yaw", yaw)):
            try:
                ok = not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
            except OverflowError:
                ok = False
            if not ok:
                raise StandbyError(f"{k} 要是有限数")
            xs.append(float(v))
        with self.db.tx() as c:
            c.execute("INSERT INTO homes(robot_id, map_id, map_version, name, x, y, yaw, "
                      "marked_at_ms) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(robot_id, map_id, "
                      "map_version) DO UPDATE SET name=excluded.name, x=excluded.x, "
                      "y=excluded.y, yaw=excluded.yaw, marked_at_ms=CASE WHEN "
                      # 回执与晚到的 home_marked 事件按同样的值登记两遍:「什么时候标的」不跟着变
                      "name=excluded.name AND x=excluded.x AND y=excluded.y AND yaw=excluded.yaw "
                      "THEN marked_at_ms ELSE excluded.marked_at_ms END",
                      (robot_id, map_id, map_version, name, *xs, int(self._now())))

    def home(self, robot_id: str, map_id: str, map_version: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM homes WHERE robot_id=? AND map_id=? AND map_version=?",
                             (robot_id, map_id, map_version))
        return self._home_row(rows[0]) if rows else None

    def homes(self, robot_id: str) -> list[dict[str, Any]]:
        return [self._home_row(r) for r in self.db.query(
            "SELECT * FROM homes WHERE robot_id=? ORDER BY map_id, map_version", (robot_id,))]

    @staticmethod
    def _home_row(r) -> dict[str, Any]:
        return {"name": r["name"], "map_id": r["map_id"], "map_version": r["map_version"],
                "x": r["x"], "y": r["y"], "yaw": r["yaw"], "marked_at_ms": r["marked_at_ms"]}

    def mark_standby(self, robot_id: str, name: str, data: Any,
                     default: bool | None = None) -> None:
        """狗在当前位置报了位置、只标待命点(W13a):登记成待命点,原点不动。``default=None``:这台狗
        还没有默认待命点(或者默认的在别的图上)就设成默认,不然不动原来的默认标记。"""
        if not isinstance(data, dict) or data.get("target") != "standby":
            raise StandbyError("狗没按「只标待命点」回(老代理会把它当成标原点)")
        mid, ver = data["map_id"], data["map_version"]
        if default is None:
            d = self.default(robot_id)
            if d is None or (d["map_id"], d["map_version"]) != (mid, ver):
                default = True
        self.set(robot_id, name, map_id=mid, map_version=ver, x=data["x"], y=data["y"],
                 yaw=data["yaw"], default=default)

    def _on_home_marked(self, robot_id: str, d: dict[str, Any]) -> None:
        """回执等超时了、狗其实标了(W00c6f 内审应修 1):按狗发的事件补登记。**只认这台狗最新那条没被
        拒的** ``mark_home``(狗按收到的先后执行,之后那条狗收下了或者还没回话,就是那条算;那条被拒了、
        过期了,狗上还是前一条的)。晚到的旧事件不许盖掉后来标的。"""
        rows = self.db.query(
            "SELECT task_id FROM commands WHERE robot_id=? AND kind='mark_home' AND "
            "(ack_result IS NULL OR ack_result NOT IN ('rejected', 'expired')) "
            "ORDER BY issued_at DESC, rowid DESC LIMIT 1", (robot_id,))
        if not rows or rows[0]["task_id"] != d.get("task_id"):
            log.info("%s 标原点的事件 %s 不是最新那条,不登记", robot_id, d.get("task_id"))
            return
        try:
            self.mark(robot_id, str(d.get("name", "home")), d)
        except (StandbyError, KeyError, TypeError) as exc:
            log.warning("%s 标了原点,站点补登记原点没成: %s", robot_id, exc)
            self.dispatcher.feed.publish({"kind": "standby_failed", "robot_id": robot_id,
                                          "after": d.get("task_id"),
                                          "reason": "狗上的原点已经换了,站点登记原点没成: "
                                                    f"{exc}"})

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

    def _checked_default(self, robot_id: str, name: str | None = None) -> dict[str, Any]:
        """默认待命点(给了 ``name`` 就是那一个),且狗加载的地图与版本对得上。
        不成抛 ``DispatchRefused``。"""
        if name is None:
            p = self.default(robot_id)
            if p is None:
                raise DispatchRefused(f"{robot_id} 没有默认待命点")
        else:
            p = next((q for q in self.list(robot_id) if q["name"] == name), None)
            if p is None:
                raise DispatchRefused(f"{robot_id} 没有待命点 {name}")
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

    async def return_to(self, robot_id: str, *, issued_by: str,
                        name: str | None = None) -> dict[str, Any]:
        p = self._checked_default(robot_id, name)
        target = MapPose(map_id=p["map_id"], map_version=p["map_version"], frame_id="map",
                         x=p["x"], y=p["y"], yaw=p["yaw"]).to_wire()
        return await self.dispatcher.goto(robot_id, target, None, issued_by=issued_by,
                                          priority=STANDBY_RETURN,
                                          task_id=f"{STANDBY_PREFIX}{uuid.uuid4().hex[:12]}")

    def _on_event(self, robot_id: str, e: Event) -> None:
        if e.kind == "home_marked" and isinstance(e.data, dict):
            return self._on_home_marked(robot_id, e.data)
        task_id = e.data.get("task_id") if isinstance(e.data, dict) else None
        if e.kind not in _RETURN_AFTER or not task_id or task_id.startswith(STANDBY_PREFIX):
            return
        if self.hold is not None and self.hold(robot_id, task_id):
            return                      # W22:到了拦截点要驱离,回程由驱离结束时派
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

    def _target(self, robot_id: str, after: str) -> str | None:
        """跑完 ``after`` 那一趟之后回哪个待命点(W14):派单时记在运行记录里的;没记(不是排程派的、
        老记录、排程没写)或这台狗没有这个名字的点 → ``None``(默认的)。不看当前任务包(W14 外审)。"""
        if not after.startswith("sched-"):
            return None
        rows = self.db.query("SELECT entry_id, standby_name FROM schedule_runs WHERE task_id=? "
                             "AND outcome='started' LIMIT 1", (after,))
        name = rows[0]["standby_name"] if rows else ""
        if not name:
            return None
        if not any(p["name"] == name for p in self.list(robot_id)):
            log.warning("%s 的排程 %s 要回待命点 %s,这台狗没有这个点:回默认的", robot_id,
                        rows[0]["entry_id"], name)
            return None
        return name

    def _route_back(self, robot_id: str, task_id: str,
                    name: str | None = None) -> Mission | None:
        """跑完的那一趟要不要沿来路回、来路是什么。``None`` = 照旧直线 ``goto``(狗会规划,或者跑完的
        是 ``goto``)。直线的狗、却不能确定狗停在最后一个巡检点 → 抛 ``StandbyError``(不回,不退回
        直线),见模块说明。"""
        if self._path_kind(robot_id) == "planned":
            return None
        rows = self.db.query("SELECT kind, payload FROM commands WHERE task_id=? AND robot_id=? "
                             "AND kind IN ('patrol', 'goto') ORDER BY issued_at DESC LIMIT 1",
                             (task_id, robot_id))
        if not rows:
            raise StandbyError(f"直线的狗要沿来路回待命点,{task_id} 的命令记录没了,来路不明")
        if rows[0]["kind"] == "goto":
            return None
        try:
            payload = json.loads(rows[0]["payload"])
            m = parse_mission(payload["mission"])
            version = payload.get("map_version")
        except (ValueError, KeyError, TypeError, MissionError, ContractError) as exc:
            raise StandbyError(f"直线的狗要沿来路回待命点,取不到 {task_id} 的来路: {exc}") from exc
        p = self._checked_default(robot_id, name)
        if (m.map_id, version) != (p["map_id"], p["map_version"]):
            raise StandbyError(f"{task_id} 跑在 {m.map_id}:{version},待命点 {p['name']} 登记在 "
                               f"{p['map_id']}:{p['map_version']}(地图或版本对不上,来路坐标不可信)")
        want = len(m.waypoints) * m.policy.loops
        got = self._arrived(robot_id, task_id)
        if got != want:
            raise StandbyError(f"{task_id} 只报了 {got}/{want} 个点到了,不知道狗停在哪,不回")
        return m

    def _arrived(self, robot_id: str, task_id: str) -> int:
        rows = self.db.query("SELECT COUNT(*) AS n FROM events WHERE robot_id=? AND "
                             "kind='patrol_waypoint' AND json_extract(data, '$.task_id')=? AND "
                             "json_extract(data, '$.ok')=1", (robot_id, task_id))
        return int(rows[0]["n"])

    async def _return_along(self, robot_id: str, came: Mission,
                            name: str | None = None) -> dict[str, Any]:
        """回程巡检:来路倒序(只要位姿)+ 待命点;继承原任务的 policy,只改失败处置、圈数、电量处置。"""
        from d1max_site.dispatcher import MAX_PATROL_WAYPOINTS
        p = self._checked_default(robot_id, name)
        if len(came.waypoints) + 1 > MAX_PATROL_WAYPOINTS:
            raise StandbyError(f"来路 {len(came.waypoints)} 个航点,回程放不下")
        home = Pose.from_xy_yaw(p["x"], p["y"], p["yaw"])
        # 点位名不许带 ``..``(照片按点位名归档),待命点名许(``SAFE_ID``);也不许跟来路的点重名。
        label = p["name"].replace(".", "_")
        taken = {w.name for w in came.waypoints}
        last, n = f"待命点·{label}", 1
        while last in taken:
            n += 1
            last = f"待命点·{label}·{n}"
        wps = tuple(MissionWaypoint(name=w.name, pose=w.pose) for w in reversed(came.waypoints))
        wps += (MissionWaypoint(name=last, pose=home),)
        policy = replace(came.policy, on_waypoint_failed="abort", loops=1,
                         on_battery_low="continue")
        mission = Mission(mission=f"回待命点·{label}", map_id=p["map_id"], waypoints=wps,
                          policy=policy)
        return await self.dispatcher.patrol(robot_id, mission.to_wire(), issued_by="standby:auto",
                                     priority=STANDBY_RETURN,
                                     task_id=f"{STANDBY_PREFIX}{uuid.uuid4().hex[:12]}")

    async def _auto(self, robot_id: str, after: str) -> None:
        if after.startswith(TELEOP_TASK_PREFIX) and self._path_kind(robot_id) != "planned":
            # 遥控放租之后直线的狗不自动回(W00c6b 内审):起点是人刚开到的任意位置,人在场。
            log.info("%s 遥控放租之后不自动回待命点(直线的狗,人手动叫)", robot_id)
            return
        try:
            why = self.refusal(robot_id) if self.refusal is not None else ""
            if why:
                raise StandbyError(why)             # W00c6i:要人监护的狗没人监护,不自己回
            r = await self._dispatch_back(robot_id, after)
            ack = r.get("ack", {}) if isinstance(r, dict) else {}
            if ack.get("result") not in (None, "accepted", "duplicate"):
                # 狗拒收(没人监护、忙……)不是异常,以前不推;值守的人要看得见狗没回去(W00c6i 内审)。
                raise StandbyError(f"狗没收回待命点: {ack.get('reason') or ack.get('result')}")
        except Exception as exc:  # noqa: BLE001 - 回不去:记日志、推给值守的人,下次任务完成再试
            log.warning("%s 在 %s 结束后回待命点没派成: %s", robot_id, after, exc)
            self.dispatcher.feed.publish({"kind": "standby_failed", "robot_id": robot_id,
                                          "after": after, "reason": str(exc)})

    async def _dispatch_back(self, robot_id: str, after: str) -> Any:
        """派回程;碰上**暂时性**的拒绝限时再试(W09h 内审再议 1):代理重连时先补事件、后发状态,
        补上来的「任务完成」触发回程时,站点还没估出钟差(重连后要攒十来条遥测)、状态还不新鲜 ——
        原来只试一次、推一条 ``standby_failed``,狗就停在最后一个巡检点。"""
        name = self._target(robot_id, after)
        for attempt in range(STANDBY_TRANSIENT_RETRIES + 1):
            try:
                came = self._route_back(robot_id, after, name)
                if came is None:
                    return await self.return_to(robot_id, issued_by="standby:auto", name=name)
                return await self._return_along(robot_id, came, name)
            except DispatchRefused as exc:
                if attempt < STANDBY_TRANSIENT_RETRIES and any(
                        k in str(exc) for k in _TRANSIENT_REFUSALS):
                    log.info("%s 回待命点暂时派不了(%s),%g s 后再试", robot_id, exc,
                             STANDBY_RETRY_EVERY_S)
                    await self._sleep(STANDBY_RETRY_EVERY_S)
                    continue
                raise
        raise AssertionError("不会到这儿")

    async def close(self) -> None:
        """收掉还在等回执的自动回程。在关派遣器之前调。"""
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except BaseException:  # noqa: BLE001 - 取消与其他异常都只是收尾
                pass
