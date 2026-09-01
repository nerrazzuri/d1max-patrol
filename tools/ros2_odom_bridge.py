#!/usr/bin/env python3
"""把 patrol_agent 的 odom 帧桥成 ROS2 的 /odom + TF。

**这个文件是故意写成零依赖的。**

它跑在 ROS2 的系统 Python 下(``/usr/bin/python3``,能 import rclpy),
不是本仓库的 venv —— venv 里没有 rclpy,而往 venv 里塞 ROS2 是条死路。
所以这里不 import 任何 ``d1max_patrol`` 的东西,线协议就地手写一遍。
协议本身只有一行 JSON,重复这点代价换来"能直接跑"是值的。

    /usr/bin/python3 tools/ros2_odom_bridge.py --agent 127.0.0.1:8090

前提是 patrol_agent 已经在跑(见 docs/真机联调手册.md S1)。

为什么需要它:``slam_toolbox`` 要有 ``odom -> base_link`` 的 TF 才肯建图。
如果厂商自己的栈已经发了这个 TF(现场第一件事就是查这个,见
docs/明天ROS2与建图手册.md R2),那**别跑这个脚本** —— 两个源同时发同一条
TF,tf2 会打架,而且打得很隐蔽。

顺带可以发静态外参(``--static``),坐标抄自《SDK 开发指南》2.10 节的
传感器安装位置表(单位 mm,相对机身 BASE 原点)。
"""

import argparse
import json
import math
import socket
import sys
import threading

try:
    import rclpy
    from geometry_msgs.msg import TransformStamped
    from nav_msgs.msg import Odometry
    from rclpy.node import Node
    from tf2_ros import TransformBroadcaster
    from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster
except ImportError:  # pragma: no cover - 只在没 source ROS2 时走到
    sys.exit(
        "import rclpy 失败。这个脚本要用 ROS2 的系统 Python 跑,不是本仓库的 venv:\n"
        "    source /opt/ros/humble/setup.bash\n"
        "    /usr/bin/python3 tools/ros2_odom_bridge.py --agent 127.0.0.1:8090"
    )


#: 《SDK 开发指南》2.10 节,单位从 mm 换成 m。
#:
#: 假设(待真机验证): 表里只给了平移,没给姿态。这里按"前雷达朝前、后雷达
#: 绕 z 转 180 度"处理 —— 这是最自然的猜法,但**没有任何文档支持**。
#: 建图效果不对的时候,第一个该怀疑的就是这里。
SENSOR_OFFSETS = {
    "front_lidar": (0.4043, 0.0, -0.0377, 0.0),
    "rear_lidar": (-0.4043, 0.0, -0.0377, math.pi),
    "front_camera": (0.4123, 0.0, 0.0378, 0.0),
    "rear_camera": (-0.4123, 0.0, 0.0378, math.pi),
    "imu_central": (0.0, 0.0, 0.0569, 0.0),
}


def _yaw_to_quat(yaw: float) -> tuple:
    """只绕 z 转的四元数,返回 (x, y, z, w) —— ROS 的顺序。"""
    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


