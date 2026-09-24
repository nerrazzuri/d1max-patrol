"""备份盘的 HTTP 面。

这一层只测接口的形状:路由通不通、挂载点核对没有、默认动不动盘、扫盘炸了
会不会把整屏掀翻。真正的判断逻辑归 ``tests/engine/test_backup*.py`` 管。

**最要紧的是「不认识的挂载点要 404」那条**:请求体里那个 mount 来自外面,
不核对的话,一个 POST 就能让狗往文件系统上的任意路径写标记、往任意目录拷
归档。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from d1max_agent.engine.archive import STAMP_FMT
from d1max_agent.engine.backup import init_target, read_marker, read_sync_state
from d1max_agent.engine.removable import DiskRole, Removable
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
    """**盘要先认过**:容量是照着盘上的标记文件量的,没有标记就不量(见下一条)。"""
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    (t,) = C.get_json(s, "/api/backup/targets")["targets"]
    assert t["total_bytes"] > 0
    assert t["free_bytes"] >= 0


def test_读不到标记的盘不报容量而不是报根盘的容量(one_disk):
    """盘卸载之后挂载点常常作为一个空目录留在根文件系统上。

    照着那个路径量到的是**根盘**的容量 —— 把根盘的几百 GB 当成备份盘的余量,
    是这一屏能犯的最贵的一个错。宁可报不出来,也不报错的那个数。
    """
    _, s, _ = one_disk
    (t,) = C.get_json(s, "/api/backup/targets")["targets"]
    assert t["total_bytes"] is None
    assert t["free_bytes"] is None
    assert "容量报不出来" in t["detail"]


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
    assert read_sync_state(mount, robot_sn=ctx.identity.sn).last_sync_ms > 0


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


def test_同步在飞的时候清盘要_409(one_disk):
    # 清盘那行 rmtree 删的正是同步这一刻在读的目录树。撞上的那次,备份盘上落的
    # 是残的一趟,而"只增不删"保证它永远不会被重拷 —— 两边都缺一块。
    # **不真起两条线程去撞**:那种红在别人机器上复现不了。直接摆出"有一轮
    # 同步在飞"这个状态,测的是那道闸门本身。
    _, s, mount = one_disk
    s._syncing.add(mount.as_posix())
    try:
        body = C.get_err(s, "/api/storage/sweep", 409, method="POST",
                         payload={"apply": True})
        assert "正在同步" in json.dumps(body, ensure_ascii=False)
    finally:
        s._syncing.discard(mount.as_posix())


def test_同步在飞的时候只出方案的清盘不受影响(one_disk):
    # ``{"apply": false}`` 只出方案不动盘,没有任何东西可撞。
    _, s, mount = one_disk
    s._syncing.add(mount.as_posix())
    try:
        body = C.get_json(s, "/api/storage/sweep", method="POST",
                          payload={"apply": False})
        assert body["applied"] is False
        assert "sweep" in body
    finally:
        s._syncing.discard(mount.as_posix())


def test_清盘在飞的时候同步要_409(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    s._sweeping = True
    try:
        body = C.get_err(s, "/api/backup/sync", 409, method="POST",
                         payload={"mount": mount.as_posix(), "apply": True})
        assert "清盘" in json.dumps(body, ensure_ascii=False)
    finally:
        s._sweeping = False


def _write_run(ctx, mission: str, days_ago: float, *,
               retention_days: int = 90) -> Path:
    """在**真的** runs_root 下摆一趟跑完了的归档。

    **manifest 里必须有 summary** —— ``scan_runs`` 靠它判"没人在写这一趟了"。
    时间戳从此刻往回推,不写死:写死的日期今天能过,一年之后会因为"那趟归档
    过期了"而莫名其妙地红。写法照 ``tests/app/test_api_retention.py``。
    """
    stamp = (datetime.now(timezone.utc)
             - timedelta(days=days_ago)).strftime(STAMP_FMT)
    run = Path(ctx.runs_root) / mission / stamp
    (run / "photos").mkdir(parents=True)
    (run / "photos" / f"P0__front__{stamp}.jpg").write_bytes(b"\xff\xd8j\xff\xd9")
    (run / "manifest.json").write_text(json.dumps({
        "mission": {"mission": mission, "map_id": "map_test", "waypoints": [],
                    "policy": {"retention_days": retention_days}},
        "started_at": stamp, "fingerprint": {},
        "summary": {"state": "COMPLETED", "total": 1},
    }, ensure_ascii=False), encoding="utf-8")
    return run


def test_盘上躺着一趟早就过期的归档也不许常年报红(one_disk):
    # ``days_left`` 把已经过期的 run 钳到 0.0,而"已经过期但还在盘上"是正常
    # 状态 —— 清盘按水位触发,不按到期。不把过期的滤掉,每一台没配镜像盘的狗
    # 大约 83 天后就会永久顶着红色 PUSH"再过 0 天就有归档要被永久删掉了",
    # 而实际上什么也没在被删(spec §7.6 纪律 2 禁止的常年报警)。
    #
    # **这条必须跑在真数据上。** 空 tmp_path 上 runs 是空的,soonest 恒为
    # None,这条错永远不会显形 —— 那正是它躲过八轮任务评审的原因。
    ctx, s, _ = one_disk
    _write_run(ctx, "一号厂房", 400)      # 早就过期,还躺在盘上
    _write_run(ctx, "一号厂房", 1)        # 还剩 89 天
    body = C.get_json(s, "/api/storage")
    assert body["runs_total"] == 2
    assert body["backup"]["level"] == "neutral"
    assert "未配备份盘" in body["backup"]["detail"]


def test_真有一趟快到期的时候照顶(one_disk):
    # 上一条不许把这一条一起关掉:滤掉的只是**已经过期**的,没到期的照算。
    ctx, s, _ = one_disk
    _write_run(ctx, "一号厂房", 400)
    _write_run(ctx, "一号厂房", 87)       # 还剩 3 天
    body = C.get_json(s, "/api/storage")
    assert body["backup"]["level"] == "push"
    assert "永久删掉" in body["backup"]["detail"]


# --------------------------------------------------------------- 自动同步
#
# 镜像盘那一列在 spec §7.5 的表里写着"依赖人:否"。在这条后台协程落地之前,
# 全仓 apply_sync 的唯一调用点是带 {"apply": true} 的 HTTP 路由 —— 也就是说
# 镜像盘其实只有人点一下才会写,而文案、docstring 和规格都说它不用人管。
#
# 这里测的是 ``_autosync_once`` 这一层,**不在测试里真睡 60 秒**。


def test_自动同步会往镜像盘上写(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    run = _write_run(ctx, "一号厂房", 1)
    ctx.bridge.call(s._autosync_once)
    key = f"一号厂房/{run.name}"
    assert (mount / "runs" / "一号厂房" / run.name / "manifest.json").is_file()
    assert key in read_sync_state(mount, robot_sn=ctx.identity.sn).done


def test_自动同步一个字节都不往交付盘上写(one_disk):
    # 交付盘是人插上来手动取走的。自动往上写违反 §7.5:人拔走的那份会比他
    # 以为的多,而他正是靠"我知道这块盘上有什么"在做交付。
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.TRANSFER,
                now_ms=1_757_000_000_000)
    _write_run(ctx, "一号厂房", 1)
    ctx.bridge.call(s._autosync_once)
    assert not (mount / "runs").exists()


def test_引擎在跑的时候整趟跳过(one_disk, monkeypatch):
    # 巡检的时候不跟它抢盘 I/O:归档正在往 runs_root 里写,而这边要递归 stat
    # 整棵树再拷几百兆。
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    _write_run(ctx, "一号厂房", 1)
    monkeypatch.setattr(type(ctx.engine), "running",
                        property(lambda _self: True))
    ctx.bridge.call(s._autosync_once)
    assert not (mount / "runs").exists()


def test_这块盘上已经有一轮同步在飞就让路(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    _write_run(ctx, "一号厂房", 1)
    s._syncing.add(mount.as_posix())
    try:
        ctx.bridge.call(s._autosync_once)
        assert not (mount / "runs").exists()
    finally:
        s._syncing.discard(mount.as_posix())


def test_清盘在飞的时候自动同步也让路(one_disk):
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    _write_run(ctx, "一号厂房", 1)
    s._sweeping = True
    try:
        ctx.bridge.call(s._autosync_once)
        assert not (mount / "runs").exists()
    finally:
        s._sweeping = False


def test_扫盘炸了不许把这条循环炸掉(bridge, tmp_path):
    # 一块坏盘让这条循环死掉之后,没有任何人会发现 —— 备份本来就是那个
    # "平时看不见它在不在工作"的东西。
    _, s = _server(bridge, tmp_path, BoomProbe())
    try:
        bridge.call(s._autosync_once)
    finally:
        s.stop()


def test_扫盘连炸两次这条循环也不许死_第三圈照常拷(one_disk, monkeypatch):
    """一条悄悄死掉的自动同步是这一卷立志要消灭的那种静默失败。

    取 ``engine.running`` 和扫盘原来在兜底外面:任何一个抛出来,整条
    ``_autosync_loop`` 就没了 —— 没有回溯、没有日志、页面上什么也不变,
    而镜像盘从此再也不同步,所有人都以为有第二份。

    **这里测的是那条真的循环,不是 ``_autosync_once``** —— "循环还活着"这件事
    只有循环自己答得了。周期临时调到 10 毫秒,不真睡 60 秒。
    """
    import time

    from d1max_patrol.app import server as S

    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    run = _write_run(ctx, "一号厂房", 1)

    真扫盘, 扫了几次 = S.scan_or_unknown, []

    async def _前两次炸(probe):
        扫了几次.append(1)
        if len(扫了几次) <= 2:
            raise OSError("stale NFS file handle")
        return await 真扫盘(probe)

    # ``AppServer.start`` 已经把这条循环建起来了(周期 60 秒)。先收掉,
    # 换上快周期和会炸的扫盘,再建一条新的。
    ctx.bridge.call(s._autosync_stop)
    monkeypatch.setattr(S, "scan_or_unknown", _前两次炸)
    monkeypatch.setattr(S, "_AUTOSYNC_S", 0.01)
    ctx.bridge.call(s._autosync_start)
    try:
        落地 = mount / "runs" / "一号厂房" / run.name / "manifest.json"
        截止 = time.monotonic() + 10.0
        while not 落地.is_file() and time.monotonic() < 截止:
            time.sleep(0.02)
        # 前两圈炸了,循环还活着才会有第三圈,第三圈才拷得出这个文件。
        assert 落地.is_file()
        assert len(扫了几次) >= 3
    finally:
        ctx.bridge.call(s._autosync_stop)


def test_同步跑完之后挂载点从在飞的名单里退出来(one_disk):
    # 退不出来的话,这块盘从此再也不会被自动同步碰,而且 /api/backup/eject
    # 会永远回"这块盘正在同步,现在不能拔"。
    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    _write_run(ctx, "一号厂房", 1)
    ctx.bridge.call(s._autosync_once)
    assert s._syncing == set()
    body = C.get_json(s, "/api/backup/eject", method="POST",
                      payload={"mount": mount.as_posix()})
    assert body["ok"] is True


def test_排方案那段窗口里这块盘就已经算在飞了(one_disk, monkeypatch):
    """``_pick`` 和 ``plan_sync`` 是这条路由最慢的两步。

    记晚了的那一边,这段时间里并发进来的 ``/api/backup/eject`` 会照着一份空的
    ``_syncing`` 回一句"可以拔了" —— 人真拔了,下一秒这边就开始往一个已经不在
    的挂载点上拷。**不真起两条线程去撞**:在 ``plan_sync`` 里就地看那个集合。
    """
    from d1max_patrol.app import server as S

    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    真排方案, 看到的 = S.plan_sync, []

    def _排方案的时候看一眼(*a, **kw):
        看到的.append(set(s._syncing))
        return 真排方案(*a, **kw)

    monkeypatch.setattr(S, "plan_sync", _排方案的时候看一眼)
    C.get_json(s, "/api/backup/sync", method="POST",
               payload={"mount": mount.as_posix(), "apply": True})
    assert 看到的 == [{mount.as_posix()}]


def test_只出方案不占在飞的名单(one_disk, monkeypatch):
    # 光看名单的人不该把弹出按钮也一起锁住:出方案不动盘。
    from d1max_patrol.app import server as S

    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    真排方案, 看到的 = S.plan_sync, []

    def _排方案的时候看一眼(*a, **kw):
        看到的.append(set(s._syncing))
        return 真排方案(*a, **kw)

    monkeypatch.setattr(S, "plan_sync", _排方案的时候看一眼)
    C.get_json(s, "/api/backup/sync", method="POST",
               payload={"mount": mount.as_posix()})
    assert 看到的 == [set()]


def test_盘况接口把镜像盘满了这件事报出来(one_disk, monkeypatch):
    """spec §7.6:满了不删旧的,报出来停同步。

    ``full`` 在真机上要把盘写到只剩 64 MB 才出得来,离机造不出;这里替掉
    ``plan_sync`` 的回值 —— 测的是 ``_storage`` 有没有把 ``.full`` 接过去,
    "什么时候算满"归 ``tests/engine/test_backup.py`` 管。
    """
    from d1max_patrol.app import server as S

    ctx, s, mount = one_disk
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    真排方案 = S.plan_sync

    def _满了(*a, **kw):
        return dataclasses.replace(真排方案(*a, **kw), full=True)

    monkeypatch.setattr(S, "plan_sync", _满了)
    body = C.get_json(s, "/api/storage")
    assert body["backup"]["level"] == "push"
    assert "满" in body["backup"]["detail"]
    assert "不会被自动删掉" in body["backup"]["detail"]
