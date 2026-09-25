"""站点的遥控台(W00c5c,受决策 7 约束)。

「允许站点转发经过认证、持有控制租约的人类实时操作输入;禁止站点自主生成、重放或在操作者断线后
延续运动指令。」这里只做**转发**:

- **一台狗至多一个租约**。有人握着时别人来开回 409 并说是谁;**管理员可以写明理由强制接管**:旧连接
  先被告知、关掉,狗先停(旧租约放掉、等狗那头的遥控任务停稳),再授予新的一代。
- **连接就是租约的载体**:手机的 WebSocket 断了(或判死)→ 当场发零速帧、放租;租约每 ``renew_s``
  续一次,续不上就结束。狗那头租约到期、帧有效期到期也会自己停 —— 站点这头不在断线后延续任何东西。
- **帧**:手机只给 ``vx/wz``;站点按狗报的遥控限速(HAL 能力的一半)夹一次,加上代次、单调序号、
  站点发出时刻、300 ms 有效期,经 QoS 0 的专用主题发出去。**不缓存、不插值、不补发**。
- **没画面不许动**:``video_ok(robot)`` 为假时不转发运动,只发一次零速并告诉手机。
- **审计**:谁、哪台狗、租约起止、结束原因、接管人与理由。**不记每一帧摇杆值。**
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from d1max_contract.messages import AckResult
from d1max_contract.teleop import (
    FRAME_TTL_DEFAULT_MS,
    LEASE_TTL_DEFAULT_MS,
    TeleopFrame,
    TeleopLease,
)
from d1max_site.dispatcher import TELEOP_TASK_PREFIX, DispatchRefused

log = logging.getLogger(__name__)


class TeleopRefused(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Session:
    robot_id: str
    epoch: int
    operator: str
    max_vx: float
    max_wz: float
    started_ms: int
    row_id: int
    ws: Any = None
    seq: int = 0
    #: 上一帧有没有在动(用来在「没画面」时只发一次零速)。
    moving: bool = False
    video_told: bool | None = None
    renew_failures: int = 0
    ended: threading.Event = field(default_factory=threading.Event)
    end_reason: str = ""
    #: 发帧的锁:序号 + 发出去是一步(手机那条线程和续租线程都会发);结束之后只许发那一帧零速。
    send_lock: threading.Lock = field(default_factory=threading.Lock)


class TeleopDesk:
    def __init__(self, dispatcher, loop, *, audit, now_ms: Callable[[], int],
                 video_ok: Callable[[str], bool], lease_ttl_ms: int = LEASE_TTL_DEFAULT_MS,
                 frame_ttl_ms: int = FRAME_TTL_DEFAULT_MS, renew_s: float = 1.0,
                 wait_released_s: float = 6.0) -> None:
        self.dispatcher = dispatcher
        self.loop = loop
        self.audit = audit
        self._now = now_ms
        self._video_ok = video_ok
        self.lease_ttl_ms = lease_ttl_ms
        self.frame_ttl_ms = frame_ttl_ms
        self.renew_s = renew_s
        self.wait_released_s = wait_released_s
        self._sessions: dict[str, Session] = {}
        #: 正在开租约的狗(还没到狗那头要回来):同一台狗再来一个当场 409。
        self._opening: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 开

    def active(self, robot_id: str) -> Session | None:
        with self._lock:
            s = self._sessions.get(robot_id)
            return s if s is not None and not s.ended.is_set() else None

    def open(self, robot_id: str, user, *, takeover_reason: str = "") -> Session:
        """给 ``user`` 开一个遥控租约。拒绝抛 :class:`TeleopRefused`(HTTP 状态码 + 原因)。

        锁里只做判断、占位;接管的等待(最多 ``wait_released_s``)和问狗都在锁外 —— 这段时间里
        同一台狗的停车、查询、别的狗的遥控都不许被挡住。"""
        name, role = str(user), getattr(user, "role", "")
        with self._lock:
            if robot_id in self._opening:
                raise TeleopRefused(409, f"{robot_id} 正在开遥控,稍后再试")
            cur = self.active(robot_id)
            if cur is not None:
                if not takeover_reason:
                    raise TeleopRefused(409, f"{cur.operator} 正在遥控 {robot_id}")
                if role != "admin":
                    raise TeleopRefused(403, "只有管理员能强制接管")
            self._opening.add(robot_id)
        try:
            if cur is not None:
                self._takeover(cur, by=name, reason=takeover_reason)
            return self._grant(robot_id, name, takeover_reason)
        finally:
            with self._lock:
                self._opening.discard(robot_id)

    def _grant(self, robot_id: str, name: str, takeover_reason: str) -> Session:
        if not self._video_ok(robot_id):
            raise TeleopRefused(409, f"{robot_id} 没有画面:没画面不许动")
        caps = self._caps(robot_id)
        epoch, row_id = self._new_lease_row(robot_id, name, takeover_reason)
        try:
            r = self.loop.call(lambda: self.dispatcher.teleop_grant(
                robot_id, lease_epoch=epoch, operator=name, lease_ttl_ms=self.lease_ttl_ms,
                issued_by=name), timeout_s=self.dispatcher.ack_timeout_s + 5)
        except DispatchRefused as exc:
            self._end_row(row_id, "refused")
            raise TeleopRefused(409, str(exc)) from exc
        except Exception as exc:
            self._end_row(row_id, "refused")
            raise TeleopRefused(502, f"狗没回话: {type(exc).__name__}") from exc
        ack = r.get("ack", {})
        if ack.get("result") != AckResult.ACCEPTED.value:
            self._end_row(row_id, "refused")
            raise TeleopRefused(409, f"狗没接遥控: {ack.get('result')} {ack.get('reason', '')}")
        # 开的时候查过画面是有的:之后只在变了的时候告诉手机。
        s = Session(robot_id=robot_id, epoch=epoch, operator=name, max_vx=caps[0],
                    max_wz=caps[1], started_ms=self._now(), row_id=row_id, video_told=True)
        with self._lock:
            self._sessions[robot_id] = s
        self._audit(name, "teleop_start", robot_id, {"lease_epoch": epoch,
                                                     "takeover_reason": takeover_reason})
        self._ensure_thread()
        return s

    def _caps(self, robot_id: str) -> tuple[float, float]:
        c = self.dispatcher.clients.get(robot_id)
        t = (c.capabilities.tasks.get("teleop") if c is not None and c.capabilities else None) or {}
        vx, wz = t.get("max_vx"), t.get("max_wz")
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (vx, wz)):
            raise TeleopRefused(409, f"{robot_id} 没报遥控能力")
        return float(vx), float(wz)

    def _new_lease_row(self, robot_id: str, operator: str, takeover_reason: str
                       ) -> tuple[int, int]:
        with self.dispatcher.db.tx() as c:
            row = c.execute("SELECT MAX(epoch) AS e FROM teleop_leases WHERE robot_id=?",
                            (robot_id,)).fetchone()
            epoch = (row["e"] or 0) + 1
            rid = c.execute("INSERT INTO teleop_leases(robot_id, epoch, operator, started_at, "
                            "takeover_reason) VALUES (?,?,?,?,?)",
                            (robot_id, epoch, operator, self._now(),
                             takeover_reason or None)).lastrowid
        return epoch, rid

    def _end_row(self, row_id: int, reason: str, *, by: str = "", takeover_reason: str = ""
                 ) -> None:
        with self.dispatcher.db.tx() as c:
            c.execute("UPDATE teleop_leases SET ended_at=?, end_reason=?, takeover_by=COALESCE(?, "
                      "takeover_by) WHERE id=? AND ended_at IS NULL",
                      (self._now(), reason, by or None, row_id))

    def _takeover(self, cur: Session, *, by: str, reason: str) -> None:
        """强制接管:旧连接先被告知、关掉,狗先停(旧租约放掉、等狗的遥控任务停稳)。"""
        self.close(cur, "taken_over", by=by, detail={"takeover_reason": reason})
        deadline = time.monotonic() + self.wait_released_s
        while time.monotonic() < deadline:
            c = self.dispatcher.clients.get(cur.robot_id)
            task = c.status.task if c is not None and c.status is not None else None
            if task is None or not task.task_id.startswith(TELEOP_TASK_PREFIX):
                return
            time.sleep(0.1)
        raise TeleopRefused(409, f"{cur.robot_id} 上一位的遥控还没停稳,稍后再试")

    # ------------------------------------------------------------ 帧

    def on_message(self, s: Session, text: str) -> None:
        """手机发来的一条:``{"vx", "wz"}`` 或 ``{"kind": "release"}``。看不懂的丢掉。"""
        if s.ended.is_set():
            return
        try:
            d = json.loads(text)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        if d.get("kind") == "release":
            self.close(s, "released")
            return
        vx, wz = d.get("vx"), d.get("wz")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (vx, wz)):
            return
        vx = max(-s.max_vx, min(s.max_vx, float(vx)))
        wz = max(-s.max_wz, min(s.max_wz, float(wz)))
        ok = self._video_ok(s.robot_id)
        if ok != s.video_told:
            s.video_told = ok
            self._tell(s, {"kind": "video", "ok": ok})
        if not ok:
            # 没画面不许动:不转发运动;在动的话发一次零速。
            if s.moving:
                self._send_frame(s, 0.0, 0.0)
            return
        self._send_frame(s, vx, wz)

    def _send_frame(self, s: Session, vx: float, wz: float, *, final: bool = False) -> None:
        """发一帧。序号与发出在同一把锁里(先编号的先发);结束之后只有 ``final``(那一帧零速)能发。"""
        with s.send_lock:
            if s.ended.is_set() and not final:
                return
            s.seq += 1
            f = TeleopFrame(lease_epoch=s.epoch, seq=s.seq, sent_at=self._now(),
                            ttl_ms=self.frame_ttl_ms, vx=vx, wz=wz)
            s.moving = bool(vx or wz)
            try:
                self.loop.call(lambda: self.dispatcher.teleop_frame(s.robot_id, f), timeout_s=2.0)
            except Exception:
                log.warning("遥控帧发不出去(%s)", s.robot_id, exc_info=True)

    def _tell(self, s: Session, msg: dict[str, Any]) -> None:
        ws = s.ws
        if ws is None:
            return
        try:
            ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            log.debug("告诉手机失败", exc_info=True)

    # ------------------------------------------------------------ 关

    def close(self, s: Session, reason: str, *, by: str = "",
              detail: dict[str, Any] | None = None) -> None:
        """结束这一个租约(幂等)。先发零速、放租,再告诉手机、关连接,最后记账。"""
        with self._lock:
            if s.ended.is_set():
                return
            s.ended.set()
            s.end_reason = reason
            if self._sessions.get(s.robot_id) is s:
                del self._sessions[s.robot_id]
        if s.moving or reason != "released":
            self._send_frame(s, 0.0, 0.0, final=True)
        try:
            self.loop.call(lambda: self.dispatcher.teleop_lease(
                s.robot_id, TeleopLease(action="release", lease_epoch=s.epoch), timeout_s=2.0),
                timeout_s=5.0)
        except Exception:
            log.warning("放租发不出去(%s 代次 %d),狗那头租约到期自己停", s.robot_id, s.epoch,
                        exc_info=True)
        self._tell(s, {"kind": "ended", "reason": reason})
        if s.ws is not None:
            try:
                s.ws.close(1000, reason)
            except Exception:  # noqa: BLE001
                pass
        self._end_row(s.row_id, reason, by=by)
        self._audit(by or s.operator, "teleop_end", s.robot_id,
                    {"lease_epoch": s.epoch, "reason": reason, "operator": s.operator,
                     **(detail or {})})

    def halt(self, robot_id: str, user) -> dict[str, Any]:
        """停车:走 ``cmd``,不走遥控连接;这台狗上的遥控租约也结束。"""
        r = self.loop.call(lambda: self.dispatcher.halt(robot_id, issued_by=str(user)),
                           timeout_s=self.dispatcher.ack_timeout_s + 5)
        s = self.active(robot_id)
        if s is not None:
            threading.Thread(target=self.close, args=(s, "halt"), kwargs={"by": str(user)},
                             daemon=True).start()
        return r

    # ------------------------------------------------------------ 续租与守卫

    def step(self) -> None:
        """每 ``renew_s``:狗掉线/不新鲜 → 结束;续租,连续两次续不上 → 结束。"""
        with self._lock:
            sessions = [s for s in self._sessions.values() if not s.ended.is_set()]
        for s in sessions:
            if self.dispatcher.is_stale(s.robot_id):
                self.close(s, "robot_offline")
                continue
            c = self.dispatcher.clients.get(s.robot_id)
            if c is not None and c.status is not None and not c.status.online:
                self.close(s, "robot_offline")
                continue
            try:
                ack = self.loop.call(lambda s=s: self.dispatcher.teleop_lease(
                    s.robot_id, TeleopLease(action="renew", lease_epoch=s.epoch,
                                            lease_ttl_ms=self.lease_ttl_ms),
                    timeout_s=self.renew_s), timeout_s=self.renew_s + 2)
                ok = ack.result is AckResult.ACCEPTED
            except Exception:  # noqa: BLE001 - 续不上就算一次失败
                ok = False
            s.renew_failures = 0 if ok else s.renew_failures + 1
            if s.renew_failures >= 2:
                self.close(s, "lease_lost")

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="teleop-renew")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.renew_s):
            try:
                self.step()
            except Exception:
                log.exception("遥控续租这一拍炸了")

    def close_all(self, reason: str = "site_stopping") -> None:
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            self.close(s, reason)

    def _audit(self, actor: str, action: str, robot_id: str, detail: dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(actor=actor, action=action, target=robot_id, status=200,
                              detail=detail)
        except Exception:
            log.exception("遥控审计记不下来")
