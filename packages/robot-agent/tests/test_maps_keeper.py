"""W00c5d 第二部分:狗上正在用的那一张图。下载边下边核,对不上整张不装;
载入成功才换正在用的,别的删掉。"""

from __future__ import annotations

import hashlib
import json

import pytest

from d1max_agent.maps import MapInstallError, MapKeeper
from d1max_contract.maps import MapRef


def _ref(map_id, version, files):
    return MapRef.from_wire({"map_id": map_id, "version": version, "files": [
        {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
        for n, d in files.items()]})


class 站点:
    def __init__(self):
        self.files: dict[tuple[str, str, str], bytes] = {}
        self.fail = False

    def add(self, map_id, version, files):
        for n, d in files.items():
            self.files[(map_id, version, n)] = d
        return _ref(map_id, version, files)

    def fetch(self, map_id, version, name):
        if self.fail:
            raise ConnectionError("站点连不上")
        data = self.files[(map_id, version, name)]
        for i in range(0, len(data), 3):
            yield data[i:i + 3]


@pytest.fixture
def k(tmp_path):
    s = 站点()
    return MapKeeper(tmp_path / "maps", fetch=s.fetch), s


def test_装好_提交_重启还认得_别的图删掉(k, tmp_path):
    keeper, site = k
    a = site.add("estate-1", "7", {"a.pgm": b"old map", "home.json": b'{"x":1,"y":2,"yaw":0.5}'})
    keeper.install(a)
    keeper.commit(a)
    assert keeper.active() == a and keeper.home_of(a) == (1.0, 2.0, 0.5)
    b = site.add("estate-1", "8", {"a.pgm": b"new map!"})
    d = keeper.install(b)
    assert (d / "a.pgm").read_bytes() == b"new map!"
    assert keeper.active() == a, "装好不等于换了:载入成功才提交"
    keeper.commit(b)
    again = MapKeeper(tmp_path / "maps", fetch=site.fetch)
    assert again.active() == b
    assert not (tmp_path / "maps" / "estate-1" / "7").exists(), "狗上只留正在用的那一张"
    assert again.home_of(b) is None


def test_哈希或大小对不上_整张不装_照旧用原来的(k, tmp_path):
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"good"})
    keeper.install(a)
    keeper.commit(a)
    b = site.add("m", "2", {"x.pgm": b"good2", "y.yaml": b"y"})
    site.files[("m", "2", "y.yaml")] = b"z"               # 路上坏了一个字节
    with pytest.raises(MapInstallError):
        keeper.install(b)
    assert not (tmp_path / "maps" / "m" / "2").exists() and keeper.active() == a
    site.files[("m", "2", "y.yaml")] = b"yy"              # 比清单大
    with pytest.raises(MapInstallError):
        keeper.install(b)
    site.fail = True
    with pytest.raises(MapInstallError):
        keeper.install(site.add("m", "3", {"x.pgm": b"3"}))
    assert keeper.active() == a


def test_载不进去就扔掉_正在用的缺了文件就当没有(k, tmp_path):
    keeper, site = k
    a = site.add("m", "1", {"x.pgm": b"good"})
    keeper.install(a)
    keeper.commit(a)
    b = site.add("m", "2", {"x.pgm": b"bad"})
    keeper.install(b)
    keeper.discard(b)
    assert not (tmp_path / "maps" / "m" / "2").exists() and keeper.active() == a
    (tmp_path / "maps" / "m" / "1" / "x.pgm").unlink()
    assert keeper.active() is None
    (tmp_path / "maps" / "active.json").write_text(json.dumps({"map_id": "../x"}))
    assert keeper.active() is None
