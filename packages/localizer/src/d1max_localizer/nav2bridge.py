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

log = logging.getLogger("d1max.nav2bridge")

PROTO = 1
RECONNECT_S = 2.0
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
        self.tf = TransformBroadcaster(self.node)
        self.nav = ActionClient(self.node, NavigateToPose, "navigate_to_pose")
        self.clear = [self.node.create_client(ClearEntireCostmap, s) for s in (
            "/local_costmap/clear_entirely_local_costmap",
            "/global_costmap/clear_entirely_global_costmap")]
        self.node.create_subscription(Twist, "/cmd_vel", self._on_cmd, 10)
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
        self.goals: dict[int, Any] = {}
        self._hb = 0

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

    def _on_cmd(self, t: Any) -> None:
        self.send_threadsafe({"type": "cmd", "t_ms": self.t_ms, "vx": float(t.linear.x),
                              "vy": float(t.linear.y), "wz": float(t.angular.z)})

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
        fut = self.nav.send_goal_async(goal)
        fut.add_done_callback(lambda f: self._accepted(gid, f))

    def _accepted(self, gid: int, fut: Any) -> None:
        try:
            h = fut.result()
        except Exception as exc:                        # noqa: BLE001
            self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": "rejected",
                                  "reason": str(exc)[:200]})
            return
        if not h.accepted:
            self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": "rejected",
                                  "reason": "Nav2 不收这个目标"})
            return
        self.goals[gid] = h
        self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": "accepted"})
        h.get_result_async().add_done_callback(lambda f: self._done(gid, f))

    def _done(self, gid: int, fut: Any) -> None:
        from action_msgs.msg import GoalStatus
        self.goals.pop(gid, None)
        try:
            status = fut.result().status
        except Exception as exc:                        # noqa: BLE001
            self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": "aborted",
                                  "reason": str(exc)[:200]})
            return
        state = {GoalStatus.STATUS_SUCCEEDED: "succeeded",
                 GoalStatus.STATUS_CANCELED: "canceled"}.get(status, "aborted")
        why = "" if state != "aborted" else "Nav2 放弃了(规划不出、脱困都试过还走不动、超时)"
        self.send_threadsafe({"type": "goal_state", "goal_id": gid, "state": state,
                              "reason": why})

    def on_cancel(self, m: dict[str, Any]) -> None:
        h = self.goals.get(m["goal_id"])
        if h is not None:
            h.cancel_goal_async()

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
        if not self.map_sent:
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
                # 收到旧目标的结果也会认错)
                for h in list(self.goals.values()):
                    h.cancel_goal_async()
                self.goals.clear()
            await asyncio.sleep(RECONNECT_S)

    async def _ticker(self) -> None:
        while True:
            self._hb += 1
            self.send({"type": "hb", "seq": self._hb})
            self.check_ready()
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
