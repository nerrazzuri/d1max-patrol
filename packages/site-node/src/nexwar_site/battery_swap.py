"""没有充电桩时保安换电(W73 第四期,设计 §7.4)。

- 快要补能前 :data:`SOON_MIN` 分钟:P2 ``swap_soon``「约 HH:MM A 需要换电池,请准备两块满电电池」。
- 值守判它「不够跑一圈,先补能」(``GuardDuty.topup_line``)、这台没桩:它回待命点(= 换电位,值守的
  「等」那一步本来就叫待命点派回去),**站着不趴下**。到了、空着 → P1 ``swap_needed``。
- 进度**按两块电池的在位、电量自动判**(``Telemetry.battery_packs``,保安不用点「下一步」):
  ``first``(先换低的那块)→ ``first_out``(拔了:不要动另一块)→ ``first_low``(插上的不满)→
  ``second``(现在换另一块)→ ``second_out`` → 两块都 ≥ ``SWAP_OK_PCT``(80%)= 换好。
  不分块报的老机器人只说「换电池」(``any``),电量上来算换好。
- 保安一直不来、换到一半走了、插回的不满:每一步没进展 :data:`REPUSH_MIN` 分钟再推一次(说清哪块
  还没好),:data:`MANAGER_MIN` 分钟推给经理(``swap_overdue``)—— 按最后一次进展计时(外审 r1 #9)。
- 低电判出来时不在换电位:站点派它回待命点(= 换电位),没走成 :data:`RESEND_S` 秒后再派(外审 r1 #3)。
  这一趟按「去补能」发(``charge``:狗上出发线只看中止线、路上低电不掉头 —— 跟去充电桩那一趟一样,
  外审 r2 #2);到没到按派去的同一个点判(站点接的是 ``StandbyManager.swap_point``,外审 r2 #3)。
  **有一块拔着绝不派它走**:回换电位的路上被拔了一块,就地转去引导装回(外审 r2 #1)。
- 正在拔着的那一步,报警派遣不打断(:meth:`refuse`,§7.5);**有一块拔着**(不管站点记没记着
  换电)值守、排程都不派它(:meth:`busy`,外审 r1 #1)。
- 拔着一块的时候判进度,**先让装回拔着的那块**,绝不引导去拔剩下那块(外审 r1 #2:站点重启后
  ``[None, 20]`` 曾提示「先换 2 号」= 断电)。拔错了(先换的那块还没好,另一块被拔了)→ ``wrong_out``。

现场 11-05:
- **遥控不中止**:缺一块电池时,保安正要遥控它走到换电位(``teleop-`` 那一趟照常)。
- **单电池运行**:一块空着连续 :data:`SINGLE_PACK_MIN` 分钟、又不在换电流程里(不是低电换电那一趟)
  = 这台就是只插一块在跑,不拦、不中止、不叫人装回。低电换电走到一半拔着的那块空再久也照样引导装回。
  站点重启后先按「拔着」算,满 :data:`SINGLE_PACK_MIN` 分钟才转成单电池。

状态只在内存里(站点重启按电池重新判进度:哪块低、哪块拔了都读得出来;拔着一块的重启后
直接接上引导)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from nexwar_site.dispatcher import TELEOP_TASK_PREFIX
from nexwar_site.energy import SWAP_OK_PCT

log = logging.getLogger(__name__)

SOON_MIN = 15
REPUSH_MIN = 10
MANAGER_MIN = 20
#: 派它回换电位没走成(没接、半路停了),过这么久再派。
RESEND_S = 60
#: 离换电位这么近、手上没任务 = 到了。
AT_HOME_M = 1.5
#: 拔着一块时机器人还在跑任务(外审 r3 #1):每隔这么久(秒)再发一次中止,发够这么多次报警。
HALT_RETRY_S = 5
HALT_ALERT = 3
#: 发过中止以后,连着听得见这么久(毫秒)、手上不是它才算停了(外审 r4 #1:断线时离线状态里「没任务」、
#: 刚连回来那几拍的空状态都不算;跟值守的 ``SETTLE_HEARD_MS`` 一样)。
HALT_SETTLE_MS = 5_000
#: 一块空着连续这么久(分钟)、不在低电换电流程里 = 单电池运行(现场 11-05)。
SINGLE_PACK_MIN = 10


class SwapDesk:
    def __init__(self, dispatcher: Any, *, now_ms: Callable[[], int],
                 need: Callable[[str], float | None], eta_min: Callable[[str], float | None],
                 has_charger: Callable[[str], bool],
                 home: Callable[[str, str, str], dict[str, Any] | None]) -> None:
        self.dispatcher = dispatcher
        self._now = now_ms
        self._need, self._eta, self._has_charger, self._home = need, eta_min, has_charger, home
        self.alerts: Any = None
        #: 派回换电位用(``StandbyManager``,站点接上)。
        self.standby: Any = None
        self.s: dict[str, dict[str, Any]] = {}
        self._soon_told: set[str] = set()
        #: 进度变化(给值守记录、测试):``{"robot_id", "step", "at_ms"}``,只留最近 200 条。
        self.events: list[dict[str, Any]] = []
        self._heard_since: dict[str, int] = {}
        #: 哪块空着、从什么时候起(单电池判断用):``rid → (几号, ms)``。
        self._absent: dict[str, tuple[int, int]] = {}

    # ------------------------------------------------------------ 读

    def _client(self, rid: str) -> Any:
        return self.dispatcher.clients.get(rid)

    def _packs(self, rid: str) -> list[float | None] | None:
        c = self._client(rid)
        t = c.telemetry if c is not None else None
        p = getattr(t, "battery_packs", None) if t is not None else None
        return list(p) if p else None

    def _pct(self, rid: str) -> float | None:
        c = self._client(rid)
        t = c.telemetry if c is not None else None
        return float(t.battery_pct) if t is not None and t.battery_pct is not None else None

    def _gap(self, rid: str) -> int | None:
        """空着的那块是几号(1 / 2);两块都在、不分块报 → ``None``。"""
        packs = self._packs(rid)
        if not packs or len(packs) < 2:
            return None
        return next((i + 1 for i, v in enumerate(packs[:2]) if v is None), None)

    def _single(self, rid: str) -> bool:
        """单电池运行:同一块空着连续 :data:`SINGLE_PACK_MIN` 分钟(站点看见的)。"""
        n, a = self._gap(rid), self._absent.get(rid)
        return n is not None and a is not None and a[0] == n \
            and self._now() - a[1] >= SINGLE_PACK_MIN * 60_000

    def missing(self, rid: str) -> int | None:
        """拔着的那块是几号(1 / 2);两块都在、不分块报、单电池运行(不在低电换电流程里)→ ``None``。"""
        n = self._gap(rid)
        if n is None:
            return None
        st = self.s.get(rid)
        if (st is None or st.get("by_gap")) and self._single(rid):
            return None
        return n

    def busy(self, rid: str) -> str:
        """值守、排程问:能不能派它出去。有一块拔着、在等换电池 → 不派的理由;不然空串。"""
        n = self.missing(rid)
        if n is not None:
            return f"{rid} 的 {n} 号电池拔着,装回去之前不派"
        if rid in self.s:
            return f"{rid} 在等换电池"
        return ""

    def _idle(self, rid: str) -> bool:
        c = self._client(rid)
        return c is not None and c.status is not None and bool(c.status.online) \
            and c.status.task is None

    def _arrived(self, rid: str) -> bool:
        c = self._client(rid)
        if c is None or c.status is None or not c.status.online or c.status.task is not None:
            return False
        pose = getattr(c.telemetry, "pose", None) if c.telemetry is not None else None
        if pose is None:
            return False
        h = self._home(rid, pose.map_id, pose.map_version)
        if h is None:
            return True                                  # 没标待命点:站在哪儿就在哪儿换
        return math.hypot(pose.x - float(h["x"]), pose.y - float(h["y"])) <= AT_HOME_M

    # ------------------------------------------------------------ 判进度

    def _step(self, rid: str, st: dict[str, Any]) -> str:
        packs = self._packs(rid)
        if packs is None or len(packs) < 2:
            pct = self._pct(rid)
            return "done" if pct is not None and pct >= SWAP_OK_PCT else "any"
        if st.get("first") is None:
            gone = [i for i in (0, 1) if packs[i] is None]
            if gone:                                     # 拔着一块:先装回它(另一块是唯一的电)
                st["first"] = gone[0] + 1
            else:
                st["first"] = min((packs[0], 0), (packs[1], 1))[1] + 1
        a, b = st["first"] - 1, 2 - st["first"]
        pa, pb = packs[a], packs[b]
        if pa is None:
            return "first_out"
        if pb is None:
            return "second_out" if pa >= SWAP_OK_PCT else "wrong_out"
        if pa < SWAP_OK_PCT:
            return "first_low" if st.get("pulled_first") else "first"
        if pb < SWAP_OK_PCT:
            return "second"
        return "done"

    def refuse(self, rid: str) -> str:
        """报警派遣问:有一块拔着 → 不派的理由(这一块装上再说);不然空串。"""
        n = self.missing(rid)
        return f"{rid} 正在换电池({n} 号拔了,这一步不打断)" if n is not None else ""

    # ------------------------------------------------------------ 每拍

    async def tick(self) -> None:
        now = self._now()
        for rid in sorted(self.dispatcher.clients):
            n = self._gap(rid)                           # 哪块空着、从什么时候起
            if n is None:
                self._absent.pop(rid, None)
            elif self._absent.get(rid, (0, 0))[0] != n:
                self._absent[rid] = (n, now)
            if self._heard(rid):                         # 每台每拍记:连着听得见从什么时候起
                self._heard_since.setdefault(rid, now)
            else:
                self._heard_since.pop(rid, None)
            try:
                await self._one(rid)
            except Exception:                            # 一台出错不影响别的台
                log.exception("%s 换电进度判不出来", rid)

    def _note(self, rid: str, step: str) -> None:
        self.events.append({"robot_id": rid, "step": step, "at_ms": self._now()})
        del self.events[:-200]

    async def _one(self, rid: str) -> None:
        now = self._now()
        st = self.s.get(rid)
        if st is not None and st.get("by_gap") and self._single(rid):
            log.info("%s 一块空了 %d 分钟以上:当单电池运行,不再叫人装回", rid, SINGLE_PACK_MIN)
            self._finish(rid)
            return
        if st is None:
            if self._has_charger(rid):
                self._soon_told.discard(rid)
                return
            need = self._need(rid)
            by_gap = need is None and self.missing(rid) is not None
            if by_gap:
                need = 0.0                               # 拔着一块(站点重启了?):直接接上引导
            if need is None:
                eta = self._eta(rid)
                if eta is not None and eta <= SOON_MIN and rid not in self._soon_told:
                    at = datetime.fromtimestamp((now + eta * 60_000) / 1000).strftime("%H:%M")
                    self._raise("swap_soon", rid, f"{rid} 即将换电",
                                f"约 {at} {rid} 需要换电池,请准备两块满电电池")
                    self._soon_told.add(rid)
                elif eta is None or eta > SOON_MIN + 5:
                    self._soon_told.discard(rid)
                return
            st = self.s[rid] = {"step": "going", "since_ms": now, "told_ms": None,
                                "first": None, "line": need, "sent_ms": None, "by_gap": by_gap}
            self._note(rid, "going")
            if self.missing(rid) is not None:
                st["arrived_ms"] = now                   # 已经在换了:不用回换电位
                st["step"] = "resumed"
        if await self._halt_if_moving(rid, st, now):
            return
        step = st["step"]
        if step == "going":
            # 外审 r2 #1:拔着一块 → 不再派它回换电位,就地引导装回(另一块是唯一的电)
            if self.missing(rid) is None and not self._arrived(rid):
                await self._send_back(rid, st, now)
                return
            st["arrived_ms"] = now
        new = self._step(rid, st)
        if new == "first_out":
            st["pulled_first"] = True
        if new != step:
            st["step"] = new
            st["progress_ms"] = now                      # 有进展:提醒、升级从这里重新计时
            st["repushed"] = st["managed"] = False
            self._note(rid, new)
        if new == "done":
            self._finish(rid)
            return
        if st["told_ms"] is None:
            title = f"{rid} 已到换电位,请来换电池" if self._arrived(rid) \
                else f"{rid} 有电池拔着,请到机器人旁边装回"
            self._raise("swap_needed", rid, title, self._text(st))
            st["told_ms"] = now                          # 真发出去了才算告诉过(没发成下一拍补)
            return
        idle = now - st["progress_ms"]
        if not st["repushed"] and idle >= REPUSH_MIN * 60_000:
            self._raise("swap_needed", rid, f"{rid} 还在等换电池({REPUSH_MIN} 分钟没进展)",
                        self._text(st))
            st["repushed"] = True
        if not st["managed"] and idle >= MANAGER_MIN * 60_000:
            self._raise("swap_overdue", rid, f"{rid} 换电池 {MANAGER_MIN} 分钟没进展",
                        f"{rid} 在换电位等了 {MANAGER_MIN} 分钟,电池还没换好(现在:"
                        f"{self._text(st)}),请安排人去")
            st["managed"] = True

    def _running(self, rid: str) -> str:
        c = self._client(rid)
        return c.status.task.task_id if c is not None and c.status is not None \
            and c.status.task is not None else ""

    def _heard(self, rid: str) -> bool:
        c = self._client(rid)
        if c is None or c.status is None or not c.status.online:
            return False
        fresh = getattr(self.dispatcher, "_fresh", None)
        return not callable(fresh) or bool(fresh(c))

    def _terminal(self, rid: str, tid: str) -> bool:
        db = getattr(self.dispatcher, "db", None)
        if db is None or not tid:
            return False
        return bool(db.query(
            "SELECT 1 FROM events WHERE robot_id=? AND kind IN "
            "('task_done','task_failed','task_aborted','task_preempted') "
            "AND json_extract(data, '$.task_id')=? LIMIT 1", (rid, tid)))

    def _halted(self, rid: str, tid: str, now: int) -> bool:
        """发过中止的那一趟停了:有它的结果事件;或连着听得见够 :data:`HALT_SETTLE_MS`、
        手上不是它。"""
        if self._terminal(rid, tid):
            return True
        if not self._heard(rid):
            self._heard_since.pop(rid, None)
            return False
        since = self._heard_since.setdefault(rid, now)
        return now - since >= HALT_SETTLE_MS and self._running(rid) != tid

    def _quiet(self, rid: str, now: int) -> bool:
        """连着听得见够 :data:`HALT_SETTLE_MS`,手上没有任务。"""
        since = self._heard_since.get(rid) if self._heard(rid) else None
        return since is not None and now - since >= HALT_SETTLE_MS and not self._running(rid)

    async def _halt_if_moving(self, rid: str, st: dict[str, Any], now: int) -> bool:
        """外审 r3 #1:有一块拔着、机器人手上还有任务(回换电位那一趟、人派的…)→ 先让它停:发中止,
        没停每 :data:`HALT_RETRY_S` 秒再发,发够 :data:`HALT_ALERT` 次报警。停下之前不叫人过来装电池
        (人蹲在一台还在走的机器人旁边)。→ ``True`` = 还在停,这一拍别的不办。

        外审 r4 #1:记住要停的那一趟;之后手上「没任务」(断线的离线状态、刚连回来的空状态)不算停了,
        要有 :meth:`_halted` 的证据。"""
        if self.missing(rid) is None:
            for k in ("halt_tries", "halt_tid"):
                st.pop(k, None)
            return False
        tid = self._running(rid) if self._heard(rid) else ""
        if tid.startswith(TELEOP_TASK_PREFIX):
            # 现场 11-05:保安遥控它(多半正是开去换电位)—— 不中止;以前每 4 秒停一次遥控
            for k in ("halt_tries", "halt_tid"):
                st.pop(k, None)
            return False
        pending = st.get("halt_tid")
        if tid and tid != pending:
            st["halt_tid"], st["halt_tries"] = tid, 0
        elif not tid:
            if not pending:
                # 外审 r5 #1:没记下要停的那一趟(断线比这一拍先到)也要停的证据 —— 连着听得见够久、
                # 手上确实空着才叫人;离线、刚连回来,机器人在不在走说不准,先不叫
                return not self._quiet(rid, now)
            if self._halted(rid, pending, now):
                log.info("%s 拔着电池时那一趟 %s 停下了", rid, pending)
                for k in ("halt_tries", "halt_tid"):
                    st.pop(k, None)
                return False
            tid = pending
        if now - st.get("halt_ms", -10**12) < HALT_RETRY_S * 1000:
            return True
        st["halt_ms"] = now
        st["halt_tries"] = tries = st.get("halt_tries", 0) + 1
        log.warning("%s 拔着一块电池还在跑 %s:发中止(第 %d 次)", rid, tid, tries)
        try:
            await self.dispatcher.abort(rid, tid, issued_by="site:swap")
        except Exception as exc:  # noqa: BLE001 - 没发成:下个周期再发
            log.warning("%s 换电时停不下 %s:%s", rid, tid, exc)
        if tries == HALT_ALERT:
            self._raise("swap_stuck", rid, f"{rid} 拔着电池还在走,停不下来",
                        f"{rid} 有一块电池拔着,站点发了 {tries} 次中止,{tid} 还在跑;"
                        "先别靠近,用遥控器或急停让它停下")
        return True

    async def _send_back(self, rid: str, st: dict[str, Any], now: int) -> None:
        """低电判出来时不在换电位、手上没任务:派它回待命点(= 换电位);没走成过一阵再派。"""
        if self.standby is None or not self._idle(rid) or self.missing(rid) is not None:
            return
        if st["sent_ms"] is not None and now - st["sent_ms"] < RESEND_S * 1000:
            return
        st["sent_ms"] = now
        try:
            await self.standby.return_to(rid, issued_by="site:swap", charge=True)
        except Exception as exc:  # noqa: BLE001 - 没标待命点、派不出去:原地换,到时候按原地判
            log.warning("%s 回换电位没派成:%s", rid, exc)

    def _finish(self, rid: str) -> None:
        self.s.pop(rid, None)
        self._soon_told.discard(rid)
        if self.alerts is not None:
            for k in ("swap_needed", "swap_overdue", "swap_soon"):
                if self.alerts.has_open(rid, k):
                    self.alerts.resolve_all(rid, k, who="site:swap_done")
        log.info("%s 换好电池", rid)

    def _raise(self, kind: str, rid: str, title: str, detail: str) -> None:
        if self.alerts is None:
            return
        # 站点的告警台自己取时间(``alert_store.AlertDesk``,不收 now_ms;11-04 现场多传了它,
        # 换电告警一条都没发出去)
        self.alerts.raise_alert(kind=kind, robot=rid, title=title, detail=detail, merge=False)

    @staticmethod
    def _text(st: dict[str, Any]) -> str:
        f, step, ok = st.get("first"), st["step"], f"{SWAP_OK_PCT:.0f}%"
        if f is None or step == "any":
            return "请换上满电电池(热插拔,机器人站着不断电)"
        o = 3 - f
        return {
            "first_out": f"{f} 号已拔下:装上一块满电的 {f} 号;{o} 号先别动(它是机器人现在唯一的电)",
            "first_low": f"装上的 {f} 号电量不到 {ok}:换一块满电的(先别动 {o} 号)",
            "second": f"{f} 号好了,现在换 {o} 号(读到 {ok} 以上算换好)",
            "second_out": f"{o} 号已拔下:装上一块满电的 {o} 号",
            "wrong_out": f"{o} 号被拔了:先把它装回去;{f} 号还没换好,两块不要同时拔",
        }.get(step, f"先换 {f} 号(电量低的那块),换好、电量读到 {ok} 以上再换 {o} 号;"
                    f"不要两块一起拔")

    def view(self) -> dict[str, Any]:
        out = []
        for rid, st in sorted(self.s.items()):
            f = st.get("first")
            out.append({"robot_id": rid, "step": st["step"], "first": f,
                        "second": (3 - f) if f else None, "packs": self._packs(rid),
                        "battery_pct": self._pct(rid), "since_ms": st["since_ms"],
                        "told_ms": st["told_ms"], "ok_pct": SWAP_OK_PCT})
        return {"robots": out}
