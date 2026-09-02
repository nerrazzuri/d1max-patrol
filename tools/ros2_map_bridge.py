#!/usr/bin/env python3
"""把 ROS2 的 ``/map``(``nav_msgs/OccupancyGrid``)吐成一条 TCP/JSONL 流。

**跟 ros2_pose_bridge.py 一样,这个文件是故意写成零依赖的。**

它跑在 ROS2 的系统 Python 下(``/usr/bin/python3``,能 import rclpy),
不是本仓库的 venv。所以这里不 import 任何 ``d1max_patrol`` 的东西,线协议
就地手写一遍;权威定义在 ``src/d1max_patrol/protocol/map_frames.py``,
改协议要同时改两处(那边有一条测试拿本文件的字面量来对)。

    source /opt/ros/humble/setup.bash
    /usr/bin/python3 tools/ros2_map_bridge.py --listen 0.0.0.0:8092

前提是 slam_toolbox 在跑并且已经在发 ``/map``(见
docs/建图定位与巡检管线.md 第三节)。

**方向是单向的:只出不进。** app 连上来只读,一个字节都发不回来 ——
建图这一路上不存在"被下指令"的可能。

**降频在发送前做。** ``/map`` 的更新时机由 slam_toolbox 决定,可能一秒
好几张;一张 64 万格的图哪怕压过也有几百 KB,全发出去只会把链路堵死,
而 UI 上一秒钟看四遍和看一遍没有区别。距上次发送不足 ``1/max-hz`` 的
整帧直接丢掉 —— 丢的是旧的那张,留下的永远是最新的。
"""

import argparse
import base64
import json
import math
import socket
import struct
import sys
import threading
import time

try:
    import rclpy
    from nav_msgs.msg import OccupancyGrid
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
except ImportError:  # pragma: no cover - 只在没 source ROS2 时走到
    sys.exit(
        "import rclpy 失败。这个脚本要用 ROS2 的系统 Python 跑,不是本仓库的 venv:\n"
        "    source /opt/ros/humble/setup.bash\n"
        "    /usr/bin/python3 tools/ros2_map_bridge.py --listen 0.0.0.0:8092"
    )

#: 与 map_frames.PROTO_VERSION 对齐。两边不一致时客户端会主动断开。
PROTO_VERSION = 1

#: 一对 = int8 的值 + 小端 uint32 的次数。与 map_frames._PAIR 对齐。
_PAIR = struct.Struct("<bI")
_MAX_RUN = 0xFFFFFFFF


def _quat_to_yaw(x, y, z, w):
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _encode_rle(cells):
    """游程编码。与 map_frames.encode_rle 对齐,越界值就地夹到合法区间。

    这里**不抛**:桥挂掉等于建图 UI 全黑,而 ROS 那边偶尔冒一个越界值
    (驱动或插件的锅)不该让整个建图过程看不见图。权威实现在收端会再校
    一次,真出错在那边看得见。
    """
    out = bytearray()
    run_value = None
    run_len = 0
    for cell in cells:
        value = -1 if cell < -1 else (100 if cell > 100 else int(cell))
        if value == run_value:
            run_len += 1
            if run_len == _MAX_RUN:
                out += _PAIR.pack(run_value, run_len)
                run_value, run_len = None, 0
            continue
        if run_value is not None:
            out += _PAIR.pack(run_value, run_len)
        run_value, run_len = value, 1
    if run_value is not None:
        out += _PAIR.pack(run_value, run_len)
    return base64.b64encode(bytes(out)).decode("ascii")


class MapBridge(Node):
    def __init__(self, host, port, topic, max_hz):
        super().__init__("d1max_map_bridge")
        self._topic = topic
        self._min_gap = 1.0 / max_hz
        self._last_sent = 0.0
        self._sent = 0
        self._dropped = 0

        self._clients = []
        self._lock = threading.Lock()

        # /map 是 transient_local 的:订阅者晚来也能拿到最后一张。少了这个
        # QoS,slam_toolbox 不更新图的时候桥就一帧都收不到。
        qos = QoSProfile(
            depth=1,
            history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(OccupancyGrid, topic, self._on_map, qos)

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(8)
        self.get_logger().info(
            f"地图桥在 {host}:{port} 监听,{topic} 最快 {max_hz:g} Hz")

        threading.Thread(target=self._accept_loop, daemon=True).start()

    # ---------------------------------------------------------------- 连接

    def _accept_loop(self):
        while rclpy.ok():
            try:
                sock, addr = self._server.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = json.dumps({
                "t": "hello", "proto": PROTO_VERSION, "topic": self._topic,
            }, ensure_ascii=False) + "\n"
            try:
                sock.sendall(hello.encode("utf-8"))
            except OSError:
                sock.close()
                continue
            with self._lock:
                self._clients.append(sock)
            self.get_logger().info(f"客户端接入 {addr}")

    def _broadcast(self, line):
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

    # ---------------------------------------------------------------- 地图

    def _on_map(self, msg):
        now = time.monotonic()
        if now - self._last_sent < self._min_gap:
            # 降频在**编码之前**:压一张 64 万格的图本身就要几十毫秒,
            # 压完再丢等于白烧 CPU,而这个进程还要跟 slam_toolbox 抢核。
            self._dropped += 1
            return
        self._last_sent = now

        info = msg.info
        pos = info.origin.position
        rot = info.origin.orientation
        frame = {
            "t": "map", "proto": PROTO_VERSION,
            "w": int(info.width), "h": int(info.height),
            "res": float(info.resolution),
            "ox": float(pos.x), "oy": float(pos.y),
            "oyaw": _quat_to_yaw(rot.x, rot.y, rot.z, rot.w),
            "rle": _encode_rle(msg.data),
            "ts_ms": int(time.time() * 1000),
        }
        self._broadcast(json.dumps(frame, ensure_ascii=False) + "\n")
        self._sent += 1
        if self._sent % 20 == 1:
            self.get_logger().info(
                f"map #{self._sent}: {info.width}x{info.height} "
                f"@ {info.resolution:.3f}m,rle {len(frame['rle'])}B,"
                f"降频丢了 {self._dropped} 帧")

    def shutdown(self):
        with self._lock:
            clients, self._clients = self._clients, []
        for sock in clients:
            sock.close()
        self._server.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", default="127.0.0.1:8092",
                    help="监听地址,默认 127.0.0.1:8092。跨机要写 0.0.0.0:8092")
    ap.add_argument("--topic", default="/map",
                    help="占据栅格话题。slam_toolbox 默认发 /map")
    ap.add_argument("--max-hz", type=float, default=1.0,
                    help="发送上限,超出的帧整帧丢掉。默认 1")
    args = ap.parse_args()

    host, _, port_s = args.listen.rpartition(":")
    if not host or not port_s.isdigit():
        print(f"--listen 要写成 HOST:PORT,收到的是 {args.listen!r}", file=sys.stderr)
        return 2
    if args.max_hz <= 0:
        print("--max-hz 必须是正数", file=sys.stderr)
        return 2

    rclpy.init()
    try:
        node = MapBridge(host, int(port_s), args.topic, args.max_hz)
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
