"""Nav2 转接节点(W47,`docs/W47-导航换Nav2-设计.md`):代理 ↔ Nav2。
报文见 :mod:`d1max_contract.navbridge`。

ROS 那一侧(系统 Python + rclpy),跟 Nav2 一起起(``d1max-nav2.service``)。做的事:

- 连代理的 Unix 套接字(``--socket``),握手、心跳;断了隔几秒重连。
- 代理每拍的 ``tick`` → 发 ``/clock``(Nav2 一律 ``use_sim_time``)、TF ``odom→base_link``、``/odom``;
  ``map_odom`` 有才发 TF ``map→odom``(定位不可信时不发,Nav2 那边 TF 过期自己停)。
- ``grid``(感知栅格,拍下时的里程位姿)→ 换到此刻的 ``base_link`` 下,两份点云:
  ``/d1max/obs_occ``(挡,标记)、``/d1max/obs_free``(看得见是空的格,只用来清)。Nav2 的障碍层吃这两份。
- ``map`` → 读地图目录的 ``floor.pgm/.yaml``,禁行区烧成占用,发 ``/map``(transient local);清一遍两张
  代价地图。
- ``goal`` / ``cancel`` → ``NavigateToPose`` 动作;结果回 ``goal_state``。
- 订 ``/cmd_vel``(Nav2 速度平滑器、脱困动作的出口)→ ``cmd`` 给代理。
- ``tick`` 里带的 ``speed``(代理按「看得清就快」算的)→ ``/speed_limit``(变了才发)。
- 每秒看一次 Nav2 就没就绪(动作服务在、地图发过、时钟在走),变了发 ``ready``。

用法::

    python3 -m d1max_localizer.nav2bridge --socket /var/lib/d1max/agent/nav2.sock
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from d1max_contract.navbridge import PROTO  # 跟代理同一个协议号(不另写一份)

log = logging.getLogger("d1max.nav2bridge")

RECONNECT_S = 2.0
#: 新目标收下之后这么久(秒)里收到的 /cmd_vel 不发(复查 N3):上一个目标的控制器排在队里的旧速度,
#: 回调时再标上新目标号也证明不了是新目标的。控制器 20 Hz,队里的旧速度几拍就排完了。
CMD_QUIET_S = 0.2
#: 规划出的路线终点离目标超过这么远(米),不当成这个目标的路线(复查 N6:同一拍规划的旧路线)。
PLAN_END_TOL_M = 0.4
#: 新目标排着等上一个在 Nav2 里结束,最多等这么久(秒),过了回拒(复查第三轮 P1)。
QUEUE_MAX_S = 5.0
#: Nav2 里会出速度、出路线的动作(和导航本身)。复查第四轮 P1/P4/P2:Nav2 取消下层动作只等 20 ms,
#: 等不到也照样报导航结束;这些动作**自己的**动作状态只有在执行循环真退出(最后一条速度、路线发完)以后
#: 才变终态。它们都没有在跑的目标,才算上一个目标真停了,才放下一个。
#: 复查第五轮补 ``assisted_teleop``(params.yaml 里配着,走同一个行为速度出口)。
PRODUCERS = ("navigate_to_pose", "navigate_through_poses", "follow_path",
             "compute_path_to_pose", "compute_path_through_poses",
             "backup", "wait", "spin", "drive_on_heading", "assisted_teleop")
#: 出脱困(行为)速度的那几个。
BEHAVIORS = ("backup", "wait", "spin", "drive_on_heading", "assisted_teleop")
#: 起来以后等这么久(秒)再派目标、报就绪:Nav2 那边补发(transient local)的动作状态
#: 先处理完(复查第五轮)。
STATUS_SYNC_S = 2.0
#: action_msgs/GoalStatus:收下了、在跑、正在取消 —— 都算还在出东西。
_LIVE_STATUS = (1, 2, 3)
#: 时钟这么久(墙钟秒)没走就当代理没在送 tick:不就绪。
CLOCK_STALE_S = 2.0
#: 障碍点云里点的高度(米,在 Nav2 障碍层的 min/max 之间)。
OBS_Z = 0.3
#: 里程一下跳这么远(米)当被搬走了(仿真里换场景):清代价地图。
JUMP_M = 1.0


def _yaw_q(yaw: float) -> tuple[float, float, float, float]:
    return (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))


def unpack(s: str, size: int) -> np.ndarray:
    from d1max_contract.obsbridge import unpack_bits
    return np.frombuffer(unpack_bits(s, size), dtype=np.uint8).reshape(size, size)


def grid_points(m: dict[str, Any], odom_now: tuple[float, float, float]
                ) -> tuple[np.ndarray, np.ndarray]:
    """感知栅格(拍下时机身系,行 = 前、列 = 左、中心在机身)→ 此刻机身系的 (挡的点, 空的点),各 N×2。
    """
    size, res = int(m["size"]), float(m["res"])
    occ, known = unpack(m["occ"], size), unpack(m["known"], size)
    idx = (np.arange(size) - size / 2 + 0.5) * res
    X, Y = np.meshgrid(idx, idx, indexing="ij")
    gx, gy, gyaw = m["pose"]
    nx, ny, nyaw = odom_now
    c, s = math.cos(gyaw), math.sin(gyaw)
    wx, wy = gx + c * X - s * Y, gy + s * X + c * Y          # 里程系
    c2, s2 = math.cos(nyaw), math.sin(nyaw)
    dx, dy = wx - nx, wy - ny
    bx, by = c2 * dx + s2 * dy, -s2 * dx + c2 * dy           # 此刻机身系
    o = (occ == 1) & (known == 1)
    f = (occ == 0) & (known == 1)
    return np.stack([bx[o], by[o]], axis=1), np.stack([bx[f], by[f]], axis=1)


def decimate(pts: list[tuple[float, float]], step: float = 0.2,
             most: int = 256) -> list[list[float]]:
    """路径抽稀:相邻两点至少隔 ``step`` 米,头尾都留;还多就等间隔再挑,最多 ``most`` 个。"""
    if not pts:
        return []
    out = [pts[0]]
    for p in pts[1:-1]:
        if math.dist(p, out[-1]) >= step:
            out.append(p)
    if len(pts) > 1:
        out.append(pts[-1])
    if len(out) > most:
        k = (len(out) - 1) / (most - 1)
        out = [out[round(i * k)] for i in range(most)]
    return [[round(float(x), 3), round(float(y), 3)] for x, y in out]


def occupancy(map_dir: Path, nogo: list[list[list[float]]]
              ) -> tuple[np.ndarray, float, tuple[float, float]]:
    """地图目录 → (占用 int8 栅格,行从下往上:0 空、100 挡;分辨率;原点)。禁行区烧成挡。"""
    from d1max_contract.planning.costmap import load_floor, polygon_mask
    free, res, origin = load_floor(map_dir)
    occ = np.where(free, 0, 100).astype(np.int8)
    for poly in nogo:
        occ[polygon_mask(tuple(tuple(p) for p in poly), occ.shape, res, origin)] = 100
    return occ, res, origin


class Nav2Bridge:
    def __init__(self, sock: Path, *, name: str = "d1max-nav2bridge") -> None:
        import rclpy
        from geometry_msgs.msg import TransformStamped, Twist
        from nav2_msgs.action import NavigateToPose
        from nav2_msgs.msg import SpeedLimit
        from nav2_msgs.srv import ClearEntireCostmap
        from nav_msgs.msg import OccupancyGrid, Odometry
        from rclpy.action import ActionClient
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from rosgraph_msgs.msg import Clock
        from sensor_msgs.msg import PointCloud2
        from tf2_ros import TransformBroadcaster

        self.sock = sock
        self.name = name
        self._m = {"TransformStamped": TransformStamped, "OccupancyGrid": OccupancyGrid,
                   "Odometry": Odometry, "Clock": Clock, "NavigateToPose": NavigateToPose,
                   "ClearEntireCostmap": ClearEntireCostmap}
        rclpy.init()
        self.node = rclpy.create_node("d1max_nav2bridge")
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        self.p_clock = self.node.create_publisher(Clock, "/clock", 10)
        self.p_odom = self.node.create_publisher(Odometry, "/odom", 10)
        self.p_map = self.node.create_publisher(OccupancyGrid, "/map", latched)
        self.p_occ = self.node.create_publisher(PointCloud2, "/d1max/obs_occ", 5)
        self.p_free = self.node.create_publisher(PointCloud2, "/d1max/obs_free", 5)
        #: 代理每拍算的「此刻许跑多快」→ Nav2 控制器的限速(MPPI 按它缩速度上限)。
        self.p_speed = self.node.create_publisher(SpeedLimit, "/speed_limit", 10)
        self._speed_msg = SpeedLimit
        self._speed: float | None = None
        self.tf = TransformBroadcaster(self.node)
        self.nav = ActionClient(self.node, NavigateToPose, "navigate_to_pose")
        self.clear = [self.node.create_client(ClearEntireCostmap, s) for s in (
            "/local_costmap/clear_entirely_local_costmap",
            "/global_costmap/clear_entirely_global_costmap")]
        # 复查 P1:订控制器的原始输出 /cmd_vel_nav,不经速度平滑器(不起它)——
        # 平滑器 OPEN_LOOP 会把上一个目标的速度慢慢衰减,目标换了它还在出旧方向的速度。
        # 加减速由代理和机器人自己管。复查第三轮 P4:脱困动作(BackUp 等)单独一路 /cmd_vel_behavior
        # (d1max_nav2.launch.py 里接的),标上 beh:它常在规划不出来时做,代理不要求先有路线。
        self.node.create_subscription(Twist, "/cmd_vel_nav", self._on_cmd, 10)
        self.node.create_subscription(Twist, "/cmd_vel_behavior", self._on_beh, 10)
        from action_msgs.msg import GoalStatusArray
        from rclpy.qos import qos_profile_action_status_default
        for name in PRODUCERS:                    # 复查第四轮:生产端自己停没停
            self.node.create_subscription(
                GoalStatusArray, f"/{name}/_action/status",
                lambda msg, name=name: self._on_status(name, msg),
                qos_profile_action_status_default)
        from nav_msgs.msg import Path as PathMsg
        self.node.create_subscription(PathMsg, "/plan", self._on_plan, 5)
        self._init_goal_book()
        self.status_sync_until = self._mono() + STATUS_SYNC_S    # 复查第五轮:补发的状态先处理完
        from std_srvs.srv import Trigger
        self._trigger = Trigger
        #: Nav2 的生命周期管理器:全部节点激活了才算就绪(动作服务在不等于激活了,早发的目标会被拒)。
        self.lm = self.node.create_client(Trigger, "/lifecycle_manager_navigation/is_active")
        self.nav2_active = False
        self._exec = MultiThreadedExecutor(num_threads=2)
        self._exec.add_node(self.node)
        threading.Thread(target=self._exec.spin, name="rclpy", daemon=True).start()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.t_ms = 0
        self._clock_wall = -1e9
        self.odom: tuple[float, float, float] | None = None
        self.map_sent = False
        self._ready: tuple[bool, str] | None = None
        self._hb = 0

    def _init_goal_book(self) -> None:
        """目标账(外审 R8):``pending`` = 发给 Nav2、还没回收没收下的(值:代理是不是已经取消了);
        ``goals`` = 收下了的;``active_gid`` = 正在走的(规划出的路径、速度按它标);``active_since_ms``
        = 它收下时的 ``/clock``(早于这个时刻规划的路线不算它的,外审 R10)。rclpy 线程和事件循环都碰,
        一把锁。"""
        self._goal_lock = threading.Lock()
        self.pending: dict[int, bool] = {}
        self.goals: dict[int, Any] = {}
        #: 正在走的目标号(Nav2 收下的最近那个);规划出的路径、速度按它报。
        self.active_gid: int | None = None
        self.active_since_ms = 0
        #: 每个目标的终点(复查 N6:核路线是不是它的)。
        self.goal_xy: dict[int, tuple[float, float]] = {}
        self._mono = time.monotonic
        self._cmd_quiet_until = 0.0
        #: 上一个目标在 Nav2 里结束(有终态结果)时的 ``/clock``:这个时刻及以前规划的路线不是新目标的
        #: (复查第三轮 P2)。
        self.last_end_ms = 0
        #: 排着的新目标(上一个在 Nav2 里还没结束,复查第三轮 P1)和开始排的单调钟。
        self.queued: dict[str, Any] | None = None
        self.queued_at = 0.0
        #: :data:`PRODUCERS` 里每个动作此刻有没有在跑的目标(按它自己的动作状态,复查第四轮)。
        self.producing: dict[str, bool] = {}
        #: 派出最近这个目标以后,哪几类生产端出现过新的目标号(= 这个目标的那一类开跑了):
        #: ``plan`` 有了才认路线(复查第四轮 P2),``ctl`` 有了才转控制器的速度、``beh`` 有了而且还在跑
        #: 才转脱困的速度(复查第五轮)。
        self.opened: set[str] = set()
        #: 每个动作最近一份状态列表里的目标号(列表保留最近的目标,含已结束的);
        #: 派出时记一份,之后出现的新号 = 派出以后才开始的。
        self.goal_ids: dict[str, set[bytes]] = {}
        self.known_ids: set[bytes] = set()
        #: 这个时刻(单调钟)以前不派目标:刚起来,补发的动作状态还没处理完(复查第五轮)。
        self.status_sync_until = 0.0

    def cancel_all(self) -> None:
        """代理断了:它派的目标没人管了,全部取消;还没收下的记着,收下了马上取消(外审 R8)。"""
        with self._goal_lock:
            for h in list(self.goals.values()):
                h.cancel_goal_async()
            # 不清账:取消要等 Nav2 回终态结果(_done 里删);
            # 新代理连上派的目标排着等它们结束(复查第三轮 P1)
            for gid in self.pending:
                self.pending[gid] = True
            self.active_gid = None
            self.queued = None                # 排着的也不发了(代理断了没人管它)

    # ------------------------------------------------------------ 发给代理

    def send(self, msg: dict[str, Any]) -> None:
        from d1max_contract.navbridge import encode
        w = self.writer
        if w is None:
            return
        try:
            w.write(encode(msg))
        except Exception as exc:                        # noqa: BLE001 - 断了等重连
            log.warning("发给代理没成:%s", exc)

    def send_threadsafe(self, msg: dict[str, Any]) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.send, msg)

    # ------------------------------------------------------------ ROS 回调(rclpy 线程)

    def _on_plan(self, msg: Any) -> None:
        gid = self.active_gid
        if gid is None or "plan" not in self.opened:
            # 复查第四轮 P2:派出这个目标以后规划动作还没重新开跑 —— 来的路线是老规划的
            # (时间戳不是目标代次,Smac 拿到锁才填)
            return
        st = getattr(getattr(msg, "header", None), "stamp", None)
        if st is not None:
            at = int(st.sec) * 1000 + int(st.nanosec) // 1_000_000
            if 0 < at < self.active_since_ms:
                return                    # 外审 R10:收下这个目标以前规划的(上一个目标的),不标成它的

        goal = self.goal_xy.get(gid)
        if goal is not None and msg.poses:
            end = msg.poses[-1].pose.position
            if math.hypot(end.x - goal[0], end.y - goal[1]) > PLAN_END_TOL_M:
                return                    # 复查 N6:终点不是这个目标的(同一拍规划的旧路线)
        pts = decimate([(p.pose.position.x, p.pose.position.y) for p in msg.poses])
        self.send_threadsafe({"type": "path", "goal_id": gid, "pts": pts})

    def _on_cmd(self, t: Any, src: str = "ctl") -> None:
        if self._mono() < self._cmd_quiet_until:
            return                        # 复查 N3:新目标刚收下,队里可能还排着上一个的旧速度
        with self._goal_lock:
            # 复查第五轮:这个目标自己的控制器(脱困:脱困动作)开跑了才转 —— 之前来的是上一个目标的
            # 生产端晚到的。不要求脱困「此刻还在跑」:状态和速度两路在两个线程里处理,「结束」常比
            # 最后几条后退速度先处理完,要求了就把合法的后退丢掉(navbench 终点贴墙 4/5 → 0/3)
            ok = "ctl" in self.opened if src == "ctl" else "beh" in self.opened
        if not ok:
            return
        self.send_threadsafe({"type": "cmd", "t_ms": self.t_ms, "vx": float(t.linear.x),
                              "vy": float(t.linear.y), "wz": float(t.angular.z),
                              "goal_id": self.active_gid,       # 外审 R7:标上是哪个目标的
                              "src": src})

    def _on_beh(self, t: Any) -> None:
        self._on_cmd(t, "beh")

    # ------------------------------------------------------------ 收代理的

    def _stamp(self, t_ms: int) -> Any:
        from builtin_interfaces.msg import Time
        return Time(sec=t_ms // 1000, nanosec=(t_ms % 1000) * 1_000_000)

    def _tf(self, parent: str, child: str, pose: list[float], stamp: Any) -> Any:
        t = self._m["TransformStamped"]()
        t.header.stamp, t.header.frame_id, t.child_frame_id = stamp, parent, child
        t.transform.translation.x, t.transform.translation.y = float(pose[0]), float(pose[1])
        q = _yaw_q(float(pose[2]))
        (t.transform.rotation.x, t.transform.rotation.y, t.transform.rotation.z,
         t.transform.rotation.w) = q
        return t

    def on_tick(self, m: dict[str, Any]) -> None:
        if m["t_ms"] <= self.t_ms:
            return
        self.t_ms = m["t_ms"]
        self._clock_wall = time.monotonic()
        stamp = self._stamp(self.t_ms)
        clk = self._m["Clock"]()
        clk.clock = stamp
        self.p_clock.publish(clk)
        odom = tuple(m["odom"])
        if self.odom is not None and math.dist(odom[:2], self.odom[:2]) > JUMP_M:
            self.clear_costmaps()
        self.odom = odom
        tfs = [self._tf("odom", "base_link", m["odom"], stamp)]
        if m.get("map_odom") is not None:
            tfs.append(self._tf("map", "odom", m["map_odom"], stamp))
        self.tf.sendTransform(tfs)
        o = self._m["Odometry"]()
        o.header.stamp, o.header.frame_id, o.child_frame_id = stamp, "odom", "base_link"
        o.pose.pose.position.x, o.pose.pose.position.y = float(odom[0]), float(odom[1])
        q = _yaw_q(float(odom[2]))
        (o.pose.pose.orientation.x, o.pose.pose.orientation.y, o.pose.pose.orientation.z,
         o.pose.pose.orientation.w) = q
        o.twist.twist.linear.x, o.twist.twist.linear.y = float(m["vel"][0]), float(m["vel"][1])
        o.twist.twist.angular.z = float(m["vel"][2])
        self.p_odom.publish(o)
        sp = m.get("speed")
        if sp is not None and sp != self._speed:
            self._speed = sp
            lim = self._speed_msg()
            lim.header.stamp, lim.percentage, lim.speed_limit = stamp, False, float(sp)
            self.p_speed.publish(lim)

    def on_grid(self, m: dict[str, Any]) -> None:
        if self.odom is None:
            return
        from sensor_msgs_py.point_cloud2 import create_cloud_xyz32
        from std_msgs.msg import Header
        occ, free = grid_points(m, self.odom)
        h = Header(stamp=self._stamp(self.t_ms), frame_id="base_link")
        z = np.full((len(occ), 1), OBS_Z, dtype=np.float32)
        self.p_occ.publish(create_cloud_xyz32(h, np.hstack([occ.astype(np.float32), z])))
        z = np.full((len(free), 1), OBS_Z, dtype=np.float32)
        self.p_free.publish(create_cloud_xyz32(h, np.hstack([free.astype(np.float32), z])))

    def on_map(self, m: dict[str, Any]) -> None:
        try:
            occ, res, origin = occupancy(Path(m["dir"]), m.get("nogo", []))
        except Exception as exc:                        # noqa: BLE001 - 说清楚、不就绪
            log.error("地图读不了:%s", exc)
            self.map_sent = False
            self._set_ready(False, f"地图读不了:{exc}"[:200])
            return
        g = self._m["OccupancyGrid"]()
        g.header.stamp, g.header.frame_id = self._stamp(self.t_ms), "map"
        g.info.resolution = float(res)
        g.info.height, g.info.width = occ.shape
        g.info.origin.position.x, g.info.origin.position.y = float(origin[0]), float(origin[1])
        g.info.origin.orientation.w = 1.0
        g.data = occ.ravel().tolist()
        self.p_map.publish(g)
        self.map_sent = True
        log.info("发了地图 %s(%d×%d、%.2f m,禁行区 %d 个)", m["map_id"], occ.shape[1],
                 occ.shape[0], res, len(m.get("nogo", [])))
        self.clear_costmaps()

    def clear_costmaps(self) -> None:
        for c in self.clear:
            if c.service_is_ready():
                c.call_async(self._m["ClearEntireCostmap"].Request())

    def on_goal(self, m: dict[str, Any]) -> None:
        """复查第三、四轮 P1:Nav2 的速度、路线不带目标号,只有上一个目标在 Nav2 里**真停了**(导航和
        :data:`PRODUCERS` 里每个动作自己的状态都没有在跑的)再发下一个,之后来的才能当新目标的。
        新目标一律先排着(只排最新的一个,被顶掉的回拒),在事件循环这一个地方派(:meth:`_pump`):
        不会越过排着的那个(复查第四轮 Q1);排太久(:data:`QUEUE_MAX_S`)回拒。"""
        with self._goal_lock:
            old, self.queued, self.queued_at = self.queued, m, self._mono()
        if old is not None:
            self.send({"type": "goal_state", "goal_id": old["goal_id"], "state": "rejected",
                       "reason": "又来了一个新目标"})
        self._pump()

    def queued_waiting(self) -> bool:
        """有目标排着、还没派出去(这时规划动作开跑不算新目标的)。"""
        return self.queued is not None

    def _busy(self) -> bool:
        return (bool(self.goals) or bool(self.pending) or any(self.producing.values())
                or self._mono() < self.status_sync_until)

    def reap_gone_producers(self) -> None:
        """复查第五轮:某个动作服务没了(状态话题没有发布者),它不会再出东西 —— 它记着的「在跑」清掉,
        不然 Nav2 重启以后新的空闲服务不发状态,这条旧标记一直挡着新目标。"""
        cleared = False
        with self._goal_lock:
            for name in PRODUCERS:
                if self.producing.get(name) and \
                        self.node.count_publishers(f"/{name}/_action/status") == 0:
                    self.producing[name] = False
                    cleared = True
        if cleared:
            self._dispatch_soon()

    def _on_status(self, name: str, msg: Any) -> None:
        live = any(int(st.status) in _LIVE_STATUS for st in msg.status_list)
        with self._goal_lock:
            self.producing[name] = live
            # 看目标号不看「在跑」:状态话题只留最新一条,快的话「在跑」那条会被「完成」盖掉
            ids = {bytes(st.goal_info.goal_id.uuid) for st in msg.status_list}
            self.goal_ids[name] = ids
            if ids - self.known_ids and not self.queued_waiting():
                # 派出以后才开始的(老的派之前确认停了):这一类开门
                self.opened.add("plan" if name.startswith("compute_path") else
                                "ctl" if name == "follow_path" else
                                "beh" if name in BEHAVIORS else "nav")
        if not live:
            self._dispatch_soon()

    def _pump(self) -> None:
        """事件循环里派排着的目标(只在这儿派)。"""
        with self._goal_lock:
            if self.queued is None or self._busy():
                return
            m, self.queued = self.queued, None
            expired = self._mono() - self.queued_at > QUEUE_MAX_S
            if not expired:
                self.last_end_ms = max(self.last_end_ms, self.t_ms)
                self.opened.clear()               # 等新目标自己的规划、控制器、脱困开跑
                self.known_ids = set().union(*self.goal_ids.values())
        if expired:
            self.send({"type": "goal_state", "goal_id": m["goal_id"], "state": "rejected",
                       "reason": f"上一个目标 Nav2 {QUEUE_MAX_S:g} 秒还没结束"})
            return
        self._send_goal(m)

    def _dispatch_soon(self) -> None:
        """rclpy 线程里来的(结束、拒绝、动作状态):回事件循环那边派(跟收目标同一个线程)。"""
        loop = getattr(self, "loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(self._pump)
        else:
            self._pump()

    def expire_queued(self) -> None:
        with self._goal_lock:
            m = self.queued
            if m is None or self._mono() - self.queued_at <= QUEUE_MAX_S:
                return
            self.queued = None
        self.send({"type": "goal_state", "goal_id": m["goal_id"], "state": "rejected",
                   "reason": f"上一个目标 Nav2 {QUEUE_MAX_S:g} 秒还没结束"})

    def _send_goal(self, m: dict[str, Any]) -> None:
        from geometry_msgs.msg import PoseStamped
        gid = m["goal_id"]
        goal = self._m["NavigateToPose"].Goal()
        ps = PoseStamped()
        ps.header.frame_id = "map"
        ps.header.stamp = self._stamp(self.t_ms)
        ps.pose.position.x, ps.pose.position.y = float(m["x"]), float(m["y"])
        q = _yaw_q(float(m["yaw"]))
        (ps.pose.orientation.x, ps.pose.orientation.y, ps.pose.orientation.z,
         ps.pose.orientation.w) = q
        goal.pose = ps
        if not self.nav.server_is_ready():
            self.send({"type": "goal_state", "goal_id": gid, "state": "rejected",
                       "reason": "Nav2 的导航动作服务不在"})
            return
        with self._goal_lock:
            self.pending[gid] = False
            self.goal_xy[gid] = (float(m["x"]), float(m["y"]))
        fut = self.nav.send_goal_async(goal)
        fut.add_done_callback(lambda f: self._accepted(gid, f))

    def _accepted(self, gid: int, fut: Any) -> None:
        try:
            h = fut.result()
            why = "" if h.accepted else "Nav2 不收这个目标"
        except Exception as exc:                        # noqa: BLE001
            h, why = None, str(exc)[:200] or "Nav2 不收这个目标"
        # 复查 N4:核取消、从 pending 挪到 goals 在同一段锁里做完(原来两段锁之间能插进取消)
        with self._goal_lock:
            canceled = self.pending.pop(gid, False)
            if h is not None and not why:
                self.goals[gid] = h
                if canceled:
                    # 外审 R8:收下之前代理已经取消了(或者断了):马上取消,不当成在走的
                    h.cancel_goal_async()
                else:
                    self.active_gid = gid
                    self.active_since_ms = self.t_ms
                    self._cmd_quiet_until = self._mono() + CMD_QUIET_S
            else:
                self.goal_xy.pop(gid, None)
        if why:
            self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": "rejected",
                                  "reason": why})
            self._dispatch_soon()
            return
        if canceled:
            h.get_result_async().add_done_callback(lambda f: self._done(gid, f))
            return
        self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": "accepted"})
        h.get_result_async().add_done_callback(lambda f: self._done(gid, f))

    def _done(self, gid: int, fut: Any) -> None:
        from action_msgs.msg import GoalStatus
        with self._goal_lock:
            self.goals.pop(gid, None)
            self.goal_xy.pop(gid, None)
            if self.active_gid == gid:
                self.active_gid = None
            self.last_end_ms = max(self.last_end_ms, self.t_ms)
        try:
            status = fut.result().status
        except Exception as exc:                        # noqa: BLE001
            self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": "aborted",
                                  "reason": str(exc)[:200]})
            self._dispatch_soon()
            return
        state = {GoalStatus.STATUS_SUCCEEDED: "succeeded",
                 GoalStatus.STATUS_CANCELED: "canceled"}.get(status, "aborted")
        why = "" if state != "aborted" else "Nav2 放弃了(规划不出、脱困都试过还走不动、超时)"
        self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": state,
                              "reason": why})
        self._dispatch_soon()

    def on_cancel(self, m: dict[str, Any]) -> None:
        gid = m["goal_id"]
        with self._goal_lock:
            dropped = self.queued is not None and self.queued["goal_id"] == gid
            if dropped:
                self.queued = None
        if dropped:
            self.send({"type": "goal_state", "goal_id": gid, "state": "canceled", "reason": ""})
            return
        with self._goal_lock:
            h = self.goals.get(gid)
            if h is not None:
                h.cancel_goal_async()
                if self.active_gid == gid:
                    self.active_gid = None    # 代理不要它了:之后的速度、路线不再标成它的
            elif gid in self.pending:
                self.pending[gid] = True          # 外审 R8:还没收下,记着,收下了马上取消

    # ------------------------------------------------------------ 就绪

    def _set_ready(self, ok: bool, why: str) -> None:
        if self._ready != (ok, why):
            self._ready = (ok, why)
            log.info("就绪:%s %s", ok, why)
            self.send({"type": "ready", "ok": ok, "reason": why})

    def _poll_active(self) -> None:
        if not self.lm.service_is_ready():
            self.nav2_active = False
            return

        def done(f: Any) -> None:
            try:
                self.nav2_active = bool(f.result().success)
            except Exception:                           # noqa: BLE001
                self.nav2_active = False
        self.lm.call_async(self._trigger.Request()).add_done_callback(done)

    def check_ready(self) -> None:
        self._poll_active()
        if self._mono() < self.status_sync_until:
            self._set_ready(False, "等 Nav2 的动作状态同步")
        elif not self.map_sent:
            self._set_ready(False, "还没收到地图")
        elif time.monotonic() - self._clock_wall > CLOCK_STALE_S:
            self._set_ready(False, "代理没在送时钟")
        elif not self.nav.server_is_ready():
            self._set_ready(False, "Nav2 的导航动作服务还没起来")
        elif not self.nav2_active:
            self._set_ready(False, "Nav2 还在启动(生命周期没全部激活)")
        else:
            self._set_ready(True, "")

    # ------------------------------------------------------------ 主循环

    async def run(self) -> None:
        from d1max_contract.errors import ContractError
        from d1max_contract.navbridge import FROM_AGENT, MAX_LINE, parse
        self.loop = asyncio.get_running_loop()
        handlers = {"tick": self.on_tick, "grid": self.on_grid, "map": self.on_map,
                    "goal": self.on_goal, "cancel": self.on_cancel}
        while True:
            try:
                reader, self.writer = await asyncio.open_unix_connection(str(self.sock),
                                                                         limit=MAX_LINE + 2)
            except OSError as exc:
                log.info("连不上代理(%s),%g s 后再试", exc, RECONNECT_S)
                await asyncio.sleep(RECONNECT_S)
                continue
            self._ready = None
            self.send({"type": "hello", "proto": PROTO, "name": self.name, "version": "1"})
            log.info("连上代理了")
            ticker = asyncio.create_task(self._ticker())
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        m = parse(line, allowed=FROM_AGENT)
                    except ContractError as exc:
                        log.warning("丢掉一行:%s", exc)
                        continue
                    fn = handlers.get(m["type"])
                    if fn is not None:
                        fn(m)
                    elif m["type"] == "error":
                        log.error("代理说:%s", m["reason"])
            except (OSError, ValueError) as exc:
                log.warning("跟代理的连接断了:%s", exc)
            finally:
                ticker.cancel()
                self.writer = None
                self.map_sent = False
                # 代理断了:它派的目标没人管了,全部取消(不然 Nav2 还按旧目标出速度,重连的代理
                # 收到旧目标的结果也会认错);还没收下的收下了马上取消
                self.cancel_all()
            await asyncio.sleep(RECONNECT_S)

    async def _ticker(self) -> None:
        while True:
            self._hb += 1
            self.send({"type": "hb", "seq": self._hb})
            self.check_ready()
            self.reap_gone_producers()
            self.expire_queued()
            await asyncio.sleep(1.0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--socket", type=Path,
                   default=Path(os.environ.get("D1MAX_NAV2_SOCKET",
                                               "/var/lib/d1max/agent/nav2.sock")))
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    b = Nav2Bridge(a.socket)
    try:
        asyncio.run(b.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
