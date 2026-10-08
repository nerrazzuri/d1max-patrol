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
(防区没映射)、``unreachable``(W23:拦截点走不到)、``ignored_type``(类型不认)、``disarmed``(W20:这个防区按当前模式撤防,只记账、不派狗、
不报告警)、``no_robot``、``dispatch_failed``;派出去的那条,结果由事件回写 ``result``。
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
import threading
import uuid
from collections.abc import Callable
from typing import Any

from d1max_contract.charging import LOW_PCT
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
#: 告诉值守的人(W16):一条入侵落到这几种去向之一就报一次告警(``on_outcome``)。出动了的、
#: 合并进已出动的,
#: 说「有入侵、谁去了」;没狗、没映射、派失败,说「有入侵、没狗去」。``duplicate``、``ignored_type``
#: 不报。
ALERT_OUTCOMES = frozenset({"dispatched", "merged", "no_robot", "unmapped", "dispatch_failed",
                            "unreachable"})
#: 每个事件源每分钟最多收这么多条(W16,W00c2c 取舍 6):摄像头抽风、密钥漏了被人刷,不能把站点和狗
#: 拖垮。超了回 429,记日志,报一次告警(``on_throttled``)。一个庄园的入侵事件远到不了这个数。
RATE_PER_MIN = 30
#: 狗到了拦截点拍一张,用哪个相机(W17:现场照片挂在告警上)。狗没报能拍就不拍。
ARRIVAL_CAMERA = "front"
#: 告警没报成的入侵,补报多久以内的(W16 外审)。
RETELL_MS = 24 * 3600_000
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
        #: 一条入侵有了去向就报告警(W16):``(这一条的账)``。站点主程序接到告警源上。每条只报一次:
        #: **报成了**才在账里记 ``told_ms``;报不出去的(告警库一时写不进)留着,``retell`` 每拍补
        #: (W16 外审:原先报之前就记「说过了」,一失败这条入侵就再没有告警)。
        self.on_outcome: Callable[[dict[str, Any]], None] | None = None
        self._tell_lock = threading.Lock()
        self._telling: set[int] = set()                # 正在报的(接口线程与事件循环都会报)
        #: 事件源被限流了(W16):``(事件源名)``。
        self.on_throttled: Callable[[str], None] | None = None
        #: 布防模式(W20,``modes.ArmingDesk``)。没接(老测试、命令行)就当全布防。
        self.arming: Any = None
        #: 正在驱离的狗(W22,``DeterrenceDesk.busy``):不派它去别的拦截点。
        self.busy: Callable[[], set[str]] | None = None
        #: 回充(W13,``ChargeDesk.refuse``):正在回充、电量不够的不派 → 理由;能派 → 空串。
        self.charging: Callable[[str], str] | None = None
        #: 拦截点走不走得到(W23,``intercept_reach.InterceptReach``)。没接(命令行、老测试)就不查。
        self.reach: Any = None
        #: 每个事件源最近一分钟收过的时刻。接口是多线程的:「清掉一分钟前的、判断、
        #: 记一笔」得在一把锁里
        #: 一次做完(W16 外审:不加锁 40 个并发请求全放行)。
        self._rate_lock = threading.Lock()
        self._recent: dict[str, list[int]] = {}
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
        from d1max_site.sealbox import seal_value
        secret = secrets.token_hex(32)
        sealed = seal_value(self.db, secret)              # 加密落库(W30,决策 43)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM incident_sources WHERE name=?", (name,)).fetchone():
                raise IncidentError(f"事件源 {name} 已经登记过了")
            c.execute("INSERT INTO incident_sources VALUES (?,?,?)", (name, sealed, self._now()))
        return secret

    def rotate_secret(self, name: str) -> str:
        """换一个事件源的共享密钥(W16):旧的当场作废,返回新的(只这一次给出)。"""
        from d1max_site.sealbox import seal_value
        secret = secrets.token_hex(32)
        sealed = seal_value(self.db, secret)
        with self.db.tx() as c:
            cur = c.execute("UPDATE incident_sources SET secret=? WHERE name=?", (sealed, name))
            if cur.rowcount == 0:
                raise IncidentError(f"没有事件源 {name}")
        return secret

    def remove_source(self, name: str) -> None:
        """删一个事件源(W16):它的回调从此验签不过。已经入账的事件留着。"""
        with self.db.tx() as c:
            if c.execute("DELETE FROM incident_sources WHERE name=?", (name,)).rowcount == 0:
                raise IncidentError(f"没有事件源 {name}")

    def sources(self) -> list[dict[str, Any]]:
        """登记过的事件源(不带密钥)。"""
        return [{"name": r["name"], "created_at": r["created_at"]} for r in self.db.query(
            "SELECT name, created_at FROM incident_sources ORDER BY name")]

    def allow(self, source: str) -> bool:
        """这个事件源这一分钟还能不能再收一条(W16 限流)。验签过了再调。超了报一次(``on_throttled``,
        这一分钟里只报第一下)。"""
        first_over = False
        with self._rate_lock:
            now = self._now()
            hits = [t for t in self._recent.get(source, []) if now - t < 60_000]
            ok = len(hits) < RATE_PER_MIN
            if ok or len(hits) == RATE_PER_MIN:        # 收下的,或第一下超(之后同一分钟不再说)
                first_over = not ok
                hits.append(now)
            self._recent[source] = hits
        if first_over:                                 # 写日志、报告警在锁外:不在锁里碰库
            log.warning("事件源 %s 一分钟超过 %d 条:限流", source, RATE_PER_MIN)
            if self.on_throttled is not None:
                try:
                    self.on_throttled(source)
                except Exception:
                    log.exception("事件源 %s 限流的告警报不出去", source)
        return ok

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
        problem = note = key = ""
        if self.reach is not None:
            # W23:设的时候就查;走不到、站不下、不在图上的不收(说原因)。**指纹在查之前取**
            # (W23 外审):查的这段时间禁行区、待命点改了,存下的指纹就跟现在的对不上,对账会重查
            key = self.reach.key(map_id, map_version, xs[0], xs[1])
            r = self.reach.check(name, map_id, map_version, xs[0], xs[1])
            if r.problem:
                raise IncidentError(f"拦截点 {name} 设不了:{r.problem}")
            note = r.note
        with self.db.tx() as c:
            c.execute("INSERT INTO intercepts(name, map_id, map_version, x, y, yaw, reach, "
                      "reach_note, reach_key) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO "
                      "UPDATE SET map_id=excluded.map_id, map_version=excluded.map_version, "
                      "x=excluded.x, y=excluded.y, yaw=excluded.yaw, reach=excluded.reach, "
                      "reach_note=excluded.reach_note, reach_key=excluded.reach_key",
                      (name, map_id, map_version, *xs, problem, note, key))

    def recheck_intercepts(self) -> int:
        """站点每 30 秒(杂事线程)对账一次:指纹变了的(地图、禁行区、待命点改过)重查。回重查了几个。
        派单只读这里查好的结果 —— 事件循环里不规划。"""
        if self.reach is None:
            return 0
        n = 0
        for p in self.db.query("SELECT * FROM intercepts"):
            key = self.reach.key(p["map_id"], p["map_version"], p["x"], p["y"])
            if key == p["reach_key"]:
                continue
            r = self.reach.check(p["name"], p["map_id"], p["map_version"], p["x"], p["y"])
            with self.db.tx() as c:
                c.execute("UPDATE intercepts SET reach=?, reach_note=?, reach_key=? WHERE name=? "
                          "AND x=? AND y=? AND map_version=?",
                          (r.problem, r.note, key, p["name"], p["x"], p["y"], p["map_version"]))
            if r.problem and r.problem != p["reach"]:
                log.warning("拦截点 %s 现在走不到了: %s", p["name"], r.problem)
            n += 1
        return n

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

    def intercepts(self) -> dict[str, list[dict[str, Any]]]:
        """拦截点与防区(W16,手机用)。"""
        latest = self._latest_versions()
        return {"intercepts": [dict(r) | {"newer_version": latest.get(r["map_id"])
                                          if latest.get(r["map_id"]) not in (None, r["map_version"])
                                          else None}
                               for r in self.db.query("SELECT * FROM intercepts ORDER BY name")],
                "zones": [dict(r) for r in self.db.query("SELECT * FROM zones ORDER BY zone")]}

    def _latest_versions(self) -> dict[str, str]:
        """每张图站点上最新的那一版(W23:拦截点登记在旧版本上的,手机上标「要在新图上重设」)。"""
        try:
            rows = self.db.query("SELECT map_id, version FROM maps ORDER BY created_ms")
        except sqlite3.Error:
            return {}
        return {r["map_id"]: r["version"] for r in rows}

    def remove_intercept(self, name: str) -> None:
        """删一个拦截点(W16)。还有防区指着它就不删(不然那几个防区的入侵就成了「没映射」,不知不觉)。"""
        with self.db.tx() as c:
            zs = [r["zone"] for r in c.execute("SELECT zone FROM zones WHERE intercept=?",
                                               (name,)).fetchall()]
            if zs:
                raise IncidentError(f"防区 {'、'.join(zs)} 还指着拦截点 {name}:先改那几个防区")
            if c.execute("DELETE FROM intercepts WHERE name=?", (name,)).rowcount == 0:
                raise IncidentError(f"没有拦截点 {name}")

    def unmap_zone(self, zone: str) -> None:
        """防区不再派狗(W16):之后这个防区的入侵记「没映射」、照样报告警。"""
        with self.db.tx() as c:
            if c.execute("DELETE FROM zones WHERE zone=?", (zone,)).rowcount == 0:
                raise IncidentError(f"没有防区 {zone}")

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
        from d1max_site.sealbox import open_value
        key = bytes.fromhex(open_value(self.db, rows[0]["secret"])) if rows else b"\0" * 32
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
        别的事件任务(同是 80,狗会回 busy);正在跑巡检或回程的可以(会被抢占)。定位不行的
        ``dispatchable`` 已经挡掉了(就绪里有定位)。电量 ≤ 30% 的排最后(W28,决策 46:只有它能去时照派,
        入侵优先);再离拦截点最近的优先;没有位姿的排后面;再按 robot_id。"""
        best: list[tuple[bool, int, float, str]] = []
        why = []
        for r in self.dispatcher.registry.list():
            rid = r.robot_id
            if r.revoked:
                continue
            reason = self.dispatcher.dispatchable(rid, "goto")
            c = self.dispatcher.clients.get(rid)
            if not reason and self.dispatcher.autonomy(rid) != "autonomous":
                # W00c6i:事件多半在夜里、没人在场;要人监护的真狗不派。
                reason = f"{rid} 要人监护,不接事件派遣"
            if not reason:
                running = c.status.task.task_id if c.status and c.status.task else ""
                if running.startswith(INCIDENT_PREFIX):
                    reason = f"{rid} 正在处理另一个事件 {running}"
                elif rid in self._open_incident_robots():
                    # 派了还没回结果(或正等回执):狗报的 status 可能还没跟上 —— 以账为准。
                    reason = f"{rid} 正在处理另一个事件(已派出、未结束)"
            if not reason and self.busy is not None and rid in self.busy():
                reason = f"{rid} 正在驱离(W22)"
            if not reason and self.charging is not None:
                reason = self.charging(rid)              # W13:在充电、电量不到 50% 不派(决策 45)
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
            low = c.telemetry is not None and c.telemetry.battery_pct <= LOW_PCT
            if pose is not None and pose.map_id == point["map_id"]:
                best.append((low, 0, math.hypot(pose.x - point["x"], pose.y - point["y"]), rid))
            else:
                best.append((low, 1, 0.0, rid))
        if not best:
            return None, ";".join(why) or "没有登记的狗"
        return sorted(best)[0][3], ""

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
                    "outcome, note, detail, told_ms) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (source, ev["event_id"], ev["type"], ev["zone"], self._now(),
                     ev.get("occurred_at"), "received", "",
                     json.dumps(ev.get("detail") or {}, ensure_ascii=False))).lastrowid
            except sqlite3.IntegrityError:
                first = c.execute("SELECT * FROM incidents WHERE source=? AND event_id=?",
                                  (source, ev["event_id"])).fetchone()
                return dict(first) | {"outcome": "duplicate"}
            if ev["type"] not in TYPES:
                return self._set(c, iid, outcome="ignored_type")
            if self.arming is not None:
                on, mode = self.arming.armed(ev["zone"])
                if not on:
                    from d1max_site.modes import LABEL
                    return self._set(c, iid, outcome="disarmed",
                                     note=f"{LABEL[mode]}模式:这个防区撤防,只记录")
            z = c.execute("SELECT intercept FROM zones WHERE zone=?", (ev["zone"],)).fetchone()
            if z is None:
                return self._set(c, iid, outcome="unmapped", note="防区没映射到拦截点")
            point = self.intercept(z["intercept"])
            if point.get("reach"):
                # W23:查好的结果说走不到(地图、禁行区改过之后):不派,照样报「没狗去」
                return self._set(c, iid, outcome="unreachable", intercept=point["name"],
                                 note=f"拦截点走不到:{point['reach']}")
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
                                           priority=EVENT, task_id=row["task_id"],
                                           photo=ARRIVAL_CAMERA)
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
        """首条派失败:并进它的第一条提升为新的出动(重新选狗),其余的改并到这一条。
        **提升也是一次新的出动,要再看一次模式**(W20 外审):并进来的时候布防、首条失败时已经撤防的,
        并进它的全部改记 ``disarmed``,一条都不派(同防区才会合并,所以是一起撤的)。
        **拦截点也要再看一次**(W23 外审):首条等回执期间拦截点被对账成走不到的,并进来的全部记
        ``unreachable``(照样报「没狗去」),不派。"""
        disarmed: list[int] = []
        with self.db.tx() as c:
            nxt = c.execute("SELECT id, intercept, zone FROM incidents WHERE merged_into=? AND "
                            "outcome='merged' ORDER BY id LIMIT 1", (failed,)).fetchone()
            if nxt is None:
                return
            on, mode = self.arming.armed(nxt["zone"]) if self.arming is not None else (True, "")
            point = self.intercept(nxt["intercept"])
            stop: tuple[str, str] | None = None
            if not on:
                from d1max_site.modes import LABEL
                stop = ("disarmed", f"首条没派成时已是{LABEL[mode]}模式:这个防区撤防,不再派")
            elif point is not None and point.get("reach"):
                stop = ("unreachable", f"拦截点走不到:{point['reach']}")
            if stop is not None:
                disarmed = [r["id"] for r in c.execute(
                    "SELECT id FROM incidents WHERE merged_into=? AND outcome='merged' ORDER BY id",
                    (failed,)).fetchall()]
                c.execute("UPDATE incidents SET outcome=?, note=? WHERE merged_into=? "
                          "AND outcome='merged'", (*stop, failed))
            else:
                got = self._reserve(c, nxt["id"], point) if point else self._set(
                    c, nxt["id"], outcome="unmapped", note="拦截点没了")
                c.execute("UPDATE incidents SET merged_into=? WHERE merged_into=? AND "
                          "outcome='merged' AND id<>?", (nxt["id"], failed, nxt["id"]))
                c.execute("UPDATE incidents SET merged_into=NULL WHERE id=?", (nxt["id"],))
        if disarmed:
            for i in disarmed:
                self._publish(self._row(i))
            return
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
        if row.get("outcome") in ALERT_OUTCOMES:
            self._tell(row["id"])

    def _tell(self, rid: int) -> bool:
        """报这条入侵的告警,报成了记 ``told_ms``。已经说过的、别的线程正在报的不报。
        报告警失败不许带走
        派遣(同排程):记日志、不记 ``told_ms``,``retell`` 下一拍再报。回报成了没有。"""
        if self.on_outcome is None:
            return False
        with self._tell_lock:
            row = self._row(rid)
            if rid in self._telling or row.get("told_ms") is not None \
                    or row.get("outcome") not in ALERT_OUTCOMES:
                return False
            self._telling.add(rid)
        try:
            if row.get("outcome") == "merged" and row.get("merged_into") is not None:
                lead = self._row(row["merged_into"])        # 并进来的:说是谁在去、哪一趟
                row = row | {"robot_id": lead.get("robot_id"), "task_id": lead.get("task_id")}
            point = self.intercept(row["intercept"]) if row.get("intercept") else None
            if point is not None:                           # 告警带拦截点在图上的位置(W17)
                row = row | {"intercept_pose": {k: point[k] for k in (
                    "name", "map_id", "map_version", "x", "y", "yaw")}}
            try:
                self.on_outcome(row)
            except Exception:
                log.exception("入侵 %s 的告警报不出去:下一拍再报", rid)
                return False
            with self.db.tx() as c:
                c.execute("UPDATE incidents SET told_ms=? WHERE id=?", (self._now(), rid))
            return True
        finally:
            with self._tell_lock:
                self._telling.discard(rid)

    def retell(self) -> int:
        """补报告警没报成的入侵(上次报不出去的、站点重启前没来得及报的)。只补最近 ``RETELL_MS``
        里的:
        更早的再报出来也只是噪音(事件页里看得见)。站点告警循环每拍调。回补成了几条。"""
        if self.on_outcome is None:
            return 0
        rows = self.db.query(
            f"SELECT id FROM incidents WHERE told_ms IS NULL AND received_at>=? AND outcome IN "
            f"({','.join('?' * len(ALERT_OUTCOMES))}) ORDER BY id",
            (self._now() - RETELL_MS, *sorted(ALERT_OUTCOMES)))
        return sum(self._tell(r["id"]) for r in rows)

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
