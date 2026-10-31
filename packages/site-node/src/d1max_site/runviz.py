"""运行可视化(站点这一半):每台狗正在走的规划路径、最近走过的轨迹、被挡 / 重规划的状态。

**全在内存里,不落库**:站点重启后路径靠狗 10 s 内的定时重发补上、轨迹从重启后重新攒、被挡状态靠狗的
下一条事件(狗没再发就当没有)。这是显示用的辅助信息,不是记录 —— 要查「当时怎么走的」用运行记录里的
轨迹(W17 之后的巡检记录),不是这里。

- **规划路径**:狗在遥测的 ``nav`` 块里报(``d1max_agent.nav_report``,抽稀、最多 64 点、
  限频)。块没有了
  (导航结束、老狗)就清;块里只带序号不带点(路径没变)就沿用上一份。站点这边再校验一遍:点数、数值有限、
  形状不对的丢掉(不信任线上的东西)。
- **轨迹**:用遥测里**已经有**的地图位姿,挪了 :data:`TRAIL_STEP_M` 才记一个点,每台狗最多
  :data:`TRAIL_MAX` 个点(满了丢最老的),超过 :data:`TRAIL_AGE_MS` 的丢掉;换了图就从头记;位置一下子
  跳了 :data:`TRAIL_JUMP_M` 以上(重定位)另起一段 —— 不把跳变连成一条穿墙的线。定位不可信时遥测没有
  位姿,那段不记。**只画这一趟**(W41,用户 2026-10-26 定:原来是 30 分钟内一整条线、不分任务,来回测、
  遥控器开回起点的路叠在一起看不清):只记**任务在跑、而且控制权在我们手上**时走的路;新任务一开始就清掉
  上一趟的;遥控器(或别的上装)拿走控制权那段不记,拿回来以后另起一段(``trail_cuts``:从这些下标起是
  新的一段,界面不连线)。任务结束后上一趟的线留着,到下一趟开始或者 30 分钟。
- **被挡**:狗的 ``nav_blocked`` 事件(带原因、位置、已经等了几秒)→ 状态里写「被挡 N 秒:原因」;
  ``nav_unblocked``、这一趟结束、狗掉线都清。任务在跑、被挡超过 :data:`BLOCK_ALERT_S` 报 P2
  ``nav_blocked``(带被挡的位置),清了自动解决。*判据的由来*:现在狗自己等满 20 s 就放弃这一段(报
  ``run_abort``),所以这条 P2 在现有的规划后端上走不到 60 s —— 它是兜底:狗的等待上限以后放宽、
  或者别的后端在原地等得更久时,站点不靠狗自己放弃才叫人。
- **重规划**:``nav_replan`` 事件 → 状态里写「重规划 N 次(偏离路径 / 区域换了 / 绕障)」,
  这一趟结束清。
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from d1max_contract.messages import Event, Status, TaskState, Telemetry

log = logging.getLogger(__name__)

#: 每台狗最多记这么多个轨迹点;两点之间至少隔这么远(米);点超过这么久就丢(毫秒)。
TRAIL_MAX = 240
TRAIL_STEP_M = 0.3
TRAIL_AGE_MS = 30 * 60_000
#: 位置一下子跳了这么远(米)就当重定位,轨迹从头记。
TRAIL_JUMP_M = 3.0
#: 规划路径点数上限(站点这边的校验;狗最多报 64)。
PATH_MAX = 128
#: 任务在跑、被挡超过这么久(秒)报 P2。
BLOCK_ALERT_S = 60
#: 重规划原因 → 人话的键(界面按语言翻)。
REPLAN_WHY = ("off_route", "zones", "near_nogo", "detour", "preview")


@dataclass
class _Blocked:
    since_ms: int
    reason: str
    x: float | None
    y: float | None
    map_id: str
    map_version: str
    alerted: bool = False


@dataclass
class _Robot:
    map: tuple[str, str] | None = None
    trail: deque = field(default_factory=lambda: deque(maxlen=TRAIL_MAX))
    path: list[list[float]] | None = None
    path_seq: int | None = None
    blocked: _Blocked | None = None
    replan: dict[str, Any] | None = None
    running: bool = False
    task_id: str = ""
    #: 控制权在我们手上(``Status.ready.control``);遥控器拿走了就不记轨迹。
    control: bool = False
    #: 下一个点另起一段(控制权被拿走过、位置跳了)。
    cut: bool = False
    #: 当前这一段的号(轨迹点带着它,视图按它切段)。
    seg: int = 0


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) else None


def clean_path(raw: Any) -> list[list[float]] | None:
    """线上的路径 → 干净的 ``[[x, y], ...]``;形状不对、点太多、没有两个点以上就是 ``None``。"""
    if not isinstance(raw, list) or not 2 <= len(raw) <= PATH_MAX:
        return None
    out = []
    for p in raw:
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            return None
        x, y = _num(p[0]), _num(p[1])
        if x is None or y is None:
            return None
        out.append([round(x, 2), round(y, 2)])
    return out


class RunViz:
    def __init__(self, *, now_ms: Callable[[], int], alerts: Any = None,
                 block_alert_s: float = BLOCK_ALERT_S) -> None:
        self._now = now_ms
        #: ``AlertDesk``(同步,只许在事件循环里用);``None`` = 不报告警(测试、只看图)。
        self.alerts = alerts
        self.block_alert_s = block_alert_s
        self._r: dict[str, _Robot] = {}
        #: 不被挡了、告警簿却没解决成的狗,tick 再来。
        self._to_resolve: set[str] = set()
        #: 上行回调在事件循环线程里写,接口(HTTP 线程)读:轨迹的 deque 遍历时不许被改。
        self._lock = threading.RLock()

    def attach(self, dispatcher: Any) -> None:
        dispatcher.on_telemetry(self.on_telemetry)
        dispatcher.on_status(self.on_status)
        dispatcher.on_event(self.on_event)
        dispatcher.runviz = self

    def _get(self, rid: str) -> _Robot:
        with self._lock:
            return self._r.setdefault(rid, _Robot())

    # ------------------------------------------------------------ 上行

    def on_telemetry(self, rid: str, t: Telemetry) -> None:
        r = self._get(rid)
        if t.pose is not None:
            with self._lock:
                self._pose(r, t.pose)
        nav = t.nav
        if not isinstance(nav, dict):
            r.path, r.path_seq = None, None                 # 没在导航(或老狗):清
            return
        seq = nav.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
        if "path" in nav:
            path = clean_path(nav["path"])
            if path is not None:
                r.path, r.path_seq = path, seq
            else:
                log.warning("%s 报的规划路径形状不对,丢掉", rid)
        elif r.path is not None and seq != r.path_seq:
            # 序号涨了、点还没到(限频):旧的先留着,比空着强,下一份来了就换
            pass

    def _pose(self, r: _Robot, p: Any) -> None:
        now = self._now()
        m = (p.map_id, p.map_version)
        x, y = _num(p.x), _num(p.y)
        if x is None or y is None:
            return
        if r.map != m:
            r.map = m
            r.trail.clear()
        if not (r.running and r.control):
            r.cut = True                                     # 不是我们在跑任务:不记,回来另起一段
            return
        while r.trail and now - r.trail[0][2] > TRAIL_AGE_MS:
            r.trail.popleft()
        if r.trail and not r.cut:
            lx, ly, _, _ = r.trail[-1]
            d = math.hypot(x - lx, y - ly)
            if d >= TRAIL_JUMP_M:
                r.cut = True                                 # 重定位跳了:不连成穿墙的线
            elif d < TRAIL_STEP_M:
                return
        if r.cut:
            r.seg += 1
            r.cut = False
        r.trail.append((round(x, 2), round(y, 2), now, r.seg))

    def on_status(self, rid: str, s: Status) -> None:
        r = self._get(rid)
        if not s.online:
            self._clear_run(rid, r)
            r.running = False
            r.cut = True
            return
        running = s.task is not None and s.task.state is TaskState.RUNNING
        tid = s.task.task_id if running and s.task is not None else ""
        if running and tid != r.task_id:
            with self._lock:
                r.trail.clear()                              # W41:新的一趟,只画这一趟
        r.task_id = tid
        if r.running and not running:
            self._clear_run(rid, r)                          # 这一趟结束了
        r.running = running
        r.control = bool(s.ready.control)
        if not r.control:
            r.cut = True

    def on_event(self, rid: str, e: Event) -> None:
        d = e.data if isinstance(e.data, dict) else {}
        r = self._get(rid)
        if e.kind == "nav_blocked":
            waited = _num(d.get("waited_s")) or 0.0
            r.blocked = _Blocked(
                since_ms=self._now() - int(max(0.0, min(waited, 3600.0)) * 1000),
                reason=str(d.get("reason") or "")[:200], x=_num(d.get("x")), y=_num(d.get("y")),
                map_id=str(d.get("map_id") or ""), map_version=str(d.get("map_version") or ""))
        elif e.kind == "nav_unblocked":
            self._unblock(rid, r)
        elif e.kind == "nav_replan":
            why = str(d.get("why") or "")
            n = d.get("n")
            r.replan = {"why": why if why in REPLAN_WHY else "off_route", "at_ms": self._now(),
                        "n": n if isinstance(n, int) and not isinstance(n, bool) else 0}
        elif e.kind in ("task_done", "task_failed", "task_aborted"):
            self._clear_run(rid, r)

    # ------------------------------------------------------------ 状态

    def _clear_run(self, rid: str, r: _Robot) -> None:
        r.replan = None
        r.path, r.path_seq = None, None
        self._unblock(rid, r)

    def _unblock(self, rid: str, r: _Robot) -> None:
        had, r.blocked = r.blocked, None
        if had is not None and had.alerted:
            self._resolve(rid)

    def _resolve(self, rid: str) -> None:
        if self.alerts is None:
            return
        try:
            self.alerts.resolve_all(rid, "nav_blocked", who="site:nav_unblocked")
            self._to_resolve.discard(rid)
        except Exception:
            log.exception("%s 不被挡了,P2 解决不了,下一拍再来", rid)
            self._to_resolve.add(rid)

    def tick(self) -> None:
        """每几秒调一次:被挡超过门槛且任务在跑 → 报 P2;不在跑的狗被挡状态清掉。"""
        if self.alerts is None:
            return
        for rid in list(self._to_resolve):
            self._resolve(rid)
        now = self._now()
        for rid, r in list(self._r.items()):
            b = r.blocked
            if b is None:
                continue
            if not r.running:
                self._unblock(rid, r)                        # 没任务了:不算被挡
                continue
            if b.alerted or now - b.since_ms < self.block_alert_s * 1000:
                continue
            try:
                ctx: dict[str, Any] = {}
                if b.x is not None and b.y is not None and b.map_id:
                    ctx["pose"] = {"map_id": b.map_id, "map_version": b.map_version,
                                   "x": b.x, "y": b.y, "at_ms": b.since_ms}
                if r.task_id:
                    ctx["task_id"] = r.task_id
                self.alerts.raise_alert(
                    kind="nav_blocked", robot=rid, title="机器人被挡住了,一分钟还未完成",
                    detail=f"被挡 {(now - b.since_ms) // 1000} 秒:{b.reason or '没说原因'};"
                           "去看看路上是不是有东西,或者遥控把它挪开。能走了此条自动解除",
                    context=ctx)
            except Exception:
                log.exception("%s 被挡的 P2 报不出去,下一拍再报", rid)
                continue
            b.alerted = True

    # ------------------------------------------------------------ 读

    def nav_summary(self, rid: str) -> dict[str, Any] | None:
        """给狗视图(``/api/robots``)用的一小块:被挡多久、为什么、重规划几次。没有就是 ``None``。"""
        r = self._r.get(rid)
        if r is None or (r.blocked is None and r.replan is None):
            return None
        now = self._now()
        out: dict[str, Any] = {}
        if r.blocked is not None:
            b = r.blocked
            out["blocked"] = {"since_ms": b.since_ms, "for_s": max(0, (now - b.since_ms) // 1000),
                              "reason": b.reason, "x": b.x, "y": b.y,
                              "map_id": b.map_id, "map_version": b.map_version}
        if r.replan is not None:
            out["replan"] = dict(r.replan)
        return out

    def view(self, rid: str | None = None) -> dict[str, Any]:
        """``GET /api/runviz``:每台狗的路径、轨迹、被挡位置。只给有东西可画的狗。"""
        now = self._now()
        out: dict[str, Any] = {}
        with self._lock:
            items = [(k, r, [(x, y, sg) for x, y, ms, sg in r.trail if now - ms <= TRAIL_AGE_MS])
                     for k, r in self._r.items() if rid is None or k == rid]
        for k, r, pts in items:
            summary = self.nav_summary(k) or {}
            if not pts and r.path is None and not summary:
                continue
            trail = [[x, y] for x, y, _ in pts]
            cuts = [i for i in range(1, len(pts)) if pts[i][2] != pts[i - 1][2]]
            out[k] = {"map": None if r.map is None else {"map_id": r.map[0], "version": r.map[1]},
                      "path": r.path or [], "trail": trail, "trail_cuts": cuts, **summary}
        return {"robots": out, "trail_max": TRAIL_MAX}
