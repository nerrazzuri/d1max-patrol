"""站点库升级(RC 加固 H10):从历史上每一个留了快照的库结构升上来,跟全新装的**一模一样**。

快照在 ``fixtures/schema/site-vNN.sql``:那一版的 ``db.py`` 建出来的空库(从 git 历史里生成,
别手改)。
补列、补表、补索引要是只写进了「新建」那一段、忘了写进「升级」那一段(或者反过来),这里就红。

**规矩**:改 ``SCHEMA_VERSION`` 的那个提交,要把改之前那一版的快照加进来(下面有测试看着)。
生成办法:``sqlite3 <那一版建的空库> .dump``,去掉 sqlite_sequence。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from d1max_site.db import SCHEMA_VERSION, SchemaTooNew, SiteDB

FIX = Path(__file__).parent / "fixtures" / "schema"
SNAPSHOTS = {int(re.search(r"v(\d+)", p.name).group(1)): p for p in sorted(FIX.glob("site-v*.sql"))}
#: 从这一版起,每升一版都要留上一版的快照(更早的只留了几个有代表性的)。
EVERY_VERSION_FROM = 54


_LITERAL = re.compile(r"('(?:[^']|'')*')")


def _outside_literals(sql: str, fix) -> str:
    """只动引号**外面**的:字符串常量原样留着(RC 加固复查 M1:原来整段小写、去空白,
    ``CHECK(kind='P1')`` 跟 ``CHECK(kind='p1')`` 比出来一样,它们管的范围其实不同)。"""
    parts = _LITERAL.split(sql)
    return "".join(x if k % 2 else fix(x) for k, x in enumerate(parts))


def _norm(sql: str | None) -> str:
    """关键字、标识符不分大小写,引号外的空白压成一个;字符串常量不动。"""
    return _outside_literals(sql or "", lambda x: re.sub(r"\s+", " ", x).lower()).strip()


def _checks(sql: str | None) -> list[str]:
    """建表语句里的每一条 ``CHECK (...)``(括号配平着取;字符串常量里的括号不算),引号外的空白去掉、
    大小写不分,字符串常量不动。补列带进来的也在这句里。"""
    text, out = sql or "", []
    blank = _LITERAL.sub(lambda m: "'" + "_" * (len(m.group(0)) - 2) + "'", text)   # 等长、没括号
    for m in re.finditer(r"\bCHECK\s*\(", blank, re.I):
        depth, k = 1, m.end()
        while k < len(blank) and depth:
            depth += {"(": 1, ")": -1}.get(blank[k], 0)
            k += 1
        out.append(_outside_literals(text[m.end():k - 1],
                                     lambda x: re.sub(r"\s+", "", x).lower()))
    return sorted(out)


def _shape(path: Path) -> dict[str, object]:
    """库的结构,跟建的先后无关:

    - 每张表的列(名字、类型、非空、默认值、主键位次)、``WITHOUT ROWID`` / ``STRICT``;
    - 索引:盖的列、每一列的排序方向和排序规则、是不是唯一、部分索引的条件;
    - 外键、``CHECK``、列上的 ``COLLATE``、触发器、视图(RC 加固外审 M1:原来只比列和索引盖的列,两个
      只差一个 ``REFERENCES`` 的库比出来一样,「一模一样」说大了)。"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        master = {(r[0], r[1]): r[2] for r in con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        tables = [n for (t, n) in master if t == "table"]
        cols = {t: sorted((c[1], c[2].upper(), c[3], c[4], c[5])
                          for c in con.execute(f"PRAGMA table_info({t})")) for t in tables}
        flags = {r[1]: tuple(r[3:6]) for r in con.execute("PRAGMA table_list")
                 if r[1] in tables}                       # 列数、WITHOUT ROWID、STRICT
        idx = {}
        for t in tables:
            for row in con.execute(f"PRAGMA index_list({t})"):
                name, unique, partial = row[1], row[2], row[4]
                keys = tuple((c[2], c[3], c[4].upper()) for c in con.execute(
                    f"PRAGMA index_xinfo({name})") if c[5])     # 列、是不是倒序、排序规则
                on = tuple(k[0] for k in keys)
                key = name if not name.startswith("sqlite_autoindex") else f"auto:{t}:{on}"
                where = _norm(master.get(("index", name))).partition(" where ")[2]   # 常量原样
                idx[key] = (t, unique, keys, partial, where)
        fks = {t: sorted(tuple(r[2:8]) for r in con.execute(f"PRAGMA foreign_key_list({t})"))
               for t in tables}
        checks = {t: _checks(master[("table", t)]) for t in tables}
        collate = {t: sorted(m.lower() for m in re.findall(
            r"\bCOLLATE\s+(\w+)", master[("table", t)] or "", re.I)) for t in tables}
        other = {f"{t}:{n}": _norm(sql) for (t, n), sql in master.items()
                 if t in ("trigger", "view")}
        return {"cols": cols, "flags": flags, "idx": idx, "fks": fks, "checks": checks,
                "collate": collate, "other": other}
    finally:
        con.close()


