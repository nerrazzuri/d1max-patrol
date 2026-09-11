"""告警落盘 + AlertBook 修剪。spec §4.3 第 1 级;销挂账 75。"""

from __future__ import annotations

import json
from pathlib import Path

from d1max_patrol.engine.alerts import AlertBook


def 读盘(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_不给spool时行为跟今天一模一样(tmp_path: Path) -> None:
    """已有的 13 个测试文件里 AlertBook() 的用法一个都不许改。"""
    book = AlertBook()
    assert book.spool_path is None
    book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=1_000)
    assert len(book.all()) == 1
    assert book.trim() == 0
    assert len(book.all()) == 1


def test_给了spool就一行一条落盘(tmp_path: Path) -> None:
    spool = tmp_path / "alerts.jsonl"
    book = AlertBook(spool=spool)
    book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=1_000)
    book.raise_alert(kind="disk_80", robot="D1", title="盘快满了", now_ms=2_000)
    rows = 读盘(spool)
    assert [r["kind"] for r in rows] == ["stuck", "disk_80"]
    assert rows[0]["title"] == "卡住了"


def test_落盘的字段跟to_wire完全一致(tmp_path: Path) -> None:
    """**不许另造一套格式。** 服务器那边读的和手机那边读的必须是同一个形状。"""
    spool = tmp_path / "alerts.jsonl"
    book = AlertBook(spool=spool)
    alert = book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=1_000)
    assert 读盘(spool)[0] == alert.to_wire()


def test_合并进同一条也要再落一行(tmp_path: Path) -> None:
    """盘上是**事件流**不是当前状态:count 从 1 变 2 这件事本身要留痕。"""
    spool = tmp_path / "alerts.jsonl"
    book = AlertBook(spool=spool)
    book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=1_000)
    book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=2_000)
    rows = 读盘(spool)
    assert [r["count"] for r in rows] == [1, 2]
    assert len({r["key"] for r in rows}) == 1


def test_ack和resolve也落盘(tmp_path: Path) -> None:
    spool = tmp_path / "alerts.jsonl"
    book = AlertBook(spool=spool)
    alert = book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=1_000)
    book.ack(alert.key, who="值班甲", now_ms=2_000)
    book.resolve(alert.key, who="值班甲", now_ms=3_000)
    rows = 读盘(spool)
    assert len(rows) == 3
    assert rows[1]["acked_by"] == "值班甲"
    assert rows[2]["resolved_ms"] == 3_000


def test_修剪只剪已解决的_没解决的一条不动(tmp_path: Path) -> None:
    """挂账 75 的解。**修剪不是丢数据** —— 盘上一条不少,只是不再在内存里拿着。"""
    book = AlertBook(spool=tmp_path / "alerts.jsonl", keep_closed=2)
    开着的 = book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=1_000)
    for n in range(5):
        a = book.raise_alert(kind="finding", robot=f"D{n + 2}", title="发现", now_ms=1_000)
        book.resolve(a.key, now_ms=2_000)
    assert len(book.all()) == 6
    assert book.trim() == 3
    留下的 = book.all()
    assert len(留下的) == 3
    assert 开着的.key in {a.key for a in 留下的}
    assert len(读盘(tmp_path / "alerts.jsonl")) == 11


def test_不给spool就不修剪(tmp_path: Path) -> None:
    """没有盘上那一份,修剪就真的是丢数据了。**不许丢。**"""
    book = AlertBook(keep_closed=1)
    for n in range(5):
        a = book.raise_alert(kind="finding", robot=f"D{n}", title="发现", now_ms=1_000)
        book.resolve(a.key, now_ms=2_000)
    assert book.trim() == 0
    assert len(book.all()) == 5


def test_修剪之后seq不倒退(tmp_path: Path) -> None:
    """_seq 是"这个 robot/kind 发过几号"的计数,剪掉内存里的条目之后,
    下一条的号还得往后走 —— 撞号会让服务器把两条不同的告警当成同一条。
    """
    book = AlertBook(spool=tmp_path / "alerts.jsonl", keep_closed=0)
    for n in range(3):
        a = book.raise_alert(kind="finding", robot="D1", title="发现", now_ms=1_000 * (n + 1))
        book.resolve(a.key, now_ms=1_000 * (n + 1) + 1)
    book.trim()
    新的 = book.raise_alert(kind="finding", robot="D1", title="发现", now_ms=99_000)
    assert 新的.key.endswith("#4")


def test_spool目录不存在也建得出来(tmp_path: Path) -> None:
    spool = tmp_path / "没建过" / "alerts.jsonl"
    book = AlertBook(spool=spool)
    book.raise_alert(kind="stuck", robot="D1", title="卡住了", now_ms=1)
    assert spool.exists()
