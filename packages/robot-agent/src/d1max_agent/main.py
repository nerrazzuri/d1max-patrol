"""``d1max-agent`` 的入口(W00b 决定 3、4)。

装配:Transport(``mqtt://`` 走 Paho,``memory://`` 走进程内 broker —— 给演示与联调)
× HAL(``sim``;``d1max`` 经 TCP 接常驻旁路进程,W00d)× 引擎(``assembly.build_engine``)
× ``AgentRuntime``。
一切都跑在 ``LoopBridge`` 的事件循环线程里,``asyncio.sleep(period)`` 每拍 ``step`` 一次。

狗上只有这一个进程在开车(W00c5e:老的 HTTP 面 ``server.py`` 与 ``--legacy-http`` 退役,手机只连站点)。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from d1max_agent import AGENT_VERSION
from d1max_agent.assembly import EngineParts, build_engine
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.registration import Registration
from d1max_contract.transport import Transport
from d1max_patrol.protocol.nav_types import Pose

log = logging.getLogger(__name__)


def wall_ms() -> int:
    return int(time.time() * 1000)


def _map_arg(text: str) -> tuple[str, str]:
    map_id, sep, version = text.rpartition(":")
    if not sep or not map_id or not version:
        raise argparse.ArgumentTypeError(f"--map 要写成 <map_id>:<version>,收到 {text!r}")
    return map_id, version


def _home_arg(text: str) -> Pose:
    parts = text.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"--home 要写成 x,y,yaw,收到 {text!r}")
    try:
        x, y, yaw = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--home 里不是数: {text!r}") from exc
    return Pose.from_xy_yaw(x, y, yaw)


def _transport_arg(text: str) -> str:
    if text == "memory://" or text.startswith(("mqtt://", "mqtts://")):
        return text
    raise argparse.ArgumentTypeError(
        f"--transport 只认 mqtt://host[:port]、mqtts://host[:port] 或 memory://,收到 {text!r}")


def _hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"要写成 host:port,收到 {text!r}")
    return host, int(port)


#: 站点狗专用接收口的默认端口(W00c5d)。
INTAKE_PORT = 8444

#: ``--hal d1max`` 的旁路进程与单位换算默认值(W00d 决定三 A:**都待真机实测**)。
D1MAX_DEFAULTS = {"sidecar": ("127.0.0.1", 8090), "mps_per_unit": 0.4, "radps_per_unit": 1.0,
                  "deadband": 0.05, "max_fraction": 0.5, "stopped_eps": 0.02}


def resolve_autonomy(args: argparse.Namespace) -> str:
    """自主级别(W00c6i):显式给了就用它;没给的话**只有仿真**可自主,别的适配器一律要人监护
    (内审:默认偏收紧,以后加的品牌也不例外)。"""
    if args.autonomy is not None:
        return args.autonomy
    return "autonomous" if args.hal == "sim" else "supervised"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max-agent",
                                description=f"D1 Max 机器人代理 {AGENT_VERSION}")
    p.add_argument("--transport", type=_transport_arg, required=True,
                   help="mqtt://host[:port]、mqtts://host[:port] 或 memory://(进程内,演示用)")
    p.add_argument("--hal", choices=("sim", "d1max"), default="sim", help="品牌适配器")
    p.add_argument("--autonomy", choices=("supervised", "autonomous"), default=None,
                   help="自主级别(W00c6i):supervised = goto/巡检只在有人现场监护时才接;"
                        "默认 --hal d1max 是 supervised、sim 是 autonomous。**真狗改成 autonomous "
                        "要等 W11 避障真机验收过了、用户同意**")
    d1 = p.add_argument_group("--hal d1max(比例换算的几个数都待真机实测)")
    d1.add_argument("--sidecar", type=_hostport, default=None,
                    help="旁路进程 host:port,默认 127.0.0.1:8090")
    d1.add_argument("--mps-per-unit", type=float, default=None,
                    help="Move 比例 1.0 对应的 m/s,默认 0.4")
    d1.add_argument("--radps-per-unit", type=float, default=None,
                    help="转向比例 1.0 对应的 rad/s,默认 1.0")
    d1.add_argument("--deadband", type=float, default=None, help="低于这个 m/s 拒,默认 0.05")
    d1.add_argument("--max-fraction", type=float, default=None, help="比例上限,默认 0.5")
    d1.add_argument("--stopped-eps", type=float, default=None,
                    help="里程速度低于它算停了(m/s、rad/s),默认 0.02;要大于站着时的噪声")
    d1.add_argument("--invert-yaw", action="store_true", help="转向方向跟 SDK 相反时翻过来")
    v = p.add_argument_group("视频经站点(W00c5b):站点要看时按需推 SRT")
    v.add_argument("--camera-host", default="192.168.234.1",
                   help="--hal d1max:相机 RTSP 所在主机(rtsp://<host>:8554/front|back)")
    v.add_argument("--video-transcode", action="store_true",
                   help="相机不是 H.264 时转成 H.264 再推(默认原样转封装,不占 CPU)")
    v.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件")
    p.add_argument("--registration", type=Path, required=True, help="站点签发的注册文件")
    p.add_argument("--store-dir", type=Path, required=True, help="幂等记录、事件簿、代次落盘的目录")
    p.add_argument("--runs-root", type=Path, default=None,
                   help="引擎归档目录(老路子,不上传);给了 --outbox 就不要再给")
    o = p.add_argument_group("发件箱(W00c5d,决策 8):运行记录边跑边传,站点确认即删")
    o.add_argument("--outbox", type=Path, default=None,
                   help="发件箱目录(运行记录落 <发件箱>/runs);可以指到临时硬盘")
    o.add_argument("--outbox-max-gb", type=float, default=20.0,
                   help="发件箱上限(GB),到了就不接新的巡检;默认 20")
    o.add_argument("--outbox-mount", default=os.environ.get("D1MAX_OUTBOX_MOUNT", ""),
                   help="发件箱所在的挂载点(临时硬盘);给了就要求它真的挂着,不然不起"
                        "(缺省读环境变量 D1MAX_OUTBOX_MOUNT)")
    o.add_argument("--release-root", type=Path,
                   default=Path(os.environ["D1MAX_RELEASE_ROOT"])
                   if os.environ.get("D1MAX_RELEASE_ROOT") else None,
                   help="双槽发布根目录(站点下发版本,W00c5d 第三部分);缺省读 D1MAX_RELEASE_ROOT")
    o.add_argument("--mapping", action="store_true",
                   help="开建图(W00c5d 第二部分:站点下 mapping/map_build);要本机有 ROS 2")
    o.add_argument("--intake", default=None,
                   help="站点的狗专用接收口 https://host:port;"
                        "mqtts 时默认 https://<broker 主机>:8444")
    p.add_argument("--map", type=_map_arg, required=True, help="已加载地图 <map_id>:<version>")
    p.add_argument("--home", type=_home_arg, default=None,
                   help="原点 x,y,yaw;不给的话预飞检查 home 那一项红,goto 一律 failed")
    p.add_argument("--period", type=float, default=0.1, help="每拍间隔(秒)")
    p.add_argument("--tls-ca", default=None, help="mqtts:站点 CA 证书")
    p.add_argument("--tls-cert", default=None, help="mqtts:本机证书(站点 enroll 签发,CN=robot_id)")
    p.add_argument("--tls-key", default=None, help="mqtts:本机私钥")
    return p


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = build_parser()
    args = p.parse_args(argv)
    tls = (args.tls_ca, args.tls_cert, args.tls_key)
    given = [t is not None for t in tls]
    if args.transport.startswith("mqtts://"):
        # 站点 broker 要双向认证(W00c1):缺哪一件都连不上,不如起动时就说清楚。
        if not all(given):
            p.error("mqtts:// 要带齐 --tls-ca、--tls-cert、--tls-key")
    elif any(given):
        p.error("--tls-* 只用于 mqtts://")
    if (args.outbox is None) == (args.runs_root is None):
        p.error("--outbox 与 --runs-root 给且只给一个(运行记录落在哪)")
    if args.mapping and args.outbox is None:
        p.error("--mapping 要配 --outbox(录包和生成的图都写进发件箱传给站点)")
    if args.outbox is not None:
        if not args.outbox.is_absolute():
            p.error(f"--outbox 要是绝对路径(D1MAX_OUTBOX 没设?):{args.outbox!s}")
        mount = args.outbox_mount
        if mount:
            m = Path(mount)
            if not os.path.ismount(m):
                p.error(f"发件箱要在 {m} 上,可它没挂上(临时硬盘没插好?)—— 不往系统盘上写")
            if m.resolve() not in (args.outbox.resolve(), *args.outbox.resolve().parents):
                p.error(f"--outbox {args.outbox} 不在 {m} 底下")
        args.runs_root = args.outbox / "runs"
        if args.outbox_max_gb <= 0:
            p.error("--outbox-max-gb 要大于 0")
        if args.intake is None and args.transport.startswith("mqtts://"):
            host = urlsplit(args.transport).hostname
            args.intake = f"https://{host}:{INTAKE_PORT}"
        if args.intake is not None and not args.intake.startswith("https://"):
            p.error("--intake 只认 https://(狗的身份是 mTLS 证书)")
        if args.intake is not None and not all(given):
            p.error("--intake 要带 --tls-ca、--tls-cert、--tls-key(狗的身份是证书)")
    d1max_only = [k for k in D1MAX_DEFAULTS if getattr(args, k) is not None]
    if args.invert_yaw:
        d1max_only.append("invert_yaw")
    if args.hal == "d1max":
        for k, v in D1MAX_DEFAULTS.items():
            if getattr(args, k) is None:
                setattr(args, k, v)
    elif d1max_only:
        p.error(f"{', '.join('--' + k.replace('_', '-') for k in d1max_only)} 只用于 --hal d1max")
    return args


@dataclass
class Assembled:
    bridge: Any
    runtime: AgentRuntime
    parts: EngineParts
    hal: Any
    broker: MemoryBroker | None
    period_s: float
    _stop: threading.Event
    pump: Any = None

    def start(self) -> None:
        """在 bridge 的循环里起运行时,并开始每拍 step;发件箱一起起。"""
        self.bridge.call(self.runtime.start, timeout_s=30.0)
        self.bridge.spawn(lambda: self._drive())
        if self.pump is not None:
            self.pump.start()

    async def _drive(self) -> None:
        dt = self.period_s
        while not self._stop.is_set():
            tick = getattr(self.hal, "tick", None)
            if tick is not None:                 # sim 自己要推进;真 HAL 没有 tick
                try:
                    tick(dt)
                except Exception:
                    log.exception("HAL 的 tick 炸了,运行时这一拍照走")
            try:
                await self.runtime.step(dt)
            except Exception:
                log.exception("运行时一拍炸了,下一拍继续")
            await asyncio.sleep(dt)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.bridge.call(self.runtime.close, timeout_s=10.0)
        except Exception:
            log.exception("运行时关不干净")
        # 发件箱最后停(先把狗停好、控制权放掉;发件箱停不等手上那一块传完)。
        if self.pump is not None:
            try:
                self.pump.stop()
            except Exception:
                log.exception("发件箱停不干净")
        self.bridge.stop()


def _make_transport(url: str, client_id: str, broker: MemoryBroker | None, *,
                    tls: tuple[str | None, str | None, str | None] = (None, None, None)
                    ) -> Transport:
    if url == "memory://":
        assert broker is not None
        return MemoryTransport(broker, client_id)
    from d1max_contract import paho_transport
    ca, cert, key = tls
    return paho_transport.PahoTransport(url, client_id=client_id, tls_ca=ca, tls_cert=cert,
                                        tls_key=key)


def build(args: argparse.Namespace) -> Assembled:
    from d1max_patrol.app.bridge import LoopBridge

    registration = Registration.load(args.registration)
    broker = MemoryBroker() if args.transport == "memory://" else None
    bridge = LoopBridge()
    bridge.start()

    def _assemble() -> tuple[Any, EngineParts, AgentRuntime]:
        media: dict | None
        if args.hal == "sim":
            from d1max_adapter_sim.robot import SimRobot
            from d1max_agent.bridges.sim_media import sim_media
            hal: Any = SimRobot(now_ms=wall_ms)
            media = sim_media(wall_ms)
        else:
            from d1max_adapter_d1max.hal import D1MaxHal
            host, port = args.sidecar
            hal = D1MaxHal(host, port, mps_per_unit=args.mps_per_unit,
                           radps_per_unit=args.radps_per_unit, deadband_mps=args.deadband,
                           max_fraction=args.max_fraction, invert_yaw=args.invert_yaw,
                           stopped_eps=args.stopped_eps, now_ms=wall_ms)
            media = None                          # RTSP 取图归后面的工单
        parts = build_engine(hal, runs_root=args.runs_root, now_ms=wall_ms, map_id=args.map[0],
                             home=args.home, media=media)
        transport = _make_transport(args.transport, registration.robot_id, broker,
                                    tls=(args.tls_ca, args.tls_cert, args.tls_key))
        from d1max_agent.video_push import VideoPusher, lavfi_source, rtsp_source
        source = lavfi_source if args.hal == "sim" else rtsp_source(args.camera_host)
        video = VideoPusher(source=source, ffmpeg=args.ffmpeg, transcode=args.video_transcode)
        pump = keeper = mapper = None
        if args.outbox is not None:
            pump, keeper, mapper = _outbox(args, registration, parts)
        runtime = AgentRuntime(transport=transport, registration=registration, hal=hal,
                               store_dir=args.store_dir, now_ms=wall_ms, loaded_map=args.map,
                               parts=parts, video=video, autonomy=resolve_autonomy(args),
                               storage_facts=pump.facts if pump is not None else None,
                               maps=keeper, mapper=mapper,
                               releases=_releases(args, registration))
        if pump is not None:
            runtime._outbox_retry = pump.retry_refused
        return hal, parts, runtime, pump

    async def _in_loop():
        return _assemble()

    hal, parts, runtime, pump = bridge.call(_in_loop, timeout_s=30.0)
    return Assembled(bridge=bridge, runtime=runtime, parts=parts, hal=hal, broker=broker,
                     period_s=args.period, _stop=threading.Event(), pump=pump)


class _NoIntake:
    """没有接收口(memory:// 演示):只攒不传,文件都留在发件箱里。"""

    def put(self, req):
        from d1max_agent.engine.uploader import SinkError
        raise SinkError("没有配站点接收口")


def _outbox(args: argparse.Namespace, registration: Registration, parts: EngineParts
            ) -> tuple[Any, Any, Any]:
    """发件箱 + 后台线程;站点下发地图、录包重建(W00c5d 第二部分)。

    上传、下载都走站点的狗专用口,**mTLS**:跟 MQTT 同一套证书(身份就是证书)。发件箱分三块:
    运行记录 ``runs``、建图录包 ``bags``、重建出来的图 ``maps``,各自一个队列,一条线程轮着传。
    返回 (发件箱线程, 地图工作副本, 录包重建)。"""
    import ssl

    from d1max_agent.engine.http_sink import HttpSink
    from d1max_agent.mapping import (
        DONE,
        MappingService,
        bag_classify,
        map_classify,
        map_settled,
    )
    from d1max_agent.maps import MapKeeper, https_fetch
    from d1max_agent.outbox import Outbox, OutboxPump
    cap = int(args.outbox_max_gb * 2**30)
    sinks: list[Any] = [_NoIntake()] * 3
    keeper = None
    if args.intake is not None:
        ctx = ssl.create_default_context(cafile=args.tls_ca)
        ctx.load_cert_chain(args.tls_cert, args.tls_key)
        sinks = [HttpSink(args.intake, ssl_context=ctx),
                 HttpSink(args.intake + "/bags", ssl_context=ctx),
                 HttpSink(args.intake + "/maps", ssl_context=ctx)]
        keeper = MapKeeper(Path(args.store_dir) / "maps", fetch=https_fetch(args.intake, ctx))
    mapper = None
    if args.mapping:
        from d1max_patrol.app.mapping import MappingConfig, MappingOrchestrator
        from d1max_patrol.app.procs import ProcManager
        store = Path(args.store_dir)
        work = store / "mapwork"
        orch = MappingOrchestrator(ProcManager(store / "logs"),
                                   MappingConfig(bags_dir=args.outbox / "bags", maps_dir=work))
        mapper = MappingService(orch, bags_root=args.outbox / "bags",
                                maps_out=args.outbox / "maps", work_dir=work)
    runs = Outbox(args.outbox, cap_bytes=cap, sink=sinks[0], sn=registration.robot_id,
                  now_ms=wall_ms)
    bags = Outbox(args.outbox, cap_bytes=cap, sink=sinks[1], sn=registration.robot_id,
                  now_ms=wall_ms, sub="bags", run_depth=1, classify=bag_classify,
                  settled=mapper.bag_settled if mapper is not None
                  else (lambda p: (p / DONE).is_file()),
                  delete_lock=mapper.lock if mapper is not None else None)
    maps = Outbox(args.outbox, cap_bytes=cap, sink=sinks[2], sn=registration.robot_id,
                  now_ms=wall_ms, sub="maps", run_depth=2, classify=map_classify,
                  settled=map_settled)

    def _active() -> set[Path]:
        # 引擎跑完之后 archive 还指着上一趟(W00c5d 内部评审):只有正在跑的那一趟算「正在写」,
        # 不然最后一趟一直删不掉。
        a = parts.engine.archive
        return {a.path} if a is not None and parts.engine.running else set()
    runs.active = _active
    # 每一趟带随机后缀:传完就删,狗的钟往回拨也不会跟站点上早就收齐的那一趟撞名(内部评审)。
    import secrets
    parts.engine.run_suffix = lambda: f"{secrets.randbelow(10 ** 6):06d}"
    pump = OutboxPump(runs, more=(bags, maps))
    if mapper is not None:
        from d1max_agent.mapping import storage_pressure
        mapper.pressure = lambda: storage_pressure(pump.facts())
    return pump, keeper, mapper


def _releases(args: argparse.Namespace, registration: Registration) -> Any:
    """站点下发版本(W00c5d 第三部分):有双槽根目录、有站点接收口才开。"""
    if args.release_root is None or args.intake is None:
        return None
    import functools
    import ssl

    from d1max_agent.engine.release import Layout
    from d1max_agent.release_ops import (
        ReleaseOps,
        build_venv,
        default_python,
        https_fetch,
        pip_args_from_env,
    )
    ctx = ssl.create_default_context(cafile=args.tls_ca)
    ctx.load_cert_chain(args.tls_cert, args.tls_key)

    def restart() -> None:
        # 代理单元是 Restart=always:自己好好退出,systemd 从新的 current 起来。等 3 秒,让回执与
        # 「在切了」那条事件先出去。
        threading.Timer(3.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    return ReleaseOps(Layout(root=args.release_root), fetch=https_fetch(args.intake, ctx),
                      build=functools.partial(build_venv, pip_args=pip_args_from_env(),
                                              python=default_python()),
                      work=Path(args.store_dir) / "release-dl", now_ms=wall_ms,
                      restart=restart, sn=registration.robot_id)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    assembled = build(args)
    try:
        assembled.start()
    except Exception as exc:  # noqa: BLE001 - 连不上 broker 之类:退非零,交给 systemd 重试
        log.error("d1max-agent 起不来: %r", exc)
        print(f"d1max-agent 起不来: {exc!r}", file=sys.stderr, flush=True)
        assembled.stop()
        return 1
    got: list[str] = []

    def _on_term(signum, frame):              # systemd 停服务发 SIGTERM:走同一条收尾路
        got.append(signal.Signals(signum).name)
        assembled._stop.set()

    signal.signal(signal.SIGTERM, _on_term)
    print(f"d1max-agent 起来了:transport={args.transport} hal={args.hal} "
          f"robot={assembled.runtime.registration.robot_id}", flush=True)
    try:
        while not assembled._stop.wait(0.5):
            pass
        if got:
            print(f"收到 {got[0]},收尾中……", flush=True)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C,收尾中……", flush=True)
    finally:
        assembled.stop()
    return 0


if __name__ == "__main__":      # pragma: no cover - 入口
    sys.exit(main())
