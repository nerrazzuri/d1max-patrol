"""备份盘:认盘、同步状态、规划、执行。

**这一层不碰引擎状态,只碰文件。** 所以每一条都能在临时目录里跑完,
而备份这件事在真机上恰恰是最难复现的——盘要插着、要挂上、要有空间。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from d1max_patrol.engine.backup import (
    EMPTY_STATE,
    FREE_MARGIN_BYTES,
    BackupError,
    SyncState,
    Target,
    apply_sync,
    copy_run,
    init_target,
    marker_path,
    plan_sync,
    read_marker,
    read_state,
    resolve_targets,
    state_path,
    write_state,
)
from d1max_patrol.engine.removable import DiskRole, Removable, blocks_takeoff, read_role


def test_写下的标记能被第一卷那个读取器读出来(tmp_path):
    # 一个模块写、另一个模块读同一个文件。这条测试是那道缝的唯一守卫。
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    role, sn = read_role(tmp_path)
    assert role is DiskRole.MIRROR
    assert sn == "D1M-0007"


def test_标记落在第一卷约定的那个路径上(tmp_path):
    assert marker_path(tmp_path) == tmp_path / ".d1max-backup" / "target.json"


def test_读得回完整的标记(tmp_path):
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.TRANSFER,
                label="一号厂房交付盘", now_ms=1_757_000_000_000)
    assert read_marker(tmp_path) == Target(
        mount=tmp_path, role=DiskRole.TRANSFER, sn="D1M-0007",
        label="一号厂房交付盘", created_at_ms=1_757_000_000_000)


def test_没有标记的盘读出来是空(tmp_path):
    assert read_marker(tmp_path) is None


def test_标记文件坏了当成没有标记而不是抛(tmp_path):
    # 抛出去的那一边,一块坏盘会把整趟"认盘"炸掉,连边上那几块好盘都列不出来,
    # 而人正等着看那份列表决定往哪块盘上拷。
    marker_path(tmp_path).parent.mkdir(parents=True)
    marker_path(tmp_path).write_text("{不是 json", encoding="utf-8")
    assert read_marker(tmp_path) is None


def test_盘上已经有别的狗的标记要拒绝而不是覆盖(tmp_path):
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    with pytest.raises(BackupError) as e:
        init_target(tmp_path, robot_sn="D1M-0008", role=DiskRole.MIRROR,
                    now_ms=1_757_000_100_000)
    assert "D1M-0007" in str(e.value)
    assert "D1M-0008" in str(e.value)
    # 原来那份原封不动。
    assert read_marker(tmp_path).sn == "D1M-0007"


def test_同一台狗重新初始化是允许的(tmp_path):
    # 换角色(镜像盘改成交付盘)是个正当操作,不该被"已有标记"挡住。
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    got = init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.TRANSFER,
                      now_ms=1_757_000_100_000)
    assert got.role is DiskRole.TRANSFER
    assert read_marker(tmp_path).role is DiskRole.TRANSFER


def test_角色只许写镜像或取走(tmp_path):
    with pytest.raises(BackupError) as e:
        init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.UNKNOWN,
                    now_ms=1_757_000_000_000)
    assert "unknown" in str(e.value)
    assert not marker_path(tmp_path).exists()


def test_写标记是原子的_不留临时文件(tmp_path):
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    assert not list(marker_path(tmp_path).parent.glob("*.tmp"))
    raw = json.loads(marker_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["role"] == "mirror"
    assert raw["sn"] == "D1M-0007"


def _plug(tmp_path, name: str, *, sn: str, role: DiskRole) -> Removable:
    """造一块已经初始化过的盘,回一个 ``Removable`` —— 就是探针扫出来的样子。"""
    mount = tmp_path / name
    mount.mkdir()
    init_target(mount, robot_sn=sn, role=role, label=name,
                now_ms=1_757_000_000_000)
    return Removable(mount=mount, role=role, sn=sn)


def test_本狗的镜像盘可用(tmp_path):
    disk = _plug(tmp_path, "u1", sn="D1M-0007", role=DiskRole.MIRROR)
    (got,) = resolve_targets([disk], robot_sn="D1M-0007")
    assert got.usable is True
    assert got.role is DiskRole.MIRROR
    assert got.label == "u1"


def test_本狗的交付盘可用(tmp_path):
    disk = _plug(tmp_path, "u1", sn="D1M-0007", role=DiskRole.TRANSFER)
    (got,) = resolve_targets([disk], robot_sn="D1M-0007")
    assert got.usable is True


def test_别的狗的盘不可用_而且说清楚两边分别是谁(tmp_path):
    disk = _plug(tmp_path, "u1", sn="D1M-0008", role=DiskRole.MIRROR)
    (got,) = resolve_targets([disk], robot_sn="D1M-0007")
    assert got.usable is False
    assert "D1M-0008" in got.detail
    assert "D1M-0007" in got.detail


def test_没初始化的盘不可用_说的是还没初始化(tmp_path):
    mount = tmp_path / "u1"
    mount.mkdir()
    disk = Removable(mount=mount, role=DiskRole.UNKNOWN, sn="")
    (got,) = resolve_targets([disk], robot_sn="D1M-0007")
    assert got.usable is False
    assert "还没初始化" in got.detail
    # 客户自己的相机卡长这个样子。**一个字节都不许往上写。**
    assert not (mount / "runs").exists()


def test_标记读不出来的盘按没初始化算(tmp_path):
    mount = tmp_path / "u1"
    marker_path(mount).parent.mkdir(parents=True)
    marker_path(mount).write_text("{坏了", encoding="utf-8")
    disk = Removable(mount=mount, role=DiskRole.UNKNOWN, sn="")
    (got,) = resolve_targets([disk], robot_sn="D1M-0007")
    assert got.usable is False
    assert "还没初始化" in got.detail


def test_狗自己没身份的时候不拿_SN_去卡(tmp_path):
    # ``app/identity.py`` 查不到机身 SN 时明写 "unknown"。拿它去比,
    # 一只读不出身份的狗会被拦得连自己的镜像盘都用不了。
    disk = _plug(tmp_path, "u1", sn="D1M-0007", role=DiskRole.MIRROR)
    (got,) = resolve_targets([disk], robot_sn="unknown")
    assert got.usable is True


def test_列出来的顺序跟扫到的顺序一致(tmp_path):
    a = _plug(tmp_path, "u1", sn="D1M-0007", role=DiskRole.MIRROR)
    b = _plug(tmp_path, "u2", sn="D1M-0007", role=DiskRole.TRANSFER)
    got = resolve_targets([b, a], robot_sn="D1M-0007")
    assert [s.mount for s in got] == [b.mount, a.mount]


def test_能不能备份跟拦不拦起飞是两件事(tmp_path):
    # 同一块盘: 拿来备份是可以的(人就是插它来拷数据的),同时它拦起飞
    # (凸出来的盘挂在走动的狗身上是个杠杆)。两个结论相反,不许合成一个。
    disk = _plug(tmp_path, "u1", sn="D1M-0007", role=DiskRole.TRANSFER)
    (got,) = resolve_targets([disk], robot_sn="D1M-0007")
    assert got.usable is True
    assert blocks_takeoff([disk], robot_sn="D1M-0007") == (disk,)


def test_从没同步过的盘读出来是空状态(tmp_path):
    assert read_state(tmp_path, robot_sn="D1M-0007") == EMPTY_STATE


def test_写下的状态读得回(tmp_path):
    state = SyncState(robot_sn="D1M-0007", last_sync_ms=1_757_000_000_000,
                      done=frozenset({"一号厂房/20260901T010203Z"}))
    write_state(tmp_path, state)
    assert read_state(tmp_path, robot_sn="D1M-0007") == state


def test_写状态是原子的_不留临时文件(tmp_path):
    write_state(tmp_path, SyncState(robot_sn="D1M-0007"))
    assert not list(state_path(tmp_path).parent.glob("*.tmp"))


def test_状态文件坏了当成空状态而不是抛(tmp_path):
    # 抛出去,备份就此彻底停摆 —— 而没人会发现,因为备份本来就是那个"平时
    # 看不见它在不在工作"的东西。当成没同步过最坏是重拷一遍,不毁任何东西。
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text("{坏了", encoding="utf-8")
    assert read_state(tmp_path, robot_sn="D1M-0007") == EMPTY_STATE


def test_状态里记的_SN_跟这台狗对不上就当成没同步过(tmp_path):
    # 这块盘被重新初始化给了这台狗,但旧进度还在。那些键指的是另一只狗的归档。
    write_state(tmp_path, SyncState(robot_sn="D1M-0008", last_sync_ms=1,
                                    done=frozenset({"一号厂房/20260901T010203Z"})))
    assert read_state(tmp_path, robot_sn="D1M-0007") == EMPTY_STATE


def test_狗没身份的时候不拿_SN_去卡状态(tmp_path):
    state = SyncState(robot_sn="D1M-0007", last_sync_ms=1,
                      done=frozenset({"一号厂房/20260901T010203Z"}))
    write_state(tmp_path, state)
    assert read_state(tmp_path, robot_sn="unknown") == state


def test_记新的一趟是并集_不覆盖(tmp_path):
    state = SyncState(robot_sn="D1M-0007", last_sync_ms=1,
                      done=frozenset({"甲/20260901T010203Z"}))
    got = state.with_done(["乙/20260902T010203Z"], now_ms=1_757_000_000_000)
    assert got.done == {"甲/20260901T010203Z", "乙/20260902T010203Z"}
    assert got.last_sync_ms == 1_757_000_000_000
    assert got.robot_sn == "D1M-0007"


NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _make_run(runs_root, mission: str, stamp: str, *, settled: bool = True,
              size: int = 32):
    """在 ``runs_root`` 下造一趟归档,形状跟 ``archive.RunArchive`` 写出来的一样。"""
    run = runs_root / mission / stamp
    (run / "photos").mkdir(parents=True)
    (run / "events.jsonl").write_text('{"kind":"start"}\n', encoding="utf-8")
    (run / "photos" / "a.jpg").write_bytes(b"x" * size)
    manifest = {"mission": {"name": mission}}
    if settled:
        manifest["summary"] = {"photos": 1}
    (run / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return run


def test_没同步过的时候所有安定的归档都要拷(tmp_path):
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "一号厂房", "20260901T010203Z")
    _make_run(runs, "一号厂房", "20260902T010203Z")
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=10 * 1024 * 1024 * 1024)
    assert [i.key for i in plan.items] == [
        "一号厂房/20260901T010203Z", "一号厂房/20260902T010203Z"]
    assert plan.full is False


def test_正在写的归档要跳过(tmp_path):
    # 拷过去的是半份 events.jsonl,而下次它安定了我们已经把它记成拷过了 ——
    # 于是盘上永远是半份。
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "一号厂房", "20260906T113000Z", settled=False)
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=10 * 1024 * 1024 * 1024)
    assert plan.items == ()
    assert plan.skipped_unsettled == ("一号厂房/20260906T113000Z",)


def test_已经同步过的不再拷(tmp_path):
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "一号厂房", "20260901T010203Z")
    _make_run(runs, "一号厂房", "20260902T010203Z")
    write_state(mount, SyncState(robot_sn="D1M-0007", last_sync_ms=1,
                                 done=frozenset({"一号厂房/20260901T010203Z"})))
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=10 * 1024 * 1024 * 1024)
    assert [i.key for i in plan.items] == ["一号厂房/20260902T010203Z"]
    assert plan.already == ("一号厂房/20260901T010203Z",)


def test_盘上多出来的目录既不出现在计划里也不产生任何删除(tmp_path):
    # 狗按水位删掉的那些,正是备份存在的全部理由。
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    (mount / "runs" / "早就删了的任务" / "20250101T000000Z").mkdir(parents=True)
    _make_run(runs, "一号厂房", "20260901T010203Z")
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=10 * 1024 * 1024 * 1024)
    assert [i.key for i in plan.items] == ["一号厂房/20260901T010203Z"]
    # 计划这个结构里根本没有"要删什么"这个概念。有那个字段,早晚有人往里填。
    assert not hasattr(plan, "to_delete")
    assert "delete" not in plan.to_wire()
    assert (mount / "runs" / "早就删了的任务" / "20250101T000000Z").is_dir()


def test_计划按时间从旧到新(tmp_path):
    # 最老的那几趟离到期最近,也就是最快会被狗自己删掉的那几趟。
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "乙", "20260903T010203Z")
    _make_run(runs, "甲", "20260901T010203Z")
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=10 * 1024 * 1024 * 1024)
    assert [i.key for i in plan.items] == [
        "甲/20260901T010203Z", "乙/20260903T010203Z"]


def test_目标路径落在盘的_runs_目录下(tmp_path):
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "一号厂房", "20260901T010203Z")
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=10 * 1024 * 1024 * 1024)
    assert plan.items[0].dest == mount / "runs" / "一号厂房" / "20260901T010203Z"


def test_落后多少就是所有还没同步的安定归档(tmp_path):
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    a = _make_run(runs, "甲", "20260901T010203Z")
    _make_run(runs, "乙", "20260906T113000Z", settled=False)
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=10 * 1024 * 1024 * 1024)
    assert plan.behind == 1
    assert plan.behind_bytes == sum(
        p.stat().st_size for p in a.rglob("*") if p.is_file())


def test_盘装不下的时候只排能装下的_而且报满(tmp_path):
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "甲", "20260901T010203Z", size=1000)
    _make_run(runs, "乙", "20260902T010203Z", size=1000)
    one = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                    free_bytes=10 * 1024 * 1024 * 1024).items[0].size_bytes
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=FREE_MARGIN_BYTES + one)
    assert [i.key for i in plan.items] == ["甲/20260901T010203Z"]
    assert plan.full is True
    assert plan.behind == 2


def test_一趟都装不下的时候计划是空的但落后数照报(tmp_path):
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "甲", "20260901T010203Z", size=1000)
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW, free_bytes=0)
    assert plan.items == ()
    assert plan.full is True
    assert plan.behind == 1


def test_报满那句话里要说清楚不会删任何东西(tmp_path):
    # 删旧的那一边,是在"备份盘"这三个字上撒谎。
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir()
    _make_run(runs, "甲", "20260901T010203Z", size=1000)
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW, free_bytes=0)
    assert "不会删" in plan.detail
    assert "换一块" in plan.detail


def _plan(tmp_path, *, size: int = 32, free: int | None = None):
    """造两趟归档 + 一块干净的盘,回 ``(runs, mount, plan)``。"""
    runs, mount = tmp_path / "runs", tmp_path / "u1"
    mount.mkdir(exist_ok=True)
    _make_run(runs, "甲", "20260901T010203Z", size=size)
    _make_run(runs, "乙", "20260902T010203Z", size=size)
    plan = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                     free_bytes=free if free is not None else 10 * 1024 ** 3)
    return runs, mount, plan


def test_拷完盘上有一模一样的文件(tmp_path):
    runs, mount, plan = _plan(tmp_path)
    res = apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007")
    assert res.failed == ()
    assert sorted(res.copied) == ["乙/20260902T010203Z", "甲/20260901T010203Z"]
    src = runs / "甲" / "20260901T010203Z" / "events.jsonl"
    dst = mount / "runs" / "甲" / "20260901T010203Z" / "events.jsonl"
    assert dst.read_bytes() == src.read_bytes()


def test_子目录里的照片也拷过去了(tmp_path):
    runs, mount, plan = _plan(tmp_path)
    apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007")
    assert (mount / "runs" / "甲" / "20260901T010203Z"
            / "photos" / "a.jpg").is_file()


def test_拷过去之后内容不对会被核对出来(tmp_path):
    # 备份最坏的失败模式不是没拷,是拷了但拷坏了 —— 它同时销毁了"我们有第二份"
    # 这个信念。备份盘、读卡器、松掉的排线,坏的方式都是静悄悄的。
    runs, mount, plan = _plan(tmp_path)

    def _坏拷贝(src, dest):
        copy_run(src, dest)
        (dest / "events.jsonl").write_text("被改坏了", encoding="utf-8")

    res = apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007",
                     copy=_坏拷贝)
    assert res.copied == ()
    assert len(res.failed) == 2
    assert all("哈希" in why for _, why in res.failed)


def test_核对没过的那一趟不记进已同步(tmp_path):
    # 记早了,下次同步会跳过它 —— 于是那份坏数据永远不会被修。
    runs, mount, plan = _plan(tmp_path)

    def _坏拷贝(src, dest):
        copy_run(src, dest)
        (dest / "events.jsonl").write_text("被改坏了", encoding="utf-8")

    apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007",
               copy=_坏拷贝)
    assert read_state(mount, robot_sn="D1M-0007").done == frozenset()


def test_一趟失败不影响别的趟(tmp_path):
    runs, mount, plan = _plan(tmp_path)

    def _只坏一趟(src, dest):
        copy_run(src, dest)
        if src.parent.name == "甲":
            (dest / "events.jsonl").write_text("被改坏了", encoding="utf-8")

    res = apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007",
                     copy=_只坏一趟)
    assert res.copied == ("乙/20260902T010203Z",)
    assert [k for k, _ in res.failed] == ["甲/20260901T010203Z"]
    assert read_state(mount, robot_sn="D1M-0007").done == {
        "乙/20260902T010203Z"}


def test_拷贝不留临时文件(tmp_path):
    runs, mount, plan = _plan(tmp_path)
    apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007")
    assert not list((mount / "runs").rglob("*.tmp"))


def test_拷完写状态_上次同步时间跟着走(tmp_path):
    runs, mount, plan = _plan(tmp_path)
    apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007")
    state = read_state(mount, robot_sn="D1M-0007")
    assert state.last_sync_ms == 1_757_000_000_000
    assert state.robot_sn == "D1M-0007"


def test_没有新东西要拷也算同步过一次(tmp_path):
    # "同步过了,没有新东西"是一次成功的同步。不更新时间的那一边,值守屏上
    # 那块"上次同步"会一直停在很久以前 —— 一块好盘看起来像块死盘。
    runs, mount, plan = _plan(tmp_path)
    apply_sync(plan, now_ms=1_757_000_000_000, robot_sn="D1M-0007")
    again = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                      free_bytes=10 * 1024 ** 3)
    assert again.items == ()
    res = apply_sync(again, now_ms=1_757_000_900_000, robot_sn="D1M-0007")
    assert res.copied == ()
    assert read_state(mount, robot_sn="D1M-0007").last_sync_ms == 1_757_000_900_000


def test_报满的计划照拷能装下的那些(tmp_path):
    runs, mount, plan = _plan(tmp_path, size=1000)
    one = plan.items[0].size_bytes
    tight = plan_sync(runs, mount, robot_sn="D1M-0007", now=NOW,
                      free_bytes=FREE_MARGIN_BYTES + one)
    res = apply_sync(tight, now_ms=1_757_000_000_000, robot_sn="D1M-0007")
    assert res.copied == ("甲/20260901T010203Z",)
    assert res.full is True
    assert "不会删" in res.detail
