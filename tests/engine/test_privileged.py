"""``Privileged``:app 与 CLI 调特权助手(``deploy/d1max-privileged``)的那一层。

真去 ``sudo`` 的只有 ``runner``,这里全部注入假的 —— 记下 argv、按脚本回
``CompletedProcess``。测的是三件事:助手不在时退回现状(开发机);助手在时
argv 精确、返回值按 stdout 最后一行;失败要变成 ``PrivilegedError`` 且带原话。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from d1max_patrol.engine.privileged import Privileged, PrivilegedError
from d1max_patrol.engine.selfcheck import RestartPlan

服务重启 = RestartPlan("service", ("systemctl", "restart", "d1max-patrol"), "没上装")
整机重启 = RestartPlan("machine", ("systemctl", "reboot"), "有上装")


class _假sudo:
    def __init__(self, 剧本: dict[str, tuple[int, str, str]] | None = None,
                 炸: BaseException | None = None) -> None:
        self.剧本 = 剧本 or {}
        self.炸 = 炸
        self.调用: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.调用.append(list(argv))
        if self.炸 is not None:
            raise self.炸
        rc, out, err = self.剧本.get(argv[3], (0, "", ""))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


def _有助手(tmp_path: Path, 假: _假sudo) -> Privileged:
    helper = tmp_path / "d1max-privileged"
    helper.write_text("#!/bin/bash\n", encoding="utf-8")
    return Privileged(helper=helper, runner=假)


def _没助手(tmp_path: Path, 假: _假sudo) -> Privileged:
    return Privileged(helper=tmp_path / "不存在", runner=假)


# ----------------------------------------------------------- available / present

def test_助手文件不在就不可用而且一次sudo都不跑(tmp_path):
    假 = _假sudo()
    p = _没助手(tmp_path, 假)
    assert p.present() is False
    assert p.available() is False
    assert 假.调用 == []


def test_check退0才算可用_argv精确(tmp_path):
    假 = _假sudo({"check": (0, "", "")})
    p = _有助手(tmp_path, 假)
    assert p.available() is True
    assert 假.调用 == [["sudo", "-n", str(p.helper), "check"]]


@pytest.mark.parametrize("坏法", [
    _假sudo({"check": (1, "", "sudo: a password is required")}),
    _假sudo(炸=OSError("sudo 不在")),
    _假sudo(炸=subprocess.TimeoutExpired("sudo", 5)),
])
def test_check不通_炸了_超时都算不可用(tmp_path, 坏法):
    assert _有助手(tmp_path, 坏法).available() is False


# ----------------------------------------------------------- install_unit

def test_没助手时装单元是跳过_不是失败(tmp_path):
    假 = _假sudo()
    got = _没助手(tmp_path, 假).install_unit("2026-09-20-77b2de")
    assert got.startswith("skipped:")
    assert "install.sh" in got, "要告诉人怎么补上"
    assert 假.调用 == []


@pytest.mark.parametrize("stdout, 期望", [("installed\n", "installed"),
                                          ("unchanged\n", "unchanged"),
                                          ("", "installed")])
def test_装单元按stdout最后一行回话(tmp_path, stdout, 期望):
    假 = _假sudo({"install-unit": (0, stdout, "")})
    p = _有助手(tmp_path, 假)
    assert p.install_unit("2026-09-20-77b2de") == 期望
    assert 假.调用 == [["sudo", "-n", str(p.helper), "install-unit", "2026-09-20-77b2de"]]


def test_装单元被拒要带上助手的原话(tmp_path):
    假 = _假sudo({"install-unit": (1, "",
                                  "d1max-privileged: 第 8 行:User 只能是 robot,给的是 root\n")})
    with pytest.raises(PrivilegedError, match="User 只能是 robot"):
        _有助手(tmp_path, 假).install_unit("2026-09-20-77b2de")


def test_装单元时sudo炸了或超时也是PrivilegedError(tmp_path):
    with pytest.raises(PrivilegedError):
        _有助手(tmp_path, _假sudo(炸=OSError("sudo 不在"))).install_unit("2026-09-20-77b2de")
    慢 = _有助手(tmp_path, _假sudo(炸=subprocess.TimeoutExpired("sudo", 30)))
    with pytest.raises(PrivilegedError, match="超时"):
        慢.install_unit("2026-09-20-77b2de")


# ----------------------------------------------------------- restart

def test_没助手时重启argv原样(tmp_path):
    p = _没助手(tmp_path, _假sudo())
    assert p.restart_argv(服务重启) == 服务重启.argv
    assert p.restart_argv(整机重启) == 整机重启.argv
    p.ensure_can_restart()          # 不抛:开发机退回裸 systemctl


def test_有助手时重启和整机重启都走助手(tmp_path):
    p = _有助手(tmp_path, _假sudo())
    assert p.restart_argv(服务重启) == ("sudo", "-n", str(p.helper), "restart")
    assert p.restart_argv(整机重启) == ("sudo", "-n", str(p.helper), "reboot")


def test_有助手但sudo不通_ensure要抛(tmp_path):
    p = _有助手(tmp_path, _假sudo({"check": (1, "", "sudo: a password is required")}))
    with pytest.raises(PrivilegedError, match="重启命令发不出去"):
        p.ensure_can_restart()


def test_有助手且sudo通_ensure不抛(tmp_path):
    _有助手(tmp_path, _假sudo({"check": (0, "", "")})).ensure_can_restart()
