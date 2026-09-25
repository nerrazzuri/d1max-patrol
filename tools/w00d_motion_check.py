#!/usr/bin/env python3
"""W00d 真机运动检查 —— **会动机器,必须有人在场守着硬急停**。

不带 ``--i-am-present`` 直接退出,连旁路进程都不连。

前提:旁路进程已在开机窗口抢到控制权,狗**已经站起**(本工具不替人站起);周围 2 m 净空。

**命令按 SDK 的比例值给**(``--turn-fraction``、``--fwd-fraction``,默认 0.3,上限 0.35),不按 m/s:
要量的正是「比例值 → m/s」的系数,拿一个没实测过的系数去换算只会把错带进来。工具把 HAL 的系数设成
1、比例上限设成 0.35,再发的速度都不会被夹;万一被夹了(``clamped``)就中止。

走五步,每步之后都 ``stop`` 并等停稳;任何异常、Ctrl+C 都先停车再退出:

1. **原地慢转**:比例 ``--turn-fraction`` 转 ``--turn-s`` 秒(默认 3 s),看里程里转了多少、往哪边转
   (HAL 约定正值逆时针;反了就给代理加 ``--invert-yaw``),折算 ``radps_per_unit``。
2. **前进**:比例 ``--fwd-fraction`` 走 ``--fwd-s`` 秒(默认 2 s),折算 ``mps_per_unit``。
3. **停止用时**:第 2 步末尾发 ``stop``,量到里程速度归零用了多久。
4. **ttl 到期自停**:按第 2 步的比例发一条 ``ttl=500 ms`` 的速度后不再续,量多久停。
5. **死区**:绕过 HAL 的死区检查,直接给旁路进程发比例 ``--deadband-fraction``(默认 0.1)1 s,
   看里程动没动 —— 旧记录「0.11 几乎不动、0.3 明显走」。

结果写进 ``runs/field-logs/w00d-motion-<时刻>.json``(``--log-dir`` 可改),末尾给出建议的
``--mps-per-unit`` / ``--radps-per-unit`` / ``--invert-yaw``(实测速度 ÷ 实际发出的比例)。

用法::

    .venv/bin/python tools/w00d_motion_check.py --i-am-present [--sidecar 127.0.0.1:8090]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: 这一趟的比例上限:比代理的默认上限(0.5)还紧。现场站着人。
MAX_FRACTION = 0.35
MAX_STEP_S = 4.0
#: 等停稳的上限。超过就报失败,不再往下走。
SETTLE_S = 3.0
TICK_S = 0.1


def _hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"要写成 host:port,收到 {text!r}")
    return host, int(port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="W00d 真机运动检查(会动机器,要人在场)")
    p.add_argument("--i-am-present", action="store_true",
                   help="我在现场、守着硬急停、周围净空。不给这个不跑")
    p.add_argument("--sidecar", type=_hostport, default=("127.0.0.1", 8090))
    p.add_argument("--turn-fraction", type=float, default=0.3, help="转向比例值(正=想要逆时针)")
    p.add_argument("--turn-s", type=float, default=3.0)
    p.add_argument("--fwd-fraction", type=float, default=0.3, help="前进比例值")
    p.add_argument("--fwd-s", type=float, default=2.0)
    p.add_argument("--deadband-fraction", type=float, default=0.1)
    p.add_argument("--log-dir", type=Path, default=ROOT / "runs" / "field-logs")
    return p


def _wrap(a: float) -> float:
    return math.remainder(a, 2 * math.pi)


async def _settle(hal: Any) -> float | None:
    """等 ``stopped()``;返回用了多少秒,超时返回 None。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < SETTLE_S:
        if await hal.stopped():
            return round(time.monotonic() - t0, 3)
        await asyncio.sleep(0.02)
    return None


async def _send(hal: Any, vx: float, wz: float, ttl_ms: int, seq: int = 1) -> None:
    from d1max_contract.hal import VelocityCommand

    got = await hal.set_velocity(VelocityCommand(seq=seq, ttl_ms=ttl_ms, frame="base",
                                                 vx=vx, vy=0.0, wz=wz))
    if got.rejected:
        raise RuntimeError(f"速度被拒:{got.reason}")
    if got.clamped:
        raise RuntimeError(f"速度被夹了(发 {vx},{wz},实际 {got.applied_vx},{got.applied_wz}):"
                           "建议值会算错,中止")


async def _drive(hal: Any, vx: float, wz: float, seconds: float) -> None:
    t_end, seq = time.monotonic() + seconds, 0
    while time.monotonic() < t_end:
        seq += 1
        await _send(hal, vx, wz, 300, seq)
        await asyncio.sleep(TICK_S)


