#!/usr/bin/env python3
"""把 ROS2 的定位结果(TF ``loc_map -> base_link``)吐成一条 TCP/JSONL 流。

**这个文件跟 ros2_odom_bridge.py 一样,是故意写成零依赖的。**

它跑在 ROS2 的系统 Python 下(``/usr/bin/python3``,能 import rclpy),
不是本仓库的 venv —— venv 里没有 rclpy,往 venv 里塞 ROS2 是条死路。
所以这里不 import 任何 ``d1max_patrol`` 的东西,线协议就地手写一遍;
权威定义在 ``src/d1max_patrol/protocol/pose_frames.py``,改协议要同时改两处。

    source /opt/ros/humble/setup.bash
    /usr/bin/python3 tools/ros2_pose_bridge.py --listen 0.0.0.0:8091

前提是 slam_toolbox 的 localization 节点已经在跑并且已经收敛(见
docs/建图定位与巡检管线.md 第四节),也就是 TF 树上真有 ``loc_map``。

**方向是单向的:只出不进。** 客户端(``LocalNavBackend``)连上来只读,
一个字节都发不回来。运动指令走另一条线(patrol_agent 的 8090)。这样
定位这一路上不存在"被下指令"的可能,读一条 TCP 流就是读一份只读遥测。

为什么不复用 8090:那条线是 C++ 旁路进程开的,它不认识 ROS,而它自己吐的
``odom`` 是腿式里程、会漂,只能当交叉校验。地图系的位姿只有这里有。
"""

import argparse
import json
import math
import socket
import sys
import threading
import time

try:
    import rclpy
    from rclpy.node import Node
    from tf2_ros import Buffer, TransformException, TransformListener
except ImportError:  # pragma: no cover - 只在没 source ROS2 时走到
    sys.exit(
        "import rclpy 失败。这个脚本要用 ROS2 的系统 Python 跑,不是本仓库的 venv:\n"
        "    source /opt/ros/humble/setup.bash\n"
        "    /usr/bin/python3 tools/ros2_pose_bridge.py --listen 0.0.0.0:8091"
    )

#: 与 pose_frames.PROTO_VERSION 对齐。两边不一致时客户端会主动断开。
PROTO_VERSION = 1


def _quat_to_yaw(x: float, y: float, z: float, w: float) -> float:
    """只取绕 z 的那一路。四足在地面上跑,roll/pitch 对导航没用。"""
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class PoseBridge(Node):
    def __init__(self, host: str, port: int, map_frame: str, base_frame: str,
                 hz: float) -> None:
        super().__init__("d1max_pose_bridge")
        self._map_frame = map_frame
        self._base_frame = base_frame
        self._period = 1.0 / hz

        self._buffer = Buffer()
        self._listener = TransformListener(self._buffer, self)

        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(8)
        self.get_logger().info(
            f"定位桥在 {host}:{port} 监听,{map_frame} -> {base_frame} @ {hz:g} Hz")

        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._pose_loop, daemon=True).start()

    # ---------------------------------------------------------------- 连接

    def _accept_loop(self) -> None:
        while rclpy.ok():
            try:
                sock, addr = self._server.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = json.dumps({
                "t": "hello", "proto": PROTO_VERSION,
                "map_frame": self._map_frame, "base_frame": self._base_frame,
            }, ensure_ascii=False) + "\n"
            try:
                sock.sendall(hello.encode("utf-8"))
            except OSError:
                sock.close()
                continue
            with self._lock:
                self._clients.append(sock)
            self.get_logger().info(f"客户端接入 {addr}")

    def _broadcast(self, line: str) -> None:
        payload = line.encode("utf-8")
        with self._lock:
            clients = list(self._clients)
        for sock in clients:
            try:
                sock.sendall(payload)
            except OSError:
                with self._lock:
                    if sock in self._clients:
                        self._clients.remove(sock)
                sock.close()
                self.get_logger().info("客户端掉了")

    # ---------------------------------------------------------------- 位姿

    def _pose_loop(self) -> None:
        """固定频率发,**查不到 TF 也照发一帧 ok=false**。

        这条流因此同时是心跳: 客户端只要一段时间收不到任何帧,就知道桥本身
        没了,而不用去猜"是定位丢了还是进程死了"。这两件事的处置完全不同。
        """
        n = 0
        while rclpy.ok():
            started = time.monotonic()
            ts_ms = int(time.time() * 1000)
            try:
                tf = self._buffer.lookup_transform(
                    self._map_frame, self._base_frame, rclpy.time.Time())
            except TransformException as exc:
                self._broadcast(json.dumps(
                    {"t": "pose", "ok": False, "reason": str(exc)[:200],
                     "ts_ms": ts_ms}, ensure_ascii=False) + "\n")
            else:
                t = tf.transform.translation
                r = tf.transform.rotation
                yaw = _quat_to_yaw(r.x, r.y, r.z, r.w)
                self._broadcast(json.dumps(
                    {"t": "pose", "ok": True, "x": t.x, "y": t.y, "yaw": yaw,
                     "ts_ms": ts_ms}, ensure_ascii=False) + "\n")
                n += 1
                if n % 100 == 1:
                    self.get_logger().info(
                        f"pose #{n}: x={t.x:.3f} y={t.y:.3f} yaw={yaw:.3f}")
            slack = self._period - (time.monotonic() - started)
            if slack > 0:
                time.sleep(slack)

    def shutdown(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, []
        for sock in clients:
            sock.close()
        self._server.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", default="127.0.0.1:8091",
                    help="监听地址,默认 127.0.0.1:8091。跨机要写 0.0.0.0:8091")
    ap.add_argument("--map-frame", default="loc_map",
                    help="定位地图帧。slam_toolbox 的 localization 模式默认发这个")
    ap.add_argument("--base-frame", default="base_link")
    ap.add_argument("--hz", type=float, default=10.0)
    args = ap.parse_args()

    host, _, port_s = args.listen.rpartition(":")
    if not host or not port_s.isdigit():
        print(f"--listen 要写成 HOST:PORT,收到的是 {args.listen!r}", file=sys.stderr)
        return 2
    if args.hz <= 0:
        print("--hz 必须是正数", file=sys.stderr)
        return 2

    rclpy.init()
    try:
        node = PoseBridge(host, int(port_s), args.map_frame, args.base_frame,
                          args.hz)
    except OSError as exc:
        print(f"监听 {args.listen} 失败: {exc}", file=sys.stderr)
        rclpy.shutdown()
        return 1
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
