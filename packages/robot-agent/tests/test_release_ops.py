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
    free = {"b": 10 ** 12}
    o = ReleaseOps(layout, fetch=lambda name: iter([served[name]]),
                   build=build, work=tmp_path / "work", now_ms=lambda: 5,
                   restart=lambda: restarts.append(1), disk_free=lambda p: free["b"])
    o.free = free
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
    assert o.current() == NEW and restarts == [1]
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
    assert rel.read_pending(layout) is None


def test_提交之后站点照样能退回上一版_一样连上才算(ops, tmp_path):
    """W00c5d 第三部分内部评审:新版连上站点就提交了,之后站点点「退」要能退(切回盘上上一版)。"""
    o, served, restarts, built, layout = ops
    (layout.release_dir(OLD) / "venv").mkdir(parents=True, exist_ok=True)
    (layout.release_dir(OLD) / "venv" / SENTINEL).write_text("ok")
    o.install(_ref(served, NEW, _tar(_包(tmp_path, NEW))))
    o.activate(NEW)
    assert o.commit_if_pending() == NEW and rel.read_pending(layout) is None
    assert o.previous() == OLD
    assert o.rollback() == OLD and o.current() == OLD and restarts == [1, 1]
    p = rel.read_pending(layout)
    assert p is not None and (p.to, p.src) == (OLD, NEW), "退回去的那一版也要连上才算"
    assert o.commit_if_pending() == OLD
    assert o.previous() is None
    with pytest.raises(ReleaseOpError, match="没有"):
        o.rollback()


def test_切到正在跑的那版_直接拒(ops, tmp_path):
    o, served, restarts, built, layout = ops
    (layout.release_dir(OLD) / "venv").mkdir(parents=True, exist_ok=True)
    (layout.release_dir(OLD) / "venv" / SENTINEL).write_text("ok")
    with pytest.raises(ReleaseOpError, match="在跑"):          # engine.release 自己就拒
        o.activate(OLD)
    assert restarts == [] and rel.read_pending(layout) is None


def test_盘够不够_量的是双槽那块盘(ops, tmp_path):
    o, served, restarts, built, layout = ops
    assert o.disk_ok(10 ** 6)
    o.free["b"] = 4 * 10 ** 6 + 100 * 1024 ** 2
    assert not o.disk_ok(10 ** 6), "包的 4 倍再加 512 MiB 余量"
    assert not o.disk_ok(0), "只切、只退也要留 512 MiB(在途标记、幂等记录、事件簿)"
    o.free["b"] = 600 * 1024 ** 2
    assert o.disk_ok(0)


def test_装了好几版不切_多的删掉_在跑的上一版刚装的留着(ops, tmp_path):
    o, served, restarts, built, layout = ops
    names = ["2026-09-22-dddddd", "2026-09-23-eeeeee", "2026-09-21-cccccc"]  # 最后装的名字最旧
    for n in names:
        o.install(_ref(served, n, _tar(_包(tmp_path, n))))
        assert n in set(rel.installed(layout)), "刚装的不许被删"
    left = set(rel.installed(layout))
    assert OLD in left and names[-1] in left, "在跑的、刚装的都留着"
    assert len(left) <= rel.KEEP_RELEASES + 1


def test_上一版是比在跑的旧的里面最新的那一版(ops, tmp_path):
    o, served, restarts, built, layout = ops
    older, newest = "2026-09-10-aaaaaa", "2026-09-25-bbbbbb"
    for n in (older, newest):
        o.install(_ref(served, n, _tar(_包(tmp_path, n))))
    (layout.release_dir(OLD) / "venv").mkdir(parents=True, exist_ok=True)
    (layout.release_dir(OLD) / "venv" / SENTINEL).write_text("ok")
    o.activate(newest)
    assert o.previous() == OLD, "不是最旧的那一版"


def test_起来时清掉上次装到一半的临时文件(tmp_path):
    work = tmp_path / "work"
    (work / "x.x").mkdir(parents=True)
    (work / "x.tar.gz").write_bytes(b"half")
    ReleaseOps(rel.Layout(root=tmp_path / "opt"), fetch=lambda n: iter([]),
               build=lambda p, s: None, work=work, now_ms=lambda: 1, restart=lambda: None)
    assert not work.exists() or not any(work.iterdir())


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


@pytest.mark.parametrize("evil", ["../../etc/x", "/abs", "link", f"{NEW}/src/../src/y.py",
                                  f"{NEW}/src\\y.py"])
def test_包里的坏路径_链接_一律拒(ops, tmp_path, evil):
    """``..`` 一律不许(哪怕绕回里面)、反斜杠不许:站点打的包里不会有这种名字,有就是被动过。"""
    o, served, restarts, built, layout = ops
    ti = tarfile.TarInfo(evil if evil != "link" else f"{NEW}/src/l")
    if evil == "link":
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/shadow"
    else:
        ti.size = 1
    with pytest.raises(ReleaseOpError, match="包里"):         # 解开那一关拒的,不是靠后面的指纹
        o.install(_ref(served, NEW, _tar(_包(tmp_path, NEW), evil=[ti])))
    assert not layout.release_dir(NEW).exists()
    assert not (tmp_path / "etc").exists()


