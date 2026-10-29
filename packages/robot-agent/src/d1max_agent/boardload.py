"""上装主控板满载脚本 ``d1max-board-load``(硬件组散热测试 C2)。

在 RK3588 上把巡逻时最大的那份负载复现出来,一直跑到温度稳定,同时每 10 秒记温度、频率、风扇
(跟代理的 ``boardhealth`` 同一份 CSV)。各样负载都能单开:

- ``--replay-bag B --prior DIR``:拿录包回放定位器(``d1max-loc replay``,MOLA 真算),跑完再跑 ——
  最接近建图、定位的真实负载。没有录包时用 ``--cpu N``(N 个满载进程)顶。
- ``--video N``:N 路 720p15 软件编码(ffmpeg libx264 ultrafast,画面推流转码那份)。
- ``--write-dir DIR --write-mbps M``:录像写盘 —— 按码率往 NVMe 写、每 2 秒落一次盘(跟录像一样),
  只留最近 ``--write-keep-gb``,跑完删干净。
- ``--upload HOST:PORT --upload-mbps M``:上传 —— TCP 按码率往对面灌;对面(笔记本、站点)跑
  ``d1max-board-load --sink PORT`` 收了就丢。走 4G / WiFi 时就是路由器满功率。
- ``--payload FILE``:灯、警笛按巡逻占空比开关(``--strobe 30/300`` = 每 300 秒开 30 秒)。
  **要先停代理**(继电器板的串口只能一个人用);警笛很响,缺省不开(``--siren 5/600`` 才开)。

结束(``--minutes`` 到了、Ctrl+C)时打总结:最高、最后的 SoC 温度、降没降过频、风扇停没停;给了
``--ambient``(室温)就按温升换算成 40 °C 环境下的温度。
温度稳定 = 跑满 2 小时、最近 30 分钟变化不到 1 °C。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

#: 温度稳定的判据(硬件组 C2):至少跑这么久、最近这么长时间里最高最低差不到 STABLE_C。
STABLE_MIN_S = 2 * 3600.0
STABLE_WINDOW_S = 30 * 60.0
STABLE_C = 1.0
#: 温升换算的目标环境温度。
TARGET_AMBIENT_C = 40.0
#: 停子进程时先礼后兵:发 SIGTERM 等这么久,还在就 SIGKILL。
STOP_WAIT_S = 5.0


# ------------------------------------------------------------ 小件

def parse_duty(text: str) -> tuple[float, float]:
    """``"开/周期"``(秒)→ ``(开, 周期)``;``"0"`` = 不开。"""
    if text.strip() == "0":
        return 0.0, 0.0
    try:
        on_s, period = (float(x) for x in text.split("/"))
    except ValueError as exc:
        raise ValueError(f"占空比写成「开几秒/每几秒」,比如 30/300;收到 {text!r}") from exc
    if not (0 < on_s <= period):
        raise ValueError(f"占空比要 0 < 开 ≤ 周期;收到 {text!r}")
    return on_s, period


def duty_on(t: float, on_s: float, period: float) -> bool:
    """跑了 ``t`` 秒时这一路该不该开(每个周期开头开 ``on_s`` 秒)。"""
    return on_s > 0 and period > 0 and (t % period) < on_s


def project(temp: float, ambient: float | None, target: float = TARGET_AMBIENT_C) -> float | None:
    """按温升换算:室温 ``ambient`` 下测到 ``temp``,环境到 ``target`` 时大约多少度。"""
    return None if ambient is None else temp + (target - ambient)


class Stability:
    """温度稳没稳:跑满 :data:`STABLE_MIN_S`,最近 :data:`STABLE_WINDOW_S` 里最高最低差不到
    :data:`STABLE_C`。"""

    def __init__(self) -> None:
        self._pts: deque[tuple[float, float]] = deque()
        self._t0: float | None = None

    def feed(self, t: float, temp: float) -> None:
        if self._t0 is None:
            self._t0 = t
        self._pts.append((t, temp))
        while self._pts and self._pts[0][0] < t - STABLE_WINDOW_S:
            self._pts.popleft()

    def stable(self, t: float) -> bool:
        if self._t0 is None or t - self._t0 < STABLE_MIN_S or not self._pts:
            return False
        if self._pts[0][0] > t - STABLE_WINDOW_S + 60:      # 窗口里的数还不够半小时
            return False
        temps = [v for _, v in self._pts]
        return max(temps) - min(temps) < STABLE_C


# ------------------------------------------------------------ 写盘、上传

class DiskWriter:
    """按码率往目录里写(录像写盘):每 ``segment_mb`` 一个文件,每 ``sync_every_s`` 落一次盘,只留最近
    ``keep_mb``;停的时候删干净。在线程里跑。"""

    def __init__(self, root: Path, *, mbps: float, keep_mb: float = 4096, segment_mb: float = 64,
                 sync_every_s: float = 2.0, fsync: Any = os.fsync) -> None:
        self.root = Path(root) / f"d1max-board-load-{os.getpid()}"
        self.rate = mbps * 1e6 / 8                       # 字节/秒
        self.keep = keep_mb * 1024 * 1024
        self.seg = segment_mb * 1024 * 1024
        self.sync_every = sync_every_s
        self._fsync = fsync
        self.written = 0
        self.max_kept_bytes = 0
        self.error = ""
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._th = threading.Thread(target=self._run, name="board-load-write", daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(10)
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self) -> None:
        block = os.urandom(256 * 1024)                    # 随机数:压不了,跟录像一样
        files: deque[tuple[Path, int]] = deque()
        n = 0
        t0 = time.monotonic()
        last_sync = t0
        fh = None
        try:
            while not self._stop.is_set():
                if fh is None or fh.tell() >= self.seg:
                    if fh is not None:
                        fh.close()
                    n += 1
                    p = self.root / f"seg-{n:06d}.bin"
                    fh = p.open("wb")
                    files.append((p, 0))
                due = (time.monotonic() - t0) * self.rate - self.written
                if due < len(block):
                    time.sleep(min(0.05, (len(block) - due) / self.rate))
                else:
                    fh.write(block)
                    self.written += len(block)
                    p, sz = files[-1]
                    files[-1] = (p, sz + len(block))
                if time.monotonic() - last_sync >= self.sync_every:
                    fh.flush()
                    self._fsync(fh.fileno())
                    last_sync = time.monotonic()
                kept = sum(sz for _, sz in files)
                while kept > self.keep and len(files) > 1:   # 只留最近 keep_mb(正在写的那段不删)
                    old, sz = files.popleft()
                    old.unlink(missing_ok=True)
                    kept -= sz
                self.max_kept_bytes = max(self.max_kept_bytes, kept)
        except OSError as exc:
            self.error = str(exc)
        finally:
            if fh is not None:
                fh.close()


class Sender:
    """按码率往 ``host:port`` 灌 TCP(上传)。对面不在:记下原因,过 ``retry_s`` 再连。"""

    def __init__(self, host: str, port: int, *, mbps: float, retry_s: float = 5.0) -> None:
        self.addr = (host, port)
        self.rate = mbps * 1e6 / 8
        self.retry_s = retry_s
        self.sent = 0
        self.error = ""
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        self._th = threading.Thread(target=self._run, name="board-load-upload", daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(10)

    def _run(self) -> None:
        block = os.urandom(64 * 1024)
        while not self._stop.is_set():
            try:
                with socket.create_connection(self.addr, timeout=5) as s:
                    self.error = ""
                    t0, base = time.monotonic(), self.sent
                    while not self._stop.is_set():
                        due = (time.monotonic() - t0) * self.rate - (self.sent - base)
                        if due < len(block):
                            time.sleep(min(0.05, (len(block) - due) / self.rate))
                            continue
                        s.sendall(block)
                        self.sent += len(block)
            except OSError as exc:
                self.error = str(exc)
                self._stop.wait(self.retry_s)


class Sink:
    """收上传的那头:收了就丢,数一数收了多少。"""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self.addr = (host, port)
        self.received = 0
        self._stop = threading.Event()
        self._srv: socket.socket | None = None

    def start(self) -> int:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(self.addr)
        srv.listen(4)
        srv.settimeout(0.2)
        self._srv = srv
        threading.Thread(target=self._accept, name="board-load-sink", daemon=True).start()
        return srv.getsockname()[1]

    def _accept(self) -> None:
        assert self._srv is not None
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._drain, args=(c,), daemon=True).start()

    def _drain(self, c: socket.socket) -> None:
        c.settimeout(0.5)
        with c:
            while not self._stop.is_set():
                try:
                    b = c.recv(256 * 1024)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not b:
                    return
                self.received += len(b)

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            self._srv.close()


# ------------------------------------------------------------ 子进程负载

def replay_cmd(loc: str, bag: Path, prior: Path, init: str, out: Path) -> list[str]:
    return [loc, "replay", "--bag", str(bag), "--prior", str(prior), "--init", init,
            "--out", str(out)]


def video_cmd(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
            "testsrc2=size=1280x720:rate=15", "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-g", "30", "-f", "null", "-"]


CPU_BURN = [sys.executable, "-c", "while True:\n    pass"]


class Repeat:
    """一直跑一个命令:退出了(录包回放完了)再起。停的时候按记下的进程号先 TERM 再 KILL。"""

    def __init__(self, name: str, cmd: list[str]) -> None:
        self.name, self.cmd = name, cmd
        self.proc: subprocess.Popen[bytes] | None = None
        self.runs = 0

    def step(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
            self.runs += 1

    def stop(self) -> None:
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)             # 它自己起的子进程(MOLA)一起
            p.wait(STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(STOP_WAIT_S)
        except ProcessLookupError:
            pass


class Duty:
    """灯、警笛按占空比开关(``{路: (开, 周期)}``)。"""

    def __init__(self, payload: Any, plan: dict[str, tuple[float, float]]) -> None:
        self.p = payload
        self.plan = {k: v for k, v in plan.items() if v[0] > 0 and payload.has(k)}
        self._on: dict[str, bool] = dict.fromkeys(self.plan, False)
        self.error = ""

    async def step(self, t: float) -> None:
        for out, (on_s, period) in self.plan.items():
            want = duty_on(t, on_s, period)
            if want == self._on[out]:
                continue
            try:
                await self.p.set(out, want, on_s + 5.0)
                self._on[out] = want
            except Exception as exc:                     # noqa: BLE001 - 下一秒再试
                self.error = f"{out}: {exc}"

    async def off(self) -> None:
        for out in self.plan:
            try:
                await self.p.set(out, False, 0)
            except Exception as exc:                     # noqa: BLE001
                self.error = f"{out}: {exc}"


# ------------------------------------------------------------ 主程序

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="d1max-board-load",
        description="上装主控板满载(散热测试 C2):复现巡逻时的最大负载,每 10 秒记温度、频率、风扇。")
    p.add_argument("--sink", type=int, metavar="PORT",
                   help="只当收上传的那头(笔记本、站点上跑):收了就丢")
    p.add_argument("--minutes", type=float, default=150.0, help="跑多久(分钟,缺省 150)")
    p.add_argument("--replay-bag", type=Path, help="录包:回放定位器(MOLA 真算),跑完再跑")
    p.add_argument("--prior", type=Path, help="回放用的先验图目录(配 --replay-bag)")
    p.add_argument("--init", default="0,0,0", help="回放的初始位姿 x,y,yaw(缺省 0,0,0)")
    p.add_argument("--cpu", type=int, default=0, help="N 个满载进程(没有录包时顶建图那份)")
    p.add_argument("--video", type=int, default=0, help="N 路 720p15 软件编码")
    p.add_argument("--write-dir", type=Path, help="录像写盘的目录(放 NVMe 上)")
    p.add_argument("--write-mbps", type=float, default=8.0, help="写盘码率(Mbit/s,缺省 8)")
    p.add_argument("--write-keep-gb", type=float, default=4.0, help="写盘最多留多少(GB,缺省 4)")
    p.add_argument("--upload", metavar="HOST:PORT", help="上传:往对面灌 TCP(对面跑 --sink)")
    p.add_argument("--upload-mbps", type=float, default=4.0, help="上传码率(Mbit/s,缺省 4)")
    p.add_argument("--payload", type=Path, help="上装配置(/etc/d1max/payload.json):灯、警笛按占空比"
                                                "开关。要先停代理")
    p.add_argument("--strobe", default="30/300", help="警灯占空比(开几秒/每几秒,0 = 不开)")
    p.add_argument("--spotlight", default="60/300", help="聚光灯占空比")
    p.add_argument("--siren", default="0", help="警笛占空比(很响,缺省不开)")
    p.add_argument("--board", type=Path, default=Path("/etc/d1max/board.json"),
                   help="风扇测速配置(没有就只记温度、频率)")
    p.add_argument("--log-dir", type=Path, help="温度日志目录(缺省同代理:/var/lib/d1max/board)")
    p.add_argument("--period", type=float, default=10.0, help="多少秒记一次(缺省 10)")
    p.add_argument("--status-every", type=float, default=60.0, help="多少秒打一行状态(缺省 60)")
    p.add_argument("--ambient", type=float, help="室温(°C):按温升换算到 40 °C 环境")
    p.add_argument("--stop-when-stable", action="store_true", help="温度稳定了就停")
    return p


def _monitor(a: argparse.Namespace) -> Any:
    from dataclasses import replace

    from d1max_agent.boardhealth import BoardConfig, BoardMonitor
    cfg = BoardConfig.load(a.board) if a.board.is_file() else BoardConfig()
    cfg = replace(cfg, period_s=a.period, **({"log_dir": str(a.log_dir)} if a.log_dir else {}))
    m = BoardMonitor(cfg, emit=lambda kind, d: print(f"[{kind}] {d}", flush=True))
    m.start()
    return m


def _status(t: float, m: Any, loads: list[str]) -> str:
    soc = max(m.soc.values()) if m.soc else None
    fans = ",".join(f"{k}={'?' if v is None else v}" for k, v in m.rpm.items())
    freqs = ",".join(f"{k}={v}" for k, v in sorted(m.freqs.items()))
    return (f"[{t / 60:6.1f} min] SoC {'?' if soc is None else f'{soc:.1f}'} °C"
            f" NVMe {'?' if m.nvme is None else f'{m.nvme:.0f}'} °C 级别 {m.level}"
            f" 风扇 {fans or '-'} 频率 {freqs or '-'}"
            f"{' 降频 ' + ','.join(m.throttle) if m.throttle else ''} | {' '.join(loads)}")


async def _run(a: argparse.Namespace) -> int:
    m = _monitor(a)
    stab = Stability()
    reps: list[Repeat] = [Repeat(f"cpu{i}", CPU_BURN) for i in range(a.cpu)]
    tmp: tempfile.TemporaryDirectory[str] | None = None
    if a.replay_bag:
        loc = shutil.which("d1max-loc") or "d1max-loc"
        tmp = tempfile.TemporaryDirectory(prefix="d1max-board-load-")
        reps.append(Repeat("replay", replay_cmd(loc, a.replay_bag, a.prior, a.init,
                                                Path(tmp.name) / "out")))
    if a.video:
        ff = shutil.which("ffmpeg")
        if ff is None:
            print("没有 ffmpeg:--video 做不了", file=sys.stderr)
            return 2
        reps += [Repeat(f"video{i}", video_cmd(ff)) for i in range(a.video)]
    writer = DiskWriter(a.write_dir, mbps=a.write_mbps, keep_mb=a.write_keep_gb * 1024) \
        if a.write_dir else None
    sender = None
    if a.upload:
        host, _, port = a.upload.rpartition(":")
        sender = Sender(host, int(port), mbps=a.upload_mbps)
    duty = payload = None
    if a.payload:
        from d1max_adapter_d1max.payload import Payload, PayloadConfig
        payload = Payload(PayloadConfig.load(a.payload))
        await payload.start()
        duty = Duty(payload, {"strobe": parse_duty(a.strobe),
                              "spotlight": parse_duty(a.spotlight), "siren": parse_duty(a.siren)})
    loads = [r.name for r in reps] + (["写盘"] if writer else []) + (["上传"] if sender else []) \
        + ([f"灯/警笛({','.join(duty.plan)})"] if duty else [])
    print(f"开始满载:{' '.join(loads)};跑 {a.minutes:g} 分钟,日志 {m.cfg.log_dir}", flush=True)
    peak = None
    throttled: set[str] = set()
    fans_stopped: set[str] = set()
    t0 = time.monotonic()
    next_sample = next_status = 0.0
    told_stable = False
    halt = asyncio.Event()                     # Ctrl+C、systemctl stop:收尾、照样出总结
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, halt.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    try:
        if writer:
            writer.start()
        if sender:
            sender.start()
        while (t := time.monotonic() - t0) < a.minutes * 60 and not halt.is_set():
            for r in reps:
                r.step()
            if duty:
                await duty.step(t)
            if t >= next_sample:
                next_sample = t + a.period
                await asyncio.to_thread(m.sample)
                if m.soc:
                    soc = max(m.soc.values())
                    peak = soc if peak is None else max(peak, soc)
                    stab.feed(t, soc)
                throttled |= set(m.throttle)
                fans_stopped |= set(m.stopped())
            if t >= next_status:
                next_status = t + a.status_every
                extra = [f"写 {writer.written / 1e9:.2f} GB"] if writer else []
                if sender:
                    err = f" {sender.error}" if sender.error else ""
                    extra.append(f"传 {sender.sent / 1e9:.2f} GB{err}")
                print(_status(t, m, loads + extra), flush=True)
            if not told_stable and stab.stable(t):
                told_stable = True
                print(f"[{t / 60:.1f} min] 温度稳定了(最近 30 分钟变化不到 1 °C)", flush=True)
                if a.stop_when_stable:
                    break
            await asyncio.sleep(0.5)
    finally:
        for r in reps:
            r.stop()
        if writer:
            writer.stop()
        if sender:
            sender.stop()
        if duty:
            await duty.off()
        if payload is not None:
            await payload.close()
        m.close()
        if tmp is not None:
            tmp.cleanup()
    last = max(m.soc.values()) if m.soc else None
    print("——— 总结 ———")
    hi = "?" if peak is None else f"{peak:.1f}"
    lo = "?" if last is None else f"{last:.1f}"
    calm = "稳定了" if told_stable else "还没稳定(要跑满 2 小时、最近 30 分钟变化 < 1 °C)"
    print(f"跑了 {(time.monotonic() - t0) / 60:.1f} 分钟;SoC 最高 {hi} °C,最后 {lo} °C;温度{calm}")
    if a.ambient is not None and peak is not None:
        hot = project(peak, a.ambient)
        print(f"室温 {a.ambient:g} °C → 换算到 40 °C 环境:SoC 最高约 {hot:.1f} °C")
    slow = "、".join(sorted(throttled)) or "没有"
    stop = "、".join(sorted(fans_stopped)) or "没有"
    print(f"降过频:{slow};风扇停过:{stop}")
    if writer and writer.error:
        print(f"写盘出错:{writer.error}")
    if duty and duty.error:
        print(f"灯 / 警笛出错:{duty.error}")
    print(f"温度日志:{m.cfg.log_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.sink is not None:
        sink = Sink(a.sink)
        port = sink.start()
        print(f"收上传:在 {port} 端口收,收了就丢(Ctrl+C 停)", flush=True)
        try:
            while True:
                time.sleep(10)
                print(f"已收 {sink.received / 1e9:.2f} GB", flush=True)
        except KeyboardInterrupt:
            sink.stop()
        return 0
    if a.replay_bag and not a.prior:
        build_parser().error("--replay-bag 要配 --prior(回放用的先验图)")
    if not (a.replay_bag or a.cpu or a.video or a.write_dir or a.upload or a.payload):
        build_parser().error("一样负载都没开:至少给 --replay-bag / --cpu / --video / --write-dir / "
                             "--upload / --payload 里的一样")
    for k in ("strobe", "spotlight", "siren"):
        try:
            parse_duty(getattr(a, k))
        except ValueError as exc:
            build_parser().error(f"--{k}:{exc}")
    try:
        return asyncio.run(_run(a))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
