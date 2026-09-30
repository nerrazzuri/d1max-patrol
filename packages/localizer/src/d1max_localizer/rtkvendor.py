"""厂家 RTK 的辅助进程(W09e 决定 3,``--rtk vendor``):订厂家驱动发的 ``/rtk_pvh``
(``robots_dog_msgs/UniRtkPvh``),每条一行 JSON 吐到标准输出,代理(没有 ROS)读它。

厂家消息的字段名没核过(真机项):按几个候选名取,取不到的不带。消息包 ``robots_dog_msgs`` 只在狗上有;
找不到就说清楚、退 2(代理隔一会儿再起)。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from typing import Any

#: 我们的键 → 厂家消息里可能的字段名(按顺序试)。
FIELDS = {
    "pos_type": ("pos_type", "position_type", "postype"),
    "svs_num": ("svs_num", "sv_num", "sat_num"),
    "diff_age_s": ("diff_age_s", "diff_age", "age"),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "longitude"),
    "alt": ("alt", "height", "altitude"),
    "lat_std": ("lat_std", "latitude_std", "lat_sigma"),
    "lon_std": ("lon_std", "longitude_std", "lon_sigma"),
}


def to_json(msg: Any) -> str | None:
    """一条厂家消息 → 一行 JSON;没有经纬度的不出。"""
    out: dict[str, Any] = {}
    for key, names in FIELDS.items():
        for n in names:
            v = getattr(msg, n, None)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                out[key] = v
                break
    if "lat" not in out or "lon" not in out:
        return None
    return json.dumps(out)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="d1max-rtk-vendor")
    ap.add_argument("--topic", default="/rtk_pvh")
    a = ap.parse_args(argv)
    try:
        import rclpy
        from robots_dog_msgs.msg import UniRtkPvh
    except ImportError as exc:
        print(f"厂家 RTK 辅助进程起不来:{exc}(要在狗上、source 过厂家的 ROS 环境)", file=sys.stderr,
              flush=True)
        return 2
    rclpy.init(args=None)
    node = rclpy.create_node("d1max_rtk_vendor")

    def on(msg: Any) -> None:
        line = to_json(msg)
        if line is not None:
            print(line, flush=True)
    node.create_subscription(UniRtkPvh, a.topic, on, 10)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