def _load(sql: Path, dest: Path) -> None:
    con = sqlite3.connect(dest)
    con.executescript(sql.read_text(encoding="utf-8"))
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def fresh(tmp_path_factory) -> dict[str, object]:
    p = tmp_path_factory.mktemp("fresh") / "s.db"
    SiteDB(p).close()
    return _shape(p)


@pytest.mark.parametrize("version", sorted(SNAPSHOTS))
def test_从老版本升上来_结构跟全新装的一模一样(version, fresh, tmp_path):
    p = tmp_path / "s.db"
    _load(SNAPSHOTS[version], p)
    con = sqlite3.connect(p)
    assert con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == str(version)
    con.close()
    SiteDB(p).close()
    got = _shape(p)
    for table, want in fresh["cols"].items():
        assert got["cols"].get(table) == want, f"从 {version} 升上来,表 {table} 跟新装的不一样"
    assert set(got["cols"]) == set(fresh["cols"]), "多了、少了表"
    assert got["idx"] == fresh["idx"], f"从 {version} 升上来,索引跟新装的不一样"
    con = sqlite3.connect(p)
    assert con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] \
        == str(SCHEMA_VERSION)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


@pytest.mark.parametrize("version", sorted(SNAPSHOTS))
def test_升两遍跟升一遍一样_重启不会再动结构(version, tmp_path):
    p = tmp_path / "s.db"
    _load(SNAPSHOTS[version], p)
    SiteDB(p).close()
    once = _shape(p)
    SiteDB(p).close()
    assert _shape(p) == once


def test_老库里的数据升上来还在_新加的列有默认值(tmp_path):
    p = tmp_path / "s.db"
    _load(SNAPSHOTS[46], p)                     # 46:刚有推送表的那一版
    con = sqlite3.connect(p)
    con.execute("INSERT INTO accounts(name, role, salt, pw_hash, created_at) "
                "VALUES ('old-admin','admin',x'00',x'00',1)")
    con.execute("INSERT INTO push_devices(reg_id, account, platform, updated_ms) "
                "VALUES ('rid','old-admin','android',1)")
    con.commit()
    con.close()
    db = SiteDB(p)
    try:
        assert db.query("SELECT role FROM accounts WHERE name='old-admin'")[0]["role"] == "admin"
        row = db.query("SELECT provider, session FROM push_devices WHERE reg_id='rid'")[0]
        assert (row["provider"], row["session"]) == ("jpush", "")
    finally:
        db.close()


def test_库比程序新_不开_不改它(tmp_path):
    p = tmp_path / "s.db"
    SiteDB(p).close()
    con = sqlite3.connect(p)
    con.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION + 1),))
    con.commit()
    con.close()
    before = _shape(p)
    with pytest.raises(SchemaTooNew):
        SiteDB(p)
    assert _shape(p) == before
    con = sqlite3.connect(p)
    assert con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] \
        == str(SCHEMA_VERSION + 1)
    con.close()


def test_快照没断档_升版本号要留上一版的快照():
    assert min(SNAPSHOTS) <= 40 and len(SNAPSHOTS) >= 8
    missing = [v for v in range(EVERY_VERSION_FROM, SCHEMA_VERSION) if v not in SNAPSHOTS]
    assert not missing, f"缺这几版的快照(升 SCHEMA_VERSION 时要加): {missing}"
    for v, p in SNAPSHOTS.items():
        assert f"VALUES('schema','{v}')" in p.read_text(encoding="utf-8"), p.name


