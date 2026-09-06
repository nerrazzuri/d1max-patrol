"""备份盘的 HTTP 面。

这一层只测接口的形状:路由通不通、挂载点核对没有、默认动不动盘、扫盘炸了
会不会把整屏掀翻。真正的判断逻辑归 ``tests/engine/test_backup*.py`` 管。

**最要紧的是「不认识的挂载点要 404」那条**:请求体里那个 mount 来自外面,
不核对的话,一个 POST 就能让狗往文件系统上的任意路径写标记、往任意目录拷
归档。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_patrol.engine.backup import init_target, read_marker, read_state
from d1max_patrol.engine.removable import DiskRole, Removable
from tests.app import conftest as C
from tests.conftest import SomeDisks


class BoomProbe:
    """扫盘就炸的探针。stale mount 在真机上就是这样。"""

    async def scan(self):
        raise OSError("stale NFS file handle")


def _disks(*mounts: Path):
    return SomeDisks(*[
        Removable(mount=m, role=DiskRole.UNKNOWN, sn="") for m in mounts])


def _server(bridge, tmp_path, probe):
    from d1max_patrol.app.server import AppServer
    ctx = C.make_ctx(bridge, tmp_path, removable=probe)
    s = AppServer(ctx, port=0)
    s.start()
    return ctx, s


@pytest.fixture
def one_disk(bridge, tmp_path):
    """一块插着的、还没初始化的盘。"""
    mount = tmp_path / "media" / "u1"
    mount.mkdir(parents=True)
    ctx, s = _server(bridge, tmp_path, _disks(mount))
    yield ctx, s, mount
    s.stop()


def test_targets_列出认到的盘(one_disk):
    _, s, mount = one_disk
    body = C.get_json(s, "/api/backup/targets")
    assert body["scanned"] is True
    assert [t["mount"] for t in body["targets"]] == [mount.as_posix()]
    assert body["targets"][0]["usable"] is False
    assert "还没初始化" in body["targets"][0]["detail"]


def test_targets_带容量和剩余(one_disk):
    _, s, _ = one_disk
    (t,) = C.get_json(s, "/api/backup/targets")["targets"]
    assert t["total_bytes"] > 0
    assert t["free_bytes"] >= 0


def test_targets_带上次同步时间和落后多少(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    (t,) = C.get_json(s, "/api/backup/targets")["targets"]
    assert t["usable"] is True
    assert t["last_sync_ms"] == 0
    assert t["behind"] == 0


def test_targets_没插盘的时候是空列表而不是报错(bridge, tmp_path):
    _, s = _server(bridge, tmp_path, None)
    try:
        body = C.get_json(s, "/api/backup/targets")
        assert body["scanned"] is True
        assert body["targets"] == []
    finally:
        s.stop()


def test_targets_扫盘炸了要降级答完而不是_500(bridge, tmp_path):
    # 认不出插着什么,不等于这一屏该消失 —— 人正是在盘出事的时候来看它的。
    _, s = _server(bridge, tmp_path, BoomProbe())
    try:
        body = C.get_json(s, "/api/backup/targets")
        assert body["scanned"] is False
        assert body["targets"] == []
        assert "认不出" in body["detail"]
    finally:
        s.stop()


def test_init_把盘认成这台狗的(one_disk):
    ctx, s, mount = one_disk
    code, body, _ = C.request(s, "/api/backup/init", method="POST",
                              payload={"mount": mount.as_posix(),
                                       "role": "mirror", "label": "内置镜像"})
    assert code == 200, body
    got = read_marker(mount)
    assert got.sn == ctx.identity.sn
    assert got.role is DiskRole.MIRROR
    assert got.label == "内置镜像"


def test_init_插错盘要_409_而且原来的标记原封不动(one_disk):
    _, s, mount = one_disk
    init_target(mount, robot_sn="别的狗", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    body = C.get_err(s, "/api/backup/init", 409, method="POST",
                     payload={"mount": mount.as_posix(), "role": "mirror"})
    assert "别的狗" in json.dumps(body, ensure_ascii=False)
    assert read_marker(mount).sn == "别的狗"


def test_init_不认识的挂载点要_404(one_disk):
    # 不核对的话,一个 POST 就能让狗往文件系统上的任意路径写标记。
    _, s, _ = one_disk
    C.get_err(s, "/api/backup/init", 404, method="POST",
              payload={"mount": "/etc", "role": "mirror"})
    assert not Path("/etc/.d1max-backup").exists()


def test_sync_默认只给方案不动盘(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    body = C.get_json(s, "/api/backup/sync", method="POST",
                      payload={"mount": mount.as_posix()})
    assert body["applied"] is False
    assert "items" in body["plan"]
    assert not (mount / "runs").exists()


def test_sync_要_apply_true_才真拷(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    run = Path(ctx.runs_root) / "一号厂房" / "20260101T000000Z"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"summary": {"photos": 0}}), encoding="utf-8")
    body = C.get_json(s, "/api/backup/sync", method="POST",
                      payload={"mount": mount.as_posix(), "apply": True})
    assert body["applied"] is True
    assert body["result"]["copied"] == ["一号厂房/20260101T000000Z"]
    assert (mount / "runs" / "一号厂房" / "20260101T000000Z"
            / "manifest.json").is_file()
    assert read_state(mount, robot_sn=ctx.identity.sn).last_sync_ms > 0


def test_eject_说可以拔了(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.TRANSFER,
                now_ms=1_757_000_000_000)
    body = C.get_json(s, "/api/backup/eject", method="POST",
                      payload={"mount": mount.as_posix()})
    assert body["ok"] is True
    assert "可以拔" in body["detail"]


def test_盘况接口里带_backup_块(one_disk):
    # §5.1 那块玻璃上的第六项要的三个数,数据口先通,玻璃留给第 8 卷去画。
    _, s, _ = one_disk
    body = C.get_json(s, "/api/storage")
    assert body["backup"]["level"] == "neutral"
    assert "未配备份盘" in body["backup"]["detail"]
