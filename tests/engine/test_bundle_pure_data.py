"""任务包里不许有可执行的东西。**四道闸,一道一条测试。**

写成 ``if a or b or c or d`` 的话,删掉其中一道测试照样绿 —— 所以这里
每一道都有一条只有它能拦得住的样本。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from d1max_patrol.engine.bundle import (
    ALLOWED_SUFFIXES,
    BundleError,
    scan_pure_data,
    verify_pure_data,
)

跳过windows = pytest.mark.skipif(
    os.name == "nt",
    reason="Windows 上 chmod 设不了可执行位(真验证见 task-6-report.md)。"
    "符号链接这台机器上是能建的,不用这条 skip —— 见下面对应的测试。")


def 干净的包(tmp_path):
    # exist_ok=True: 有条测试要在同一个 tmp_path 上调两次(先 scan 再 verify),
    # 第二次照原样重建同一棵树 —— 不加 exist_ok 第二次调用会因为目录已经在
    # 而 FileExistsError,这跟被测的四道闸无关,是这个 helper 自己的坑。
    d = tmp_path / "bundle"
    (d / "missions").mkdir(parents=True, exist_ok=True)
    (d / "maps").mkdir(exist_ok=True)
    (d / "bundle.yaml").write_text("bundle_id: x\n", encoding="utf-8")
    (d / "missions" / "night.json").write_text('{"schema": 1}', encoding="utf-8")
    (d / "maps" / "floor1.pgm").write_bytes(b"P5\n2 2\n255\n\x00\x01\x02\x03")
    (d / "README.md").write_text("# 说明\n", encoding="utf-8")
    return d


def 门(vs) -> list[str]:
    return sorted({v.gate for v in vs})


def test_干净的包一条都不报(tmp_path):
    assert scan_pure_data(干净的包(tmp_path)) == ()
    verify_pure_data(干净的包(tmp_path))        # 不抛


# ---- 第 1 道:后缀白名单 --------------------------------------------------

#: Windows 的 os.stat() 是 CRT 模拟出来的,对这几个后缀天生就带执行位
#: (S_IXUSR 等),不用注入 mode_of、不用 chmod —— 这是这台机器上验证过的
#: 平台事实(见 task-6-report.md)。所以在 Windows 上,拿这几个后缀建出来的
#: 文件会被 suffix 和 exec_bit 两道闸一起拦下,不是只有 suffix 一道。
_WINDOWS天生带执行位的后缀 = {".exe", ".bat", ".cmd", ".com"}


@pytest.mark.parametrize("名", ["run.py", "run.pyc", "boot.sh", "lib.so",
                               "x.exe", "go.bat", "a.ps1", "blob.bin",
                               "noext", "x.PY"])
def test_白名单外的后缀一律拒(tmp_path, 名):
    d = 干净的包(tmp_path)
    (d / 名).write_bytes(b"data")
    vs = scan_pure_data(d)
    if os.name == "nt" and Path(名).suffix.lower() in _WINDOWS天生带执行位的后缀:
        assert "suffix" in 门(vs)
    else:
        assert 门(vs) == ["suffix"]
    assert any(v.gate == "suffix" and v.path == 名 for v in vs)


def test_白名单里的都放行(tmp_path):
    d = 干净的包(tmp_path)
    for i, 后缀 in enumerate(sorted(ALLOWED_SUFFIXES)):
        (d / f"f{i}{后缀}").write_bytes(b"data")
    assert scan_pure_data(d) == ()


def test_没有后缀的也拒(tmp_path):
    """黑名单会放过它。白名单不会 —— 这正是换白名单的理由。"""
    d = 干净的包(tmp_path)
    (d / "Makefile").write_bytes(b"all:\n")
    assert 门(scan_pure_data(d)) == ["suffix"]


# ---- 第 2 道:符号链接 ----------------------------------------------------
#
# 这三条**不用** 跳过windows:这台开发机上符号链接是真能建的(已经用
# Path.symlink_to() 验证过),「Windows 上建不了符号链接」这个前提在这台机
# 器上不成立,跳过反而会让 symlink 这道闸在这里从没被真正验证过。


def test_指向包外的符号链接要拒(tmp_path):
    """``maps/x.pgm -> /etc/shadow``:包确实是纯数据,但「归档这个包」
    就成了「归档 /etc/shadow」。
    """
    d = 干净的包(tmp_path)
    外头 = tmp_path / "secret.txt"
    外头.write_text("s", encoding="utf-8")
    (d / "maps" / "leak.pgm").symlink_to(外头)
    assert 门(scan_pure_data(d)) == ["symlink"]


def test_包内部的符号链接一样拒(tmp_path):
    """**内部的也拒。** 「只拦指向包外的」要解析路径,而解析路径这件事本身
    就是能被绕的(``a/../../etc``、中途再套一层链接)。一律拒才是条清楚的界。
    """
    d = 干净的包(tmp_path)
    (d / "maps" / "same.pgm").symlink_to(d / "maps" / "floor1.pgm")
    assert 门(scan_pure_data(d)) == ["symlink"]


def test_指向目录的符号链接不会让扫描转不出来(tmp_path):
    """``a -> ..`` 是个环。扫描要是跟着链接走,打包器会在这儿挂死 ——
    而打包器正是那个本该**抓住**符号链接的东西。
    """
    d = 干净的包(tmp_path)
    (d / "maps" / "loop").symlink_to(d, target_is_directory=True)
    assert 门(scan_pure_data(d)) == ["symlink"]      # 而且是回来了的


# ---- 第 3 道:可执行位 ----------------------------------------------------

def test_带可执行位的文件要拒_注入模式(tmp_path):
    """**这一条在 Windows 上也跑。**

    Windows 的 ``os.stat`` 给普通文件的 mode 里根本没有可执行位,所以真
    ``chmod`` 那条只能在 Linux 上跑。判据本身(拿到这个 mode 该怎么判)
    跟平台无关,所以把取 mode 的那一步注进来 —— 跟 §8.5 「时间必须可注入」
    是同一个道理,只不过这里注的是平台事实。
    """
    d = 干净的包(tmp_path)
    坏的 = d / "README.md"

    def mode_of(p):
        真 = os.stat(p).st_mode
        return 真 | stat.S_IXUSR if os.path.samefile(p, 坏的) else 真

    vs = scan_pure_data(d, mode_of=mode_of)
    assert 门(vs) == ["exec_bit"]
    assert vs[0].path == "README.md"


@pytest.mark.parametrize("位", [stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH])
def test_三个可执行位任意一个都拒(tmp_path, 位):
    d = 干净的包(tmp_path)
    vs = scan_pure_data(d, mode_of=lambda p: os.stat(p).st_mode | 位)
    assert 门(vs) == ["exec_bit"]


@跳过windows
def test_真chmod也拒(tmp_path):
    """**这一条是那道闸在真机上到底有没有用的唯一证据。**
    注入版证明的是判据,这一条证明的是 ``os.stat`` 那一头接对了。
    """
    d = 干净的包(tmp_path)
    目标 = d / "README.md"
    目标.chmod(目标.stat().st_mode | stat.S_IXUSR)
    assert 门(scan_pure_data(d)) == ["exec_bit"]


# ---- 第 4 道:shebang -----------------------------------------------------

def test_改了后缀的脚本靠shebang抓(tmp_path):
    """``deploy.txt`` 后缀在白名单里、没有可执行位、不是链接 ——
    **前三道全放行。** 只有这一道拦得住。
    """
    d = 干净的包(tmp_path)
    (d / "deploy.txt").write_text("#!/bin/sh\nrm -rf /\n", encoding="utf-8")
    vs = scan_pure_data(d)
    assert 门(vs) == ["shebang"]
    assert vs[0].path == "deploy.txt"


def test_正文里的井号叹号不算(tmp_path):
    """只看**头两个字节**。文档正文里出现 ``#!`` 是很正常的事。"""
    d = 干净的包(tmp_path)
    (d / "notes.md").write_text("# 说明\n\n用 `#!/bin/sh` 开头的叫 shebang\n",
                                encoding="utf-8")
    assert scan_pure_data(d) == ()


