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


def 做包(tmp, name=NAME, *, sha=None):
    d = tmp / "pkgs" / name
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "src" / "x.py").write_text("print('hi')\n")
    (d / "release.json").write_text(json.dumps({
        "name": name, "version": "0.9", "requires_mission_schema": 1,
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
