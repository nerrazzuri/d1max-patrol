"""服务器落盘。**§4.2 的服务器那一半在这儿:哈希是这儿算的。**"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from d1max_console.store import ConsoleStore, PathRefused, safe_join


def test_收一整块_回执里的哈希是自己算的(tmp_path: Path) -> None:
    s = ConsoleStore(tmp_path)
    got = s.put("D1MAX-01", "巡检一/20260911T101500Z", "events.jsonl",
                offset=0, data=b'{"kind": "started"}\n')
    assert got.size == 20
    assert got.sha256 == hashlib.sha256(b'{"kind": "started"}\n').hexdigest()


def test_续传_第二块接在第一块后面(tmp_path: Path) -> None:
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"aaa")
    got = s.put("D1MAX-01", "r/t", "events.jsonl", offset=3, data=b"bbb")
    assert got.size == 6
    assert got.sha256 == hashlib.sha256(b"aaabbb").hexdigest()


def test_offset跳号_拒绝写_并且报真实大小(tmp_path: Path) -> None:
    """中间缺一段。**不许补零,不许照写** —— 那会造出一个哈希永远对不上的文件。"""
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"aaa")
    got = s.put("D1MAX-01", "r/t", "events.jsonl", offset=99, data=b"bbb")
    assert got.size == 3, "报的是盘上真实的大小,狗看见这个数会 rewind"
    assert got.sha256 == hashlib.sha256(b"aaa").hexdigest()


def test_offset往回_是重传_截断后重写(tmp_path: Path) -> None:
    """新块必须比旧块**短**,这样才能验到 ``truncate()`` 真的在干活 ——
    等长覆盖测不出少了 truncate 有什么区别,盘上会不会留下陈旧的尾巴。"""
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"AAAABBBB")
    got = s.put("D1MAX-01", "r/t", "events.jsonl", offset=4, data=b"C")
    assert got.size == 5, "旧的 BBBB 必须被截掉,不能留在 C 后面"
    assert got.sha256 == hashlib.sha256(b"AAAAC").hexdigest()


@pytest.mark.parametrize("坏", [
    "../etc/passwd",
    "a/../../b",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "a//b",
    "",
    "   ",
    "a/\x00b",
    "a/\nb",
    # 反斜杠族。单独一段里带反斜杠,pathlib 在 Windows 上会把它当分隔符
    # 拆成两层子目录,**始终还在 root 底下**,所以这条只有字符闸能拦得住,
    # 结果闸对它完全无感(复评 M3:删掉反斜杠字符闸,这条从绿转红)。
    "a\\b",
    "..\\..\\x",
    # UNC 与盘符相对路径的另外两种写法,C:/Windows/win.ini 之外的形状。
    "\\\\host\\share",
    "C:foo",
    # 冒号但不是"单字母盘符"形状,pathlib 不会把它当换盘符处理,
    # 结果还是落在 root 底下,同样只有字符闸拦得住(复评 M5 的对偶用例)。
    "photos:secret",
    # 尾随空格/点。Win32 在真正 open() 的时候会把最后一段的尾随点和空格
    # 剥掉,resolve() 和按段查都看不出来,只能整段拒掉(复评应修一)。
    ".. ",
    "...",
    ". ",
    "a.",
    "a..",
    # 超长段,不拒会在磁盘层炸成 OSError,跟"盘坏了"长得一样。
    "L" * 260,
    # Windows 保留设备名,不拒同样会在磁盘层炸出跟"盘坏了"一样的 OSError。
    "NUL",
    "NUL.txt",
    "CON",
    "COM1",
    "LPT1",
])
def test_路径穿越一律拒(tmp_path: Path, 坏: str) -> None:
    """**这是这一卷唯一一道拦路径穿越的闸。** 拒绝就是拒绝,不清洗后接着用。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "r/t", 坏, offset=0, data=b"x")


def test_尾随点造成的路径别名_直接拒绝(tmp_path: Path) -> None:
    """``events.jsonl.`` 和 ``events.jsonl`` 会被 Win32 当成同一个文件 ——
    这不是"清洗一下接着用"能解决的,必须整段拒掉,已经落盘的东西不能被
    这种别名覆盖(复评应修二的原样复现)。"""
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"original")
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "r/t", "events.jsonl.", offset=0, data=b"ZZZZZZZZ")
    # 原文件必须原封不动,不能被这个"别名" rel 覆盖掉。
    assert s.path_of("D1MAX-01", "r/t", "events.jsonl").read_bytes() == b"original"


