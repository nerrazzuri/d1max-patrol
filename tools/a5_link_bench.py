"""在另一台机器上收狗的雷达、相机话题,量链路够不够(商业化 A5,决策 50)。

自带算力板的前提:**雷达点云能经狗内网的以太网从厂商 Orin 送到我们的板子上,帧率不掉、延迟够小**。
板子没到之前先拿笔记本顶:笔记本插进狗的内网,起好 zenoh 路由(``config/zenoh_router.json5`` 或者
``deploy/d1max-zenohd-start --print`` 打出来的那份),跑这个工具。

    python tools/a5_link_bench.py --seconds 60 /front_lidar /rear_lidar
    python tools/a5_link_bench.py --seconds 60 --json runs/a5/link.json /front_lidar

每个话题打:帧数、帧率、带宽(MB/s)、延迟(收到的时刻减报文头时间戳,p50/p95/最大)、断档(两帧间隔
超过中位间隔两倍的次数)、PASS/FAIL。**延迟里含两台机器的钟差**:两边都对上同一个时间源再量,
或者用 ``--offset-ms`` 减掉已知的钟差。

**钟对不上的时候**(两只狗上都见过:雷达时间戳跟 Orin 的钟差了两百多天):中位延迟的绝对值超过 10 秒,
就当两个钟不是一个钟,改看**抖动**:每一帧的延迟减去最小的那一帧,p95 —— 也就是网络在最快那一帧
之上又多拖了多久。结论按抖动判,并标上 ``clock_mismatch``。

门槛(``--min-hz-ratio``、``--max-p95-ms``)是占位的,真机量过以后按定位器的要求改。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 中位延迟的绝对值超过它(10 秒),就当时间戳跟本机不是一个钟。
CLOCK_MISMATCH_MS = 10_000.0


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


@dataclass
class TopicStats:
    """一个话题收到的每一帧:收到的时刻(秒)、报文头时间戳(秒,没有就 None)、字节数。"""

    name: str
    recv: list[float] = field(default_factory=list)
    lat_ms: list[float] = field(default_factory=list)
    nbytes: int = 0

    def add(self, recv_s: float, stamp_s: float | None, nbytes: int, *,
            offset_ms: float = 0.0) -> None:
        self.recv.append(recv_s)
        self.nbytes += nbytes
        if stamp_s is not None and stamp_s > 0:
            self.lat_ms.append((recv_s - stamp_s) * 1000.0 - offset_ms)

    def summary(self, seconds: float) -> dict[str, Any]:
        n = len(self.recv)
        gaps_s = [b - a for a, b in zip(self.recv, self.recv[1:], strict=False)]
        med = statistics.median(gaps_s) if gaps_s else 0.0
        return {"topic": self.name, "frames": n,
                # A 阶段外审 M2:带时间戳、算得出延迟的帧有几帧(0 = 延迟没测到,不能判通过)
                "lat_samples": len(self.lat_ms),
                "hz": round(n / seconds, 2) if seconds > 0 else 0.0,
                "median_hz": round(1.0 / med, 2) if med > 0 else 0.0,
                "mb_per_s": round(self.nbytes / seconds / 1e6, 3) if seconds > 0 else 0.0,
                "lat_p50_ms": round(_pct(self.lat_ms, 0.5), 1),
                "lat_p95_ms": round(_pct(self.lat_ms, 0.95), 1),
                "lat_max_ms": round(max(self.lat_ms), 1) if self.lat_ms else 0.0,
                # 抖动:每帧延迟减最快那一帧(钟差抵掉了)
                "jitter_p95_ms": round(_pct([x - min(self.lat_ms) for x in self.lat_ms], 0.95), 1)
                if self.lat_ms else 0.0,
                "clock_mismatch": bool(self.lat_ms)
                and abs(_pct(self.lat_ms, 0.5)) > CLOCK_MISMATCH_MS,
                "gaps": sum(1 for g in gaps_s if med > 0 and g > 2 * med),
                "max_gap_ms": round(max(gaps_s) * 1000.0, 1) if gaps_s else 0.0}


def verdict(s: dict[str, Any], *, want_hz: float, min_hz_ratio: float,
            max_p95_ms: float) -> tuple[str, str]:
    """一个话题过没过:``("PASS"|"FAIL", 为什么)``。"""
    if s["frames"] == 0:
        return "FAIL", "一帧都没收到(zenoh 路由起了吗?话题名对吗?)"
    if s["hz"] < want_hz * min_hz_ratio:
        return "FAIL", f"帧率 {s['hz']} Hz,不到要的 {want_hz} Hz 的 {min_hz_ratio:.0%}"
    if not s.get("lat_samples"):
        return "UNKNOWN", "帧都收到了,但没有一帧带时间戳:延迟没测到,不能算通过"
    if s["clock_mismatch"]:
        if s["jitter_p95_ms"] > max_p95_ms:
            return "FAIL", (f"时间戳跟本机不是一个钟,按抖动看:p95 {s['jitter_p95_ms']} ms,超过 "
                            f"{max_p95_ms} ms")
        return "PASS", "时间戳跟本机不是一个钟,按抖动判的"
    if s["lat_p95_ms"] > max_p95_ms:
        return "FAIL", f"延迟 p95 {s['lat_p95_ms']} ms,超过 {max_p95_ms} ms(钟差减了吗?)"
    return "PASS", ""


def render(rows: list[dict[str, Any]]) -> str:
    out = [f"{'话题':<20}{'帧':>7}{'Hz':>8}{'MB/s':>8}{'p50ms':>12}{'p95ms':>12}{'抖动p95':>9}"
           f"{'断档':>6}  结论"]
    for r in rows:
        out.append(f"{r['topic']:<20}{r['frames']:>7}{r['hz']:>8}{r['mb_per_s']:>8}"
                   f"{r['lat_p50_ms']:>12}{r['lat_p95_ms']:>12}{r['jitter_p95_ms']:>9}"
                   f"{r['gaps']:>6}  {r['verdict']}"
                   + (f"({r['why']})" if r["why"] else ""))
    total = sum(r["mb_per_s"] for r in rows)
    out.append(f"合计 {total:.2f} MB/s ≈ {total * 8:.0f} Mbit/s")
    return "\n".join(out)


def _collect(topics: list[str], seconds: float, offset_ms: float) -> dict[str, TopicStats]:
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import PointCloud2

    rclpy.init()
    node = rclpy.create_node("d1max_a5_link_bench")
    stats = {t: TopicStats(t) for t in topics}

    def cb(topic: str):
        def f(msg: Any) -> None:
            st = msg.header.stamp
            stats[topic].add(time.time(), st.sec + st.nanosec * 1e-9,
                             len(msg.data), offset_ms=offset_ms)
        return f
    for t in topics:
        node.create_subscription(PointCloud2, t, cb(t), qos_profile_sensor_data)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        rclpy.spin_once(node, timeout_sec=0.1)
    node.destroy_node()
    rclpy.shutdown()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("topics", nargs="+")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--want-hz", type=float, default=10.0, help="雷达标称帧率(缺省 10)")
    ap.add_argument("--min-hz-ratio", type=float, default=0.95)
    ap.add_argument("--max-p95-ms", type=float, default=50.0)
    ap.add_argument("--offset-ms", type=float, default=0.0, help="已知钟差(本机减狗),从延迟里减掉")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    stats = _collect(a.topics, a.seconds, a.offset_ms)
    rows = []
    for t in a.topics:
        s = stats[t].summary(a.seconds)
        s["verdict"], s["why"] = verdict(s, want_hz=a.want_hz, min_hz_ratio=a.min_hz_ratio,
                                         max_p95_ms=a.max_p95_ms)
        rows.append(s)
    print(render(rows))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"seconds": a.seconds, "topics": rows}, ensure_ascii=False,
                                     indent=1), encoding="utf-8")
    return 0 if all(r["verdict"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
