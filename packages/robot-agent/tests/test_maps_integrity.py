"""W09g 正在用的图的完整性:``active()`` 只读声明;``verify_active()`` 完整校验(清单跟声明一样、
每个文件是普通文件、大小、sha256);``install()`` 同一版本也逐个核,坏了重新下。"""

from __future__ import annotations

import json
import os

import pytest
from test_maps_keeper import 站点

from d1max_agent import maps as M
from d1max_agent.maps import MapInstallError, MapIntegrityError, MapKeeper

FILES = {"prior.mm": bytes(range(256)) * 8, "frames.json": b'{"up":[0,0,1]}', "x.pgm": b"P5 map"}


@pytest.fixture
def k(tmp_path):
    s = 站点()
    keeper = MapKeeper(tmp_path / "maps", fetch=s.fetch, sleep=lambda _: None)
    ref = s.add("estate-1", "7", FILES)
    keeper.install(ref)
    keeper.commit(ref)
    return keeper, s, ref, tmp_path / "maps" / "estate-1" / "7"


def _flip(p, at=100):
    """同尺寸改一个字节(盘坏、断电写坏):大小核不出来。"""
    b = bytearray(p.read_bytes())
    b[at] ^= 0xFF
    p.write_bytes(bytes(b))


def test_好的过_回声明(k):
    keeper, _, ref, _ = k
    assert keeper.verify_active() == ref


def test_没有_active_json_回_None(tmp_path):
    keeper = MapKeeper(tmp_path / "maps", fetch=lambda *a, **kw: iter(()))
    assert keeper.verify_active() is None and keeper.declared() is None and keeper.active() is None


def test_同尺寸内容坏了_校验不过_说是哪个文件(k):
    keeper, _, ref, d = k
    _flip(d / "prior.mm")
    assert (d / "prior.mm").stat().st_size == len(FILES["prior.mm"])
    with pytest.raises(MapIntegrityError, match="prior.mm.*sha256"):
        keeper.verify_active()
    assert keeper.active() == ref, "声明照旧(轻量读取不碰内容)"


@pytest.mark.parametrize("how", ["截短", "删掉", "换成目录"])
def test_文件缺了短了不是文件_校验不过(k, how):
    keeper, _, _, d = k
    p = d / "frames.json"
    if how == "截短":
        p.write_bytes(p.read_bytes()[:-1])
    elif how == "删掉":
        p.unlink()
    else:
        p.unlink()
        p.mkdir()
    with pytest.raises(MapIntegrityError, match="frames.json"):
        keeper.verify_active()


def test_文件换成符号链接_内容一样也不认(k, tmp_path):
    keeper, _, _, d = k
    other = tmp_path / "elsewhere.pgm"
    other.write_bytes(FILES["x.pgm"])
    (d / "x.pgm").unlink()
    os.symlink(other, d / "x.pgm")
    with pytest.raises(MapIntegrityError, match="x.pgm.*普通文件"):
        keeper.verify_active()


def test_版本目录换成符号链接_不认(k, tmp_path):
    keeper, _, _, d = k
    moved = tmp_path / "moved"
    d.rename(moved)
    os.symlink(moved, d)
    with pytest.raises(MapIntegrityError, match="符号链接"):
        keeper.verify_active()


def test_目录里的清单跟声明不一样_不认(k):
    keeper, _, _, d = k
    man = json.loads((d / "map.json").read_text())
    man["files"] = [f for f in man["files"] if f["name"] != "x.pgm"]   # 清单里少一个
    (d / "map.json").write_text(json.dumps(man))
    with pytest.raises(MapIntegrityError, match="清单"):
        keeper.verify_active()


@pytest.mark.parametrize("bad", ["", "不是 JSON", '{"map_id": 1}'])
def test_清单坏了_不认(k, bad):
    keeper, _, _, d = k
    (d / "map.json").write_text(bad)
    with pytest.raises(MapIntegrityError, match="清单"):
        keeper.verify_active()


def test_清单没了_不认(k):
    keeper, _, _, d = k
    (d / "map.json").unlink()
    with pytest.raises(MapIntegrityError, match="清单"):
        keeper.verify_active()


@pytest.mark.parametrize("bad", ["", "{", '{"map_id": "../x", "version": "1", "files": []}'])
def test_active_json_坏了_声明读得出坏_轻量读取当没有(k, tmp_path, bad):
    keeper, _, _, _ = k
    (tmp_path / "maps" / "active.json").write_text(bad)
    assert keeper.active() is None
    with pytest.raises(MapIntegrityError, match="active.json"):
        keeper.declared()
    with pytest.raises(MapIntegrityError, match="active.json"):
        keeper.verify_active()


def test_轻量读取不碰文件内容(k, monkeypatch):
    keeper, _, ref, d = k
    opened = []
    real = M._sha256
    monkeypatch.setattr(M, "_sha256", lambda p: (opened.append(p), real(p))[1])
    for _ in range(5):
        assert keeper.active() == ref
    assert opened == []
    keeper.verify_active()
    assert len(opened) == len(FILES)


def test_本地库也不认符号链接(k, tmp_path):
    keeper, _, ref, d = k
    other = tmp_path / "elsewhere.pgm"
    other.write_bytes(FILES["x.pgm"])
    (d / "x.pgm").unlink()
    os.symlink(other, d / "x.pgm")
    assert keeper.local(ref) is None


def test_同一版本再装_内容坏了就重新下_修好(k):
    """原来「正在用的就是这一版、清单一样」直接返回,不核内容 —— 站点重新下发也修不了。"""
    keeper, site, ref, d = k
    _flip(d / "prior.mm")
    site.calls.clear()
    assert keeper.install(ref) == d
    assert (d / "prior.mm").read_bytes() == FILES["prior.mm"]
    assert {c[0] for c in site.calls} == set(FILES), "重新下了"
    assert keeper.verify_active() == ref


def test_同一版本再装_好的不下(k):
    keeper, site, ref, _ = k
    site.calls.clear()
    keeper.install(ref)
    assert site.calls == []


def test_同一版本重新下失败_原来那份还在(k):
    keeper, site, ref, d = k
    _flip(d / "prior.mm")
    broken = (d / "prior.mm").read_bytes()
    site.fail = True
    with pytest.raises(MapInstallError):
        keeper.install(ref)
    assert (d / "prior.mm").read_bytes() == broken and keeper.active() == ref