def test_safe_join拦住junction逃逸(tmp_path: Path) -> None:
    """**这是第二道闸唯一拦得住、第一道闸完全无感的攻击形状。**

    ``root/SN/`` 底下建一个指向 root 外面的目录联接(junction),每一段
    单看都是合法名字,第一道按段查毫无办法;只有落盘之后 ``resolve()``
    一次、确认结果真的还在 root 底下的第二道闸能拦住它。这条测试删掉
    第二道闸会转红(复评 M4),留着它才对得上"两道都要,谁也不能替谁"
    这句话。

    本机若不能建 junction(比如权限被组策略收紧),就 ``skip`` 而不是
    假装测过 —— 复评在这台机器上实测能建,若环境不同这条会如实报告。
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    sn_dir = root / "D1MAX-01"
    sn_dir.mkdir()
    junction = sn_dir / "escape"
    # mklink 的控制台输出走系统 ANSI 代码页(中文 Windows 上是 GBK),
    # 不是 UTF-8,这里按字节收,自己挑编码解,免得 text=True 用 UTF-8
    # 硬解出 UnicodeDecodeError 把测试输出弄脏。
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, check=False,
    )
    if made.returncode != 0:
        err = made.stderr.decode("gbk", errors="replace").strip()
        pytest.skip(f"本机建不出 junction,跳过:{err}")
    with pytest.raises(PathRefused):
        safe_join(root, "D1MAX-01", "escape", "secret.txt")
    s = ConsoleStore(root)
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "escape", "PWNED.txt", offset=0, data=b"x")


def test_sn和run也要过闸(tmp_path: Path) -> None:
    """三个字段全是狗发来的,三个都不可信。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(PathRefused):
        s.put("../../root", "r/t", "a.jpg", offset=0, data=b"x")
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "../..", "a.jpg", offset=0, data=b"x")
    with pytest.raises(PathRefused):
        # 尾随空格会被 Win32 剥掉,"D1MAX-02 " 会落进 "D1MAX-02" 的目录树,
        # 等于一只狗能踩进另一只狗的地盘(复评应修二点名的第二个后果)。
        s.put("D1MAX-02 ", "r/t", "a.jpg", offset=0, data=b"x")


def test_中文路径照收(tmp_path: Path) -> None:
    """run 目录名是中文的(巡检一),照收不误,闸拦的是穿越,不是非 ASCII。"""
    s = ConsoleStore(tmp_path)
    got = s.put("D1MAX-01", "变电站一号/20260911T101500Z",
                "photos/P1__front__1757000000.jpg", offset=0, data=b"\xff\xd8")
    assert got.size == 2
    assert s.path_of("D1MAX-01", "变电站一号/20260911T101500Z",
                     "photos/P1__front__1757000000.jpg").exists()


def test_safe_join结果必须落在root底下(tmp_path: Path) -> None:
    with pytest.raises(PathRefused):
        safe_join(tmp_path, "..")
    assert safe_join(tmp_path, "a", "b") == (tmp_path / "a" / "b").resolve()


def test_数得出一只狗有哪几趟(tmp_path: Path) -> None:
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "巡检一/20260911T101500Z", "a.jpg", offset=0, data=b"x")
    s.put("D1MAX-01", "巡检一/20260911T120000Z", "a.jpg", offset=0, data=b"x")
    s.put("D1MAX-02", "巡检一/20260911T101500Z", "a.jpg", offset=0, data=b"x")
    assert s.runs_of("D1MAX-01") == ["巡检一/20260911T101500Z",
                                     "巡检一/20260911T120000Z"]
    assert s.runs_of("D1MAX-99") == []


def test_负数offset直接拒(tmp_path: Path) -> None:
    """``offset`` 也是狗发来的。不拦的话它会掉进 ``seek()``,
    冒出来一个跟"盘坏了"长得一模一样的 ``OSError``。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(ValueError, match="offset"):
        s.put("D1MAX-01", "r/t", "a.jpg", offset=-1, data=b"x")
