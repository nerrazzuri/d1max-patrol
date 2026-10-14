"""R5:狗上留有限的几张(正在用的、上一张、最近自己建的),回退不用重新下载。"""

from __future__ import annotations

import hashlib

import pytest

from d1max_agent.maps import MapInstallError, MapKeeper
from d1max_contract.maps import MapRef, map_digest


def _ref(map_id, version, files):
    return MapRef.from_wire({"map_id": map_id, "version": version, "files": [
        {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
        for n, d in files.items()]})


class 站点:
    def __init__(self):
        self.files: dict[tuple[str, str, str], bytes] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.down = False

    def add(self, map_id, version, files):
        for n, d in files.items():
            self.files[(map_id, version, n)] = d
        return _ref(map_id, version, files)

    def fetch(self, map_id, version, name, offset=0):
        self.calls.append((map_id, version, name))
        if self.down:
            raise ConnectionError("站点连不上")
        yield self.files[(map_id, version, name)][offset:]


@pytest.fixture
def k(tmp_path):
    s = 站点()
    return MapKeeper(tmp_path / "maps", fetch=s.fetch, sleep=lambda _: None), s, tmp_path / "maps"


def _dirs(root):
    return sorted(f"{m.name}/{v.name}" for m in root.iterdir() if m.is_dir() and m.name[0] != "."
                  for v in m.iterdir() if v.is_dir() and v.name[0] != ".")


def _activate(keeper, ref, **kw):
    keeper.install(ref)
    keeper.commit(ref, **kw)


def test_换图后上一张还在_回退不下载一个字节(k):
    keeper, site, root = k
    a = site.add("m", "1", {"x.pgm": b"one"})
    b = site.add("m", "2", {"x.pgm": b"two"})
    _activate(keeper, a)
    _activate(keeper, b)
    assert keeper.active() == b and keeper.previous() == a
    site.calls.clear()
    site.down = True                                   # 站点连不上也照样回退
    _activate(keeper, a)
    assert site.calls == [], "回退不该去站点下载"
    assert keeper.active() == a and keeper.previous() == b, "回退后,刚换下来的是『上一张』"


def test_磁盘有界_换很多次最多留三张(k):
    keeper, site, root = k
    refs = [site.add("m", str(i), {"x.pgm": f"v{i}".encode()}) for i in range(6)]
    for r in refs:
        _activate(keeper, r)
    assert _dirs(root) == ["m/4", "m/5"], "没有自己建的:正在用的 + 上一张"
    # 自己建的(收养)一份再多留一份:最多三张
    built = site.add("m", "built", {"x.pgm": b"built"})
    src = root.parent / "built-src"
    src.mkdir()
    (src / "x.pgm").write_bytes(b"built")
    keeper.adopt(src, built)
    for i in range(6, 9):
        _activate(keeper, site.add("m", str(i), {"x.pgm": f"v{i}".encode()}))
    got = _dirs(root)
    assert got == ["m/7", "m/8", "m/built"], got
    assert keeper.kept() == [("m", "7"), ("m", "8"), ("m", "built")]


def test_上一张被删或改坏_previous回None_不假装能回退(k):
    keeper, site, root = k
    a = site.add("m", "1", {"x.pgm": b"one"})
    b = site.add("m", "2", {"x.pgm": b"two"})
    _activate(keeper, a)
    _activate(keeper, b)
    assert keeper.previous() == a
    (root / "m" / "1" / "map.json").unlink()
    assert keeper.previous() is None


def test_指定previous为None_不留上一张_回退善后用(k):
    keeper, site, root = k
    a = site.add("m", "1", {"x.pgm": b"one"})
    b = site.add("m", "2", {"x.pgm": b"two"})
    c = site.add("m", "3", {"x.pgm": b"three"})
    _activate(keeper, a)
    _activate(keeper, b)
    _activate(keeper, c)                               # 当前 3,上一张 2
    keeper.commit(b, previous=None)                    # 3 没通过定位器:退回 2,不留 3
    assert keeper.active() == b and keeper.previous() is None
    assert _dirs(root) == ["m/2"]


def test_discard不删上一张(k):
    keeper, site, root = k
    a = site.add("m", "1", {"x.pgm": b"one"})
    b = site.add("m", "2", {"x.pgm": b"two"})
    _activate(keeper, a)
    _activate(keeper, b)
    keeper.discard(a)                                  # 上一张:回退要用,不删
    assert keeper.previous() == a and (root / "m" / "1").exists()


def test_地图摘要_只看清单内容_跟版本号无关_改一个字节就变():
    a = _ref("m", "1", {"x.pgm": b"one", "y.yaml": b"yy"})
    b = _ref("m", "2", {"y.yaml": b"yy", "x.pgm": b"one"})        # 换版本号、换顺序
    c = _ref("m", "1", {"x.pgm": b"onE", "y.yaml": b"yy"})
    assert map_digest(a) == map_digest(b) != map_digest(c)
    assert len(map_digest(a)) == 64


def test_下载失败不动当前和上一张(k):
    keeper, site, root = k
    a = site.add("m", "1", {"x.pgm": b"one"})
    b = site.add("m", "2", {"x.pgm": b"two"})
    _activate(keeper, a)
    _activate(keeper, b)
    site.down = True
    with pytest.raises(MapInstallError):
        keeper.install(site.add("m", "3", {"x.pgm": b"three"}))
    assert keeper.active() == b and keeper.previous() == a
