#!/usr/bin/env python3
"""W00d 真机探针 —— **不动机器**。

连上常驻旁路进程(``motion/patrol_agent.cpp``),只读不发动作命令(连 ``hold`` 都不发),看:
协议版本对不对(要 v3,带 ``vel``)、旁路进程握没握着控制权、运动状态(真狗站着待命时报哪个 SDK
状态 —— 适配器只把 ``General``/``Gait`` 算 READY)、电量、急停、里程帧在不在刷(多少 Hz)、故障帧。

结果打到屏幕,也写进 ``runs/field-logs/w00d-probe-<时刻>.json``(``--log-dir`` 可改)。

用法(在狗上,旁路进程已在开机窗口抢到控制权)::

    .venv/bin/python tools/w00d_probe.py [--sidecar 127.0.0.1:8090] [--watch-s 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"要写成 host:port,收到 {text!r}")
    return host, int(port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="W00d 真机探针(不动机器)")
    p.add_argument("--sidecar", type=_hostport, default=("127.0.0.1", 8090))
    p.add_argument("--watch-s", type=float, default=3.0, help="数里程帧数多少秒")
    p.add_argument("--log-dir", type=Path, default=ROOT / "runs" / "field-logs")
    return p


async def probe(host: str, port: int, watch_s: float) -> dict[str, Any]:
    from d1max_adapter_d1max.hal import D1MaxHal
    from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
    from d1max_patrol.protocol.agent_frames import PROTO_VERSION

    b = SidecarDeviceBackend(host, port)
    hal = D1MaxHal(host, port, backend=b)
    out: dict[str, Any] = {"sidecar": f"{host}:{port}", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        await hal.connect()                   # 版本不对 connect 就抛;只读,不发 hold
    except Exception as exc:  # noqa: BLE001 - 探针要把任何连不上的原因原样报出来
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        hello = b.hello
        st = b.last_state
        out["proto_want"] = PROTO_VERSION
        out["hello"] = None if hello is None else {
            "proto": hello.proto, "sdk": hello.sdk, "held": hello.held}
        out["held"] = (await hal.control_status()).held
        out["sdk_motion"] = None if st is None else st.motion.value
        out["hal_motion"] = (await hal.motion_status()).value
        out["battery"] = (await hal.battery()).percent
        out["estop"] = await hal.estop_status()
        # 两路原始值:硬急停没按时报 Recover 还是 Unknown,真机上要看(适配器要两路都 Recover)。
        out["estop_raw"] = None if st is None else {"software": st.estop_software.value,
                                                    "hardware": st.estop_hardware.value}
        frames, last, t_end = 0, b.last_odom, time.monotonic() + watch_s
        peak = {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}
        while time.monotonic() < t_end:
            if b.last_odom is not last:
                frames, last = frames + 1, b.last_odom
                for k in peak:
                    peak[k] = max(peak[k], abs(getattr(last, k)))
            await asyncio.sleep(0.002)
        out["odom_hz"] = round(frames / watch_s, 1) if watch_s > 0 else None
        # 站着不动时里程速度的噪声:适配器「停了」的阈值(stopped_eps,默认 0.02)要比它大。
        out["odom_speed_max"] = {k: round(v, 4) for k, v in peak.items()}
        odom = await hal.odometry()
        out["odom"] = {"x": odom.x, "y": odom.y, "yaw": odom.yaw, "valid": odom.valid}
        out["faults"] = [{"code": f.code, "fatal": f.fatal, "text": f.text}
                         for f in await hal.faults()]
        out["ok"] = bool(out["hello"] and out["hello"]["proto"] == PROTO_VERSION
                         and out["held"] and odom.valid)
    finally:
        await hal.close()                     # 只断 Python 这头;控制权留在旁路进程里
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    got = asyncio.run(probe(*args.sidecar, args.watch_s))
    text = json.dumps(got, ensure_ascii=False, indent=2)
    print(text)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    path = args.log_dir / f"w00d-probe-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(text + "\n", encoding="utf-8")
    print(f"已写 {path}", file=sys.stderr)
    return 0 if got.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
