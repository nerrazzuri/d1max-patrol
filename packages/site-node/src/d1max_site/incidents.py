"""外部事件派遣(W00c2c,吸收 W16)。CCTV/AI 等外部系统报「某防区有入侵」→ 站点查出拦截点 →
选一台狗 → 以事件优先级派 ``goto`` 去拦截点。到了之后由待命点那套(W00c2b)自动回。

入口是**签名的 HTTP 回调**(设计决定一 A):每个事件源登记后拿到一个共享密钥;请求带
``X-D1MAX-Source``、``X-D1MAX-Timestamp``(毫秒)、``X-D1MAX-Signature``
= ``hex(HMAC-SHA256(密钥, 时间戳 + "." + 原始请求体))``。时间差超 5 分钟拒;同一
``(source, event_id)`` 只处理一次;**同一防区 60 s 内的后续事件合并到第一条**,不重复出动。

每条事件都进 ``incidents`` 表,去向:``dispatched``、``merged``、``duplicate``、``unmapped``
(防区没映射)、``ignored_type``(类型不认)、``no_robot``、``dispatch_failed``;派出去的那条,结果由
事件回写 ``result``。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import secrets
import uuid
from collections.abc import Callable
from typing import Any

from d1max_contract.dispatch import DispatchTimeout
from d1max_contract.messages import Event, MapPose
from d1max_site.ca import SAFE_ID
from d1max_site.db import SiteDB
from d1max_site.dispatcher import Dispatcher, DispatchRefused
from d1max_site.priorities import EVENT, INCIDENT_PREFIX

log = logging.getLogger(__name__)

MAX_SKEW_MS = 5 * 60_000
MERGE_WINDOW_MS = 60_000
TYPES = frozenset({"intrusion"})
_TERMINAL = {"task_done": "done", "task_failed": "failed", "task_aborted": "aborted",
             "task_preempted": "preempted"}



class IncidentError(RuntimeError):
    """登记类的错(名字、坐标不合规矩)。"""


class IncidentAuthError(RuntimeError):
    """验签不过。站点 API 回 401。"""


class IncidentDesk:
    def __init__(self, db: SiteDB, dispatcher: Dispatcher, *, now_ms: Callable[[], int],
                 merge_window_ms: int = MERGE_WINDOW_MS) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self._now = now_ms
        self.merge_window_ms = merge_window_ms
        dispatcher.on_event(self._on_event)

    # ------------------------------------------------------------ 登记

    def add_source(self, name: str) -> str:
        """登记一个事件源,返回共享密钥(十六进制,只在这一次给出)。"""
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise IncidentError(f"事件源名只许 ASCII 字母、数字、. _ -: {name!r}")
        secret = secrets.token_hex(32)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM incident_sources WHERE name=?", (name,)).fetchone():
                raise IncidentError(f"事件源 {name} 已经登记过了")
            c.execute("INSERT INTO incident_sources VALUES (?,?,?)", (name, secret, self._now()))
        return secret

    def set_intercept(self, name: str, *, map_id: str, map_version: str, x: float, y: float,
                      yaw: float) -> None:
        """拦截点记着地图与**版本**:地图重建之后旧坐标不可信,版本对不上的狗不派。"""
        if not isinstance(name, str) or not SAFE_ID.match(name):
            raise IncidentError(f"拦截点名只许 ASCII 字母、数字、. _ -: {name!r}")
        for k, v in (("map_id", map_id), ("map_version", map_version)):
            if not isinstance(v, str) or not v:
                raise IncidentError(f"要 {k}")
        xs = []
        for k, v in (("x", x), ("y", y), ("yaw", yaw)):
            try:
                ok = not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
            except OverflowError:
                ok = False
            if not ok:
                raise IncidentError(f"{k} 要是有限数")
            xs.append(float(v))
        with self.db.tx() as c:
            c.execute("INSERT INTO intercepts(name, map_id, map_version, x, y, yaw) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                      "map_id=excluded.map_id, map_version=excluded.map_version, "
                      "x=excluded.x, y=excluded.y, yaw=excluded.yaw",
                      (name, map_id, map_version, *xs))

    def map_zone(self, zone: str, intercept: str) -> None:
        if not isinstance(zone, str) or not SAFE_ID.match(zone):
            raise IncidentError(f"防区名只许 ASCII 字母、数字、. _ -: {zone!r}")
        if self.intercept(intercept) is None:
            raise IncidentError(f"没有拦截点 {intercept}")
        with self.db.tx() as c:
            c.execute("INSERT INTO zones VALUES (?,?) ON CONFLICT(zone) DO UPDATE SET "
                      "intercept=excluded.intercept", (zone, intercept))

    def intercept(self, name: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM intercepts WHERE name=?", (name,))
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------ 验签

    def verify(self, source: str | None, timestamp: str | None, signature: str | None,
               body: bytes) -> None:
        rows = self.db.query("SELECT secret FROM incident_sources WHERE name=?", (source or "",))
        if not rows:
            raise IncidentAuthError("事件源没登记")
        try:
            ts = int(timestamp or "")
        except ValueError as exc:
            raise IncidentAuthError("时间戳不是整数毫秒") from exc
        if abs(self._now() - ts) > MAX_SKEW_MS:
            raise IncidentAuthError("时间戳离站点的钟太远(超过 5 分钟)")
        want = hmac.new(bytes.fromhex(rows[0]["secret"]), str(ts).encode() + b"." + body,
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, (signature or "").lower()):
            raise IncidentAuthError("签名对不上")

    # ------------------------------------------------------------ 处理

    def pick_robot(self, point: dict[str, Any]) -> tuple[str | None, str]:
        """→ (robot_id 或 None, 都不能派时的理由)。能派 = 在线、新鲜、就绪、地图对得上、没在跑
        别的事件任务(同是 80,狗会回 busy);正在跑巡检或回程的可以(会被抢占)。离拦截点最近的优先;
        没有位姿的排后面;再按 robot_id。"""
        best: list[tuple[int, float, str]] = []
        why = []
        for r in self.dispatcher.registry.list():
            rid = r.robot_id
            if r.revoked:
                continue
            reason = self.dispatcher.dispatchable(rid, "goto")
            c = self.dispatcher.clients.get(rid)
            if not reason:
                running = c.status.task.task_id if c.status and c.status.task else ""
                if running.startswith(INCIDENT_PREFIX):
                    reason = f"{rid} 正在处理另一个事件 {running}"
            if not reason:
                caps = c.capabilities.tasks.get("patrol", {}) if c.capabilities else {}
                loaded = (caps.get("map_id"), caps.get("map_version"))
                if loaded != (point["map_id"], point["map_version"]):
                    reason = (f"{rid} 加载的地图是 {loaded[0]}:{loaded[1]},拦截点登记在 "
                              f"{point['map_id']}:{point['map_version']}")
            if reason:
                why.append(reason)
                continue
            pose = c.telemetry.pose if c.telemetry is not None else None
            if pose is not None and pose.map_id == point["map_id"]:
                best.append((0, math.hypot(pose.x - point["x"], pose.y - point["y"]), rid))
            else:
                best.append((1, 0.0, rid))
        if not best:
            return None, ";".join(why) or "没有登记的狗"
        return sorted(best)[0][2], ""

    def _insert(self, source: str, ev: dict[str, Any], outcome: str, **kw: Any) -> int:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO incidents(source, event_id, type, zone, intercept, received_at, "
                "occurred_at, outcome, robot_id, task_id, merged_into, note, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source, ev["event_id"], ev["type"], ev["zone"], kw.get("intercept"),
                 self._now(), ev.get("occurred_at"), outcome, kw.get("robot_id"),
                 kw.get("task_id"), kw.get("merged_into"), kw.get("note", ""),
                 json.dumps(ev.get("detail") or {}, ensure_ascii=False)[:4000]))
            return cur.lastrowid

    def _update(self, rid: int, **kw: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in kw)
        with self.db.tx() as c:
            c.execute(f"UPDATE incidents SET {sets} WHERE id=?", (*kw.values(), rid))

    def _row(self, rid: int) -> dict[str, Any]:
        return dict(self.db.query("SELECT * FROM incidents WHERE id=?", (rid,))[0])

    @staticmethod
    def parse(body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise IncidentError("事件体要是 JSON 对象")
        ev = {k: body.get(k) for k in ("event_id", "type", "zone", "occurred_at", "detail")}
        for k in ("event_id", "type", "zone"):
            if not isinstance(ev[k], str) or not ev[k] or len(ev[k]) > 128:
                raise IncidentError(f"事件体要有 {k}(1–128 个字符的字符串)")
        if ev["occurred_at"] is not None and (isinstance(ev["occurred_at"], bool)
                                              or not isinstance(ev["occurred_at"], int)):
            raise IncidentError("occurred_at 要是整数毫秒")
        return ev

    async def handle(self, source: str, body: Any) -> dict[str, Any]:
        """**先验签再调它**(``verify``)。返回这条事件的账。"""
        ev = self.parse(body)
        dup = self.db.query("SELECT id FROM incidents WHERE source=? AND event_id=?",
                            (source, ev["event_id"]))
        if dup:
            return self._row(dup[0]["id"]) | {"outcome": "duplicate"}
        if ev["type"] not in TYPES:
            return self._row(self._insert(source, ev, "ignored_type"))
        z = self.db.query("SELECT intercept FROM zones WHERE zone=?", (ev["zone"],))
        if not z:
            return self._row(self._insert(source, ev, "unmapped", note="防区没映射到拦截点"))
        point = self.intercept(z[0]["intercept"])
        recent = self.db.query(
            "SELECT id FROM incidents WHERE zone=? AND outcome='dispatched' AND received_at>=? "
            "ORDER BY id DESC LIMIT 1", (ev["zone"], self._now() - self.merge_window_ms))
        if recent:
            return self._row(self._insert(source, ev, "merged", intercept=point["name"],
                                          merged_into=recent[0]["id"]))
        rid, why = self.pick_robot(point)
        if rid is None:
            row = self._row(self._insert(source, ev, "no_robot", intercept=point["name"],
                                         note=why))
            self._publish(row)
            return row
        task_id = f"{INCIDENT_PREFIX}{uuid.uuid4().hex[:12]}"
        iid = self._insert(source, ev, "dispatched", intercept=point["name"], robot_id=rid,
                           task_id=task_id, note="已发出")
        target = MapPose(map_id=point["map_id"], map_version=point["map_version"],
                         frame_id="map", x=point["x"], y=point["y"], yaw=point["yaw"]).to_wire()
        try:
            r = await self.dispatcher.goto(rid, target, None, issued_by=f"incident:{source}",
                                           priority=EVENT, task_id=task_id)
            if r["ack"]["result"] != "accepted":
                self._update(iid, outcome="dispatch_failed",
                             note=f"狗回 {r['ack']['result']}: {r['ack'].get('reason', '')}")
        except DispatchRefused as exc:
            self._update(iid, outcome="dispatch_failed", note=str(exc))
        except DispatchTimeout:
            self._update(iid, note="回执超时:可能已在路上")
        row = self._row(iid)
        self._publish(row)
        return row

    def _publish(self, row: dict[str, Any]) -> None:
        self.dispatcher.feed.publish({"kind": "incident", "incident": row})

    def _on_event(self, robot_id: str, e: Event) -> None:
        result = _TERMINAL.get(e.kind)
        task_id = e.data.get("task_id") if isinstance(e.data, dict) else None
        if result and task_id and task_id.startswith(INCIDENT_PREFIX):
            with self.db.tx() as c:
                c.execute("UPDATE incidents SET result=? WHERE task_id=?", (result, task_id))

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM incidents ORDER BY id DESC LIMIT ?",
                                               (limit,))]
