"""代理这一侧的障碍(W11 设计稿 §3–5,W08 决定 8):感知给的局部栅格 → 滚动记忆 → 守卫。

- :class:`ObstacleView`:收感知的栅格(``on_grid``,记下**代理收到时的单调钟**)与每拍的里程
  (``note_odom``);留最近 :data:`MEMORY_S` 秒的栅格,查一个点(此刻狗身系)时按里程换到每一帧当时的
  狗身系,**从新到旧**找第一个「看见过」它的帧:那一帧说挡就挡、说空就空;都没看见过 = 未知(当挡)。
  转身时身后、身侧刚看过的格子靠这个;狗自己刚站过的地方算空。
- :class:`ObstacleGuard`:一条速度命令发出去之前,按这条命令把机身矩形(+ 余量)往前推「链路延迟 +
  刹停距离 + 余量」那么远(刹停按**里程实测速度**,W08 决定 8),原地转是外接圆;扫过的格子有挡或
  未知 → 不发。机身此刻占着的那块不查。往后退(狗尾为前,W09i)一样,往后推。
- 新鲜度按代理收到时的单调钟:最新一帧 ≤ :data:`FRESH_S` 算新鲜;没新鲜的 → 守卫当挡(原地等);
  超过 :data:`LOST_S` → 「雷达掉线」(后端发 13331,引擎中止)。
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from d1max_contract.obsbridge import Grid

#: 记忆多久(秒)。前后两台半球雷达都看不见机身两侧那条带,转身、急转时扫过它全靠往前走时看过的记忆;
#: 被挡等了几秒再绕,5 s 不够(仿真里绕障转身就卡住)。取 10 s,真机调(人 10 s 内横着走进那条带的风险
#: 写进已知限制)。
MEMORY_S = 10.0
#: 工单 21(2026-10-28 C40221 下午):狗一停下超过 10 s 两侧就算没看过,之后哪怕转 5° 都要先挪、挪不了
#: 就原地等 20 s 放弃。狗站着没挪窝(离那一帧的位置不到 :data:`MEMORY_STILL_M`,原地转也算没挪)时,
#: 那一帧留到这么久;挪了窝照旧 :data:`MEMORY_S`。(站着的时候人从正侧面走进盲带,本来就看不见 ——
#: 原来的 10 s 也一样,写进已知限制。)
MEMORY_STILL_S = 60.0
MEMORY_STILL_M = 0.15
FRESH_S = 0.3
LOST_S = 2.0
ODOM_KEEP_S = 12.0
#: 感知链路的延迟(秒,雷达帧 → 代理收到;待真机量):栅格配「收到时刻 − 它」那一刻的里程位姿
#: (内审应修 4:配收到时刻的位姿,障碍会被往前多算 v·延迟)。
PERCEPTION_LATENCY_S = 0.15
#: 「狗自己刚站过的地方算空」只信这么久(内审再议:人可能跟着走进来)。
SELF_SWEPT_S = 3.0
#: W41 全局重定位:感知攒一份墙那一层用多久(跟感知节点的 ``WALL_EVERY_S`` 一样)、这一段里狗挪了不到
#: 这么远、转了不到这么多才算「没动」(动着攒的点是糊的);一份多久以内算新鲜。
WALL_SPAN_S = 1.2
WALL_STILL_M = 0.05
WALL_STILL_RAD = 0.035
WALL_MAX_AGE_S = 2.5


def _compose(a: tuple[float, float, float], p: tuple[float, float]) -> tuple[float, float]:
    """位姿 ``a``(x, y, yaw)下的局部点 → 外面的坐标。"""
    c, s = math.cos(a[2]), math.sin(a[2])
    return (a[0] + c * p[0] - s * p[1], a[1] + s * p[0] + c * p[1])


def _inv(a: tuple[float, float, float], w: tuple[float, float]) -> tuple[float, float]:
    """外面的点 → 位姿 ``a`` 下的局部坐标。"""
    c, s = math.cos(a[2]), math.sin(a[2])
    dx, dy = w[0] - a[0], w[1] - a[1]
    return (c * dx + s * dy, -s * dx + c * dy)


@dataclass
class _Frame:
    at: float
    pose: tuple[float, float, float]           # 这一帧那一刻狗在里程系里的位姿
    occ: Any                                   # numpy uint8,size × size
    known: Any
    size: int
    res: float
    #: 可疑的格子(W29 复查,决策 42):这一帧可疑 = 挡,旧帧的「空」补不上来。
    suspect: Any = None


@dataclass
class ObstacleView:
    monotonic: Callable[[], float] = time.monotonic
    memory_s: float = MEMORY_S
    frames: deque = field(default_factory=deque)
    odom: deque = field(default_factory=deque)       # (时刻, (x, y, yaw))
    #: 感知自检:none / initializing / ok / extrinsic_bad
    check: str = "none"
    reason: str = ""
    rear: bool = False
    #: 后雷达外参真标过(W09i;感知报的):狗尾为前才许自己走。
    rear_cal: bool = False
    connected: bool = False
    latency_s: float = PERCEPTION_LATENCY_S
    #: W41:感知最新一份墙那一层(狗身系的点)、收到的时刻、攒的时候狗没动。
    wall: Any = None
    wall_at: float = -math.inf
    wall_still: bool = False
    #: 工单 21:狗最近一次挪窝(离上次站的地方超过 :data:`MEMORY_STILL_M`)的位置和时刻。
    _still_at: tuple[float, float] | None = None
    _still_since: float = -math.inf

    # ------------------------------------------------------------ 喂进来

    def note_odom(self, x: float, y: float, yaw: float) -> None:
        now = self.monotonic()
        if self._still_at is None or math.hypot(x - self._still_at[0],
                                                y - self._still_at[1]) > MEMORY_STILL_M:
            self._still_at, self._still_since = (float(x), float(y)), now     # 挪了窝
        self.odom.append((now, (float(x), float(y), float(yaw))))
        while self.odom and now - self.odom[0][0] > ODOM_KEEP_S:
            self.odom.popleft()

    def pose_at(self, t: float) -> tuple[float, float, float] | None:
        """``t`` 时刻狗在里程系里的位姿(前后两拍线性插;超出范围取最近的)。"""
        if not self.odom:
            return None
        prev = None
        for at, p in self.odom:
            if at >= t:
                if prev is None:
                    return p
                (t0, p0) = prev
                f = 0.0 if at == t0 else (t - t0) / (at - t0)
                dyaw = math.remainder(p[2] - p0[2], 2 * math.pi)
                return (p0[0] + f * (p[0] - p0[0]), p0[1] + f * (p[1] - p0[1]), p0[2] + f * dyaw)
            prev = (at, p)
        return self.odom[-1][1]

    def on_grid(self, g: Grid) -> None:
        now = self.monotonic()
        self.check, self.reason, self.rear, self.rear_cal = g.check, g.reason, g.rear, g.rear_cal
        pose = self.pose_at(now - self.latency_s)
        if pose is None or g.check != "ok":
            return                                   # 没有里程配不上;自检没过的不用
        occ, known = g.bits()
        self.frames.appendleft(_Frame(at=now, pose=pose,
                                      occ=np.frombuffer(occ, dtype=np.uint8),
                                      known=np.frombuffer(known, dtype=np.uint8),
                                      size=g.size, res=g.res,
                                      suspect=np.frombuffer(g.suspect_bits(), dtype=np.uint8)))
        while self.frames:
            f, age = self.frames[-1], now - self.frames[-1].at
            if age <= self.memory_s or (age <= MEMORY_STILL_S
                                        and self._still_since <= f.at + self.memory_s):
                break                                   # 工单 21:它过期之前狗就站住了、一直没挪窝
            self.frames.pop()

    def on_wall(self, w: Any) -> None:
        """感知发来一份墙那一层(W41 全局重定位)。"""
        now = self.monotonic()
        a = self.pose_at(now - self.latency_s - WALL_SPAN_S)
        b = self.pose_at(now - self.latency_s)
        still = (a is not None and b is not None and len(self.odom) >= 2
                 and self.odom[0][0] <= now - self.latency_s - WALL_SPAN_S
                 and math.hypot(b[0] - a[0], b[1] - a[1]) <= WALL_STILL_M
                 and abs(math.remainder(b[2] - a[2], 2 * math.pi)) <= WALL_STILL_RAD)
        self.wall, self.wall_at, self.wall_still = w.points(), now, still

    def wall_scan(self, *, since: float = -math.inf,
                  max_age: float = WALL_MAX_AGE_S) -> list[tuple[float, float]] | None:
        """最新一份墙那一层:新鲜、攒的时候狗没动、``since`` 之后收到的;不然 None。"""
        if self.wall is None or not self.wall_still or self.wall_at < since \
                or self.monotonic() - self.wall_at > max_age:
            return None
        return list(self.wall)

    def on_connect(self) -> None:
        self.connected = True

    def on_disconnect(self) -> None:
        self.connected = False

    # ------------------------------------------------------------ 状态与查询

    def age(self) -> float:
        return math.inf if not self.frames else self.monotonic() - self.frames[0].at

    def state(self) -> str:
        """``ok`` / ``stale``(短暂不新鲜:原地等)/ ``lost``(雷达掉线)/ ``extrinsic_bad`` /
        ``initializing``。"""
        if self.check == "extrinsic_bad":
            return "extrinsic_bad"
        a = self.age()
        if a <= FRESH_S:
            return "ok"
        if a <= LOST_S:
            return "stale"
        if self.check == "initializing" and self.connected:
            return "initializing"
        return "lost"

    def near_ahead(self, travel: int, half_len: float, half_wid: float) -> float:
        """最新一帧(新鲜的才算)里,行进方向上机身前沿往前、机身两边各宽 ``half_wid`` 的带子里,最近的
        「挡」离前沿多远(米);没有是 inf。只看「挡」:「看不见」守卫管,
        算进来的话侧面盲带会让狗一直慢。
        近障降速用(W12),不管安全 —— 帧是此刻之前 ≤ :data:`FRESH_S` 的,差几厘米无所谓。"""
        if not self.frames or self.age() > FRESH_S:
            return math.inf
        f = self.frames[0]
        n = f.size
        c = (np.arange(n) - n / 2 + 0.5) * f.res
        occ = f.occ.reshape(n, n).astype(bool)
        x = c[:, None] * (1.0 if travel >= 0 else -1.0)       # 行 → 行进方向上的前后
        y = c[None, :]
        band = occ & (x > half_len) & (np.abs(y) <= half_wid)
        if not band.any():
            return math.inf
        return float((np.broadcast_to(x, band.shape)[band] - half_len).min())

    def lookup(self, pts: list[tuple[float, float]], pose_now: tuple[float, float, float]
               ) -> tuple[list[int], list[int]]:
        """此刻狗身系的点 → (挡的点的下标, 未知的点的下标)。numpy 一批算(内审应修 7:逐点逐帧
        在狗上一拍要几十毫秒,事件循环上)。"""
        if not pts:
            return [], []
        P = np.asarray(pts, dtype=float)
        c0, s0 = math.cos(pose_now[2]), math.sin(pose_now[2])
        wx = pose_now[0] + c0 * P[:, 0] - s0 * P[:, 1]
        wy = pose_now[1] + s0 * P[:, 0] + c0 * P[:, 1]
        res = np.full(len(P), -1, dtype=np.int8)          # -1 没看见过、0 空、1 挡
        for f in list(self.frames):
            todo = res < 0
            if not todo.any():
                break
            c, s = math.cos(f.pose[2]), math.sin(f.pose[2])
            dx, dy = wx - f.pose[0], wy - f.pose[1]
            lx, ly = c * dx + s * dy, -s * dx + c * dy
            r = np.floor(lx / f.res + f.size / 2).astype(int)
            cc = np.floor(ly / f.res + f.size / 2).astype(int)
            inb = todo & (r >= 0) & (r < f.size) & (cc >= 0) & (cc < f.size)
            k = np.where(inb, r * f.size + cc, 0)
            seen = inb & (f.known[k] == 1)
            res[seen] = f.occ[k[seen]]
            if f.suspect is not None:
                # 这一帧说这一格可疑(孤立凸起点,可能是细杆子):当挡,不往更旧的帧找(决策 42)
                sus = inb & (f.suspect[k] == 1)
                res[sus] = 1
        if (res < 0).any():
            res[(res < 0) & self._self_swept(wx, wy)] = 0
        return np.nonzero(res == 1)[0].tolist(), np.nonzero(res < 0)[0].tolist()

    #: 机身矩形的一半(查「狗自己刚站过的地方」用;跟守卫的机身尺寸一致)。
    body_hl: float = 0.465
    body_hw: float = 0.24

    def _self_swept(self, wx: Any, wy: Any) -> Any:
        """这些点(里程系)是不是狗自己最近 :data:`SELF_SWEPT_S` 秒里机身占过的地方:雷达看不见机身
        底下和机身两侧,急转时后角甩出去扫到的正是刚站过的地方 —— 狗刚站过,那里是空的。"""
        now = self.monotonic()
        out = np.zeros(len(wx), dtype=bool)
        for at, pose in reversed(self.odom):
            if now - at > SELF_SWEPT_S:
                break
            c, s = math.cos(pose[2]), math.sin(pose[2])
            dx, dy = wx - pose[0], wy - pose[1]
            lx, ly = c * dx + s * dy, -s * dx + c * dy
            out |= (np.abs(lx) <= self.body_hl) & (np.abs(ly) <= self.body_hw)
        return out


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    #: 挡住的点(此刻狗身系)—— 被挡久了后端把它们记进临时障碍层。
    hits: tuple[tuple[float, float], ...] = ()
    unknown: int = 0


@dataclass
class ObstacleGuard:
    body_len: float = 0.93
    body_wid: float = 0.48
    margin: float = 0.05
    latency: float = 0.2                     # 链路延迟(秒,待 W00d 量)
    decel: float = 0.5                       # 刹车减速度(m/s²,待真机量)
    extra: float = 0.3                       # 再留这么多(米)
    step: float = 0.05                       # 扫过区的采样间距(米)
    #: 原地转:按这一拍转过去再刹停的角度查扇形(工单 21:原来不管转多少都查整个外接圆,差 5° 也被
    #: 侧后方看不见的格子拦住)。角减速度、再留的角度待真机量;算出来过半圈就查整圈。
    ang_decel: float = 1.0                   # 弧度/秒²
    extra_rad: float = 0.2

    def turn_reach(self, wz: float) -> float:
        w = abs(wz)
        return w * self.latency + w * w / (2.0 * self.ang_decel) + self.extra_rad

    def reach(self, v: float) -> float:
        v = abs(v)
        return v * self.latency + v * v / (2.0 * self.decel) + self.extra

    def swept(self, vx: float, wz: float, v_meas: float) -> list[tuple[float, float]]:
        """这条命令扫过的机身区域里的采样点(此刻狗身系)。往前走:沿这条命令的圆弧推
        ``reach(max(实测, 命令))`` 米,每一步一个机身矩形(往后退就往后推);原地转(前进 ≈ 0、在转):
        外接圆。"""
        hl, hw = self.body_len / 2 + self.margin, self.body_wid / 2 + self.margin
        pts: set[tuple[float, float]] = set()
        s = self.step
        if abs(vx) < 1e-6 and abs(wz) > 1e-6 and self.turn_reach(wz) < math.pi:
            # 扇形:外接圆里的采样点(跟整圈一样取格子中间),转过 0 → ±turn_reach 的某一刻落在机身
            # 矩形里就算扫到;角步长让外接圆上相邻两刻不超过一格
            th, r = self.turn_reach(wz), math.hypot(hl, hw)
            n = int(r / s) + 1
            g = (np.arange(-n, n) + 0.5) * s
            X, Y = np.meshgrid(g, g, indexing="ij")
            X, Y = X.ravel(), Y.ravel()
            keep = X * X + Y * Y <= r * r
            X, Y = X[keep], Y[keep]
            hit = np.zeros(len(X), dtype=bool)
            k = max(1, int(math.ceil(th * r / s)))
            sign = 1.0 if wz > 0 else -1.0
            for i in range(k + 1):
                a = sign * th * i / k                   # 机身转了 a:把点转回 −a 看在不在矩形里
                c, sn = math.cos(a), math.sin(a)
                lx, ly = c * X + sn * Y, -sn * X + c * Y
                hit |= (np.abs(lx) <= hl) & (np.abs(ly) <= hw)
            for x, y in zip(X[hit], Y[hit], strict=True):
                pts.add((round(float(x), 3), round(float(y), 3)))
            return self._outside_body(pts, hl, hw)
        if abs(vx) < 1e-6 and abs(wz) > 1e-6:
            r = math.hypot(hl, hw)
            n = int(r / s) + 1
            for i in range(-n, n):
                for j in range(-n, n):
                    x, y = (i + 0.5) * s, (j + 0.5) * s     # 取格子中间,不压在格边上
                    if x * x + y * y <= r * r:
                        pts.add((round(x, 3), round(y, 3)))
            return self._outside_body(pts, hl, hw)
        dist = self.reach(max(abs(v_meas), abs(vx)))
        corner = math.hypot(hl, hw)
        # 角点走过的弧长只在转弯半径比机身对角线还小时才比中心的长 —— 那种急转下面直接并上外接圆
        # (内审应修 3;突变里「按角点弧长取步数」跟「并外接圆」互相顶替,只留后一条)
        k = max(1, int(dist / s))
        xs = [(i + 0.5) * s for i in range(int(-hl / s) - 1, int(hl / s) + 1)
              if abs((i + 0.5) * s) < hl] + [-hl, hl]
        ys = [(j + 0.5) * s for j in range(int(-hw / s) - 1, int(hw / s) + 1)
              if abs((j + 0.5) * s) < hw] + [-hw, hw]
        edge = [(x, y) for x in xs for y in ys
                if abs(abs(x) - hl) < s or abs(abs(y) - hw) < s]      # 机身轮廓线
        sign = -1.0 if vx < 0 else 1.0
        for t in range(k + 1):
            d = sign * dist * t / k                      # 走过的路(带方向):往后退是负的
            if abs(wz) < 1e-6 or abs(vx) < 1e-6:
                px, py, th = d, 0.0, 0.0
            else:
                rad = vx / wz
                th = d / rad
                px, py = rad * math.sin(th), rad * (1 - math.cos(th))
            c, sn = math.cos(th), math.sin(th)
            # 每一步机身的轮廓线(采样间距不大于一格,轮廓线扫过的就是整个扫掠区;起点那块是狗自己)
            for x, y in edge:
                pts.add((round(px + c * x - sn * y, 3), round(py + sn * x + c * y, 3)))
        if abs(wz) > 1e-6 and abs(vx / wz) < corner:
            # 转弯半径比机身对角线还小:跟原地转差不多,把外接圆也并进来
            n = int(corner / s) + 1
            for i in range(-n, n):
                for j in range(-n, n):
                    x, y = (i + 0.5) * s, (j + 0.5) * s
                    if x * x + y * y <= corner * corner:
                        pts.add((round(x, 3), round(y, 3)))
        return self._outside_body(pts, hl, hw)

    @staticmethod
    def _outside_body(pts: set[tuple[float, float]], hl: float, hw: float
                      ) -> list[tuple[float, float]]:
        """机身此刻占着的那块(含余量)不查:那里只能是狗自己,雷达也看不见机身底下(感知把机身的点
        滤掉了,那些格子永远是「未知」)。**分辨率**:采样间距 5 cm,紧贴这块边上不到 2 cm 的窄条可能
        漏查 —— 落在 5 cm 的余量里(内审应修 3 的复核)。"""
        return sorted(p for p in pts if abs(p[0]) > hl + 1e-9 or abs(p[1]) > hw + 1e-9)

    def check(self, vx: float, wz: float, v_meas: float, view: ObstacleView,
              pose_now: tuple[float, float, float]) -> Verdict:
        if abs(vx) < 1e-6 and abs(wz) < 1e-6:
            return Verdict(True)                     # 原地等:不动就不会撞
        st = view.state()
        if st != "ok":
            return Verdict(False, f"障碍数据{_STATE_TEXT.get(st, st)}")
        pts = self.swept(vx, wz, v_meas)
        hit, unknown = view.lookup(pts, pose_now)
        if not hit and not unknown:
            return Verdict(True)
        return Verdict(False, f"扫过区有挡 {len(hit)} 处、看不见 {len(unknown)} 处",
                       hits=tuple(pts[i] for i in hit), unknown=len(unknown))


_STATE_TEXT = {"stale": "不新鲜", "lost": "断了", "extrinsic_bad": "外参自检没过",
               "initializing": "还在自检", "none": "没有"}


def describe(state: str) -> str:
    return _STATE_TEXT.get(state, state)
