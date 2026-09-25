"""W00c5d 第二部分:站点的地图目录。同一个地图号 + 版本只登记一次;狗传上来的收齐、哈希对上才登记。"""

from __future__ import annotations

import hashlib
import json

import pytest

from d1max_site.db import SiteDB
from d1max_site.maps import MapCatalog, MapError


@pytest.fixture
def cat(tmp_path):
    return MapCatalog(tmp_path, SiteDB(tmp_path / "site.db"), now_ms=lambda: 1000)


def _files(d, **files):
    d.mkdir(parents=True, exist_ok=True)
    for n, data in files.items():
        (d / n).write_bytes(data)
    return d


def test_导入一个目录_登记_文件带哈希_同版本不许再来(cat, tmp_path):
    src = _files(tmp_path / "src", **{"estate-1.pgm": b"P5 map", "estate-1.yaml": b"res: 0.05"})
    ref = cat.import_dir(src, map_id="estate-1", version="8")
    assert [f.name for f in ref.files] == ["estate-1.pgm", "estate-1.yaml"]
    assert ref.files[0].sha256 == hashlib.sha256(b"P5 map").hexdigest()
    assert cat.get("estate-1", "8") == ref
    assert cat.list()[0]["source"] == "import"
    assert cat.file_path("estate-1", "8", "estate-1.pgm").read_bytes() == b"P5 map"
    with pytest.raises(MapError):
        cat.import_dir(src, map_id="estate-1", version="8")
    for bad in (("estate-1", "8", "map.json"), ("estate-1", "8", "../site.db"),
                ("estate-1", "9", "estate-1.pgm"), ("../x", "8", "a")):
        with pytest.raises(MapError):
            cat.file_path(*bad)


def test_狗传上来的图_收齐而且哈希对上才登记(cat):
    d = cat.incoming / "A" / "estate-1" / "9"
    pgm = b"P5 new map"
    ref = {"map_id": "estate-1", "version": "9",
           "files": [{"name": "estate-1.pgm", "size": len(pgm),
                      "sha256": hashlib.sha256(pgm).hexdigest()}]}
    assert cat.take_uploaded("A", "estate-1", "9") is None, "清单还没到"
    _files(d, **{"map.json": json.dumps(ref).encode()})
    assert cat.take_uploaded("A", "estate-1", "9") is None, "文件还没到"
    _files(d, **{"estate-1.pgm": pgm[:-1]})
    assert cat.take_uploaded("A", "estate-1", "9") is None, "还在传(大小不对)"
    _files(d, **{"estate-1.pgm": pgm})
    got = cat.take_uploaded("A", "estate-1", "9")
    assert got is not None and cat.list()[0]["source"] == "A"
    assert cat.file_path("estate-1", "9", "estate-1.pgm").read_bytes() == pgm
    wrong = cat.incoming / "A" / "estate-1" / "10"
    _files(wrong, **{"map.json": json.dumps(ref).encode()})
    with pytest.raises(MapError):
        cat.take_uploaded("A", "estate-1", "10")
