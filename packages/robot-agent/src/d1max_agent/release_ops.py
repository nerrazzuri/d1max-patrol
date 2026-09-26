"""发布经站点(W00c5d 第三部分)。站点下命令,狗在这儿做:下载、核对、落槽、建 venv、切版本、退回。

**切换与退回全用现成的 ``engine.release``**(双槽、先落在途标记再换链、开机守卫数够次数就退):
这里只是把「包从哪来」换成站点的狗专用口(mTLS),把「谁来点」换成站点的命令。

- ``install``:下载 tar.gz(**边下边算 sha256,大小对不上就停**)→ 安全解开(不许绝对路径、``..``、
  链接、设备文件)→ ``stage``(先核包内指纹,再落槽,不切)→ 建这一版自己的 venv(跟
  ``deploy/install.sh`` 4/7 一个做法:从包的临时副本装,哨兵最后才落;包里带 ``wheels/`` 就只从它装)。
- ``activate``:要空闲(调用方查);写在途标记、换链、**自己好好退出** —— 代理单元是
  ``Restart=always``,systemd 5 秒后从新的 ``current`` 起来。**不装单元**:单元只指着这一版带的
  启动脚本 ``deploy/d1max-agent-start``,启动参数归版本管,退回上一版时参数跟着代码一起回来
  (W00c5d 第三部分内部评审 B1)。**重启后连上站点才提交**(:meth:`commit_if_pending`);新版起不来,
  开机守卫(单元的 ``ExecStartPre``)数够次数就退回上一版,并留条子(:meth:`take_guard_note`)。
- ``rollback``:有在途的那一次升级就退它;已经提交了就切回盘上**上一版**(比在跑的那版旧的、
  装好了的最新一版),同样写在途标记、重启、连上才提交。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from d1max_agent.engine import release as rel
from d1max_contract.releases import ReleaseRef

log = logging.getLogger(__name__)

#: 下载:给版本名,返回一块一块的字节(调用方在线程里跑它)。
Fetch = Callable[[str], Iterable[bytes]]
#: 建 venv:``(包的一份临时副本, 槽目录)``。
BuildVenv = Callable[[Path, Path], None]
#: 这一版的依赖装齐了的哨兵(跟 install.sh 同一个)。
SENTINEL = ".deps-ok"
#: 装一版要的盘:下载的 tar.gz、解开的一份、落槽的一份、venv,按包大小的几倍估,再加一段余量。
INSTALL_DISK_FACTOR = 4
DISK_MARGIN_BYTES = 512 * 1024 ** 2
#: 包里带的离线轮子(``release pack --wheels``):有就只从它装(现场的狗上不了网)。
WHEELS_DIR = "wheels"


class ReleaseOpError(RuntimeError):
    """这一步做不了。消息给站点看。"""


def safe_extract(tar: Path, dest: Path) -> None:
    """解开发布包。**只许普通文件和目录**,路径必须落在 ``dest`` 底下 —— 包是网上来的。"""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(tar, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            name = m.name
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise ReleaseOpError(f"包里有不许的路径:{name!r}")
            if not (m.isfile() or m.isdir()):
                raise ReleaseOpError(f"包里只许普通文件和目录:{name!r}")
            target = (root / name).resolve()
            if target != root and root not in target.parents:
                raise ReleaseOpError(f"包里的路径跑到外面去了:{name!r}")
        for m in members:
            m.mode = (m.mode & 0o755) | (0o600 if m.isfile() else 0o700)   # 不带 setuid 之类
            tf.extract(m, root)


def build_venv(pkg: Path, slot: Path, *, pip_args: list[str],
               python: str = "python3") -> None:
    """跟 ``deploy/install.sh`` 4/7 一个做法:每版一个 venv,从包的**临时副本**装(不往槽里写
    egg-info),robot-agent 那几个包按依赖顺序装,**哨兵最后才落**。哨兵在就跳过。"""
    venv = slot / "venv"
    if (venv / SENTINEL).is_file():
        return
    shutil.rmtree(venv, ignore_errors=True)

    def run(*argv: str) -> None:
        got = subprocess.run(list(argv), capture_output=True, text=True, timeout=1800)
        if got.returncode != 0:
            raise ReleaseOpError(f"{argv[0]} {' '.join(argv[1:4])} …失败:"
                                 f"{(got.stderr or got.stdout).strip()[-300:]}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "pkg"
        shutil.copytree(pkg, copy)
        if (copy / WHEELS_DIR).is_dir():
            pip_args = ["--no-index", f"--find-links={copy / WHEELS_DIR}", *pip_args]
        run(python, "-m", "venv", str(venv))
        vpy = str(venv / "bin" / "python")
        run(vpy, "-m", "pip", "install", "--quiet", *pip_args, str(copy))
        agent = copy / "packages" / "robot-agent"
        if agent.is_dir():
            run(vpy, "-m", "pip", "install", "--quiet", *pip_args,
                f"{copy / 'packages' / 'contract'}[mqtt]", str(copy / "packages" / "adapter-sim"),
                str(copy / "packages" / "adapter-d1max"), str(agent))
    (venv / SENTINEL).write_text(f"d1max_patrol {slot.name}\n", encoding="utf-8")


class ReleaseOps:
    def __init__(self, layout: rel.Layout, *, fetch: Fetch, build: BuildVenv, work: Path,
                 now_ms: Callable[[], int], restart: Callable[[], None], sn: str = "",
                 disk_free: Callable[[Path], int] | None = None) -> None:
        self.layout = layout
        self._fetch = fetch
        self._restart = restart
        self._build = build
        self.work = Path(work)
        self._now = now_ms
        self.sn = sn
        self._disk_free = disk_free or _disk_free
        # 上一次装到一半被停掉(systemd 等 90 s 就 SIGKILL)留下的下载、解开的东西:起来就清。
        shutil.rmtree(self.work, ignore_errors=True)

    def current(self) -> str:
        try:
            return rel.current_name(self.layout)
        except rel.ReleaseError:
            return ""

    def ready(self, name: str) -> bool:
        """这一版落了槽、venv 也建好了。"""
        return (self.layout.release_dir(name) / "venv" / SENTINEL).is_file()

    def previous(self) -> str | None:
        """「上一版」:比在跑的那版旧的、装好了的、退过去代理起得来的最新一版。没有回 None。
        老服务那一代的槽、启动脚本坏了的槽不算(W00c5e 内部评审;判据见 ``rel.can_switch``)。"""
        cur = self.current()
        older = [n for n in rel.installed(self.layout) if n < cur and self.ready(n)
                 and self.can_switch_to(n)]
        return older[-1] if older else None

    def can_switch_to(self, name: str) -> bool:
        """从在跑的这版切到 ``name``,代理起不起得来(W00c5 外审阻断 1)。``activate`` 自己也查
        (``rel.activate``),这里给运行时在收下命令之前先拒。"""
        return rel.can_switch(self.layout, self.current(), name)

    def check_package(self, name: str) -> str:
        """槽里这一版的包现在还对不对(W00c6d):空串 = 对,否则是为什么。读整棵槽,调用方放线程里。"""
        try:
            rel.verify_slot(self.layout, name)
        except rel.ReleaseError as exc:
            return str(exc)
        except OSError as exc:
            return f"读不了槽: {exc}"
        return ""

    def can_roll_back(self) -> bool:
        """有在途的那次升级,或者盘上有上一版。"""
        return rel.read_pending(self.layout) is not None or self.previous() is not None

    def disk_ok(self, size: int) -> bool:
        """装 ``size`` 字节的包(0 = 只切、只退)盘够不够。**量的是双槽根与下载目录所在的盘**,
        不是发件箱那块(W00c5d 第三部分内部评审:发件箱可能在临时硬盘上)。"""
        need = INSTALL_DISK_FACTOR * size + DISK_MARGIN_BYTES
        for where in (self.layout.root, self.work.parent):
            try:
                if self._disk_free(where) < need:
                    return False
            except OSError:
                return False
        return True

    # ------------------------------------------------------------ 装(阻塞,在线程里调)

    def install(self, ref: ReleaseRef) -> None:
        if self.ready(ref.name):
            # 装好了就不重下;启动脚本的执行位照样补一遍 —— 以前落下的 0644 槽,站点再点一次「装」
            # 就能救回来,不用上狗重跑装机脚本(W00c5 修复内部评审)。
            rel.ensure_agent_start_executable(self.layout.release_dir(ref.name))
            return
        self.work.mkdir(parents=True, exist_ok=True)
        tar = self.work / f"{ref.name}.tar.gz"
        top = self.work / f"{ref.name}.x"
        try:
            h, n = hashlib.sha256(), 0
            with open(tar, "wb") as fh:
                try:
                    for chunk in self._fetch(ref.name):
                        n += len(chunk)
                        if n > ref.size:
                            raise ReleaseOpError("包比站点说的大")
                        h.update(chunk)
                        fh.write(chunk)
                except ReleaseOpError:
                    raise
                except Exception as exc:
                    raise ReleaseOpError(f"下载失败: {type(exc).__name__}: {exc}") from exc
            if n != ref.size or h.hexdigest() != ref.sha256:
                raise ReleaseOpError("包的大小或哈希对不上")
            shutil.rmtree(top, ignore_errors=True)
            safe_extract(tar, top)
            pkg = top / ref.name
            if not pkg.is_dir():
                raise ReleaseOpError(f"包里没有 {ref.name}/ 这一层")
            if not (pkg / rel.AGENT_START).is_file():
                # 老服务那一代的包:落了槽也切不过去(can_switch 拒),白下、白建 venv,还占一个槽位,
                # 开机守卫修链时还可能挑中它(W00c5 修复内部评审)。站点登记时也拒。
                raise ReleaseOpError(f"包里没有代理的启动脚本 {rel.AGENT_START}"
                                     "(老服务那一代的包),不装")
            try:
                rel.stage(self.layout, pkg, now_ms=self._now())   # 先核包内指纹,不对一个字节都不落
            except rel.ReleaseError as exc:
                raise ReleaseOpError(str(exc)) from exc
            self._build(pkg, self.layout.release_dir(ref.name))
        finally:
            tar.unlink(missing_ok=True)
            shutil.rmtree(top, ignore_errors=True)
        self._prune(keep_also=ref.name)

    def _prune(self, *, keep_also: str) -> None:
        """狗上最多留 ``KEEP_RELEASES + 1`` 版(决策 8):在跑的、在途的、刚装的、上一版。
        装了好几版不切的话,多出来的按名字从旧到新删。"""
        keep = {self.current(), keep_also, self.previous() or ""}
        p = rel.read_pending(self.layout)
        if p is not None:
            keep |= {p.to, p.src}
        names = list(rel.installed(self.layout))
        extra = [n for n in names if n not in keep]
        while extra and len(names) > rel.KEEP_RELEASES + 1:
            victim = extra.pop(0)
            shutil.rmtree(self.layout.release_dir(victim), ignore_errors=True)
            names.remove(victim)
            log.info("狗上版本太多,删掉 %s", victim)

    # ------------------------------------------------------------ 切、退

    def activate(self, name: str) -> dict[str, Any]:
        """写在途标记 → 换链 → 重启代理。**调用方先查空闲**。"""
        if not self.ready(name):
            raise ReleaseOpError(f"{name} 还没装好(先 release_install)")
        try:
            pending = rel.activate(self.layout, name, now_ms=self._now(), sn=self.sn)
        except rel.ReleaseError as exc:
            raise ReleaseOpError(str(exc)) from exc
        except OSError as exc:
            raise ReleaseOpError(f"没切:{exc}") from exc
        self._restart()
        return {"pending": pending.to_wire()}

    def rollback(self) -> str:
        """有在途的那次升级就退它;提交过了就切回上一版(一样写在途标记、连上才提交)。"""
        if rel.read_pending(self.layout) is not None:
            try:
                back = rel.rollback(self.layout, now_ms=self._now())
            except rel.ReleaseError as exc:
                raise ReleaseOpError(str(exc)) from exc
            self._restart()
            return back
        back = self.previous()
        if back is None:
            raise ReleaseOpError("盘上没有比在跑的这版更早、装好了的版本可退")
        self.activate(back)
        return back

    def commit_if_pending(self) -> str | None:
        """代理起来、连上站点了:在途的那一次升级算成,清标记、删多余的版本。返回提交了哪一版。"""
        p = rel.read_pending(self.layout)
        if p is None or self.current() != p.to:
            return None
        rel.commit(self.layout)
        return p.to

    def take_guard_note(self) -> dict | None:
        """开机守卫上次退回了上一版的话,它留的条子(读一次就删)。"""
        return rel.take_guard_note(self.layout)


def _disk_free(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def https_fetch(base_url: str, ssl_context: Any, *, timeout_s: float = 120.0) -> Fetch:
    """从站点的狗专用口拉发布包(mTLS,跟上传同一套证书)。"""
    import urllib.request
    from urllib.parse import quote

    def fetch(name: str) -> Iterable[bytes]:
        url = f"{base_url.rstrip('/')}/releases/{quote(name)}.tar.gz"
        with urllib.request.urlopen(url, context=ssl_context, timeout=timeout_s) as r:
            while chunk := r.read(1 << 20):
                yield chunk
    return fetch


def default_python() -> str:
    """建 venv 用哪个解释器:系统的 python3(跟 install.sh 一样)。"""
    return shutil.which("python3") or sys.executable


def pip_args_from_env() -> list[str]:
    """离线现场的 pip 参数(``D1MAX_PIP_ARGS='--no-index --find-links=…'``,跟 install.sh 一样)。"""
    import shlex
    return shlex.split(os.environ.get("D1MAX_PIP_ARGS", ""))