async def check(args: argparse.Namespace) -> dict[str, Any]:
    from d1max_adapter_d1max.hal import D1MaxHal
    from d1max_contract.hal import MotionStatus
    from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend

    host, port = args.sidecar
    b = SidecarDeviceBackend(host, port)
    # 系数取 1:HAL 收的「m/s」就是比例值,换算不引入未实测的数。死区交给第 5 步去量,不挡。
    hal = D1MaxHal(host, port, backend=b, mps_per_unit=1.0, radps_per_unit=1.0,
                   deadband_mps=0.0, max_fraction=MAX_FRACTION)
    out: dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sidecar": f"{host}:{port}",
                           "fractions": {"turn": args.turn_fraction, "fwd": args.fwd_fraction},
                           "steps": {}}
    await hal.connect()
    try:
        await hal.acquire_control()
        motion = await hal.motion_status()
        if motion is not MotionStatus.READY:
            out["ok"] = False
            out["error"] = f"狗不在待命站姿(HAL 报 {motion.value}),先站起再跑"
            return out
        steps = out["steps"]

        # 1. 原地慢转
        o0 = await hal.odometry()
        await _drive(hal, 0.0, args.turn_fraction, args.turn_s)
        await hal.stop()
        settle = await _settle(hal)
        o1 = await hal.odometry()
        dyaw = _wrap(o1.yaw - o0.yaw)
        rate = dyaw / args.turn_s
        steps["turn"] = {"fraction": args.turn_fraction, "seconds": args.turn_s,
                         "yaw_delta_rad": round(dyaw, 4), "rate_radps": round(rate, 4),
                         "direction_ok": dyaw * args.turn_fraction > 0, "settle_s": settle}
        if settle is None:
            raise RuntimeError("转完停不稳")

        # 2、3. 前进 + 停止用时
        o0 = await hal.odometry()
        await _drive(hal, args.fwd_fraction, 0.0, args.fwd_s)
        t_stop = time.monotonic()
        await hal.stop()
        settle = await _settle(hal)
        o1 = await hal.odometry()
        dist = math.hypot(o1.x - o0.x, o1.y - o0.y)
        steps["forward"] = {"fraction": args.fwd_fraction, "seconds": args.fwd_s,
                            "distance_m": round(dist, 4),
                            "speed_mps": round(dist / args.fwd_s, 4)}
        steps["stop"] = {"stop_to_still_s": None if settle is None
                         else round(time.monotonic() - t_stop, 3)}
        if settle is None:
            raise RuntimeError("stop 之后停不稳")

        # 4. ttl 到期自停
        t0 = time.monotonic()
        await _send(hal, args.fwd_fraction, 0.0, 500)
        await asyncio.sleep(0.5)
        settle = await _settle(hal)
        steps["ttl"] = {"ttl_ms": 500, "send_to_still_s": None if settle is None
                        else round(time.monotonic() - t0, 3)}
        if settle is None:
            raise RuntimeError("ttl 到期没停")

        # 5. 死区:直接给旁路进程发小比例
        o0 = await hal.odometry()
        t_end = time.monotonic() + 1.0
        while time.monotonic() < t_end:
            await b.vel(args.deadband_fraction, 0.0, 0.0, 300)
            await asyncio.sleep(TICK_S)
        await hal.stop()
        await _settle(hal)
        o1 = await hal.odometry()
        steps["deadband"] = {"fraction": args.deadband_fraction,
                             "moved_m": round(math.hypot(o1.x - o0.x, o1.y - o0.y), 4)}

        out["suggest"] = {
            "mps_per_unit": round(steps["forward"]["speed_mps"] / abs(args.fwd_fraction), 3),
            "radps_per_unit": round(abs(rate) / abs(args.turn_fraction), 3),
            "invert_yaw": not steps["turn"]["direction_ok"]}
        out["ok"] = True
    except BaseException as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, Exception):
            raise
    finally:
        try:
            await hal.stop()                  # 不管哪一步出事,先停车
        finally:
            await hal.close()                 # 只断 Python 这头;控制权留在旁路进程里
    return out


def _check_limits(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for name in ("fwd_fraction", "turn_fraction"):
        v = getattr(args, name)
        if not (math.isfinite(v) and 0 < abs(v) <= MAX_FRACTION):
            p.error(f"--{name.replace('_', '-')} 的绝对值要在 (0, {MAX_FRACTION}]")
    if args.fwd_fraction < 0:
        p.error("--fwd-fraction 只许往前")
    for name in ("turn_s", "fwd_s"):
        if not 0 < getattr(args, name) <= MAX_STEP_S:
            p.error(f"--{name.replace('_', '-')} 要在 (0, {MAX_STEP_S}]")
    if not 0 < args.deadband_fraction < 0.3:
        p.error("--deadband-fraction 要在 (0, 0.3)")


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not args.i_am_present:
        print("这个工具会让狗动起来。人在现场、守着硬急停、周围净空,再带 --i-am-present 跑。",
              file=sys.stderr)
        return 2
    _check_limits(p, args)
    got = asyncio.run(check(args))
    text = json.dumps(got, ensure_ascii=False, indent=2)
    print(text)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    path = args.log_dir / f"w00d-motion-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(text + "\n", encoding="utf-8")
    print(f"已写 {path}", file=sys.stderr)
    return 0 if got.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
