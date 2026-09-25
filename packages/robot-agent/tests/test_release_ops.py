"""W00c5d 第三部分:发布经站点,狗这头的下载、核对、落槽、建 venv、切、退、提交。
包是现做的(一棵小树 + release.json 里的指纹),下载是假的,特权助手与重启也是假的。"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from d1max_agent.engine import release as rel
from d1max_agent.release_ops import SENTINEL, ReleaseOpError, ReleaseOps
from d1max_contract.releases import ReleaseRef

OLD, NEW = "2026-09-20-aaaaaa", "2026-09-25-bbbbbb"


def _包(tmp: Path, name: str, *, extra: dict[str, bytes] | None = None) -> Path:
    d = tmp / "src" / name
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "src" / "x.py").write_text(f"VERSION = {name!r}\n")
    (d / "deploy").mkdir(exist_ok=True)
    (d / "deploy" / "d1max-agent.service").write_text("[Unit]\n")
    for k, v in (extra or {}).items():
        (d / k).write_bytes(v)
    (d / "release.json").write_text(json.dumps({
        "name": name, "version": "0.9", "content_sha256": rel.tree_sha256(d),
        "requires_mission_schema": 1, "built_at": "2026-09-25T00:00:00Z"}))
    return d


def _tar(pkg: Path, *, evil: list[tarfile.TarInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(pkg, arcname=pkg.name)
        for ti in evil or []:
            tf.addfile(ti, io.BytesIO(b"x" * ti.size) if ti.isfile() else None)
    return buf.getvalue()


class 假助手:
    def __init__(self):
        self.units = []
        self.fail = ""

    def install_agent_unit(self, name):
        if self.fail:
            raise RuntimeError(self.fail)
        self.units.append(name)
        return "installed"


@pytest.fixture
def ops(tmp_path):
    layout = rel.Layout(root=tmp_path / "opt")
    old = _包(tmp_path, OLD)
    rel.stage(layout, old, now_ms=1)
    rel._point_current(layout, OLD)
    served: dict[str, bytes] = {}
    restarts = []
    built = []

    def build(pkg, slot):
        built.append((pkg.name, slot.name))
        (slot / "venv").mkdir(parents=True, exist_ok=True)
        (slot / "venv" / SENTINEL).write_text("ok")
    o = ReleaseOps(layout, fetch=lambda name: iter([served[name]]), privileged=假助手(),
                   build=build, work=tmp_path / "work", now_ms=lambda: 5,
                   restart=lambda: restarts.append(1))
    return o, served, restarts, built, layout


def _ref(served, name, data):
    served[name] = data
    return ReleaseRef(name=name, sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def test_装_切_起来提交(ops, tmp_path):
    o, served, restarts, built, layout = ops
    ref = _ref(served, NEW, _tar(_包(tmp_path, NEW)))
    o.install(ref)
    assert o.ready(NEW) and built == [(NEW, NEW)]
    assert not any((tmp_path / "work").iterdir()), "下载、解开的临时东西收掉"
    assert o.current() == OLD, "装不等于切"
    got = o.activate(NEW)
    assert o.current() == NEW and restarts == [1] and o.privileged.units == [NEW]
    assert got["pending"]["to"] == NEW and rel.read_pending(layout) is not None
    assert o.commit_if_pending() == NEW, "代理起来、连上站点:提交"
    assert rel.read_pending(layout) is None and o.commit_if_pending() is None
    o.install(ref)                                          # 再装一遍:已经好了,不重下
    assert built == [(NEW, NEW)]


def test_切了之后退回上一版(ops, tmp_path):
    o, served, restarts, built, layout = ops
    o.install(_ref(served, NEW, _tar(_包(tmp_path, NEW))))
    o.activate(NEW)
    assert o.rollback() == OLD and o.current() == OLD and restarts == [1, 1]
    with pytest.raises(ReleaseOpError):
        o.rollback()                                        # 没有在途的那次升级了


def test_哈希不对_包比说的大_包内指纹不对_都不落槽(ops, tmp_path):
    o, served, restarts, built, layout = ops
    data = _tar(_包(tmp_path, NEW))
    served[NEW] = data
    for ref in (ReleaseRef(name=NEW, sha256="0" * 64, size=len(data)),
                ReleaseRef(name=NEW, sha256=hashlib.sha256(data).hexdigest(), size=len(data) - 1)):
        with pytest.raises(ReleaseOpError):
            o.install(ref)
    pkg = _包(tmp_path, NEW)
    (pkg / "src" / "x.py").write_text("被改过\n")            # 指纹跟 release.json 对不上
    with pytest.raises(ReleaseOpError):
        o.install(_ref(served, NEW, _tar(pkg)))
    assert not layout.release_dir(NEW).exists() and built == []


@pytest.mark.parametrize("evil", ["../../etc/x", "/abs", "link"])
def test_包里的坏路径_链接_一律拒(ops, tmp_path, evil):
    o, served, restarts, built, layout = ops
    ti = tarfile.TarInfo(evil if evil != "link" else f"{NEW}/src/l")
    if evil == "link":
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/shadow"
    else:
        ti.size = 1
    with pytest.raises(ReleaseOpError):
        o.install(_ref(served, NEW, _tar(_包(tmp_path, NEW), evil=[ti])))
    assert not layout.release_dir(NEW).exists()
    assert not (tmp_path / "etc").exists()


def test_没装好不切_装单元失败不切(ops, tmp_path):
    o, served, restarts, built, layout = ops
    with pytest.raises(ReleaseOpError):
        o.activate(NEW)
    o.install(_ref(served, NEW, _tar(_包(tmp_path, NEW))))
    o.privileged.fail = "单元校验没过"
    with pytest.raises(ReleaseOpError):
        o.activate(NEW)
    assert o.current() == OLD and restarts == [] and rel.read_pending(layout) is None
