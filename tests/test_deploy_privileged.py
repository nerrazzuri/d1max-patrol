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