def test_没装好不切(ops, tmp_path):
    o, served, restarts, built, layout = ops
    with pytest.raises(ReleaseOpError):
        o.activate(NEW)
    assert o.current() == OLD and restarts == [] and rel.read_pending(layout) is None


def test_包里的setuid_解开时去掉(ops, tmp_path):
    import os
    import stat
    o, served, restarts, built, layout = ops
    pkg = _包(tmp_path, NEW)
    buf = io.BytesIO()

    def 加setuid(ti):
        if ti.name.endswith("x.py"):
            ti.mode = 0o6755
        return ti
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(pkg, arcname=pkg.name, filter=加setuid)
    o.install(_ref(served, NEW, buf.getvalue()))
    mode = os.stat(layout.release_dir(NEW) / "src" / "x.py").st_mode
    assert not mode & (stat.S_ISUID | stat.S_ISGID) and mode & 0o100, "setuid 去掉,可执行位留着"


def test_站点给的包比说的大_下到超了就停_不接着下(ops, tmp_path):
    o, served, restarts, built, layout = ops
    got = {"n": 0}

    def 不停(name):
        while True:
            got["n"] += 1
            yield b"x" * 1000
    o._fetch = 不停
    with pytest.raises(ReleaseOpError, match="大"):
        o.install(ReleaseRef(name=NEW, sha256="0" * 64, size=5000))
    assert got["n"] <= 6


def test_venv没建成_落了槽也不许切(ops, tmp_path):
    o, served, restarts, built, layout = ops

    def 坏(pkg, slot):
        raise ReleaseOpError("pip 装不上")
    o._build = 坏
    ref = _ref(served, NEW, _tar(_包(tmp_path, NEW)))
    with pytest.raises(ReleaseOpError):
        o.install(ref)
    assert layout.release_dir(NEW).is_dir() and not o.ready(NEW)
    with pytest.raises(ReleaseOpError):
        o.activate(NEW)
    assert o.current() == OLD and restarts == []


def test_在跑的不是在途的那一版_不提交(ops, tmp_path):
    o, served, restarts, built, layout = ops
    o.install(_ref(served, NEW, _tar(_包(tmp_path, NEW))))
    o.activate(NEW)
    rel._point_current(layout, OLD)                         # 比如有人手工指回去了
    assert o.commit_if_pending() is None and rel.read_pending(layout) is not None


def test_开机守卫退回了_条子读一次就没了(ops, tmp_path):
    o, served, restarts, built, layout = ops
    o.install(_ref(served, NEW, _tar(_包(tmp_path, NEW))))
    o.activate(NEW)
    for _ in range(rel.MAX_BOOT_ATTEMPTS + 1):
        rel.boot_guard(layout, now_ms=9)
    assert o.current() == OLD
    note = o.take_guard_note()
    assert note["from"] == NEW and note["to"] == OLD and o.take_guard_note() is None


def test_建venv_哨兵最后落_失败不落_在了就跳过(tmp_path):
    import os
    import stat

    from d1max_agent.release_ops import build_venv
    log = tmp_path / "calls.log"
    fake = tmp_path / "python3"
    fake.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; fi
if [ -n "$FAIL_PIP" ] && [ "$2" = "pip" ]; then exit 1; fi
exit 0
""")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    pkg = _包(tmp_path, NEW)
    (pkg / "packages" / "robot-agent").mkdir(parents=True)
    slot = tmp_path / "slot"
    os.environ["FAIL_PIP"] = "1"
    try:
        with pytest.raises(ReleaseOpError):
            build_venv(pkg, slot, pip_args=["--no-index"], python=str(fake))
    finally:
        del os.environ["FAIL_PIP"]
    assert not (slot / "venv" / SENTINEL).exists(), "装失败不落哨兵"
    build_venv(pkg, slot, pip_args=["--no-index"], python=str(fake))
    assert (slot / "venv" / SENTINEL).is_file()
    pips = [ln for ln in log.read_text().splitlines() if ln.startswith("-m pip")]
    assert len(pips) == 3 and all("--no-index" in ln for ln in pips), "包本身、代理那几个包"
    assert "robot-agent" in pips[-1] and "adapter-sim" in pips[-1]
    n = len(log.read_text().splitlines())
    build_venv(pkg, slot, pip_args=[], python=str(fake))
    assert len(log.read_text().splitlines()) == n, "哨兵在:跳过"
    (pkg / "wheels").mkdir()                                 # 包里带离线轮子:只从它装
    (pkg / "wheels" / "x-1-py3-none-any.whl").write_bytes(b"w")
    slot2 = tmp_path / "slot2"
    build_venv(pkg, slot2, pip_args=[], python=str(fake))
    pips = [ln for ln in log.read_text().splitlines()[n:] if ln.startswith("-m pip")]
    assert pips and all("--no-index" in ln and "--find-links=" in ln and "/wheels" in ln
                        for ln in pips)
