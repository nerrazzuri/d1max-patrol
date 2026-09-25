"""外部事件派遣(W00c2c,吸收 W16)。CCTV/AI 等外部系统报「某防区有入侵」→ 站点查出拦截点 →
选一台狗 → 以事件优先级派 ``goto`` 去拦截点。到了之后由待命点那套(W00c2b)自动回。

入口是**签名的 HTTP 回调**(设计决定一 A):每个事件源登记后拿到一个共享密钥;请求带
``X-D1MAX-Source``、``X-D1MAX-Timestamp``(毫秒)、``X-D1MAX-Signature``
= ``hex(HMAC-SHA256(密钥, 时间戳 + "." + 原始请求体))``。时间差超 5 分钟拒;同一
``(source, event_id)`` 只处理一次;**同一防区 60 s 内的后续事件合并到第一条**,不重复出动。

**占位是原子的**(外审阻断 2):事件身份入账(唯一约束冲突 → ``duplicate``)、同防区合并判定、
选狗并占住它,在库的同一个事务、同一把锁里一步做完,中间没有 ``await``。还在等回执的
(``dispatching``)也算合并目标;它最终派失败了,就把并进来的第一条**提升**成新的出动,其余的改并到它。

每条事件都进 ``incidents`` 表,去向:``dispatched``、``merged``、``duplicate``、``unmapped``
(防区没映射)、``ignored_type``(类型不认)、``no_robot``、``dispatch_failed``;派出去的那条,结果由
事件回写 ``result``。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import sqlite3
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
#: 一台狗派出去的事件任务多久没回结果就不再算它「在处理事件」(防一条丢了终态的任务永远占着狗)。
OPEN_INCIDENT_MS = 30 * 60_000
MAX_DETAIL_BYTES = 4000
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_DIGITS = re.compile(r"[1-9][0-9]{0,15}")
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
        #: 首条派失败后把并进来的事件提升为新出动 —— 在后台跑,不拖住首条那个 HTTP 请求。
        self._followups: set[asyncio.Task] = set()
        dispatcher.on_event(self._on_event)
        # 上次站点停掉时还在等回执的(dispatching):没人再落账了,不收的话会占住狗与防区到
        # OPEN_INCIDENT_MS。落成失败,写明狗可能已在路上(它的终态事件照样回写 result)。
        with db.tx() as c:
            c.execute("UPDATE incidents SET outcome='dispatch_failed', "
                      "note='站点重启时还在等回执:狗可能已在路上' WHERE outcome='dispatching'")

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
        if not isinstance(intercept, str) or not SAFE_ID.match(intercept):
            raise IncidentError(f"拦截点名不合规矩: {intercept!r}")
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
        """验签。**失败的原因只进异常消息(站点记日志),对外一律「验签没过」**:不让人靠不同的
        报错探出哪些事件源登记过。未登记的源也照算一次 HMAC,时间上也不露。
        签的是**原样的时间戳头** + ``.`` + 原始请求体;时间戳只认十进制纯数字(毫秒)。"""
        rows = self.db.query("SELECT secret FROM incident_sources WHERE name=?",
                             (source if isinstance(source, str) else "",))
        key = bytes.fromhex(rows[0]["secret"]) if rows else b"\0" * 32
        stamp = timestamp if isinstance(timestamp, str) else ""
        want = hmac.new(key, stamp.encode("latin-1", "replace") + b"." + body,
                        hashlib.sha256).digest()
        sig = signature if isinstance(signature, str) else ""
        got = bytes.fromhex(sig) if _HEX64.fullmatch(sig) else b""
        if not rows:
            raise IncidentAuthError("事件源没登记")
        if not _DIGITS.fullmatch(stamp):
            raise IncidentAuthError("时间戳不是十进制纯数字毫秒")
        if abs(self._now() - int(stamp)) > MAX_SKEW_MS:
            raise IncidentAuthError("时间戳离站点的钟太远(超过 5 分钟)")
        if not hmac.compare_digest(want, got):
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
                elif rid in self._open_incident_robots():
                    # 派了还没回结果(或正等回执):狗报的 status 可能还没跟上 —— 以账为准。
                    reason = f"{rid} 正在处理另一个事件(已派出、未结束)"
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

    def _open_incident_robots(self) -> set[str]:
        rows = self.db.query(
            "SELECT DISTINCT robot_id FROM incidents WHERE outcome IN "
            "('dispatching', 'dispatched') AND result IS NULL AND robot_id IS NOT NULL "
            "AND received_at>=?",
            (self._now() - OPEN_INCIDENT_MS,))
        return {r["robot_id"] for r in rows}

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
        oa = ev["occurred_at"]
        if oa is not None and (isinstance(oa, bool) or not isinstance(oa, int)
                               or not 0 <= oa < 2**53):
            raise IncidentError("occurred_at 要是 0..2^53 的整数毫秒")
        if ev["detail"] is not None:
            if len(json.dumps(ev["detail"], ensure_ascii=False).encode()) > MAX_DETAIL_BYTES:
                ev["detail"] = {"truncated": True}      # 截一半的 JSON 读不回来,整个换掉
        return ev

    async def handle(self, source: str, body: Any) -> dict[str, Any]:
        """**先验签再调它**(``verify``)。返回这条事件的账;每条都推给 SSE。"""
        row = await self._handle(source, body)
        self._publish(row)
        return row

    async def _handle(self, source: str, body: Any) -> dict[str, Any]:
        ev = self.parse(body)
        got = self._claim(source, ev)
        if isinstance(got, dict):
            return got
        await self._dispatch(got)
        return self._row(got)

    def _claim(self, source: str, ev: dict[str, Any]) -> dict[str, Any] | int:
        """一步做完的占位(同一事务、同一把锁,中间不 await):事件身份入账 → 类型、防区 → 同防区
        合并 → 选狗并占住。返回终局的账(不用派单),或要派单的那条事件的 id(已记 ``dispatching``)。"""
        with self.db.tx() as c:
            try:
                iid = c.execute(
                    "INSERT INTO incidents(source, event_id, type, zone, received_at, occurred_at, "
                    "outcome, note, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                    (source, ev["event_id"], ev["type"], ev["zone"], self._now(),
                     ev.get("occurred_at"), "received", "",
                     json.dumps(ev.get("detail") or {}, ensure_ascii=False))).lastrowid
            except sqlite3.IntegrityError:
                first = c.execute("SELECT * FROM incidents WHERE source=? AND event_id=?",
                                  (source, ev["event_id"])).fetchone()
                return dict(first) | {"outcome": "duplicate"}
            if ev["type"] not in TYPES:
                return self._set(c, iid, outcome="ignored_type")
            z = c.execute("SELECT intercept FROM zones WHERE zone=?", (ev["zone"],)).fetchone()
            if z is None:
                return self._set(c, iid, outcome="unmapped", note="防区没映射到拦截点")
            point = self.intercept(z["intercept"])
            leader = self._merge_target(c, ev["zone"], exclude=iid)
            if leader is not None:
                return self._set(c, iid, outcome="merged", intercept=point["name"],
                                 merged_into=leader)
            return self._reserve(c, iid, point)

    def _merge_target(self, c: sqlite3.Connection, zone: str, *, exclude: int) -> int | None:
        """同防区窗口内**已出动或正在出动**(等回执)的那条。失败的不算。"""
        row = c.execute(
            "SELECT id FROM incidents WHERE zone=? AND id<>? AND outcome IN "
            "('dispatching', 'dispatched') AND received_at>=? ORDER BY id DESC LIMIT 1",
            (zone, exclude, self._now() - self.merge_window_ms)).fetchone()
        return row["id"] if row else None

    def _reserve(self, c: sqlite3.Connection, iid: int,
                 point: dict[str, Any]) -> dict[str, Any] | int:
        """选狗并占住(``dispatching``):同时到的另一条事件据此挑开它。"""
        rid, why = self.pick_robot(point)
        if rid is None:
            return self._set(c, iid, outcome="no_robot", intercept=point["name"], note=why)
        self._set(c, iid, outcome="dispatching", intercept=point["name"], robot_id=rid,
                  task_id=f"{INCIDENT_PREFIX}{uuid.uuid4().hex[:12]}", note="发送中")
        return iid

    @staticmethod
    def _set(c: sqlite3.Connection, iid: int, **kw: Any) -> dict[str, Any]:
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE incidents SET {sets} WHERE id=?", (*kw.values(), iid))
        return dict(c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone())

    async def _dispatch(self, iid: int) -> None:
        """给已占位(``dispatching``)的那条派 ``goto``,按回执落账;派失败了提升并进来的事件。"""
        row = self._row(iid)
        point = self.intercept(row["intercept"])
        target = MapPose(map_id=point["map_id"], map_version=point["map_version"],
                         frame_id="map", x=point["x"], y=point["y"], yaw=point["yaw"]).to_wire()
        try:
            r = await self.dispatcher.goto(row["robot_id"], target, None,
                                           issued_by=f"incident:{row['source']}",
                                           priority=EVENT, task_id=row["task_id"])
            if r["ack"]["result"] == "accepted":
                self._update(iid, outcome="dispatched", note="已出动")
            else:
                self._update(iid, outcome="dispatch_failed",
                             note=f"狗回 {r['ack']['result']}: {r['ack'].get('reason', '')}")
        except DispatchRefused as exc:
            self._update(iid, outcome="dispatch_failed", note=str(exc))
        except DispatchTimeout:
            self._update(iid, outcome="dispatched", note="回执超时:可能已在路上")
        except asyncio.CancelledError:
            # 站点收尾时取消(IncidentDesk.close):不许一直挂在 dispatching。
            self._update(iid, outcome="dispatch_failed", note="站点收尾时取消:狗可能已在路上")
            raise
        except Exception as exc:
            log.exception("事件 %s 派单炸了", row["event_id"])
            self._update(iid, outcome="dispatch_failed", note=f"站点内部错误: {exc}")
        if self._row(iid)["outcome"] == "dispatch_failed":
            self._promote_follower(iid)

    def _promote_follower(self, failed: int) -> None:
        """首条派失败:并进它的第一条提升为新的出动(重新选狗),其余的改并到这一条。"""
        with self.db.tx() as c:
            nxt = c.execute("SELECT id, intercept FROM incidents WHERE merged_into=? AND "
                            "outcome='merged' ORDER BY id LIMIT 1", (failed,)).fetchone()
            if nxt is None:
                return
            point = self.intercept(nxt["intercept"])
            got = self._reserve(c, nxt["id"], point) if point else self._set(
                c, nxt["id"], outcome="unmapped", note="拦截点没了")
            c.execute("UPDATE incidents SET merged_into=? WHERE merged_into=? AND "
                      "outcome='merged' AND id<>?", (nxt["id"], failed, nxt["id"]))
            c.execute("UPDATE incidents SET merged_into=NULL WHERE id=?", (nxt["id"],))
        self._publish(self._row(nxt["id"]))
        if isinstance(got, int):
            task = asyncio.get_running_loop().create_task(self._dispatch_and_publish(got))
            self._followups.add(task)
            task.add_done_callback(self._followups.discard)

    async def _dispatch_and_publish(self, iid: int) -> None:
        await self._dispatch(iid)
        self._publish(self._row(iid))

    async def close(self) -> None:
        """收掉还在等回执的提升派单。在关派遣器之前调(同 ``StandbyManager.close``)。"""
        for t in list(self._followups):
            t.cancel()
        for t in list(self._followups):
            try:
                await t
            except BaseException:  # noqa: BLE001 - 取消与其他异常都只是收尾
                pass

    async def drain(self) -> None:
        """等后台的提升派单都跑完(测试与收尾用)。"""
        while self._followups:
            await asyncio.gather(*list(self._followups), return_exceptions=True)

    def _publish(self, row: dict[str, Any]) -> None:
        self.dispatcher.feed.publish({"kind": "incident", "incident": row})

    def _on_event(self, robot_id: str, e: Event) -> None:
        result = _TERMINAL.get(e.kind)
        task_id = e.data.get("task_id") if isinstance(e.data, dict) else None
        if result and task_id and task_id.startswith(INCIDENT_PREFIX):
            with self.db.tx() as c:
                c.execute("UPDATE incidents SET result=? WHERE task_id=?", (result, task_id))
            for r in self.db.query("SELECT id FROM incidents WHERE task_id=?", (task_id,)):
                self._publish(self._row(r["id"]))

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM incidents ORDER BY id DESC LIMIT ?",
                                               (limit,))]