@pytest.mark.parametrize("甲, 乙", [
    ("CREATE TABLE p(id INTEGER PRIMARY KEY); CREATE TABLE c(p INTEGER)",
     "CREATE TABLE p(id INTEGER PRIMARY KEY); CREATE TABLE c(p INTEGER REFERENCES p(id))"),
    ("CREATE TABLE c(p INTEGER REFERENCES x(id))",
     "CREATE TABLE c(p INTEGER REFERENCES x(id) ON DELETE CASCADE)"),
    ("CREATE TABLE t(n INTEGER)", "CREATE TABLE t(n INTEGER CHECK (n >= 0))"),
    ("CREATE TABLE t(n INTEGER CHECK (n >= 0))", "CREATE TABLE t(n INTEGER CHECK (n > 0))"),
    ("CREATE TABLE t(n INTEGER)",
     "CREATE TABLE t(n INTEGER); "
     "CREATE TRIGGER g AFTER INSERT ON t BEGIN UPDATE t SET n = 1; END"),
    ("CREATE TABLE t(n INTEGER); CREATE INDEX i ON t(n)",
     "CREATE TABLE t(n INTEGER); CREATE INDEX i ON t(n) WHERE n IS NOT NULL"),
    ("CREATE TABLE t(n INTEGER); CREATE INDEX i ON t(n)",
     "CREATE TABLE t(n INTEGER); CREATE INDEX i ON t(n DESC)"),
    ("CREATE TABLE t(s TEXT); CREATE INDEX i ON t(s)",
     "CREATE TABLE t(s TEXT); CREATE INDEX i ON t(s COLLATE NOCASE)"),
    ("CREATE TABLE t(s TEXT)", "CREATE TABLE t(s TEXT COLLATE NOCASE)"),
    ("CREATE TABLE t(k TEXT PRIMARY KEY)", "CREATE TABLE t(k TEXT PRIMARY KEY) WITHOUT ROWID"),
    ("CREATE TABLE t(n INTEGER)",
     "CREATE TABLE t(n INTEGER); CREATE VIEW v AS SELECT n FROM t"),
    ("CREATE TABLE t(k TEXT CHECK (k = 'P1'))", "CREATE TABLE t(k TEXT CHECK (k = 'p1'))"),
    ("CREATE TABLE t(k TEXT CHECK (k = 'a b'))", "CREATE TABLE t(k TEXT CHECK (k = 'ab'))"),
    ("CREATE TABLE t(k TEXT CHECK (k != ')' AND k != 'x'))",
     "CREATE TABLE t(k TEXT CHECK (k != ')' AND k != 'X'))"),
    ("CREATE TABLE t(k TEXT); CREATE INDEX i ON t(k) WHERE k = 'P1'",
     "CREATE TABLE t(k TEXT); CREATE INDEX i ON t(k) WHERE k = 'p1'"),
    ("CREATE TABLE t(k TEXT); CREATE TRIGGER g AFTER INSERT ON t BEGIN UPDATE t SET k = 'A'; END",
     "CREATE TABLE t(k TEXT); CREATE TRIGGER g AFTER INSERT ON t BEGIN UPDATE t SET k = 'a'; END"),
], ids=["外键", "外键的删除规则", "CHECK 有没有", "CHECK 的内容", "触发器", "部分索引的条件",
        "索引倒序", "索引的排序规则", "列的排序规则", "WITHOUT ROWID", "视图",
        "CHECK 里常量的大小写", "CHECK 里常量的空格", "CHECK 里常量带括号",
        "部分索引条件里常量的大小写", "触发器里常量的大小写"])
def test_外审M1_结构比较认得出这些差别(tmp_path, 甲, 乙):
    """反例:两个库只差这一样,比出来要不一样。原来只比列和索引盖的列,这些全看不出。"""
    shapes = []
    for i, sql in enumerate((甲, 乙)):
        path = tmp_path / f"{i}.db"
        con = sqlite3.connect(path)
        con.executescript(sql)
        con.commit()
        con.close()
        shapes.append(_shape(path))
    assert shapes[0] != shapes[1]


def test_外审M1_写法不同但结构一样的_比出来一样(tmp_path):
    """先建表再补列(升级走的路)跟一次建全(新装走的路):语句长得不一样,结构要算一样。"""
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    con = sqlite3.connect(a)
    con.executescript("CREATE TABLE t(k TEXT PRIMARY KEY, n INTEGER CHECK (n >= 0));"
                      "CREATE INDEX i ON t(n)")
    con.close()
    con = sqlite3.connect(b)
    con.executescript("create table t(k   TEXT PRIMARY KEY);"
                      "ALTER TABLE t ADD COLUMN n INTEGER   check (N>=0);"
                      "CREATE INDEX i ON t(n)")
    con.close()
    assert _shape(a) == _shape(b)
