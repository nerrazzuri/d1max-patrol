"""站点派遣器的最小版(W00c1)。一条共享的 MQTT 连接上给每台登记的狗挂一个契约层的
``DispatchClient``;派单前查派遣条件(总设计 §3.1),命令先落库再发,回执回写;上行的
status/event/reconcile 落库并推给订阅者(站点 API 的 SSE)。

**不做**:排程、抢占规则、外部事件派遣(W00c2);账号角色(W00c3)。``issued_by`` 由调用方
(站点 API)填已认证的账号名 —— 命令、审计从第一天起就绑在人身上(总设计 §5)。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from d1max_contract.dispatch import DispatchClient, DispatchTimeout
from d1max_contract.errors import ContractError
from d1max_contract.messages import Ack, Event, MapPose, Reconcile, Status
from d1max_contract.mission import MissionError, parse_mission
from d1max_contract.topics import Topics
from d1max_contract.transport import Transport
from d1max_site.db import SiteDB
from d1max_site.priorities import STANDBY_PREFIX
from d1max_site.registry import Registry

log = logging.getLogger(__name__)

#: 多久没收到**实时** status 就算不新鲜(按站点自己的钟,见 ``DispatchClient.status_live_at``)。
#: 代理空闲时每 30 s 发一次 status。
STALE_MS = 90_000
COMMAND_TTL_MS = 60_000
#: 一趟巡检的任务定义进命令(设计决定二 A);broker 的报文上限是 256 KB,留余量。
MAX_PATROL_BYTES = 200_000
MAX_PATROL_WAYPOINTS = 500


class DispatchRefused(RuntimeError):
    """派遣条件不满足。``reason`` 给人看,站点 API 原样回 409。"""


# ------------------------------------------------------------ 订阅(SSE 用)


@dataclass(eq=False)                         # 按身份哈希:进 set 用
class FeedSub:
    """一个订阅者。线程安全:派遣器在事件循环线程里 put,SSE 在 HTTP 线程里 get。
    跟不上(队列满)就标 ``lagged``,SSE 那边据此补发一帧全量快照,而不是悄悄丢事件。"""

    q: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1000))
    lagged: bool = False

    def get(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> int:
        """清掉积压。跟不上之后先清再给全量快照:不然快照之后还会把旧的推一遍,把
        客户端的状态往回倒。返回清掉的条数。"""
        n = 0
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return n
            n += 1


class Feed:
    def __init__(self) -> None:
        self._subs: set[FeedSub] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> FeedSub:
        s = FeedSub()
        with self._lock:
            self._subs.add(s)
        return s

    def unsubscribe(self, s: FeedSub) -> None:
        with self._lock:
            self._subs.discard(s)

    def publish(self, item: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            try:
                s.q.put_nowait(item)
            except queue.Full:
                s.lagged = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._subs)


# ------------------------------------------------------------ 派遣器


class Dispatcher:
    def __init__(self, transport: Transport, db: SiteDB, registry: Registry, *,
                 now_ms: Callable[[], int], stale_ms: int = STALE_MS,
                 ack_timeout_s: float = 10.0) -> None:
        self._t = transport
        self.db = db
        self.registry = registry
        self.site_id = registry.site_id
        self._now = now_ms
        self.stale_ms = stale_ms
        self.ack_timeout_s = ack_timeout_s
        self.clients: dict[str, DispatchClient] = {}
        self.feed = Feed()
        #: 新事件(去重之后)的回调:排程执行器靠它回写这一趟的结果。
        self._event_cbs: list[Callable[[str, Event], None]] = []
        self._ack_cbs: list[Callable[[Ack], None]] = []
        #: 关了之后还在路上的上行报文不再落库(库可能已经关了)。
        self._closed = False

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """先给每台未吊销的狗登记订阅,再连 broker(持久会话补投的报文要有人接)。"""
        for r in self.registry.list():
            if not r.revoked:
                await self.add_robot(r.robot_id)
        await self._t.connect()

    async def close(self) -> None:
        self._closed = True
        await self._t.close()

    async def sync_robots(self) -> list[str]:
        """注册表里新登记(且未吊销)的狗挂上客户端。``d1max-site enroll`` 是另一个进程写的库,
        ``serve`` 定期调它。返回这次新挂上的。"""
        added = []
        for r in self.registry.list():
            if not r.revoked and r.robot_id not in self.clients:
                await self.add_robot(r.robot_id)
                added.append(r.robot_id)
        return added

    async def add_robot(self, robot_id: str) -> None:
        if robot_id in self.clients:
            return
        c = DispatchClient(self._t, Topics(site_id=self.site_id, robot_id=robot_id),
                           now_ms=self._now)
        c.on_status(lambda s, rid=robot_id: self._on_status(rid, s))
        c.on_event(lambda e, rid=robot_id: self._on_event(rid, e))
        c.on_reconcile(lambda r, rid=robot_id: self._on_reconcile(rid, r))
        c.on_ack(self._record_ack)          # 包括等的人超时走了之后才到的回执
        self.clients[robot_id] = c
        await c.attach()

    # ------------------------------------------------------------ 上行

    def _on_status(self, robot_id: str, s: Status) -> None:
        if self._closed:
            return
        wire = s.to_wire()
        with self.db.tx() as c:
            c.execute("INSERT INTO robot_state(robot_id, status, updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(robot_id) DO UPDATE SET status=excluded.status, "
                      "updated_at=excluded.updated_at",
                      (robot_id, json.dumps(wire), self._now()))
        self.feed.publish({"kind": "status", "robot_id": robot_id, "status": wire})

    def _on_event(self, robot_id: str, e: Event) -> None:
        if self._closed:
            return
        with self.db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO events(robot_id, boot_id, seq, event_id, kind, "
                            "data, stamp, received_at) VALUES (?,?,?,?,?,?,?,?)",
                            (robot_id, e.boot_id, e.seq, e.event_id, e.kind, json.dumps(e.data),
                             e.stamp, self._now()))
            fresh = cur.rowcount == 1
        if not fresh:
            return                          # 站点重启后 broker 补投的旧事件:库里有了,不再推
        for cb in list(self._event_cbs):
            try:
                cb(robot_id, e)
            except Exception:
                log.exception("事件回调炸了(%s %s),其余照常", robot_id, e.kind)
        self.feed.publish({"kind": "event", "robot_id": robot_id, "event": e.to_wire()})

    def on_event(self, cb: Callable[[str, Event], None]) -> None:
        self._event_cbs.append(cb)

    def on_ack(self, cb: Callable[[Ack], None]) -> None:
        """每条回执(包括超时之后才到的)落库之后回调。"""
        self._ack_cbs.append(cb)

    def _on_reconcile(self, robot_id: str, r: Reconcile) -> None:
        self.feed.publish({"kind": "reconcile", "robot_id": robot_id,
                           "reconcile": r.to_wire()})

    # ------------------------------------------------------------ 视图

    def robot_view(self, robot_id: str) -> dict[str, Any] | None:
        rec = self.registry.get(robot_id)
        if rec is None:
            return None
        c = self.clients.get(robot_id)
        status = c.status.to_wire() if c is not None and c.status is not None else None
        caps = (c.capabilities.to_wire()
                if c is not None and c.capabilities is not None else None)
        return {"robot_id": robot_id, "revoked": rec.revoked,
                "active": self.registry.active(robot_id, now_ms=self._now()),
                "expires_at": rec.expires_at, "status": status, "capabilities": caps,
                "fresh": self._fresh(c)}

    def robots_view(self) -> list[dict[str, Any]]:
        return [v for r in self.registry.list() if (v := self.robot_view(r.robot_id))]

    def recent_events(self, robot_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM events WHERE robot_id=? ORDER BY received_at DESC, "
                             "seq DESC LIMIT ?", (robot_id, limit))
        return [{"boot_id": r["boot_id"], "seq": r["seq"], "kind": r["kind"],
                 "data": json.loads(r["data"]), "stamp": r["stamp"]} for r in rows]

    def commands(self, robot_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM commands WHERE robot_id=? ORDER BY issued_at DESC "
                             "LIMIT ?", (robot_id, limit))
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def _fresh(self, c: DispatchClient | None) -> bool:
        return (c is not None and c.status is not None and c.status.online
                and c.status_live_at is not None
                and self._now() - c.status_live_at <= self.stale_ms)

    # ------------------------------------------------------------ 派单

    def _client_for(self, robot_id: str) -> DispatchClient:
        if self.registry.get(robot_id) is None:
            raise DispatchRefused(f"没有登记过 {robot_id}")
        if not self.registry.active(robot_id, now_ms=self._now()):
            raise DispatchRefused(f"{robot_id} 的登记已吊销或过期")
        c = self.clients.get(robot_id)
        if c is None:
            raise DispatchRefused(f"{robot_id} 还没挂上派遣客户端")
        return c

    def _check_dispatchable(self, robot_id: str, c: DispatchClient, kind: str) -> None:
        """派遣条件(总设计 §3.1):在线、新鲜、就绪、支持这种任务。"""
        s = c.status
        if s is None or not s.online:
            raise DispatchRefused(f"{robot_id} 不在线")
        if not self._fresh(c):
            raise DispatchRefused(f"{robot_id} 的状态不新鲜({self.stale_ms // 1000} s 内没收到"
                                  f"实时 status)")
        not_ready = [k for k, v in s.ready.to_wire().items() if k != "schema" and not v]
        if not_ready:
            raise DispatchRefused(f"{robot_id} 没就绪: {', '.join(sorted(not_ready))}")
        if c.capabilities is not None and kind not in c.capabilities.tasks:
            raise DispatchRefused(f"{robot_id} 不支持 {kind}")

    def dispatchable(self, robot_id: str, kind: str) -> str:
        """能不能给它派这种任务:能 → 空串;不能 → 理由。排程执行器选狗用。"""
        try:
            self._check_dispatchable(robot_id, self._client_for(robot_id), kind)
        except DispatchRefused as exc:
            return str(exc)
        return ""

    def busy(self, robot_id: str) -> str | None:
        """狗上此刻在跑的任务 id(看它自己报的 status);没有 → None。**回待命点不算忙**
        (W00c2b:优先级最低,谁来都能抢)。"""
        c = self.clients.get(robot_id)
        if c is None or c.status is None or c.status.task is None:
            return None
        if c.status.task.task_id.startswith(STANDBY_PREFIX):
            return None
        return c.status.task.task_id

    async def goto(self, robot_id: str, target: dict[str, Any], max_speed_mps: float | None,
                   *, issued_by: str, priority: int = 0,
                   task_id: str | None = None) -> dict[str, Any]:
        c = self._client_for(robot_id)
        self._check_dispatchable(robot_id, c, "goto")
        try:
            MapPose.from_wire(target)
        except ContractError as exc:
            raise DispatchRefused(f"target 不成形: {exc}") from exc
        payload: dict[str, Any] = {"target": target}
        if max_speed_mps is not None:
            payload["max_speed_mps"] = max_speed_mps
        return await self._send(c, robot_id, "goto", payload, issued_by=issued_by,
                                priority=priority, task_id=task_id)

    async def patrol(self, robot_id: str, mission: dict[str, Any], *, issued_by: str,
                     priority: int = 0, task_id: str | None = None,
                     before_send: Callable[[Any], None] | None = None) -> dict[str, Any]:
        """整趟巡检(W00c2a)。任务定义整份放进命令(设计决定二 A);``map_version`` 取狗在
        能力报文里报的已加载地图,任务的 ``map_id`` 要跟它一致。"""
        c = self._client_for(robot_id)
        self._check_dispatchable(robot_id, c, "patrol")
        try:
            m = parse_mission(mission)
        except (MissionError, ValueError) as exc:
            raise DispatchRefused(f"任务定义不成形: {exc}") from exc
        caps = c.capabilities.tasks.get("patrol", {}) if c.capabilities is not None else {}
        loaded = (caps.get("map_id"), caps.get("map_version"))
        if loaded[0] is None or loaded[1] is None:
            raise DispatchRefused(f"{robot_id} 没报已加载的地图")
        if m.map_id != loaded[0]:
            raise DispatchRefused(f"任务用的地图 {m.map_id!r},{robot_id} 加载的是 {loaded[0]!r}")
        if len(m.waypoints) > MAX_PATROL_WAYPOINTS:
            raise DispatchRefused(f"一趟最多 {MAX_PATROL_WAYPOINTS} 个航点,这趟 "
                                  f"{len(m.waypoints)} 个")
        payload = {"mission": m.to_wire(), "map_version": loaded[1]}
        if len(json.dumps(payload).encode()) > MAX_PATROL_BYTES:
            raise DispatchRefused(f"任务定义超过 {MAX_PATROL_BYTES} 字节")
        return await self._send(c, robot_id, "patrol", payload, issued_by=issued_by,
                                priority=priority, task_id=task_id, before_send=before_send)

    async def abort(self, robot_id: str, task_id: str, *, issued_by: str) -> dict[str, Any]:
        """abort 只要求登记有效:不在线也发(QoS 1 持久会话,重连后补投)。"""
        c = self._client_for(robot_id)
        return await self._send(c, robot_id, "abort", {}, issued_by=issued_by, task_id=task_id)

    async def _send(self, c: DispatchClient, robot_id: str, kind: str, payload: dict[str, Any],
                    *, issued_by: str, task_id: str | None = None, priority: int = 0,
                    before_send: Callable[[Any], None] | None = None) -> dict[str, Any]:
        if not issued_by:
            raise DispatchRefused("没有已认证的派单人")
        cmd = c.new_command(kind, payload, ttl_ms=COMMAND_TTL_MS,
                            control_epoch=self.registry.control_epoch(robot_id),
                            task_id=task_id, priority=priority)
        with self.db.tx() as tx:                    # 先落库:发出去之后进程死了也有账
            tx.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, "
                       "issued_by, issued_at, priority) VALUES (?,?,?,?,?,?,?,?)",
                       (cmd.command_id, cmd.task_id, robot_id, kind, json.dumps(payload),
                        issued_by, cmd.issued_at, priority))
        if before_send is not None:
            # 调用方要在命令**发出去之前**记账(排程执行器:这一轮算起跑过了)——回执可能丢,
            # 狗可能收到了;发完再记的话,回执一丢就会再派一趟。
            before_send(cmd)
        try:
            ack = await c.send(cmd, timeout_s=self.ack_timeout_s)
        except DispatchTimeout:
            # 狗可能收到了、可能没收到;记一笔,晚到的回执会经 on_ack 覆盖它。
            with self.db.tx() as tx:
                tx.execute("UPDATE commands SET ack_result='timeout' WHERE command_id=? "
                           "AND ack_result IS NULL", (cmd.command_id,))
            raise
        self.feed.publish({"kind": "ack", "robot_id": robot_id, "ack": ack.to_wire(),
                           "issued_by": issued_by})
        return {"command_id": cmd.command_id, "task_id": cmd.task_id,
                "ack": ack.to_wire()}

    def _record_ack(self, ack: Ack) -> None:
        if self._closed:
            return
        with self.db.tx() as c:
            c.execute("UPDATE commands SET ack_result=?, ack_reason=? WHERE command_id=?",
                      (ack.result.value, ack.reason, ack.command_id))
        for cb in list(self._ack_cbs):
            try:
                cb(ack)
            except Exception:
                log.exception("回执回调炸了(%s),其余照常", ack.command_id)
