"""起一组外部进程,并且保证它们真的死得掉。

建图这条路要拉起一串东西:``ros2 launch`` 的雷达驱动、slam_toolbox、两条
桥脚本。它们的共同点是**都不是本仓库的 Python** —— 跑在 ROS 的系统解释器
下,有自己的环境变量,还会派生一堆孙子进程。

三件事是这个模块存在的理由:

1. **就绪判据。** "进程起来了"和"它能干活了"差着好几秒。slam_toolbox 要
   等雷达出点云,桥要等端口 bind 上。没有就绪判据,下一步就会对着一个还
   没准备好的东西下指令,失败得莫名其妙。
2. **按组杀。** ``ros2 launch`` 派生的孙子进程会占着话题和端口;只杀父的
   话它们变成孤儿留在那儿,下一次建图就起不来了,而且现场根本看不出为什么。
3. **日志落盘。** 出事之后要能回头看,``stderr`` 混进 ``stdout`` 一起落 ——
   ROS 的东西一半信息在 stderr 上。

**这里不认识建图,也不认识 ROS。** 它只认 argv 和一个正则。建图的编排在
``app/mapping.py``。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: 轮询日志找就绪串的间隔。够快到人感觉不出来,又不至于把盘读糊。
_POLL_S = 0.05

#: 输出重定向到文件时 Python 是**块缓冲**的,一行"我起来了"会卡在缓冲区里
#: 直到进程退出 —— 就绪判据于是永远等不到,而进程明明已经就绪了。
#: ``ros2 launch`` 自己也是 Python,所以这一条同样管用。
#: 放在 ``os.environ`` 之后、``spec.env`` 之前:默认打开,但调用方要关得掉。
#:
#: ``PYTHONIOENCODING`` 是同一类问题的另一半:输出重定向到文件时,子进程按
#: **本机 locale** 编码写(Windows 上是 GBK),于是日志按 UTF-8 读就是乱码。
#: 钉成 UTF-8,日志在哪台机器上写的都读得出来。
#: 非 Python 的子进程(ROS 的 C++ 节点)不吃这一套,所以读日志那边仍然是
#: 容错解码 —— 这条只保证"我们起的 Python 进程"这一半干净。
_UNBUFFERED = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


class ProcError(RuntimeError):
    """进程起不来、等不到就绪,或者名字撞了。"""


@dataclass(frozen=True, slots=True)
class ProcSpec:
    """一个待起进程的完整声明。

    ``env`` 是**叠加**在 ``os.environ`` 上的,不是替换 —— 建图那几个进程
    要单独的 ``ROS_DOMAIN_ID``,但 ``PATH`` / ``LD_LIBRARY_PATH`` 之类还
    得留着,替换掉的话 ROS 自己就找不到了。
    """

    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    #: 日志里出现它就算起来了(正则)。空串表示"进程起了就算"。
    ready_pattern: str = ""
    ready_timeout_s: float = 30.0
    cwd: Path | None = None


@dataclass
class _Proc:
    spec: ProcSpec
    proc: asyncio.subprocess.Process
    log: Path
    handle: object          # 打开的日志文件句柄,收尾时关
    started_at: float


class ProcManager:
    """管一组外部进程。名字是 key,日志按名字落在 ``log_dir`` 下。"""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, _Proc] = {}

    # ---------------------------------------------------------------- 查询

    def running(self) -> list[str]:
        """还活着的进程名。自己退掉的会在这里被摘掉。"""
        self._reap()
        return sorted(self._procs)

    @property
    def log_dir(self) -> Path:
        """日志都在这儿(W00c6g:站点经代理看它们的尾巴)。"""
        return self._log_dir

    def log_path(self, name: str) -> Path:
        """这个名字的日志在哪。进程已经退了也还查得到 —— 出事之后才要看。"""
        return self._log_dir / f"{name}.log"

    def returncode(self, name: str) -> int | None:
        entry = self._procs.get(name)
        return entry.proc.returncode if entry else None

    def _reap(self) -> None:
        for name in [n for n, e in self._procs.items()
                     if e.proc.returncode is not None]:
            self._close(self._procs.pop(name))

    @staticmethod
    def _close(entry: _Proc) -> None:
        with contextlib.suppress(Exception):
            entry.handle.close()  # type: ignore[attr-defined]

    # ---------------------------------------------------------------- 起

    async def start(self, spec: ProcSpec) -> None:
        """起一个进程,等到它就绪才返回。

        起不来或等不到就绪一律抛 ``ProcError``,并且**把进程收掉** ——
        一个半死不活的 slam_toolbox 留在那儿占着话题,比它没起来更难查。
        """
        self._reap()
        if spec.name in self._procs:
            raise ProcError(f"进程 {spec.name!r} 已经在跑了")

        log = self.log_path(spec.name)
        handle = log.open("wb")
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **_UNBUFFERED, **spec.env},
                cwd=str(spec.cwd) if spec.cwd else None,
                # 单独开一个会话,这样杀的时候能按组杀。ros2 launch 会派生
                # 一堆孙子进程,只杀父的话它们会留下来占着话题和端口,下一次
                # 建图就起不来了。
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            handle.close()
            raise ProcError(f"起不来 {spec.name!r}: {exc}") from exc

        entry = _Proc(spec=spec, proc=proc, log=log, handle=handle,
                      started_at=time.monotonic())
        self._procs[spec.name] = entry
        if not spec.ready_pattern:
            return
        try:
            await self._await_ready(entry)
        except BaseException:
            # 等就绪失败(包括被取消)都要把它收掉,不留半死不活的东西。
            self._procs.pop(spec.name, None)
            await self._kill(entry, term_grace_s=1.0)
            raise

    async def _await_ready(self, entry: _Proc) -> None:
        pattern = re.compile(entry.spec.ready_pattern)
        deadline = time.monotonic() + entry.spec.ready_timeout_s
        while True:
            text = _read_text(entry.log)
            if pattern.search(text):
                return
            if entry.proc.returncode is not None:
                raise ProcError(
                    f"{entry.spec.name!r} 还没就绪就退了(returncode="
                    f"{entry.proc.returncode}),日志见 {entry.log}")
            if time.monotonic() >= deadline:
                raise ProcError(
                    f"等 {entry.spec.name!r} 就绪超时("
                    f"{entry.spec.ready_timeout_s:g}s 没等到 "
                    f"{entry.spec.ready_pattern!r}),日志见 {entry.log}")
            await asyncio.sleep(_POLL_S)

    # ---------------------------------------------------------------- 停

    async def stop(self, name: str, *, term_grace_s: float = 3.0) -> None:
        """停一个。先好好说,超过宽限期就强杀。不认识的名字当无事发生。"""
        entry = self._procs.pop(name, None)
        if entry is None:
            return
        await self._kill(entry, term_grace_s=term_grace_s)

    async def stop_all(self) -> None:
        """全停。一个停不掉不影响停下一个 —— 收尾路径上不能半途而废。"""
        for name in list(self._procs):
            with contextlib.suppress(Exception):
                await self.stop(name)

    async def _kill(self, entry: _Proc, *, term_grace_s: float) -> None:
        proc = entry.proc
        if proc.returncode is None:
            self._signal(proc, signal.SIGTERM)
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), term_grace_s)
        if proc.returncode is None:
            # 装死的进程 —— ros2 launch 偶尔就是这样。不强杀就永远等下去。
            self._signal(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), term_grace_s)
        self._close(entry)

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
        """按组发信号。

        Windows 上没有进程组这回事,退化成只杀父进程 —— **这条差异只影响
        开发机**:建图那串 ROS 进程只在 Ubuntu 上跑,而开发机上被管的都是
        单个 Python 进程,没有孙子要收。
        """
        if os.name == "nt":
            with contextlib.suppress(ProcessLookupError, OSError):
                if sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(proc.pid), sig)

    # ---------------------------------------------------------------- 等

    async def wait(self, name: str, timeout_s: float | None = None) -> int:
        """等它自己退,返回 returncode。不认识的名字抛 ``ProcError``。"""
        entry = self._procs.get(name)
        if entry is None:
            raise ProcError(f"没有叫 {name!r} 的进程")
        if timeout_s is None:
            code = await entry.proc.wait()
        else:
            try:
                code = await asyncio.wait_for(entry.proc.wait(), timeout_s)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise ProcError(f"等 {name!r} 退出超时") from exc
        self._reap()
        return code

    # ---------------------------------------------------------------- 看

    async def tail(self, name: str) -> AsyncIterator[str]:
        """跟着日志一行行往外吐,进程还在写就一直等。

        进程退了之后把剩下的读完就结束 —— 最后几行往往正是死因,不能因为
        进程没了就吞掉。
        """
        path = self.log_path(name)
        offset = 0
        while True:
            text = _read_text(path)
            if len(text) > offset:
                chunk, offset = text[offset:], len(text)
                lines = chunk.split("\n")
                tail_part = lines.pop()
                for line in lines:
                    yield line
                offset -= len(tail_part)
            entry = self._procs.get(name)
            if entry is None or entry.proc.returncode is not None:
                if len(_read_text(path)) <= offset:
                    return
            await asyncio.sleep(_POLL_S)


def _read_text(path: Path) -> str:
    """读日志。用 replace 而不是抛 —— ROS 的输出里混二进制不算稀奇。"""
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


__all__ = ["ProcError", "ProcManager", "ProcSpec"]
