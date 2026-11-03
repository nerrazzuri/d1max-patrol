"""没有充电桩时保安换电(W73 第四期,设计 §7.4)。

- 快要补能前 :data:`SOON_MIN` 分钟:P2 ``swap_soon``「约 HH:MM A 需要换电池,请准备两块满电电池」。
- 值守判它「不够跑一圈,先补能」(``GuardDuty.topup_line``)、这台没桩:它回待命点(= 换电位,值守的
  「等」那一步本来就叫待命点派回去),**站着不趴下**。到了、空着 → P1 ``swap_needed``。
- 进度**按两块电池的在位、电量自动判**(``Telemetry.battery_packs``,保安不用点「下一步」):
  ``first``(先换低的那块)→ ``first_out``(拔了:不要动另一块)→ ``first_low``(插上的不满)→
  ``second``(现在换另一块)→ ``second_out`` → 两块都 ≥ ``SWAP_OK_PCT``(80%)= 换好。
  不分块报的老机器人只说「换电池」(``any``),电量上来算换好。
- 保安一直不来::data:`REPUSH_MIN` 分钟再推一次,:data:`MANAGER_MIN` 分钟推给经理
  (``swap_overdue``)。
- 换了一块就走了:一直提示「2 号还没换」;电量线照常按低的那块判(值守那头)。
- 正在拔着的那一步,报警派遣不打断(:meth:`refuse`,§7.5)。

状态只在内存里(站点重启按电池重新判进度:哪块低、哪块拔了都读得出来)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from d1max_site.energy import SWAP_OK_PCT

log = logging.getLogger(__name__)

SOON_MIN = 15
REPUSH_MIN = 10
MANAGER_MIN = 20
#: 离换电位这么近、手上没任务 = 到了。
AT_HOME_M = 1.5


class SwapDesk:
    def __init__(self, dispatcher: Any, *, now_ms: Callable[[], int],
                 need: Callable[[str], float | None], eta_min: Callable[[str], float | None],
                 has_charger: Callable[[str], bool],
                 home: Callable[[str, str, str], dict[str, Any] | None]) -> None:
        self.dispatcher = dispatcher
        self._now = now_ms
        self._need, self._eta, self._has_charger, self._home = need, eta_min, has_charger, home
        self.alerts: Any = None
        self.s: dict[str, dict[str, Any]] = {}
        self._soon_told: set[str] = set()
        #: 进度变化(给值守记录、测试):``{"robot_id", "step", "at_ms"}``,只留最近 200 条。
        self.events: list[dict[str, Any]] = []

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
            present = [(v, i) for i, v in enumerate(packs[:2]) if v is not None]
            st["first"] = min(present)[1] + 1 if present else 1
        a, b = st["first"] - 1, 2 - st["first"]
        pa, pb = packs[a], packs[b]
        if pa is None:
            return "first_out"
        if pa < SWAP_OK_PCT:
            return "first_low" if st.get("pulled_first") else "first"
        if pb is None:
            return "second_out"
        if pb < SWAP_OK_PCT:
            return "second"
        return "done"

    def refuse(self, rid: str) -> str:
        """报警派遣问:正在拔着的那一步 → 不派的理由(这一块装上再说);不然空串。"""
        st = self.s.get(rid)
        if st is None or st.get("step") not in ("first_out", "second_out"):
            return ""
        n = st["first"] if st["step"] == "first_out" else 3 - st["first"]
        return f"{rid} 正在换电池({n} 号拔了,这一步不打断)"

    # ------------------------------------------------------------ 每拍

    def tick(self) -> None:
        for rid in sorted(self.dispatcher.clients):
            try:
                self._one(rid)
            except Exception:                            # 一台出错不影响别的台
                log.exception("%s 换电进度判不出来", rid)

    def _note(self, rid: str, step: str) -> None:
        self.events.append({"robot_id": rid, "step": step, "at_ms": self._now()})
        del self.events[:-200]

    def _one(self, rid: str) -> None:
        now = self._now()
        st = self.s.get(rid)
        if st is None:
            if self._has_charger(rid):
                self._soon_told.discard(rid)
                return
            need = self._need(rid)
            if need is None:
                eta = self._eta(rid)
                if eta is not None and eta <= SOON_MIN and rid not in self._soon_told:
                    self._soon_told.add(rid)
                    at = datetime.fromtimestamp((now + eta * 60_000) / 1000).strftime("%H:%M")
                    self._raise("swap_soon", rid, f"{rid} 快要换电池了",
                                f"约 {at} {rid} 需要换电池,请准备两块满电电池")
                elif eta is None or eta > SOON_MIN + 5:
                    self._soon_told.discard(rid)
                return
            st = self.s[rid] = {"step": "going", "since_ms": now, "told_ms": None,
                                "pushes": 0, "manager": False, "first": None, "line": need}
            self._note(rid, "going")
        step = st["step"]
        if step == "going":
            if not self._arrived(rid):
                return
            st["arrived_ms"] = now
        new = self._step(rid, st)
        if new == "first_out":
            st["pulled_first"] = True
        if new != step:
            st["step"] = new
            self._note(rid, new)
        if new == "done":
            self._finish(rid)
            return
        if st["told_ms"] is None:
            st["told_ms"] = now
            st["pushes"] = 1
            self._raise("swap_needed", rid, f"{rid} 已到换电位,请来换电池", self._text(st))
        elif st["pushes"] == 1 and now - st["told_ms"] >= REPUSH_MIN * 60_000 \
                and new in ("first", "any"):
            st["pushes"] = 2
            self._raise("swap_needed", rid, f"{rid} 还在等换电池(第 2 次提醒)", self._text(st))
        if not st["manager"] and now - st["told_ms"] >= MANAGER_MIN * 60_000 \
                and new in ("first", "any"):
            st["manager"] = True
            self._raise("swap_overdue", rid, f"{rid} 等换电池 {MANAGER_MIN} 分钟没人来",
                        f"{rid} 在换电位等了 {MANAGER_MIN} 分钟还没人换电池,请安排人去")

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
        self.alerts.raise_alert(kind=kind, robot=rid, title=title, detail=detail,
                                now_ms=self._now(), merge=False)

    @staticmethod
    def _text(st: dict[str, Any]) -> str:
        f = st.get("first")
        if f is None or st["step"] == "any":
            return "请换上满电电池(热插拔,机器人站着不断电)"
        return (f"先换 {f} 号(电量低的那块),换好、电量读到 {SWAP_OK_PCT:.0f}% 以上再换 {3 - f} 号;"
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
