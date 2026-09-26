"""MOLA 子进程看管(W09b 决定 2):节点自己管 MOLA,不另起 systemd 单元、不要 root。

- :meth:`MolaSupervisor.start` 带先验起(已经在跑就先关再起 —— 换先验 = 重启,顺带清掉 MOLA 的内部
  状态);
- 它自己退了(一帧坏点云就能让它停摆、载图失败也会退):``on_down`` 说原因,按 :data:`BACKOFF_S` 退避
  重启;跑满 :data:`STABLE_S` 退避从头算;
- :meth:`restart`:核心判它卡住了(点云在来、不出位姿)时关掉重起;
- 关的时候连子孙一起关(``ros2 launch`` 底下还有节点进程):新会话起,关时对整个进程组先 SIGINT,
  :data:`KILL_AFTER_S` 还在就 SIGKILL。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
STABLE_S = 60.0
KILL_AFTER_S = 5.0


def mola_command(prior_mm: Path, *, lidar_topic: str = "/front_lidar") -> list[str]:
    """MOLA 只定位的启动命令(参数 2026-09-27 在录包上用 ROS 跑通核过)。初值不在这里给:起来之后经
    ``/relocalize_near_pose`` 给。"""
    return ["ros2", "launch", "mola_lidar_odometry", "ros2-lidar-odometry.launch.py",
            f"lidar_topic_name:={lidar_topic}", "use_rviz:=False", "use_mola_gui:=False",
            "start_mapping_enabled:=False", f"mola_initial_map_mm_file:={prior_mm}",
            "ignore_lidar_pose_from_tf:=True",
            # 直接发 map → base_link;默认按 REP105 要 odom → base_link,狗上没有就一直刷报错(实跑见)
            "publish_localization_following_rep105:=False"]


class MolaSupervisor:
    """起、停、重启、到点重起都在一把锁里串着做(W09b 内审:原来换图时取消了正在关旧进程的起动任务,
    旧进程离了手成了孤儿;收尾时还可能又起一个);进程确认死了才放手。"""

    def __init__(self, *, command_for: Callable[[Path], Sequence[str]], on_up: Callable[[], None],
                 on_down: Callable[[str], None], backoff: Sequence[float] = BACKOFF_S,
                 env: dict[str, str] | None = None) -> None:
        self._command_for = command_for
        self._on_up, self._on_down = on_up, on_down
        self._backoff = tuple(backoff)
        self._env = env
        self._prior: Path | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._watch: asyncio.Task | None = None
        self._pending: asyncio.Task | None = None
        self._fails = 0
        self._stopping = False
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def prior(self) -> Path | None:
        return self._prior

    async def start(self, prior: Path) -> None:
        """带这个先验(重)起;已经在跑就先关干净再起。"""
        async with self._lock:
            self._stopping = False
            self._prior = prior
            self._fails = 0
            self._cancel_pending()
            await self._kill()
            await self._spawn()

    async def restart(self, why: str) -> None:
        """关掉、按退避重起同一个先验。已经在等重起就不再排(去重)。"""
        async with self._lock:
            if self._prior is None or self._stopping:
                return
            if self._pending is not None and not self._pending.done():
                return
            await self._kill()
            self._schedule(why)

    async def stop(self) -> None:
        self._stopping = True
        self._cancel_pending()
        async with self._lock:
            await self._kill()

    # ------------------------------------------------------------ 内部(持锁)

    async def _spawn(self) -> None:
        assert self._prior is not None
        cmd = list(self._command_for(self._prior))
        log.info("起 MOLA:%s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=self._env, start_new_session=True, stdin=asyncio.subprocess.DEVNULL)
        except OSError as exc:                            # 命令不在、没权限:说出来、过一会儿再试
            self._schedule(f"定位程序起不来({exc})")
            return
        self._proc = proc
        self._on_up()
        loop = asyncio.get_running_loop()
        self._watch = loop.create_task(self._wait(proc, loop.time()))

    async def _wait(self, proc: asyncio.subprocess.Process, started: float) -> None:
        rc = await proc.wait()
        _signal_group(proc.pid, signal.SIGKILL)           # 它自己退了:组里剩下的(launch 底下的)收掉
        async with self._lock:
            if self._stopping or proc is not self._proc:
                return
            self._proc = None
            if asyncio.get_running_loop().time() - started >= STABLE_S:
                self._fails = 0
            self._schedule(f"定位程序退出了(退出码 {rc})")

    def _schedule(self, why: str) -> None:
        delay = self._backoff[min(self._fails, len(self._backoff) - 1)]
        self._fails += 1
        self._on_down(f"{why},{delay:g} 秒后重启")
        log.warning("%s;%g 秒后重启 MOLA", why, delay)
        self._cancel_pending()
        self._pending = asyncio.get_running_loop().create_task(self._later(delay))

    async def _later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._lock:
            if self._stopping or self.running:
                return
            self._pending = None
            await self._spawn()

    def _cancel_pending(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None

    async def _kill(self) -> None:
        proc = self._proc
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None
        if proc is None:
            return
        if proc.returncode is None:
            _signal_group(proc.pid, signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), KILL_AFTER_S)
            except asyncio.TimeoutError:
                log.warning("MOLA %s 秒没关掉,强杀", KILL_AFTER_S)
                _signal_group(proc.pid, signal.SIGKILL)
                await proc.wait()
        # 进程组里还剩的(launch 底下的节点)一并收掉
        _signal_group(proc.pid, signal.SIGKILL)
        self._proc = None                                 # 确认死了才放手


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)
