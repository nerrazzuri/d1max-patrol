"""``Nav2NavBackend``(W47,`docs/W47-导航换Nav2-设计.md`):规划、局部避障、脱困交给 Nav2,
代理还是总闸。

继承 :class:`PlannedNavBackend`:地图、区域、禁行区回退检查、空闲进禁行区、限速、定位看门都照旧;
换掉的只有「怎么走」:

- ``goto`` / ``return_to``:Nav2 就绪 → 把目标经转接节点交给 Nav2(``NavigateToPose``)。
  **没就绪(转接节点没连上、Nav2 没起来)→ 这一段退回自己的规划导航**,记一条日志、
  发 ``nav_fallback`` 事件。
- 每拍 :meth:`_drive`(走 Nav2 的那一段):

  1. 禁行区回退检查(机身 + σ + 刹车距离)照旧:压上 → 停、FAILED;快碰到 → 这一拍刹住
     (Nav2 的地图里禁行区是致命格,它自己会绕开)。
  2. Nav2 报的目标结果:成功 → SUCCEED;中止、拒绝、被取消 → FAILED,原因写清楚。
  3. 转接节点断了、Nav2 不就绪了 → FAILED(引擎重试那一段时会退回自己的规划)。
  4. 取 Nav2 最新的速度(太旧就发零),按限速(任务、全狗、定位变差、禁行区)截,按死区整形;
     过守卫:**只拦雷达真看到的挡**。看不见的格子由 Nav2 的代价地图管 ——
     「看不见当挡」是 10-28 一串起不了步的根。

- 每拍把时钟、里程、实测速度、``map→odom``(定位可信才有)、新的感知栅格送给转接节点。
  Nav2 看到的跟守卫看到的是同一份,时间都在代理的钟上。
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from d1max_agent.bridges.hal_nav import HalNavBackend
from d1max_agent.bridges.planned_nav import PlannedNavBackend, _heading
from d1max_contract.obsbridge import pack_bits
from d1max_contract.routenet import route
from d1max_contract.zones import ZoneSet, point_in_polygon, segments_cross
from d1max_patrol.backends.base import NavRequestError
from d1max_patrol.protocol.nav_types import NavStatus, Pose

log = logging.getLogger(__name__)

#: Nav2 的速度这么久(秒)没更新就当没有,发零。
CMD_FRESH_S = 0.4
#: 守卫拦住多久报一次「被挡」(秒);拦着的时候 Nav2 自己会等、退、重规划。
GUARD_REPORT_S = 5.0
#: 引擎按路长估超时:Nav2 不报路长,按直线距离乘这个。
PATH_FACTOR = 1.5
#: 真机能走起来的最低速度(2026-10-29 C40221 实测:前进、后退 0.13 m/s 以下只踏步不走;转身
#: 0.07 rad/s 以下不转)。HAL 报的死区比它小时按它整形。
MIN_TRAVEL_MPS = 0.13
MIN_TURN_RPS = 0.07
#: 送给 Nav2 的实测速度按位置变化算,低通的系数(每拍新的占多少)。
VEL_ALPHA = 0.5
#: 扫过区里有看不见的格子(两侧盲区)时最快多快(米/秒)。看不见的不当挡(10-28 一串起不了步),
#: 但快了不安全:2026-10-29 仿真里 1.0 m/s 时人从侧面横穿,5 次撞 5 次。降到这个速度,人一进前方
#: 视野守卫就看得见、停得住。
UNKNOWN_SLOW_MPS = 0.3
# ---- 看得清就快(决策 76,用户 2026-10-29:报警要 90 s 内到位;两侧盲区让 1.0 m/s 一律跑不安全)
#: 看得清、附近没有地图之外的东西时最快多快(米/秒;还受任务、站点、全狗限速管)。
V_FAST = 1.0
#: 感知不新鲜、没配避障时最快多快。
V_BASE = 0.6
#: 看到地图之外的东西(人、箱子)时最慢降到多少。
VIS_SLOW_MPS = 0.3
#: 前方这么远、机身两边各外扩这么宽的范围里找地图之外的东西;离这么近就降到最慢。
VIS_LOOK_M = 3.0
VIS_SIDE_M = 1.2
VIS_SLOW_AT_M = 0.6
#: 看到的挡离地图上的墙这么近,当成墙(墙不会动,窄门、走廊不拖慢)。
STATIC_NEAR_M = 0.2
#: 机头前这一段真雷达本来就看不见(2026-10-08 实测约 0.47 m):前方看得清多远从这之外算「看不见」。
FRONT_BLIND_M = 0.5
#: 停下来以后离前方第一处挡或看不见的地方至少还留多少(米)。
CLEAR_MARGIN_M = 0.3
#: MPPI 倒车上限跟前进上限之比(``deploy/nav2/params.yaml`` 的 ``-vx_min / vx_max``,测试核着)。
#: MPPI 收到限速时按同一比例缩倒车上限(复查 P3):限速 0.2 时它最多倒 0.06,低于死区。
MPPI_REVERSE_RATIO = 0.3
#: 前进速度不到这个(米/秒)就当「原地转」,按地图上的墙核一道(:meth:`Nav2NavBackend._turn_hits_map`)。
TURN_VX = 0.05


def _overlap(a: tuple[tuple[float, float], ...], b: tuple[tuple[float, float], ...]) -> bool:
    """两个多边形有没有重叠(一个的顶点在另一个里面,或者有边相交)。"""
    if any(point_in_polygon(x, y, b) for x, y in a) or any(point_in_polygon(x, y, a)
                                                            for x, y in b):
        return True
    ea = list(zip(a, a[1:] + a[:1], strict=True))
    eb = list(zip(b, b[1:] + b[:1], strict=True))
    return any(segments_cross(p, q, r, t) for p, q in ea for r, t in eb)


def _compose_map_odom(est: Any, odom: tuple[float, float, float]) -> list[float]:
    """地图系里里程原点在哪:``T_map_odom = T_map_base · T_odom_base⁻¹``。"""
    ox, oy, oyaw = odom
    yaw = est.yaw - oyaw
    c, s = math.cos(yaw), math.sin(yaw)
    x = est.x - (c * ox - s * oy)
    y = est.y - (s * ox + c * oy)
    return [x, y, math.remainder(yaw, 2 * math.pi)]


class Nav2NavBackend(PlannedNavBackend):
    def __init__(self, *a: Any, link: Any = None, **kw: Any) -> None:
        super().__init__(*a, **kw)
        #: 转接服务端(:class:`~d1max_agent.navbridge.NavBridgeServer`);``None`` = 没配,
        #: 永远退回自己的规划。
        self.link = link
        #: 目标号从此刻(毫秒,取模)起算:代理重启、换一个后端,也不跟转接节点手里旧目标的号撞上
        #: (旧目标「已取消」的回执晚到,会被当成新目标的结果)。
        self._goal_id = int(time.time() * 1000) % 1_000_000_000
        #: 这一段交给 Nav2 的目标号;``None`` = 这一段走的是自己的规划(或者没在走)。
        self._nav2_goal: int | None = None
        self._map_dir: Path | None = None
        self._map_req = 0
        self._t_ms = 0
        self._grid_at: float | None = None
        self._guard_since: float | None = None
        self._guard_told = False
        #: 这一段为什么没走 Nav2(给站点看);走了是空串。
        self.fallback_reason = ""
        #: 禁行区回退检查、提前刹按哪个方向查(外审 R6:Nav2 会往后退;``None`` = 按任务的头尾方向)。
        self._travel_sign: int | None = None
        #: 这一拍限速区的上限(外审 R4;送给 Nav2 的 ``speed`` 也按它)。
        self._slow = math.inf
        #: 上一拍送给 Nav2 的限速(复查 P3:MPPI 按它缩倒车上限)。
        self._nav2_speed = V_BASE

    # ------------------------------------------------------------ 图与区域 → 转接节点

    def load_grid(self, map_dir: Path | None) -> None:
        self._map_dir = map_dir
        super().load_grid(map_dir)
        self._send_map()

    def read_grid(self, map_dir: Path | None) -> tuple[Any, str | None]:   # type: ignore[override]
        """运行时换图:线程里读(:meth:`install_grid` 回到事件循环再装);记下目录,装的时候发给 Nav2。
        """
        self._reading_dir = map_dir
        return PlannedNavBackend.read_grid(map_dir)

    def install_grid(self, base: Any, problem: str | None) -> None:
        super().install_grid(base, problem)
        self._map_dir = getattr(self, "_reading_dir", self._map_dir) if base is not None else None
        self._send_map()

    async def set_zones(self, zs: ZoneSet | None) -> None:
        await super().set_zones(zs)
        self._send_map()

    def _send_map(self) -> None:
        if self.link is None or self._map_dir is None:
            return
        self._map_req += 1
        zs = self._effective(self.zones)
        nogo = [[list(p) for p in z.polygon] for z in zs if z.keep_out]
        speed = [{"poly": [list(p) for p in z.polygon], "mps": float(z.max_speed_mps)}
                 for z in (self.zones.zones if self.zones is not None else ())
                 if z.kind == "slow" and z.max_speed_mps and not z.keep_out]
        self.link.send({"type": "map", "req": self._map_req, "map_id": self._map_id,
                        "map_version": "", "dir": str(self._map_dir), "nogo": nogo,
                        "speed": speed})

    # ------------------------------------------------------------ 起、停

    def nav2_ready(self) -> str:
        """Nav2 能不能用;能用回空串,不能用回原因。"""
        if self.link is None:
            return "没配 Nav2 转接"
        if not self.link.connected:
            return "Nav2 转接节点没连上"
        if not self.link.ready:
            return f"Nav2 没就绪({self.link.reason})"
        return ""

    async def _go(self, pose: Pose, op: str) -> None:
        why = self.nav2_ready()
        if why:
            self.fallback_reason = why
            log.warning("%s:%s,这一段退回自己的规划导航", op, why)
            if self.on_event is not None:
                self.on_event("nav_fallback", {"reason": why[:200]})
            self._nav2_goal = None
            await super()._go(pose, op)
            return
        here = await self._here()        # 先取位置:下面查状态到发目标之间不再让出(不让两次派发都过)
        if self._status is not NavStatus.STANDBY or self._planning:
            raise NavRequestError(op, f"只能在 StandBy 下启动,当前 {self._status.value}")
        if self.nogo_at(pose.position.x, pose.position.y) is not None:
            raise NavRequestError(op, "终点在禁行区里")
        self.fallback_reason = ""
        goal_xy = (pose.position.x, pose.position.y)
        # W48:有路网就先在路网上找路,途经点交给 Nav2(NavigateThroughPoses,只是路过)
        via = route(self.route_net, (here.x, here.y), goal_xy) \
            if self.route_net is not None and here is not None else []
        self._goal_id += 1
        self._nav2_goal = self._goal_id
        self.link.goal_states.clear()
        self.link.cmd = None                 # 外审 R7:上一个目标晚到的速度不许驱动这一个
        msg: dict[str, Any] = {"type": "goal", "goal_id": self._goal_id, "x": pose.position.x,
                               "y": pose.position.y, "yaw": pose.yaw}
        if via:
            msg["via"] = [[x, y] for x, y in via]
        self.link.send(msg)
        if here is not None:
            pts = [(here.x, here.y), *via, goal_xy]
            self.planned_length_m = PATH_FACTOR * sum(math.dist(a, b)
                                                      for a, b in zip(pts, pts[1:], strict=False))
        self._guard_since, self._guard_told = None, False
        log.info("%s:交给 Nav2(目标 %d)→ (%.2f, %.2f)", op, self._goal_id, pose.position.x,
                 pose.position.y)
        await HalNavBackend.goto(self, pose)

    def _cancel_nav2(self) -> None:
        g, self._nav2_goal = self._nav2_goal, None
        if g is not None and self.link is not None:
            self.link.send({"type": "cancel", "goal_id": g})

    def _travel(self, here: Any) -> tuple[tuple[float, float, float], float]:
        """走 Nav2 的那一段按真在走的方向查(外审 R6):Nav2 会往后退(脱困 BackUp、MPPI 倒车)。"""
        if self._nav2_goal is None or self._travel_sign is None:
            return super()._travel(here)
        return (here.x, here.y, here.yaw), _heading(here.yaw, self._travel_sign)

    def _backstop_to(self, here: Any, sign: int | None) -> tuple[str, Any]:
        self._travel_sign = sign
        try:
            return self._backstop(here)
        finally:
            self._travel_sign = None

    def _zone_cap_to(self, here: Any, sign: int) -> float:
        self._travel_sign = sign
        try:
            return self._zone_cap(here)
        finally:
            self._travel_sign = None

    def _slow_cap(self, here: Any, sign: int) -> float:
        """限速区的上限(外审 R4、复查 N2):机身(行进方向上从机尾到机头再往前半米、机身宽)这块矩形
        压上的限速区里最低的那个。按多边形相交算(原来每 10 cm 取一个点,窄的区会漏)。"""
        zs = self.zones.zones if self.zones is not None else ()
        slow = [z for z in zs if z.kind == "slow" and z.max_speed_mps and not z.keep_out]
        if not slow:
            return math.inf
        g = self.guard
        hl = getattr(g, "body_len", 0.93) / 2 + getattr(g, "margin", 0.05)
        hw = getattr(g, "body_wid", 0.48) / 2 + getattr(g, "margin", 0.05)
        h = _heading(here.yaw, sign)
        c, sn = math.cos(h), math.sin(h)
        rect = tuple((here.x + c * dx - sn * dy, here.y + sn * dx + c * dy)
                     for dx, dy in ((-hl, -hw), (hl + 0.5, -hw), (hl + 0.5, hw), (-hl, hw)))
        v = math.inf
        for z in slow:
            if _overlap(rect, tuple(z.polygon)):
                v = min(v, float(z.max_speed_mps))
        return v

    async def stop(self) -> None:
        self._cancel_nav2()
        await super().stop()

    async def _enter_terminal(self, status: NavStatus) -> None:
        if status is not NavStatus.SUCCEED:
            self._cancel_nav2()
        self._nav2_goal = None
        if self._guard_told and self.on_event is not None:
            self.on_event("nav_unblocked", {"waited_s": 0.0, "detour": False})
        self._guard_since, self._guard_told = None, False
        await super()._enter_terminal(status)

    def current_path(self) -> tuple[tuple[float, float], ...] | None:
        """正在走的路线(站点地图上画):走 Nav2 的那一段是 Nav2 最近一次规划出的(转接节点抽稀过);
        退回自己规划的那一段照旧。"""
        if self._nav2_goal is None:
            return super().current_path()
        p = getattr(self.link, "plan", None)
        return p[1] if p is not None and p[0] == self._nav2_goal else None

    def health(self) -> dict[str, Any]:
        """设备健康里的一条(``nav2``):Nav2 就绪没有;没就绪写清楚为什么、导航退回自己的规划。"""
        from d1max_contract.devices import device
        why = self.nav2_ready()
        if not why:
            return device("nav2", "online", "Nav2 就绪")
        return device("nav2", "offline", f"{why};导航退回自己的规划")

    # ------------------------------------------------------------ 每拍

    async def step(self, dt_s: float) -> None:
        await super().step(dt_s)
        self._feed()

    def _pose_vel(self, x: float, y: float, yaw: float) -> list[float]:
        """机身系的实测速度,**按里程位置的变化算**(低通):厂家里程报的速度低速时不可信 —— 原地踏步
        也报在走(2026-10-29 实测),Nav2 会以为狗在动。"""
        t = self._now() / 1000.0
        prev, self._vel_prev = getattr(self, "_vel_prev", None), (t, x, y, yaw)
        v = getattr(self, "_vel", [0.0, 0.0, 0.0])
        if prev is None or t - prev[0] <= 1e-3 or t - prev[0] > 1.0:
            self._vel = [0.0, 0.0, 0.0]
            return self._vel
        dt = t - prev[0]
        dx, dy = x - prev[1], y - prev[2]
        c, s = math.cos(yaw), math.sin(yaw)
        raw = [(c * dx + s * dy) / dt, (-s * dx + c * dy) / dt,
               math.remainder(yaw - prev[3], 2 * math.pi) / dt]
        self._vel = [VEL_ALPHA * r + (1 - VEL_ALPHA) * o for r, o in zip(raw, v, strict=True)]
        return self._vel

    def _feed(self) -> None:
        """时钟、里程、``map→odom``、新的感知栅格送给转接节点。"""
        if self.link is None:
            return
        self.link.tick()
        self._t_ms = max(self._t_ms + 1, int(self._now()))
        # 复查 N2:按实际许的发(不再垫到最低档 0.3);0 在 Nav2 里是「不限」,所以垫到能走起来的最低速度
        self._nav2_speed = round(max(min(getattr(self, "_vis", V_BASE), self._vlim(),
                                         self._slow), MIN_TRAVEL_MPS), 3)
        ox, oy, oyaw = self._odom_pose
        est = self.anchor.estimate((ox, oy, oyaw)) if self.anchor.ok(self.odom_ok) else None
        mo = None if est is None else _compose_map_odom(est, (ox, oy, oyaw))
        self.link.send({"type": "tick", "t_ms": self._t_ms, "odom": [ox, oy, oyaw],
                        "vel": self._pose_vel(ox, oy, oyaw), "map_odom": mo,
                        # 复查 N2:按实际许的发(不再垫到最低档 0.3);0 在 Nav2 里是「不限」,所以
                        # 垫到能走起来的最低速度(再低代理这边也会停)
                        "speed": self._nav2_speed})
        view = self.obstacles
        frames = getattr(view, "frames", None)
        if frames:
            f = frames[0]
            if f.at != self._grid_at:
                self._grid_at = f.at
                self.link.send({"type": "grid", "t_ms": self._t_ms, "pose": list(f.pose),
                                "res": float(f.res), "size": int(f.size),
                                "occ": pack_bits(bytes(f.occ), f.size),
                                "known": pack_bits(bytes(f.known), f.size)})

    async def _drive(self, here: Any, dt_s: float) -> None:
        if self._nav2_goal is None:
            await super()._drive(here, dt_s)              # 这一段走的是自己的规划
            return
        if self._hold:
            await self._send(0.0, 0.0, dt_s)
            return
        vm, lv = self._v_meas, self._last_vx
        # 外审 R6:按真在走的方向查;复查 N5:实测刚过零、上一条命令还没停(可能还在执行)时按那条的方向
        moving = (1 if vm > 0.02 else -1 if vm < -0.02 else
                  1 if lv > 1e-6 else -1 if lv < -1e-6 else None)
        verdict, z = self._backstop_to(here, moving)
        if verdict == "inside":
            await self._inside_nogo(z, here)
            return
        if verdict == "near":
            await self._send(0.0, 0.0, dt_s)              # 这一拍刹住;Nav2 的地图里那是致命格
            return
        link = self.link
        while link.goal_states:
            m = link.goal_states.popleft()
            if m["goal_id"] != self._nav2_goal or m["state"] == "accepted":
                continue
            if m["state"] == "succeeded":
                await self._enter_terminal(NavStatus.SUCCEED)
                return
            await self._fail(f"Nav2 {m['state']}:{m.get('reason', '') or '没说原因'}"[:200])
            return
        why = self.nav2_ready()
        if why:
            await self._fail(why)
            return
        c = link.cmd
        now = link._now()
        if c is None or now - c[0] > CMD_FRESH_S:
            await self._send(0.0, 0.0, dt_s)
            return
        if len(c) > 5 and c[5] != self._nav2_goal:
            # 外审 R7、复查 N3:别的目标的、转接节点那会儿没在走目标(没标号)的速度都不认
            await self._send(0.0, 0.0, dt_s)
            return
        src = c[6] if len(c) > 6 else "ctl"
        if src != "beh":
            # 复查 P1:控制器的速度要等这个目标自己的规划路线来了、而且比路线**晚到**才认 —— 控制器
            # 拿到新路线之后才出新目标的速度;路线之前收到(门槛打开前缓存着的)不管标的是谁都可能是
            # 上一个目标的。脱困动作(BackUp,复查第三轮 P4)常在规划不出来时做,不等路线;
            # 目标号照样要对。
            p = getattr(link, "plan", None)
            if p is None or p[0] != self._nav2_goal:
                await self._send(0.0, 0.0, dt_s)
                return
            prx = getattr(link, "plan_rx", {}).get(self._nav2_goal)
            if len(c) > 7 and prx is not None and c[7] < prx:
                await self._send(0.0, 0.0, dt_s)
                return
        _, _, vx, _vy, wz = c[:5]
        sign = 1 if vx >= 0 else -1
        if abs(vx) > 1e-6 and sign != moving:
            verdict, z = self._backstop_to(here, sign)     # 要换方向:往要去的那边也查一遍
            if verdict == "inside":
                await self._inside_nogo(z, here)
                return
            if verdict == "near":
                await self._send(0.0, 0.0, dt_s)
                return
        self._vis = self._vis_cap(here, sign)
        self._slow = self._slow_cap(here, sign)
        cap = min(self._vlim(), self._zone_cap_to(here, sign), self._vis, self._slow)
        vx = max(-cap, min(cap, vx))
        wz = max(-self._wmax, min(self._wmax, wz))
        vx, wz = self._deband(vx, wz, rev_max=MPPI_REVERSE_RATIO * self._nav2_speed)
        # 外审 R5:整形抬到死区以后又超了上限(限速比死区还低)—— 走不了这么慢,停,不往上抬
        if abs(vx) > cap + 1e-9:
            vx = 0.0
        if abs(wz) > self._wmax + 1e-9:
            wz = 0.0
        if self._turn_hits_map(vx, wz, here):
            from d1max_agent.obstacles import Verdict
            self._guarded(Verdict(False, "原地转会扫到地图上的墙(窄处不转)"), here)
            await self._send(0.0, 0.0, dt_s)
            return
        if self.guard is not None and self.obstacles is not None and (abs(vx) > 1e-6
                                                                     or abs(wz) > 1e-6):
            v = self._guard_check(vx, wz)
            if not v.ok and not v.hits and "障碍数据" not in v.reason \
                    and abs(vx) > UNKNOWN_SLOW_MPS:
                vx = math.copysign(UNKNOWN_SLOW_MPS, vx)      # 只有看不见的:快了就慢下来再查一遍
                v = self._guard_check(vx, wz)
            if not v.ok and (v.hits or "障碍数据" in v.reason):
                self._guarded(v, here)
                await self._send(0.0, 0.0, dt_s)
                return
        if self._guard_told and self.on_event is not None:
            self.on_event("nav_unblocked", {"waited_s": round(self._secs()
                                                              - (self._guard_since or 0), 1),
                                            "detour": True})
        self._guard_since, self._guard_told = None, False
        await self._send(vx, wz, dt_s)

    def _deband(self, vx: float, wz: float, *,
                rev_max: float = math.inf) -> tuple[float, float]:
        """运控有死区(低于它的前进速度 HAL 直接拒、转速发了也不转):Nav2 起步、收尾时会发很小的速度。
        低于死区一半的当 0,另一半抬到死区。

        复查 P3:``rev_max`` = MPPI 这会儿最多倒多快(限速 × :data:`MPPI_REVERSE_RATIO`)。
        它被限速压到死区以下时,门槛改成它的一半 —— Nav2 要的倒车照最小能走的倒车执行
        (抬上去以后超了上限的,外面照样停)。"""
        db = max(float(getattr(self._caps, "deadband_vx", 0.0) or 0.0), MIN_TRAVEL_MPS)
        if 0.0 < abs(vx) < db:
            thr = db / 2 if vx > 0 else min(db / 2, rev_max / 2)
            vx = 0.0 if abs(vx) < thr else math.copysign(db, vx)
        dbw = max(float(getattr(self._caps, "deadband_wz", 0.0) or 0.0), MIN_TURN_RPS)
        if 0.0 < abs(wz) < dbw:
            wz = 0.0 if abs(wz) < dbw / 2 else math.copysign(dbw, wz)
        return vx, wz

    def _vis_cap(self, here: Any, travel: int) -> float:
        """看得清就快(决策 76):此刻许跑多快。两样取小的,
        最高 :data:`V_FAST`、最低 :data:`VIS_SLOW_MPS`:

        1. 前方看得清多远:机身宽的走廊里、机头前 :data:`FRONT_BLIND_M` 之外,
           第一处挡或看不见的格子有多远;按「延迟 + 刹车距离 + :data:`CLEAR_MARGIN_M`」
           反推最快多快(刹车用守卫那份减速度)。
        2. 地图之外的东西有多近:前方 :data:`VIS_LOOK_M`、两边各外扩 :data:`VIS_SIDE_M` 里看到的挡,
           离地图上的墙 :data:`STATIC_NEAR_M` 以内的当墙不算;剩下的(人、箱子)最近那个越近越慢。

        感知不新鲜、没配避障::data:`V_BASE`。"""
        view, g = self.obstacles, self.guard
        if view is None or g is None or not getattr(view, "frames", None):
            return V_BASE
        from d1max_agent.obstacles import FRESH_S
        if view.age() > FRESH_S:
            return V_BASE
        f = view.frames[0]
        n, res = f.size, f.res
        c = (np.arange(n) - n / 2 + 0.5) * res
        occ = f.occ.reshape(n, n).astype(bool)
        known = f.known.reshape(n, n).astype(bool)
        sgn = 1.0 if travel >= 0 else -1.0
        X = np.broadcast_to(c[:, None] * sgn, (n, n))            # 行进方向上的前后
        Y = np.broadcast_to(c[None, :], (n, n))
        margin = getattr(g, "margin", 0.05)
        hl = getattr(g, "body_len", 0.93) / 2 + margin
        hw = getattr(g, "body_wid", 0.48) / 2 + margin
        corr = (np.abs(Y) <= hw) & (X > hl)
        bad = corr & (occ | (~known & (X > hl + FRONT_BLIND_M)))
        clear = float(X[bad].min() - hl) if bad.any() else float(X[corr].max() - hl)
        a, t = float(getattr(g, "decel", 0.5)), float(getattr(g, "latency", 0.2))
        room = clear - CLEAR_MARGIN_M
        v_clear = a * (-t + math.sqrt(t * t + 2.0 * room / a)) if room > 0 else 0.0
        v_dyn = V_FAST
        zone = occ & (X > hl - 0.2) & (X <= hl + VIS_LOOK_M) & (np.abs(Y) <= hw + VIS_SIDE_M)
        if zone.any():
            xs, ys = X[zone] * sgn, Y[zone]                        # 拍下这一帧时的机身系
            dist = np.hypot(np.maximum(np.abs(X[zone]) - hl, 0.0),
                            np.maximum(np.abs(ys) - hw, 0.0))
            dyn = ~self._near_static(xs, ys, f.pose, here)
            if dyn.any():
                d = float(dist[dyn].min())
                k = min(max((d - VIS_SLOW_AT_M) / (VIS_LOOK_M - VIS_SLOW_AT_M), 0.0), 1.0)
                v_dyn = VIS_SLOW_MPS + (V_FAST - VIS_SLOW_MPS) * k
        return max(min(V_FAST, v_clear, v_dyn), VIS_SLOW_MPS)

    def _near_static(self, xs: Any, ys: Any, frame_pose: tuple[float, float, float],
                     here: Any) -> Any:
        """拍下时机身系的点 → 离地图上的墙 :data:`STATIC_NEAR_M` 以内的(布尔数组)。
        没图都算 False。"""
        if self._base is None:
            return np.zeros(len(xs), dtype=bool)
        blocked, res, (ox, oy) = self._base
        fx, fy, fyaw = frame_pose
        c, s = math.cos(fyaw), math.sin(fyaw)
        odx, ody = fx + c * xs - s * ys, fy + s * xs + c * ys      # 里程系
        mo = _compose_map_odom(here, self._odom_pose)
        c2, s2 = math.cos(mo[2]), math.sin(mo[2])
        mx, my = mo[0] + c2 * odx - s2 * ody, mo[1] + s2 * odx + c2 * ody
        h, w = blocked.shape
        r0 = np.floor((my - oy) / res).astype(int)
        k0 = np.floor((mx - ox) / res).astype(int)
        span = int(math.ceil(STATIC_NEAR_M / res))
        near = np.zeros(len(xs), dtype=bool)
        for dr in range(-span, span + 1):
            for dk in range(-span, span + 1):
                r, k = r0 + dr, k0 + dk
                inb = (r >= 0) & (r < h) & (k >= 0) & (k < w)
                hit = np.zeros(len(xs), dtype=bool)
                hit[inb] = blocked[r[inb], k[inb]]
                near |= hit
        return near

    def _turn_hits_map(self, vx: float, wz: float, here: Any) -> bool:
        """原地转扫过的那片(守卫算的扇形,机身外)压上地图里的墙没有。不靠雷达(墙进了两侧盲区、记忆
        过期都还在),不靠 Nav2 的配置:2026-10-29 仿真里 Nav2 的脱困原地转(Spin)在 0.85 m 门里扫到
        门框。直走不查:窄处两侧本来就贴得近,定位差几厘米就会误拦。"""
        if self._base is None or self.guard is None or abs(vx) > TURN_VX or abs(wz) < 1e-6:
            return False
        blocked, res, (ox, oy) = self._base
        h, w = blocked.shape
        c, s = math.cos(here.yaw), math.sin(here.yaw)
        for px, py in self.guard.swept(0.0, wz, 0.0, (self._last_wz, self._w_meas)):
            x, y = here.x + c * px - s * py, here.y + s * px + c * py
            r, k = int((y - oy) // res), int((x - ox) // res)
            if 0 <= r < h and 0 <= k < w and blocked[r, k]:
                return True
        return False

    def _guarded(self, v: Any, here: Any) -> None:
        """守卫拦住了 Nav2 的速度(雷达真看到挡,或者障碍数据不新鲜):拦久了报一次被挡。"""
        now = self._secs()
        if self._guard_since is None:
            self._guard_since = now
            log.info("守卫拦住 Nav2 的速度:%s", v.reason)
        if not self._guard_told and now - self._guard_since >= GUARD_REPORT_S \
                and self.on_event is not None:
            self._guard_told = True
            self.on_event("nav_blocked", {"reason": v.reason[:200], "x": round(here.x, 2),
                                          "y": round(here.y, 2),
                                          "waited_s": round(now - self._guard_since, 1)})
