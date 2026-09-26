"""W00c5d 第三部分:站点的发布目录。登记前核对包内指纹、名字;打成 tar.gz(只带普通文件与目录)。"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile

import pytest

from d1max_contract.digest import tree_sha256
from d1max_site.db import SiteDB
from d1max_site.releases import ReleaseCatalog, ReleaseCatalogError

NAME = "2026-09-25-bbbbbb"


def 做包(tmp, name=NAME, *, sha=None, schema=1):
    d = tmp / "pkgs" / name
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "src" / "x.py").write_text("print('hi')\n")
    (d / "deploy").mkdir(exist_ok=True)
    (d / "deploy" / "d1max-agent-start").write_text("#!/bin/sh\n")
    (d / "deploy" / "d1max-agent-start").chmod(0o755)
    (d / "release.json").write_text(json.dumps({
        "name": name, "version": "0.9", "requires_mission_schema": schema,
        "content_sha256": sha or tree_sha256(d, skip="release.json")}))
    return d


@pytest.fixture
def cat(tmp_path):
    return ReleaseCatalog(tmp_path / "site", SiteDB(tmp_path / "site.db"), now_ms=lambda: 7)


def test_登记一版_打成tar_gz_大小哈希对得上_同一版不许再来(cat, tmp_path):
    pkg = 做包(tmp_path)
    row = cat.add(pkg, note="夜巡修了一个 bug")
    path = cat.file_path(NAME)
    assert row["size"] == path.stat().st_size
    assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with tarfile.open(path) as tf:
        assert f"{NAME}/src/x.py" in tf.getnames()
    assert cat.get(NAME).size == row["size"] and cat.list()[0]["note"] == "夜巡修了一个 bug"
    with pytest.raises(ReleaseCatalogError):
        cat.add(pkg)


def test_包里有链接不登记_狗那头解开会对不上指纹(cat, tmp_path):
    pkg = 做包(tmp_path, "2026-09-25-eeeeee")
    os.symlink("/etc/passwd", pkg / "src" / "link")
    (pkg / "release.json").write_text(json.dumps({
        "name": pkg.name, "version": "1", "content_sha256": tree_sha256(pkg, skip="release.json")}))
    with pytest.raises(ReleaseCatalogError):
        cat.add(pkg)


def test_包里有硬链接也不登记(cat, tmp_path):
    """硬链接打进 tar 是一条链接条目,狗那头不收,指纹永远对不上(W00c5d 第三部分内部评审)。"""
    pkg = 做包(tmp_path, "2026-09-25-ffffff")
    os.link(pkg / "src" / "x.py", pkg / "src" / "y.py")
    (pkg / "release.json").write_text(json.dumps({
        "name": pkg.name, "version": "1", "content_sha256": tree_sha256(pkg, skip="release.json")}))
    with pytest.raises(ReleaseCatalogError, match="链接"):
        cat.add(pkg)


def test_指纹不对_名字不对_不是发布包_都不登记(cat, tmp_path):
    with pytest.raises(ReleaseCatalogError):
        cat.add(做包(tmp_path, sha="0" * 64))
    other = 做包(tmp_path, "2026-09-25-cccccc")
    os.rename(other, other.with_name("2026-09-25-dddddd"))
    with pytest.raises(ReleaseCatalogError):
        cat.add(other.with_name("2026-09-25-dddddd"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(ReleaseCatalogError):
        cat.add(tmp_path / "empty")
    for bad in ("../x", "2026-09-25-bbbbbb"):
        with pytest.raises(ReleaseCatalogError):
            cat.file_path(bad)


def test_老服务那一代的包不登记_狗上的代理装了也切不过去(cat, tmp_path):
    """W00c5 修复内部评审:站点登记时就拒,别让管理员下发了才在狗那头失败。"""
    pkg = 做包(tmp_path)
    (pkg / "deploy" / "d1max-agent-start").unlink()
    (pkg / "release.json").write_text(json.dumps({
        "name": pkg.name, "version": "1", "content_sha256": tree_sha256(pkg, skip="release.json")}))
    with pytest.raises(ReleaseCatalogError, match="启动脚本"):
        cat.add(pkg)
    assert cat.list() == []



def test_登记时记下新版要的任务包schema_列表里有(cat, tmp_path):
    """W00c6d:升级前检查要拿它跟站点当前任务包的 schema 比。"""
    cat.add(做包(tmp_path, schema=3))
    assert cat.list()[0]["requires_mission_schema"] == 3
    assert cat.requires_mission_schema(NAME) == 3


@pytest.mark.parametrize("bad", ["2", 0, -1, True, None])
def test_要的schema不像话_登记拒(cat, tmp_path, bad):
    import json as _json
    pkg = 做包(tmp_path)
    raw = _json.loads((pkg / "release.json").read_text())
    raw["requires_mission_schema"] = bad
    (pkg / "release.json").write_text(_json.dumps(raw))
    with pytest.raises(ReleaseCatalogError, match="requires_mission_schema"):
        cat.add(pkg)


def test_老库没有这一列_补上_老版本当1(tmp_path):
    import sqlite3
    db_path = tmp_path / "old.db"
    c = sqlite3.connect(db_path)
    c.execute("CREATE TABLE releases (name TEXT PRIMARY KEY, version TEXT NOT NULL, sha256 TEXT "
              "NOT NULL, size INTEGER NOT NULL, created_ms INTEGER NOT NULL, note TEXT NOT NULL "
              "DEFAULT '')")
    c.execute("INSERT INTO releases VALUES ('2026-09-20-aaaaaa', '0.9', 'x', 1, 1, '')")
    c.commit()
    c.close()
    cat = ReleaseCatalog(tmp_path / "site", SiteDB(db_path), now_ms=lambda: 7)
    assert cat.requires_mission_schema("2026-09-20-aaaaaa") == 1


def test_老库的任务包表也补上schema列(tmp_path):
    import sqlite3
    db_path = tmp_path / "old2.db"
    c = sqlite3.connect(db_path)
    c.execute("CREATE TABLE bundles (bundle_id TEXT NOT NULL, version INTEGER NOT NULL, "
              "content_sha256 TEXT NOT NULL, timezone TEXT NOT NULL, schedule TEXT NOT NULL, "
              "imported_at INTEGER NOT NULL, imported_by TEXT NOT NULL, active INTEGER NOT NULL "
              "DEFAULT 0, PRIMARY KEY (bundle_id, version))")
    c.execute("INSERT INTO bundles VALUES ('b', 1, 'x', 'UTC', '{}', 1, 'a', 1)")
    c.commit()
    c.close()
    from d1max_site.catalog import active_bundle_schema
    assert active_bundle_schema(SiteDB(db_path)) == ("b v1", 1)