def test_空文件不炸(tmp_path):
    d = 干净的包(tmp_path)
    (d / "empty.txt").write_bytes(b"")
    assert scan_pure_data(d) == ()


def test_一个字节的文件不炸(tmp_path):
    d = 干净的包(tmp_path)
    (d / "one.txt").write_bytes(b"#")
    assert scan_pure_data(d) == ()


# ---- 合起来 --------------------------------------------------------------

def test_四道各报各的一次报全(tmp_path):
    """**不短路。** 一次把问题全列出来,写包的人才不用改一个跑一次改一个
    跑一次。
    """
    d = 干净的包(tmp_path)
    (d / "run.py").write_text("x = 1\n", encoding="utf-8")
    (d / "deploy.txt").write_text("#!/bin/sh\n", encoding="utf-8")
    vs = scan_pure_data(d, mode_of=lambda p: os.stat(p).st_mode | stat.S_IXUSR)
    assert set(门(vs)) >= {"suffix", "shebang", "exec_bit"}


def test_verify会把问题带进报错里(tmp_path):
    d = 干净的包(tmp_path)
    (d / "run.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(BundleError, match="run.py"):
        verify_pure_data(d)


def test_四道闸的名字都在(tmp_path):
    """报上去的 ``gate`` 是接口的一部分:值守屏按它分类。"""
    from d1max_patrol.engine.bundle import GATE_NAMES
    assert GATE_NAMES == ("suffix", "symlink", "exec_bit", "shebang")


def test_目录本身不参与后缀判断(tmp_path):
    """``missions`` 是个目录,没有后缀 —— 拿它当文件判就全红了。"""
    d = 干净的包(tmp_path)
    (d / "maps" / "floor1").mkdir()
    assert scan_pure_data(d) == ()