class OdomBridge(Node):
    def __init__(self, host: str, port: int, odom_frame: str, base_frame: str,
                 statics: bool) -> None:
        super().__init__("d1max_odom_bridge")
        self._odom_frame = odom_frame
        self._base_frame = base_frame
        self._pub = self.create_publisher(Odometry, "odom", 10)
        self._tf = TransformBroadcaster(self)
        self._count = 0

        if statics:
            self._publish_statics()

        self._sock = socket.create_connection((host, port), timeout=5.0)
        self._sock.settimeout(None)
        self.get_logger().info(f"已连上 patrol_agent {host}:{port}")
        threading.Thread(target=self._pump, daemon=True).start()

    def _publish_statics(self) -> None:
        caster = StaticTransformBroadcaster(self)
        self._static_caster = caster  # 不留引用会被 GC 掉,静态 TF 就没了
        msgs = []
        for name, (x, y, z, yaw) in SENSOR_OFFSETS.items():
            t = TransformStamped()
            t.header.stamp = self.get_clock().now().to_msg()
            t.header.frame_id = self._base_frame
            t.child_frame_id = name
            t.transform.translation.x = x
            t.transform.translation.y = y
            t.transform.translation.z = z
            qx, qy, qz, qw = _yaw_to_quat(yaw)
            t.transform.rotation.x = qx
            t.transform.rotation.y = qy
            t.transform.rotation.z = qz
            t.transform.rotation.w = qw
            msgs.append(t)
        caster.sendTransform(msgs)
        self.get_logger().info(f"已发 {len(msgs)} 条静态外参(来自开发指南 2.10)")

    def _pump(self) -> None:
        buf = b""
        while rclpy.ok():
            try:
                chunk = self._sock.recv(65536)
            except OSError as exc:
                self.get_logger().error(f"读 patrol_agent 出错: {exc}")
                return
            if not chunk:
                self.get_logger().error("patrol_agent 把连接关了")
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle(line)

    def _handle(self, line: bytes) -> None:
        # 单帧坏掉不能让整条链路死 —— 现场最常见的就是日志混进 stdout。
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(obj, dict) or obj.get("t") != "odom":
            return
        try:
            x = float(obj.get("x", 0.0))
            y = float(obj.get("y", 0.0))
            yaw = float(obj.get("yaw", 0.0))
            vx = float(obj.get("vx", 0.0))
            vy = float(obj.get("vy", 0.0))
            vyaw = float(obj.get("vyaw", 0.0))
        except (TypeError, ValueError):
            return

        now = self.get_clock().now().to_msg()
        qx, qy, qz, qw = _yaw_to_quat(yaw)

        msg = Odometry()
        msg.header.stamp = now
        msg.header.frame_id = self._odom_frame
        msg.child_frame_id = self._base_frame
        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.orientation.x = qx
        msg.pose.pose.orientation.y = qy
        msg.pose.pose.orientation.z = qz
        msg.pose.pose.orientation.w = qw
        msg.twist.twist.linear.x = vx
        msg.twist.twist.linear.y = vy
        msg.twist.twist.angular.z = vyaw
        self._pub.publish(msg)

        t = TransformStamped()
        t.header.stamp = now
        t.header.frame_id = self._odom_frame
        t.child_frame_id = self._base_frame
        t.transform.translation.x = x
        t.transform.translation.y = y
        t.transform.rotation.x = qx
        t.transform.rotation.y = qy
        t.transform.rotation.z = qz
        t.transform.rotation.w = qw
        self._tf.sendTransform(t)

        self._count += 1
        if self._count % 100 == 1:
            self.get_logger().info(
                f"odom #{self._count}: x={x:.3f} y={y:.3f} yaw={yaw:.3f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", default="127.0.0.1:8090",
                    help="patrol_agent 的监听地址,默认 127.0.0.1:8090")
    ap.add_argument("--odom-frame", default="odom")
    ap.add_argument("--base-frame", default="base_link")
    ap.add_argument("--static", action="store_true",
                    help="顺便发传感器静态外参。厂商已经发了 TF 就别加这个")
    args = ap.parse_args()

    host, _, port_s = args.agent.rpartition(":")
    if not host or not port_s.isdigit():
        print(f"--agent 要写成 HOST:PORT,收到的是 {args.agent!r}", file=sys.stderr)
        return 2

    rclpy.init()
    try:
        node = OdomBridge(host, int(port_s), args.odom_frame, args.base_frame,
                          args.static)
    except OSError as exc:
        print(f"连不上 patrol_agent {args.agent}: {exc}\n"
              f"先确认它在跑(见 docs/真机联调手册.md S1)", file=sys.stderr)
        rclpy.shutdown()
        return 1
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
