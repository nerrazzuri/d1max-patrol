"""站点数据访问层的边界(docs/数据访问层边界设计.md,RC 加固 H12):把现状钉成基线,只许变好不许变坏。

不改业务代码,只看源码:谁直接碰 sqlite3、SQL 里有没有不确定的函数、每张表谁在写。"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "d1max_site"

#: 可以直接 import sqlite3 的模块(设计稿第 3 节)。多一个就要先说清楚为什么。
MAY_IMPORT_SQLITE = {"db", "backup", "restore", "inventory", "selfcheck", "incidents"}

#: 不止一个模块写的表 → 写它的模块(设计稿第 4 节)。新表只许一个模块写;这里不许再多。
MULTI_WRITER = {
    "meta": {"backup", "health", "main"},
    "runs": {"evidence", "privacy", "runs"},
    "run_photos": {"evidence", "privacy"},
    "push_devices": {"accounts", "push"},
    "pending_alerts": {"deterrence", "pending_alerts"},
    "enroll_claims": {"provision", "registry"},
}

_WRITE = re.compile(r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)", re.I)
_NONDET = re.compile(r"datetime\(\s*'now'|date\(\s*'now'|CURRENT_TIMESTAMP|CURRENT_DATE|"
                     r"(?<![.\w])random\(\)|randomblob\(|julianday\(\s*'now'|"
                     r"strftime\([^)]*'now'", re.I)


def _modules() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(SRC.glob("*.py"))}


def _tables() -> set[str]:
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS ([a-z_]+)",
                          (SRC / "db.py").read_text(encoding="utf-8")))


def test_直接碰_sqlite3_的模块就这几个():
    got = {name for name, text in _modules().items()
           if re.search(r"^\s*(?:import sqlite3|from sqlite3 )", text, re.M)}
    assert got <= MAY_IMPORT_SQLITE, f"多出来的: {sorted(got - MAY_IMPORT_SQLITE)}"


def test_写连接只在_SiteDB_里开_别处开的都是只读或者临时库():
    for name, text in _modules().items():
        for m in re.finditer(r"sqlite3\.connect\(([^)]*)\)", text):
            arg = m.group(1)
            if name == "db":
                continue
            ok = "mode=ro" in arg or name in ("backup", "restore")
            assert ok, f"{name}.py 自己开了能写的连接: {m.group(0)}"


def test_SQL_里没有不确定的函数_重放出来才会一样():
    bad = {name: _NONDET.findall(text) for name, text in _modules().items()
           if _NONDET.search(text)}
    assert not bad, bad


def test_一张表一个模块写_例外就是记下来的那六张():
    tables = _tables()
    writers: dict[str, set[str]] = {}
    for name, text in _modules().items():
        if name == "db":
            continue
        for t in _WRITE.findall(text):
            if t in tables:
                writers.setdefault(t, set()).add(name)
    multi = {t: w for t, w in writers.items() if len(w) > 1}
    new = sorted(set(multi) - set(MULTI_WRITER))
    assert not new, f"新出现多个模块写的表: {new}"
    for t, w in multi.items():
        assert w <= MULTI_WRITER[t], f"{t} 多了新的写入方: {sorted(w - MULTI_WRITER[t])}"
    assert len(tables) >= 52 and not [t for t in tables if t not in writers and t != "meta"], \
        "每张表都找得到写它的模块(找不到说明这个测试的正则漏了)"


def test_基线没有悄悄过时_记下来的例外现在还是例外():
    """例外清单里的东西如果已经收干净了,要把它从清单里删掉(只许变好:变好了也要记)。"""
    modules = _modules()
    tables = _tables()
    for t, expected in MULTI_WRITER.items():
        now = {n for n, text in modules.items() if n != "db"
               and any(x == t for x in _WRITE.findall(text))}
        assert t in tables and now == expected, \
            f"{t}: 现在是 {sorted(now)},清单里是 {sorted(expected)}"
