"""自建导航后端(乙路线)—— 在自己建的图上点到点走。

设计见 ``docs/superpowers/specs/2026-09-02-自建导航后端设计.md``。三句话概括:

* **位姿**来自 ROS 侧的定位桥(``tools/ros2_pose_bridge.py``,TCP/JSONL 单向)。
* **运动**经 ``DeviceBackend.walk`` 下发给旁路进程,是**脉冲式**的:
  转一下 -> 停 -> 重新读定位 -> 再来一拍。不是连续控制器。
* **地图**是本地文件(map_server 格式的 ``.yaml`` + ``.pgm``),不经厂商。

**为什么脉冲式、为什么不接 Nav2:** ``Move`` 的量是百分比且有不小的死区
(清单 #37: 0.11 几乎不动、0.3~0.5 才走)。连续控制器在接近目标时会自然把
控制量降下去,于是指令被静默吞掉 —— 机器不动,控制器却以为自己在走。
绕开死区的唯一办法是**控制量始终保持在死区之上,靠缩短脉冲时长做精细逼近**。
这是本模块每一个常数的由来,不是调参偏好。

真理源规则(规范 §3.4)在这条路线上的对应写法: 定位桥是地图系位姿的唯一
真理源;旁路进程的 ``odom`` 是腿式里程、会漂,只能做交叉校验。所以本模块
一个字都不读 odom。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_patrol.backends.base import (
    BackendDisconnected,
    DeviceBackend,
    LocStatusEvent,
    NavBackend,
    NavConnectionError,
    NavRequestError,
    NavStatusEvent,
)
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)
from d1max_patrol.protocol.pose_frames import (
    PROTO_VERSION,
    PoseFrame,
    PoseHello,
    PoseProtocolError,
    decode_frame,
)


def wrap_angle(angle: float) -> float:
    """把角度归一到 (-pi, pi]。"""
    return math.atan2(math.sin(angle), math.cos(angle))


@dataclass(frozen=True)
class LocalNavParams:
    """控制器常数。

    改这里之前先读 ``__post_init__`` —— 它会拒绝一组物理上做不到的参数,
    而"做不到"的根源正是死区。
    """

    #: 到点判据。
    xy_tol: float = 0.25
    yaw_tol: float = 0.20
    #: 航向误差超过这个值就先纯转向,不做转+走的复合动作 —— 复合动作在脉冲式
    #: 控制下落点难预测,而我们每一拍之后都会重新测量,没必要省这一步。
    turn_enter: float = 0.35

    #: 控制量的上下限。**下限是死区,不是偏好**(#37: 0.11 几乎不动)。
    min_fwd: float = 0.30
    max_fwd: float = 0.50
    #: 假设(待真机验证): 转向那一路的死区一次都没量过(#56),先按平移同量级取。
    #: #56 量出来之后改这一个常数即可,别动别的。
    min_yaw: float = 0.30
    max_yaw: float = 0.50

    #: 误差 -> 控制量的比例。上下限会把它夹住,所以这个值不敏感。
    fwd_gain: float = 0.6
    yaw_gain: float = 0.6

    #: 一拍的标称/最短时长。短于 min_pulse_s 的脉冲没有物理意义 ——
    #: 起步和停止本身就要占掉几百毫秒。
    pulse_s: float = 0.8
    min_pulse_s: float = 0.3
    #: 每拍之后等定位追上来再测量。定位是 10 Hz 的,不等会读到旧位姿,
    #: 于是同一个误差被连着纠两次,表现为来回摆。
    settle_s: float = 0.35

    #: 假设(待真机验证): 控制量 1.0 对应的速度,仿真器按同一组数建模
    #: (#38: fwd=0.5 大致 0.6 m/s)。只用来估脉冲时长,估偏了会多走几拍,
    #: 不会走错方向 —— 每拍重新测量就是这份容错的来源。
    fwd_speed_mps: float = 1.2
    yaw_speed_rps: float = 1.5

    #: 连续多久没拿到 ok 的位姿就判 LocLost。现场 TF 抖一下很常见,
    #: 单帧丢失不算数。
    loc_lost_after_s: float = 1.5

    #: 连续这么多拍没有实质进展就判卡住。不拖到超时才发现 —— 那意味着白白
    #: 浪费一整个 goal_timeout_s,而且机器一直在原地蹬。
    stuck_pulses: int = 4
    stuck_progress_m: float = 0.02
    stuck_progress_rad: float = 0.02

    goal_timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if self.min_fwd > self.max_fwd or self.min_yaw > self.max_yaw:
            raise ValueError("控制量下限不能大于上限")
        floor_xy = self.min_fwd * self.fwd_speed_mps * self.min_pulse_s
        if self.xy_tol < floor_xy:
            raise ValueError(
                f"xy_tol={self.xy_tol} 小于死区允许的最小一步 {floor_xy:.3f} m。"
                f"控制量不能低于 min_fwd={self.min_fwd}(低于就不动了,清单 #37),"
                f"脉冲不能短于 min_pulse_s={self.min_pulse_s},"
                f"所以一步至少这么长;容差比它还小就永远收不了敛。"
                f"要收紧容差得先想办法压低死区,不是改这个数。")
        floor_yaw = self.min_yaw * self.yaw_speed_rps * self.min_pulse_s
        if self.yaw_tol < floor_yaw:
            raise ValueError(
                f"yaw_tol={self.yaw_tol} 小于死区允许的最小一转 "
                f"{floor_yaw:.3f} rad,理由同 xy_tol。")


#: 控制循环的三个阶段。分开是因为**卡住判据的量纲跟着阶段变** ——
#: 转向阶段距离根本不会变小,拿距离判卡住会把正常转身误判成卡死。
_TURN_TO_GOAL = "turn_to_goal"
_ADVANCE = "advance"
_FINAL_YAW = "final_yaw"


class LocalNavBackend(NavBackend):
    """在自建地图上点到点走。

    组合而非继承: 运动能力来自传进来的 ``DeviceBackend``(真机上是
    ``SidecarDeviceBackend``,测试里是仿真器),位姿来自定位桥。本类自己
    既不碰 SDK 也不碰 ROS,所以能在纯 stdlib 环境里被完整测试。

    对外语义跟 ``VendorNavBackend`` 对齐 —— 两者共用同一套契约测试。
    ``goto`` 下发即返回,到没到经 ``NavStatusEvent`` 推送;终态之后状态
    回落 ``StandBy``,和厂商设备的行为一致。
    """

    def __init__(
        self,
        device: DeviceBackend,
        *,
        maps_dir: str | Path = "runs/slam",
        pose_host: str = "127.0.0.1",
        pose_port: int = 8091,
        params: LocalNavParams | None = None,
    ) -> None:
        super().__init__()
        self._device = device
        self._maps_dir = Path(maps_dir)
        self._pose_host = pose_host
        self._pose_port = pose_port
        self.params = params or LocalNavParams()

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._hello: PoseHello | None = None

        self._pose: Pose | None = None
        self._pose_at = 0.0
        self._loc = LocStatus.INIT
        self._nav = NavStatus.STANDBY
        self._current_map: str | None = None
        # **限速是上限,存的是控制量**(W05)。对外按基类契约说 m/s、rad/s,
        # 换算靠 fwd_speed_mps / yaw_speed_rps。运动循环每一拍的控制量仍由误差
        # 和死区决定,只是封顶在这里;低于死区的上限没有物理意义,set_speed 拒绝。
        self._fwd_cap = self.params.max_fwd
        self._yaw_cap = self.params.max_yaw

        self._runner: asyncio.Task[None] | None = None
        self._resumed = asyncio.Event()
        self._resumed.set()          # set 表示没在暂停
        self._abort: NavStatus | None = None

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self._pose_host, self._pose_port)
        except OSError as exc:
            raise NavConnectionError(
                f"连不上定位桥 {self._pose_host}:{self._pose_port}: {exc}。"
                f"先确认 ROS 侧的 tools/ros2_pose_bridge.py 在跑") from exc
        self._pump = asyncio.create_task(self._pump_frames())
        self._watchdog = asyncio.create_task(self._watch_loc())

    async def close(self) -> None:
        """硬关。跟 ``stop()`` 不同,这里直接砍掉控制循环。

        ``stop()`` 会等当前这一拍走完(理由见 ``stop`` 的说明),``close()``
        是"这个后端不要了",没有等的必要。
        """
        runner, self._runner = self._runner, None
        if runner is not None and not runner.done():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        for name in ("_watchdog", "_pump"):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @property
    def connected(self) -> bool:
        return self._writer is not None and self._pump is not None

    @property
    def capabilities(self) -> frozenset[str]:
        """这条路线三项可选能力一项都没有 —— 下面三个方法各自解释了为什么。

        建图是 ROS 侧的离线重建,删改图是文件系统的事,重定位由 slam_toolbox
        自己管。声明出来,页面就不会把这三个按钮画成可点的。
        """
        return frozenset()

    # ------------------------------------------------------------ 位姿流

    async def _pump_frames(self) -> None:
        reader = self._reader
        assert reader is not None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    self._on_link_down("定位桥关闭了连接")
                    return
                try:
                    frame = decode_frame(line)
                except PoseProtocolError:
                    # 单帧坏掉不该弄死整条链路 —— 现场最常见的是日志混进流里。
                    continue
                self._on_frame(frame)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            self._on_link_down(f"定位链路出错: {exc}")

    async def _watch_loc(self) -> None:
        """定期复查位姿新鲜度。

        没有这个的话,"定位桥进程没了"只有在**有人来问**的时候才被发现 ——
        定位桥不发帧,``_pump_frames`` 就一直阻塞在 readline 上,没有任何
        事情会触发判定。于是订阅了 ``LocStatusEvent`` 的上层什么也收不到,
        以为定位还好着。空闲时的定位丢失恰恰是最需要被主动告知的那种。

        周期取新鲜度窗口的一半:判定最多晚半个窗口,而不是无限期。
        """
        period = max(0.05, self.params.loc_lost_after_s / 2.0)
        while True:
            await asyncio.sleep(period)
            self._refresh_loc()

    def _on_frame(self, frame: PoseHello | PoseFrame) -> None:
        if isinstance(frame, PoseHello):
            if frame.proto != PROTO_VERSION:
                self._on_link_down(
                    f"定位桥协议版本 {frame.proto},本端要 {PROTO_VERSION}")
                return
            self._hello = frame
            return
        if frame.ok:
            self._pose = Pose.from_xy_yaw(frame.x, frame.y, frame.yaw)
            self._pose_at = time.monotonic()
            self._set_loc(LocStatus.CONTINUOUS_LOC)
            return
        # ok=False 不立刻判丢 —— TF 单帧查不到很常见,由时间窗说了算。
        self._refresh_loc()

    def _on_link_down(self, reason: str) -> None:
        self._pump = None
        self._set_loc(LocStatus.LOC_LOST)
        self.emit(BackendDisconnected(reason))

    def _refresh_loc(self) -> None:
        """按位姿新鲜度判定定位状态。

        没收到过任何位姿时保持 ``Init`` —— "还没开始"和"丢了"是两回事,
        上层对这两种情况的处置不一样(前者等,后者救)。
        """
        if self._pose_at == 0.0:
            return
        if time.monotonic() - self._pose_at > self.params.loc_lost_after_s:
            self._set_loc(LocStatus.LOC_LOST)

    def _set_loc(self, status: LocStatus) -> None:
        if status is self._loc:
            return
        previous, self._loc = self._loc, status
        self.emit(LocStatusEvent(status=status, previous=previous))

    def _set_nav(self, status: NavStatus) -> None:
        if status is self._nav:
            return
        previous, self._nav = self._nav, status
        self.emit(NavStatusEvent(status=status, previous=previous))

    def _fresh_pose(self, operation: str = "goto") -> Pose:
        self._refresh_loc()
        if self._pose is None:
            raise NavRequestError(operation, "还没收到过任何位姿,定位桥连上了吗")
        if self._loc is not LocStatus.CONTINUOUS_LOC:
            raise NavRequestError(
                operation, f"定位不可用({self._loc.value}),拒绝行走")
        return self._pose

    # ------------------------------------------------------------ 地图

    async def list_maps(self) -> list[str]:
        """目录里 ``.yaml`` 与 ``.pgm`` 成对存在的才算一张图。

        只有 yaml 没有 pgm 的是半成品(存图中途被打断就会留下这种),
        列出来只会让上层在 ``load_map`` 时才炸。
        """
        if not self._maps_dir.is_dir():
            return []
        return sorted(p.stem for p in self._maps_dir.glob("*.yaml")
                      if (self._maps_dir / f"{p.stem}.pgm").exists())

    async def remove_maps(self, map_ids: Sequence[str]) -> None:
        raise NavRequestError(
            "remove_maps",
            "自建导航不删地图 —— 图是仓库里的产物,增删改交给 git")

    async def rename_map(self, old_id: str, new_id: str) -> None:
        raise NavRequestError(
            "rename_map",
            "自建导航不改地图名 —— 图是仓库里的产物,增删改交给 git")

    async def get_map_grid(self, map_id: str) -> dict[str, Any]:
        """读本地 pgm,拼成 ROS ``OccupancyGrid`` 的形状。

        形状要跟厂商后端一模一样(``header`` / ``info`` / ``data``),否则
        上层画图的那段代码就得认后端 —— 那这层抽象就白做了。
        """
        meta = self._read_map_yaml(map_id)
        width, height, pixels = _read_pgm(self._maps_dir / f"{map_id}.pgm")
        return {
            "header": {"frame_id": "map", "stamp": {"sec": 0, "nanosec": 0}},
            "info": {
                "map_load_time": {"sec": 0, "nanosec": 0},
                "resolution": meta["resolution"],
                "width": width,
                "height": height,
                "origin": {
                    "position": {"x": meta["origin"][0], "y": meta["origin"][1],
                                 "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
            },
            "data": _pgm_to_occupancy(pixels, width, height, meta),
        }

    def _read_map_yaml(self, map_id: str) -> dict[str, Any]:
        """只挑用得上的几行。

        故意不引 PyYAML 做完整解析: map_saver_cli 写出来的这份 yaml 形状
        固定,而我们只要 resolution / origin / 两个阈值 / negate。少一个
        解析器就少一处会出错的地方。
        """
        path = self._maps_dir / f"{map_id}.yaml"
        if not path.exists():
            raise NavRequestError("get_map_grid", f"没有这张图: {path}")
        meta: dict[str, Any] = {
            "resolution": 0.05,
            "origin": [0.0, 0.0, 0.0],
            "occupied_thresh": 0.65,
            "free_thresh": 0.196,
            "negate": 0,
        }
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            key, _, value = line.partition(":")
            value = value.strip()
            if key == "origin" and "[" in value:
                inner = value.split("[", 1)[1].split("]", 1)[0]
                meta["origin"] = [float(v) for v in inner.split(",")]
            elif key in ("resolution", "occupied_thresh", "free_thresh"):
                meta[key] = float(value)
            elif key == "negate":
                meta["negate"] = int(float(value))
        return meta

    # ------------------------------------------------------------ 建图

    async def start_mapping(self) -> None:
        raise NavRequestError(
            "start_mapping",
            "自建导航不经这个口建图 —— 建图是 ROS 侧 slam_toolbox 的事,"
            "见 docs/建图定位与巡检管线.md 第二节。这里明确抛错而不是假装"
            "成功,免得上层以为图已经在建了")

    async def stop_mapping(self) -> None:
        raise NavRequestError("stop_mapping", "理由同 start_mapping")

    async def mapping_status(self) -> MappingStatus | None:
        """恒为 None —— 本后端不建图,没有建图状态可报。"""
        return None

    # ------------------------------------------------------------ 路径

    def _paths_file(self, map_id: str) -> Path:
        return self._maps_dir / f"{map_id}.paths.json"

    async def list_paths(self, map_id: str) -> dict[str, list[Waypoint]]:
        path = self._paths_file(map_id)
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {name: [Waypoint.from_wire(w) for w in points]
                for name, points in raw.items()}

    async def save_path(self, map_id: str, path_id: str,
                        waypoints: Sequence[Waypoint]) -> None:
        path = self._paths_file(map_id)
        current = (json.loads(path.read_text(encoding="utf-8"))
                   if path.exists() else {})
        current[path_id] = [w.to_wire() for w in waypoints]
        self._write_paths(path, current)

    async def remove_path(self, map_id: str, path_id: str) -> None:
        path = self._paths_file(map_id)
        if not path.exists():
            return
        current = json.loads(path.read_text(encoding="utf-8"))
        current.pop(path_id, None)
        self._write_paths(path, current)

    @staticmethod
    def _write_paths(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # ------------------------------------------------------------ 定位

    async def load_map(self, map_id: str) -> None:
        """记下当前用哪张图,并顺手校验它读得出来。

        **它不会去动定位器。** slam_toolbox 是外面起的进程,换图要重启它。
        这里只保证"上层认为在用的图"和"定位器实际在用的图"是同一个名字 ——
        对不上的时候至少有个地方查得出来。

        没 ``load_map`` 就 ``goto`` 会被拒:目标点的坐标属于某张图,不说
        清楚是哪张,"走到 (1, 0)"这句话没有意义。
        """
        if map_id not in await self.list_maps():
            raise NavRequestError(
                "load_map", f"地图目录 {self._maps_dir} 里没有 {map_id}")
        self._read_map_yaml(map_id)
        self._current_map = map_id

    @property
    def current_map(self) -> str | None:
        return self._current_map

    async def reset_localization(self) -> None:
        raise NavRequestError(
            "reset_localization",
            "自建导航不能自己重定位 —— 得有人在看板上点 /initialpose。"
            "自动重定位猜错位置的代价是真的撞上去")

    async def loc_status(self) -> LocStatus | None:
        self._refresh_loc()
        return self._loc

    # ------------------------------------------------------------ 导航

    async def goto(self, pose: Pose) -> None:
        """启动一次点到点行走,**立刻返回**,进展经事件推送。

        语义与 ``VendorNavBackend.goto`` 对齐 —— 契约测试两边共用。
        """
        if self._runner is not None and not self._runner.done():
            raise NavRequestError("goto", "上一次导航还在跑,先 stop")
        if not self.connected:
            raise NavConnectionError("定位桥没连上")
        if self._current_map is None:
            raise NavRequestError("goto", "还没 load_map,不知道这个坐标属于哪张图")
        self._fresh_pose()          # 定位不可用就当场拒绝,别等跑起来才发现
        self._resumed.set()
        self._abort = None
        self._set_nav(NavStatus.ACTIVE)
        self._runner = asyncio.create_task(self._run_to(pose))

    async def stop(self) -> None:
        """请求停止,**等当前这一拍走完**才回来。

        为什么不直接 cancel: 脉冲已经下到旁路进程了,取消 Python 侧的等待
        并不会让机器停下来 —— 它照样把这一拍走完。假装"立刻停了"只会让上层
        在机器还在动的时候以为它停了,那比慢半拍危险得多。真要立刻停,发急停。

        滞后有上界:一拍。
        """
        runner = self._runner
        if runner is None or runner.done():
            return
        self._abort = NavStatus.CANCELLED
        self._resumed.set()          # 暂停中也要能停下来
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    async def pause(self) -> None:
        if self._runner is None or self._runner.done():
            return
        self._resumed.clear()
        self._set_nav(NavStatus.PAUSE)

    async def resume(self) -> None:
        if self._runner is None or self._runner.done():
            return
        self._resumed.set()
        self._set_nav(NavStatus.ACTIVE)

    async def nav_status(self) -> NavStatus | None:
        return self._nav

    async def _run_to(self, goal: Pose) -> None:
        """脉冲式的转-走-再测量循环。

        每一拍只下发一个固定时长的 ``walk``,然后重新读定位。不做连续控制,
        理由见模块开头 —— 连续控制器会把控制量降进死区。
        """
        try:
            final = await self._drive(goal)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._set_nav(NavStatus.FAILED)
            self._set_nav(NavStatus.STANDBY)
            raise
        self._set_nav(final)
        # 终态是一条事件,不是一个停留的状态:随后回落 StandBy,表示"可以
        # 接下一个点了"。厂商设备也是这个行为,契约测试因此能两边共用。
        self._set_nav(NavStatus.STANDBY)

    async def _drive(self, goal: Pose) -> NavStatus:
        p = self.params
        deadline = time.monotonic() + p.goal_timeout_s
        phase = ""
        best = math.inf
        stalled = 0

        while True:
            await self._resumed.wait()
            if self._abort is not None:
                return self._abort
            if time.monotonic() > deadline:
                return NavStatus.FAILED
            try:
                here = self._fresh_pose()
            except NavRequestError:
                # 定位丢了还继续走就是闭着眼睛开车。停下,交给上层处置。
                return NavStatus.FAILED

            dx = goal.position.x - here.position.x
            dy = goal.position.y - here.position.y
            dist = math.hypot(dx, dy)

            if dist <= p.xy_tol:
                yaw_err = wrap_angle(goal.yaw - here.yaw)
                if abs(yaw_err) <= p.yaw_tol:
                    return NavStatus.SUCCEED
                now = _FINAL_YAW
                remaining, threshold = abs(yaw_err), p.stuck_progress_rad
                action = self._turn(yaw_err)
            else:
                heading_err = wrap_angle(math.atan2(dy, dx) - here.yaw)
                if abs(heading_err) > p.turn_enter:
                    now = _TURN_TO_GOAL
                    remaining, threshold = abs(heading_err), p.stuck_progress_rad
                    action = self._turn(heading_err)
                else:
                    now = _ADVANCE
                    remaining, threshold = dist, p.stuck_progress_m
                    action = self._advance(dist)

            # 阶段一换,"还差多少"的量纲就换了 —— 拿上一阶段的 best 去比
            # 大小是把米和弧度放在一起比。重来。
            if now != phase:
                phase, best, stalled = now, math.inf, 0

            await action
            await asyncio.sleep(p.settle_s)

            if remaining < best - threshold:
                best = remaining
                stalled = 0
            else:
                # 没进展。**这是死区最典型的样子**:指令发出去了、没报错、
                # 机器一动不动(#37)。不判卡住的话会一直蹬到超时。
                stalled += 1
                best = min(best, remaining)
                if stalled >= p.stuck_pulses:
                    return NavStatus.FAILED

    async def _turn(self, error: float) -> None:
        p = self.params
        cmd = _saturate(abs(error) * p.yaw_gain, p.min_yaw, self._yaw_cap)
        seconds = _pulse_seconds(abs(error), cmd * p.yaw_speed_rps, p)
        await self._device.walk(seconds, 0.0, 0.0, math.copysign(cmd, error))

    async def _advance(self, distance: float) -> None:
        p = self.params
        cmd = _saturate(distance * p.fwd_gain, p.min_fwd, self._fwd_cap)
        seconds = _pulse_seconds(distance, cmd * p.fwd_speed_mps, p)
        await self._device.walk(seconds, cmd, 0.0, 0.0)

    # ------------------------------------------------------------ 速度

    async def get_speed(self) -> dict[str, float]:
        """当前上限,按基类契约:x m/s、y m/s(本后端恒 0,不侧移)、z rad/s。"""
        p = self.params
        return {"x": self._fwd_cap * p.fwd_speed_mps, "y": 0.0,
                "z": self._yaw_cap * p.yaw_speed_rps}

    async def set_speed(self, x: float, y: float | None = None,
                        z: float | None = None) -> dict[str, float]:
        """设上限,回报**真实生效值**(W05)。

        以前这里只记一个数、运动循环不读它:设成 0 照样 0.5 往前冲,上层看到
        零速、底层在动(D4)。现在:

        * 上限换算成控制量后封顶在 ``max_fwd``/``max_yaw``,超过就夹住并把夹住
          后的值回报出去 —— 不静默改写,回报的就是真的。
        * 低于死区(``min_fwd``/``min_yaw``)的上限**拒绝**:这台机做不到,
          假装生效比慢半拍危险(#37: 0.11 几乎不动)。0 也在此列 —— 要停用 stop。
        * 侧移本后端不做,``y`` 只接受 0/None。
        """
        p = self.params
        # **先查有限,再比大小。** ``nan <= 0`` 和 ``nan < 死区`` 都是 False,NaN 会
        # 穿过下面每一道比较直接写进上限,之后每一拍的控制量和脉冲时长都跟着 NaN
        # (外部审核复现)。三个分量在任何比较和写入之前先拦。
        for name, v in (("x", x), ("y", y), ("z", z)):
            if v is not None and not math.isfinite(float(v)):
                raise NavRequestError("set_speed", f"{name}={v} 不是有限数,拒绝")
        if float(x) <= 0:
            raise NavRequestError(
                "set_speed", "自建导航不倒退,前进上限得是正数;要停用 stop")
        fwd = float(x) / p.fwd_speed_mps
        if fwd < p.min_fwd:
            raise NavRequestError(
                "set_speed",
                f"前进上限 {x} m/s 低于死区({p.min_fwd * p.fwd_speed_mps:.2f} m/s),"
                f"这台机走不动;要停用 stop")
        if y is not None and abs(float(y)) > 1e-9:
            raise NavRequestError("set_speed", "自建导航不做侧移,y 只能是 0")
        yaw_cap = self._yaw_cap
        if z is not None:
            yaw = float(z) / p.yaw_speed_rps
            if yaw < p.min_yaw:
                raise NavRequestError(
                    "set_speed",
                    f"转向上限 {z} rad/s 低于死区({p.min_yaw * p.yaw_speed_rps:.2f} rad/s)")
            yaw_cap = min(yaw, p.max_yaw)
        self._fwd_cap = min(fwd, p.max_fwd)
        self._yaw_cap = yaw_cap
        return await self.get_speed()

    def __repr__(self) -> str:  # pragma: no cover - 只为调试好看
        return (f"<LocalNavBackend {self._pose_host}:{self._pose_port} "
                f"map={self._current_map} nav={self._nav.value} "
                f"loc={self._loc.value}>")


def _saturate(value: float, low: float, high: float) -> float:
    """把控制量夹进 [low, high]。

    注意下限是**抬高**而不是截零 —— 小误差算出来的小控制量会落进死区被静默
    吞掉(#37),那样机器压根不动。宁可走多一点,多出来的下一拍纠回来。
    """
    return min(high, max(low, value))


def _pulse_seconds(error: float, rate: float, p: LocalNavParams) -> float:
    """这一拍走多久 —— 精细逼近靠缩短时长,不靠降低控制量。"""
    if rate <= 0:
        return p.min_pulse_s
    return min(p.pulse_s, max(p.min_pulse_s, error / rate))


def _read_pgm(path: Path) -> tuple[int, int, bytes]:
    """读 P5 灰度图,返回 (宽, 高, 像素)。

    只认 map_server / map_saver_cli 写出来的那种二进制 P5,不做通用 PGM
    解析。P2(ASCII)会被明确拒掉,而不是解出一堆看似合理的垃圾。
    """
    if not path.exists():
        raise NavRequestError("get_map_grid", f"没有这个 pgm: {path}")
    blob = path.read_bytes()
    if not blob.startswith(b"P5"):
        raise NavRequestError("get_map_grid", f"不是二进制 P5 格式的 pgm: {path}")
    fields: list[int] = []
    i = 2
    while len(fields) < 3:
        while i < len(blob) and blob[i:i + 1].isspace():
            i += 1
        if blob[i:i + 1] == b"#":                  # 注释整行跳过
            while i < len(blob) and blob[i:i + 1] != b"\n":
                i += 1
            continue
        start = i
        while i < len(blob) and not blob[i:i + 1].isspace():
            i += 1
        if start == i:
            raise NavRequestError("get_map_grid", f"pgm 头部不完整: {path}")
        fields.append(int(blob[start:i]))
    width, height, _maxval = fields
    pixels = blob[i + 1:i + 1 + width * height]
    if len(pixels) != width * height:
        raise NavRequestError(
            "get_map_grid",
            f"pgm 像素不足: 头里写 {width}x{height},实际只有 {len(pixels)} 字节")
    return width, height, pixels


def _pgm_to_occupancy(pixels: bytes, width: int, height: int,
                      meta: dict[str, Any]) -> list[int]:
    """按 map_server 的规则把灰度翻成 -1/0..100 的占据值。

    两处容易错、而且错了不报错的地方:

    * **行序是反的。** pgm 从上往下存,``OccupancyGrid`` 从原点(左下角)
      往上存。不翻的话地图会上下颠倒,而颠倒的图看着仍然"像一张图"。
    * ``negate`` 会把黑白的含义整个调过来。
    """
    occupied = meta["occupied_thresh"]
    free = meta["free_thresh"]
    negate = bool(meta["negate"])
    out: list[int] = []
    for row in range(height - 1, -1, -1):
        line = pixels[row * width:(row + 1) * width]
        for value in line:
            p = value / 255.0 if negate else (255 - value) / 255.0
            if p > occupied:
                out.append(100)
            elif p < free:
                out.append(0)
            else:
                out.append(-1)
    return out


__all__ = ["LocalNavBackend", "LocalNavParams", "wrap_angle"]
