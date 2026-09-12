"""服务器落盘。**§4.2 的服务器那一半在这儿:哈希是这儿算的。**"""

from __future__ import annotations

import hashlib
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
    s = ConsoleStore(tmp_path)
    s.put("D1MAX-01", "r/t", "events.jsonl", offset=0, data=b"aaabbb")
    got = s.put("D1MAX-01", "r/t", "events.jsonl", offset=3, data=b"ccc")
    assert got.size == 6
    assert got.sha256 == hashlib.sha256(b"aaaccc").hexdigest()


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
])
def test_路径穿越一律拒(tmp_path: Path, 坏: str) -> None:
    """**这是这一卷唯一一道拦路径穿越的闸。** 拒绝就是拒绝,不清洗后接着用。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "r/t", 坏, offset=0, data=b"x")


def test_sn和run也要过闸(tmp_path: Path) -> None:
    """三个字段全是狗发来的,三个都不可信。"""
    s = ConsoleStore(tmp_path)
    with pytest.raises(PathRefused):
        s.put("../../root", "r/t", "a.jpg", offset=0, data=b"x")
    with pytest.raises(PathRefused):
        s.put("D1MAX-01", "../..", "a.jpg", offset=0, data=b"x")


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
