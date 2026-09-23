"""特权助手 ``deploy/d1max-privileged``:robot 通过 sudo 白名单能跑的唯一 root 命令。

它是提权面,所以这里测的主要是**它拒绝什么**:版本名不合规、源不在、单元里
任何一行能把服务变成 root 跑的写法。跑的是真 bash,非 root 模式 —— 脚本要求
非 root 必须显式给 ``--root/--dest/--no-systemctl``,root 下这三个参数一律拒绝。
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "d1max-privileged"
SUDOERS = ROOT / "deploy" / "sudoers-d1max"
UNIT = (ROOT / "deploy" / "d1max-patrol.service").read_text(encoding="utf-8")
NAME = "2026-09-20-77b2de"


def _机器(tmp_path: Path, *, name: str = NAME, unit: str = UNIT) -> tuple[Path, Path]:
    root = tmp_path / "opt"
    src = root / "releases" / name / "deploy" / "d1max-patrol.service"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(unit, encoding="utf-8")
    dest = tmp_path / "etc" / "systemd" / "d1max-patrol.service"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return root, dest


def _跑(*args: str, root: Path, dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HELPER), "--root", str(root), "--dest", str(dest),
         "--no-systemctl", *args],
        capture_output=True, text=True, timeout=20)


def test_语法过关而且是可执行文件():
    assert subprocess.run(["bash", "-n", str(HELPER)]).returncode == 0
    assert HELPER.stat().st_mode & stat.S_IXUSR, "install -m 0755 之前,仓库里也该是可执行的"


def test_非root不给测试参数就拒绝():
    got = subprocess.run(["bash", str(HELPER), "check"], capture_output=True, text=True)
    assert got.returncode != 0
    assert "root" in got.stderr


def test_check退0(tmp_path):
    root, dest = _机器(tmp_path)
    assert _跑("check", root=root, dest=dest).returncode == 0


def test_合法单元第一次installed第二次unchanged(tmp_path):
    root, dest = _机器(tmp_path)
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip().splitlines()[-1] == "installed"
    assert dest.read_text(encoding="utf-8") == UNIT
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip().splitlines()[-1] == "unchanged"


@pytest.mark.parametrize("坏名", ["../x", "2026-09-20-77b2de; rm -rf /", "current",
                                "2026-09-20-77B2DE", "d1max-2026-09-20-77b2de", ""])
def test_版本名不合规就拒绝并且不落盘(tmp_path, 坏名):
    root, dest = _机器(tmp_path)
    got = _跑("install-unit", 坏名, root=root, dest=dest)
    assert got.returncode != 0
    assert not dest.exists()


def test_源不存在就拒绝(tmp_path):
    root, dest = _机器(tmp_path)
    got = _跑("install-unit", "2026-09-21-aaaaaa", root=root, dest=dest)
    assert got.returncode != 0
    assert not dest.exists()


def test_源是符号链接也拒绝(tmp_path):
    """robot 可写的槽里放一条链指到别处,装进去的就不是包里那份了。"""
    root, dest = _机器(tmp_path)
    src = root / "releases" / NAME / "deploy" / "d1max-patrol.service"
    真的 = tmp_path / "elsewhere.service"
    shutil.move(src, 真的)
    src.symlink_to(真的)
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0
    assert not dest.exists()


@pytest.mark.parametrize("违规行", [
    "User=root",
    "Group=root",
    "ExecStartPre=+/bin/sh -c id",
    "ExecStartPre=!/opt/d1max/bin/python -m x",
    "ExecStart=/bin/sh",
    "ExecStart=-/usr/bin/python3",
    "EnvironmentFile=/etc/passwd",
    "EnvironmentFile=-/etc/shadow",
    "Environment=LD_PRELOAD=/x.so",
    "ExecStartPost=/bin/true",
    "ExecStopPost=/bin/true",
    "AmbientCapabilities=CAP_SYS_ADMIN",
    "CapabilityBoundingSet=CAP_SYS_ADMIN",
    "WorkingDirectory=/",
    "[Socket]",
    "随手一行没有等号",
])
def test_能把服务变成root跑的写法一律拒绝(tmp_path, 违规行):
    root, dest = _机器(tmp_path, unit=UNIT.replace("[Install]", 违规行 + "\n[Install]", 1))
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0, 违规行
    assert not dest.exists(), 违规行
    assert got.stderr.strip(), "拒绝要说一句为什么"


@pytest.mark.parametrize("绕法, 单元", [
    # User= 写在 [Unit] 段:systemd 只记一句 ignoring,服务以 root 起。
    ("User在Unit段", UNIT.replace("User=robot\n", "")
                    .replace("[Unit]\n", "[Unit]\nUser=robot\n", 1)),
    # 反斜杠续行:校验器按物理行看到 User=robot,systemd 把它并进上一行的值。
    ("续行吞掉User", UNIT.replace("User=robot\n", "Type=simple \\\nUser=robot\n", 1)
                     .replace("Type=simple\n", "", 1)),
    ("续行吞掉段头", UNIT.replace("[Service]\n", "Description=x \\\n[Service]\n", 1)),
    # Environment 一行多赋值:前缀合规,后面跟着 LD_PRELOAD。
    ("Environment多赋值", UNIT.replace("Environment=PYTHONUNBUFFERED=1\n",
                                     "Environment=D1MAX_A=1 LD_PRELOAD=/x.so\n", 1)),
    ("Environment带引号", UNIT.replace("Environment=PYTHONUNBUFFERED=1\n",
                                     'Environment="D1MAX_A=1 LD_PRELOAD=/x.so"\n', 1)),
    # 键放错段:Service 段的键写在 Unit 段被 systemd 忽略,等于没写。
    ("ExecStart在Unit段", UNIT.replace("[Unit]\n", "[Unit]\nExecStart=/opt/d1max/x\n", 1)),
])
def test_绕过校验的写法一律拒绝(tmp_path, 绕法, 单元):
    assert 单元 != UNIT, 绕法
    root, dest = _机器(tmp_path, unit=单元)
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0, 绕法
    assert not dest.exists(), 绕法


def test_NUL字节藏起来的键也拒绝(tmp_path):
    """bash 的 read 会静默丢掉 NUL,校验器看到 User=robot;systemd 把 NUL 当行
    分隔,看到的是 "Us" 和 "er=robot" 两行垃圾 —— 装进去的单元没有 User=,以 root 起。
    复评在真脚本 + 真 systemd 上复现过。"""
    root, dest = _机器(tmp_path)
    src = root / "releases" / NAME / "deploy" / "d1max-patrol.service"
    src.write_bytes(UNIT.replace("User=robot", "Us\0er=robot", 1).encode("utf-8"))
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0
    assert not dest.exists()


@pytest.mark.parametrize("控制字符", ["\x01", "\x1b", "\x7f", "\x0b"])
def test_其他控制字符也拒绝(tmp_path, 控制字符):
    root, dest = _机器(tmp_path, unit=UNIT.replace("[Install]", f"# {控制字符}\n[Install]", 1))
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0
    assert not dest.exists()


def test_合法的中文注释和CRLF照样放行(tmp_path):
    """控制字符的检查不能把 UTF-8 中文注释(0x80–0xFF)和 CRLF 一起挡掉。"""
    带CRLF = ("# 中文注释 —— 单元里本来就有\n" + UNIT).replace("\n", "\r\n")
    root, dest = _机器(tmp_path, unit=带CRLF)
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode == 0, got.stderr


@pytest.mark.parametrize("行", [
    "Wants=debug-shell.service",                 # /bin/bash 无认证 root shell 挂到 tty9
    "After=emergency.service",
    "Wants=network-online.target debug-shell.service",
    "ExecStart=/opt/d1max/../../bin/sh",
    "ExecStartPre=-/opt/d1max/current/../../../bin/sh",
    "WorkingDirectory=/opt/d1max/../..",
])
def test_借依赖拉起root进程或用点点绕出opt_d1max一律拒绝(tmp_path, 行):
    段 = "[Unit]" if 行.startswith(("Wants", "After")) else "[Service]"
    root, dest = _机器(tmp_path, unit=UNIT.replace(段 + "\n", 段 + "\n" + 行 + "\n", 1))
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0, 行
    assert not dest.exists(), 行


def test_源是FIFO时不会挂死(tmp_path):
    """robot 能 mkfifo;没有超时的话 root 的 cp 会永远阻塞在 open 上,留成孤儿。"""
    import os
    root, dest = _机器(tmp_path)
    src = root / "releases" / NAME / "deploy" / "d1max-patrol.service"
    src.unlink()
    os.mkfifo(src)
    got = subprocess.run(
        ["bash", str(HELPER), "--root", str(root), "--dest", str(dest), "--no-systemctl",
         "install-unit", NAME], capture_output=True, text=True, timeout=15)
    assert got.returncode != 0
    assert not dest.exists()


def test_拒绝时不回显那一行的内容(tmp_path):
    """源可以被 robot 换成指向 root 才读得到的文件的链;拒绝的理由只说行号和键,
    不把内容打回去 —— stderr 会原样透传到 HTTP 409 的 body 里。"""
    root, dest = _机器(tmp_path, unit=UNIT.replace(
        "[Install]", "root:$6$SECRETHASH:19000\n[Install]", 1))
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0
    assert "SECRETHASH" not in got.stderr
    assert "SECRETHASH" not in got.stdout


def test_校验的是root目录里的那份拷贝而不是源(tmp_path):
    """源在 robot 可写的槽里。脚本要先把它拷进目标目录(root 的),校验拷贝,
    再 mv 到位 —— 只打开源一次,robot 在校验与安装之间换文件也换不到装进去的那份。
    结构性断言:脚本里 validate_unit 吃的是 $TMP,而且拷贝不跟符号链接。"""
    文本 = HELPER.read_text(encoding="utf-8")
    assert 'validate_unit "$TMP"' in 文本
    assert 'validate_unit "$src"' not in 文本
    assert "cp -P" in 文本 or "cp --no-dereference" in 文本
    assert 'mktemp "$DEST.XXXXXX"' in 文本


def test_restart不在自己的cgroup里停自己():
    """服务用 Popen 起助手,助手跟服务同一个 cgroup;`systemctl stop` 会连助手
    一起杀,migrate 和 start 永远跑不到 —— OTA 之后服务停着不起。所以 restart
    必须先用 systemd-run 把真正的 stop→migrate→start 搬出这个 cgroup。"""
    文本 = HELPER.read_text(encoding="utf-8")
    assert "systemd-run" in 文本
    assert "restart-now" in 文本


def test_测试模式下restart只打印计划(tmp_path):
    root, dest = _机器(tmp_path)
    got = _跑("restart", root=root, dest=dest)
    assert got.returncode == 0, got.stderr
    assert "systemd-run" in got.stdout and "restart-now" in got.stdout


def test_少了User_robot也拒绝(tmp_path):
    """没有 User= 的单元以 root 跑 —— 跟写 User=root 一样危险。"""
    root, dest = _机器(tmp_path, unit=UNIT.replace("User=robot\n", ""))
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0
    assert not dest.exists()


def test_太大的单元也拒绝(tmp_path):
    root, dest = _机器(tmp_path, unit=UNIT + "# " + "x" * 70_000 + "\n")
    got = _跑("install-unit", NAME, root=root, dest=dest)
    assert got.returncode != 0
    assert not dest.exists()


def test_不认识的子命令退64(tmp_path):
    root, dest = _机器(tmp_path)
    assert _跑("format-disk", root=root, dest=dest).returncode == 64


def test_sudoers只有那一行():
    assert SUDOERS.read_text(encoding="utf-8") == (
        "robot ALL=(root) NOPASSWD: /usr/local/sbin/d1max-privileged\n")
    if shutil.which("visudo"):
        got = subprocess.run(["visudo", "-cf", str(SUDOERS)], capture_output=True, text=True)
        assert got.returncode == 0, got.stderr + got.stdout
