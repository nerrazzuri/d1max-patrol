"""外部进程的起、停、就绪判据与日志。

被管的进程一律是 ``sys.executable -c "..."`` —— 这一层不认识 ROS,拿
Python 当替身测得到全部行为,而且在 Windows 开发机上也跑得起来。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from d1max_patrol.app.procs import ProcError, ProcManager, ProcSpec


@pytest.fixture
async def mgr(tmp_path):
    manager = ProcManager(tmp_path / "logs")
    yield manager
    await manager.stop_all()


def _py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def _until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


# ------------------------------------------------------------------ 起


async def test_起一个进程它就在running里(mgr):
    await mgr.start(ProcSpec("sleeper", _py("import time;time.sleep(30)")))
    assert mgr.running() == ["sleeper"]


async def test_日志落到文件里(mgr):
    await mgr.start(ProcSpec("hi", _py("print('你好')")))
    await mgr.wait("hi", timeout_s=5)
    assert "你好" in mgr.log_path("hi").read_text(encoding="utf-8")


async def test_stderr也进同一份日志(mgr):
    """ROS 的东西一半信息在 stderr 上,分开落等于每次都要看两个文件。"""
    await mgr.start(ProcSpec("err", _py(
        "import sys;print('出事了', file=sys.stderr)")))
    await mgr.wait("err", timeout_s=5)
    assert "出事了" in mgr.log_path("err").read_text(encoding="utf-8")


async def test_环境变量是叠加不是替换(mgr):
    """建图那几个进程要单独的 ROS_DOMAIN_ID,但 PATH 之类还得留着。"""
    spec = ProcSpec("e", _py(
        "import os;print(os.environ['ROS_DOMAIN_ID'], bool(os.environ.get('PATH')))"),
        env={"ROS_DOMAIN_ID": "93"})
    await mgr.start(spec)
    await mgr.wait("e", timeout_s=5)
    assert "93 True" in mgr.log_path("e").read_text(encoding="utf-8")


async def test_没写环境变量的进程也拿得到PATH(mgr):
    await mgr.start(ProcSpec("p", _py("import os;print(bool(os.environ.get('PATH')))")))
    await mgr.wait("p", timeout_s=5)
    assert "True" in mgr.log_path("p").read_text(encoding="utf-8")


async def test_起不来的命令报得出是哪个(mgr):
    with pytest.raises(ProcError, match="没这个命令"):
        await mgr.start(ProcSpec("没这个命令", ("d1max-绝对不存在的可执行文件",)))
    assert mgr.running() == []


async def test_工作目录说了算(mgr, tmp_path):
    here = tmp_path / "工地"
    here.mkdir()
    await mgr.start(ProcSpec("cwd", _py("import os;print(os.getcwd())"), cwd=here))
    await mgr.wait("cwd", timeout_s=5)
    assert "工地" in mgr.log_path("cwd").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 就绪


async def test_就绪判据等到日志里那句话才返回(mgr):
    code = "import time;time.sleep(0.4);print('READY');time.sleep(30)"
    await mgr.start(ProcSpec("late", _py(code), ready_pattern="READY",
                             ready_timeout_s=10))
    assert "READY" in mgr.log_path("late").read_text(encoding="utf-8")


async def test_没写就绪判据的起了就算(mgr):
    await mgr.start(ProcSpec("q", _py("import time;time.sleep(30)")))
    assert mgr.running() == ["q"]


async def test_等不到就绪就抛并且把进程收掉(mgr):
    with pytest.raises(ProcError, match="就绪"):
        await mgr.start(ProcSpec("slow", _py("import time;time.sleep(30)"),
                                 ready_pattern="never", ready_timeout_s=0.5))
    assert mgr.running() == [], "起不来的进程不许留在那儿"


async def test_还没就绪就退了会报出退出码(mgr):
    """比"等了 30 秒"有用得多 —— 进程早就死了,不该干等到超时。"""
    with pytest.raises(ProcError, match="returncode"):
        await mgr.start(ProcSpec("dead", _py("raise SystemExit(3)"),
                                 ready_pattern="never", ready_timeout_s=10))


async def test_就绪判据是正则(mgr):
    code = "print('bridge listening on 8092');import time;time.sleep(30)"
    await mgr.start(ProcSpec("re", _py(code),
                             ready_pattern=r"listening on \d+", ready_timeout_s=10))
    assert mgr.running() == ["re"]


# ------------------------------------------------------------------ 重名


async def test_重名的进程被拒(mgr):
    await mgr.start(ProcSpec("dup", _py("import time;time.sleep(30)")))
    with pytest.raises(ProcError, match="已经在跑"):
        await mgr.start(ProcSpec("dup", _py("import time;time.sleep(30)")))


async def test_退掉之后同名可以再起(mgr):
    await mgr.start(ProcSpec("again", _py("pass")))
    await mgr.wait("again", timeout_s=5)
    await mgr.start(ProcSpec("again", _py("import time;time.sleep(30)")))
    assert mgr.running() == ["again"]


# ------------------------------------------------------------------ 停


async def test_stop会等它真的死(mgr):
    await mgr.start(ProcSpec("s", _py("import time;time.sleep(30)")))
    await mgr.stop("s")
    assert mgr.running() == []


async def test_赖着不走的会被强杀(mgr):
    """SIGTERM 装死的进程 —— ros2 launch 偶尔就是这样。"""
    code = ("import signal,time;signal.signal(signal.SIGTERM, lambda *a: None);"
            "print('ARMED');time.sleep(60)")
    await mgr.start(ProcSpec("stub", _py(code), ready_pattern="ARMED",
                             ready_timeout_s=10))
    await mgr.stop("stub", term_grace_s=0.3)
    assert mgr.running() == []


async def test_停一个不认识的名字不会炸(mgr):
    await mgr.stop("从来没起过")


async def test_stop_all_把所有都收掉(mgr):
    for name in ("a", "b", "c"):
        await mgr.start(ProcSpec(name, _py("import time;time.sleep(30)")))
    await mgr.stop_all()
    assert mgr.running() == []


async def test_stop_all_一个停不掉不影响别的(mgr, monkeypatch):
    """收尾路径上不能半途而废 —— 剩下的进程会一直占着端口。"""
    await mgr.start(ProcSpec("x", _py("import time;time.sleep(30)")))
    await mgr.start(ProcSpec("y", _py("import time;time.sleep(30)")))
    real = ProcManager._kill
    calls = {"n": 0}

    async def flaky(self, entry, *, term_grace_s):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("假装第一个杀不动")
        await real(self, entry, term_grace_s=term_grace_s)

    monkeypatch.setattr(ProcManager, "_kill", flaky)
    await mgr.stop_all()
    assert calls["n"] == 2, "第一个抛了,第二个还得试"


@pytest.mark.skipif(sys.platform == "win32", reason="进程组是 POSIX 概念")
async def test_杀的是整个进程组(mgr):
    """ros2 launch 会派生一堆孙子;只杀父的会留下孤儿占着话题。"""
    code = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print('CHILD', c.pid, flush=True);"
        "time.sleep(60)"
    )
    await mgr.start(ProcSpec("tree", _py(code), ready_pattern=r"CHILD \d+",
                             ready_timeout_s=10))
    text = mgr.log_path("tree").read_text(encoding="utf-8")
    child_pid = int(text.split("CHILD", 1)[1].split()[0])
    assert _pid_alive(child_pid)
    await mgr.stop("tree")
    assert await _until(lambda: not _pid_alive(child_pid))


@pytest.mark.skipif(sys.platform == "win32", reason="进程组是 POSIX 概念")
async def test_先好好说再动手(mgr):
    """能听懂 SIGTERM 的进程应该体面地退,而不是一上来就 SIGKILL。"""
    code = ("import signal,sys,time\n"
            "def bye(*a):\n"
            "    print('BYE', flush=True); sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, bye)\n"
            "print('ARMED', flush=True)\n"
            "time.sleep(60)\n")
    await mgr.start(ProcSpec("polite", _py(code), ready_pattern="ARMED",
                             ready_timeout_s=10))
    await mgr.stop("polite", term_grace_s=3.0)
    assert "BYE" in mgr.log_path("polite").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 等与看


async def test_等一个不认识的名字会说清楚(mgr):
    with pytest.raises(ProcError, match="没有叫"):
        await mgr.wait("从来没起过")


async def test_等超时了抛而不是永远挂着(mgr):
    await mgr.start(ProcSpec("w", _py("import time;time.sleep(30)")))
    with pytest.raises(ProcError, match="超时"):
        await mgr.wait("w", timeout_s=0.3)


async def test_wait给的是退出码(mgr):
    await mgr.start(ProcSpec("rc", _py("raise SystemExit(7)")))
    assert await mgr.wait("rc", timeout_s=5) == 7


async def test_进程自己退出后running里就没它了(mgr):
    await mgr.start(ProcSpec("short", _py("pass")))
    await mgr.wait("short", timeout_s=5)
    assert mgr.running() == []


async def test_tail_能读到还在写的日志(mgr):
    code = ("import time\n"
            "for i in range(3):\n"
            "    print('行', i, flush=True); time.sleep(0.1)\n")
    await mgr.start(ProcSpec("t", _py(code)))
    got = []
    async for line in mgr.tail("t"):
        got.append(line)
        if len(got) == 3:
            break
    assert [g.strip() for g in got] == ["行 0", "行 1", "行 2"]


async def test_tail_在进程退了之后收尾(mgr):
    """最后几行往往正是死因,不能因为进程没了就吞掉。"""
    await mgr.start(ProcSpec("last", _py("print('最后一句')")))
    await mgr.wait("last", timeout_s=5)
    got = [line async for line in mgr.tail("last")]
    assert any("最后一句" in line for line in got)


async def test_日志目录不存在会自己建(tmp_path):
    ProcManager(tmp_path / "还没有" / "这一层")
    assert (tmp_path / "还没有" / "这一层").is_dir()


async def test_进程管理不认识ROS也不认识建图():
    """这一层只认 argv 和一个正则 —— 建图的编排在 app/mapping.py。

    查的是 import 和代码里的引用,不是散文:模块的文档字符串里当然要提
    slam_toolbox,不然读的人不知道这些设计是为谁做的。
    """
    import ast
    import inspect

    from d1max_patrol.app import procs

    tree = ast.parse(inspect.getsource(procs))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith(("d1max_patrol.", "d1max_sim", "rclpy"))
                   for name in imported), f"进程管理不该依赖 {imported}"

    code = "\n".join(line for line in inspect.getsource(procs).splitlines()
                     if not line.lstrip().startswith(("#", "#:")))
    for banned in ("slam_toolbox", "ros2 launch"):
        assert f'"{banned}' not in code and f"'{banned}" not in code, \
            f"进程管理里不该把 {banned} 写死进代码"
