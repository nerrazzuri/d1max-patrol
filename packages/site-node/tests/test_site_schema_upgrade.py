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


def _shape(path: Path) -> dict[str, object]:
    """库的结构:每张表的列(名字、类型、非空、默认值、主键位次)、索引和它盖的列。跟建的先后无关。"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        cols = {t: sorted((c[1], c[2].upper(), c[3], c[4], c[5])
                          for c in con.execute(f"PRAGMA table_info({t})")) for t in tables}
        idx = {}
        for t in tables:
            for row in con.execute(f"PRAGMA index_list({t})"):
                name, unique = row[1], row[2]
                on = tuple(c[2] for c in con.execute(f"PRAGMA index_info({name})"))
                key = name if not name.startswith("sqlite_autoindex") else f"auto:{t}:{on}"
                idx[key] = (t, unique, on)
        return {"cols": cols, "idx": idx}
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
