"""删除计划与真删:水位驱动,两套策略,一条护栏(spec §4.4、§4.6)。

**这一份是全仓最该被怀疑的代码。** 它调 `shutil.rmtree`,而它删掉的在单机
模式下是世界上唯一一份。每一条约束都要有一条测试盯着。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from d1max_patrol.engine.retention import (
    MIN_NOTICE_DAYS,
    RunInfo,
    apply_sweep,
    plan_sweep,
    run_key,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _info(days_ago: float, *, mission: str = "巡检一号", retention: int = 90,
          size: int = 1000, settled: bool = True, uploaded: bool = False,
          exported: bool = False, root: Path | None = None) -> RunInfo:
    started = NOW - timedelta(days=days_ago)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    base = root if root is not None else Path("runs")
    return RunInfo(path=base / mission / stamp, mission=mission,
                   started_at=started, retention_days=retention,
                   size_bytes=size, settled=settled, uploaded=uploaded,
                   exported=exported)


def _noticed(*infos: RunInfo, days_ago: float = MIN_NOTICE_DAYS) -> dict:
    return {run_key(i): NOW - timedelta(days=days_ago) for i in infos}


def test_水位没到就一趟都不删():
    # §4.4:留着那份冗余不花任何额外成本 —— 盘反正空着。
    runs = [_info(200.0, uploaded=True), _info(190.0, uploaded=True)]
    sweep = plan_sweep(runs, now=NOW, has_upload=True, need_bytes=0)
    assert sweep.delete == ()
    assert sweep.freed_bytes == 0
    assert sweep.short_bytes == 0


def test_有服务器时只删已传的():
    传了 = _info(200.0, mission="甲", uploaded=True)
    没传 = _info(300.0, mission="乙", uploaded=False)
    sweep = plan_sweep([传了, 没传], now=NOW, has_upload=True, need_bytes=500)
    assert [r.mission for r in sweep.delete] == ["甲"]


def test_有服务器时未传的宁可腾不够也不删():
    没传 = _info(300.0, uploaded=False)
    sweep = plan_sweep([没传], now=NOW, has_upload=True, need_bytes=5000)
    assert sweep.delete == ()
    assert sweep.short_bytes == 5000
    assert "导出" in sweep.detail


def test_有服务器时不看过期不过期():
    # 分类依据是"已传/未传",不是保留期。传走了的当天就可以按水位删。
    刚传 = _info(1.0, uploaded=True)
    sweep = plan_sweep([刚传], now=NOW, has_upload=True, need_bytes=500)
    assert sweep.delete == (刚传,)


def test_单机时只删保留期外的():
    过期了 = _info(200.0, mission="甲")
    没过期 = _info(10.0, mission="乙")
    sweep = plan_sweep([过期了, 没过期], now=NOW, has_upload=False,
                       need_bytes=5000, noticed=_noticed(过期了, 没过期))
    assert [r.mission for r in sweep.delete] == ["甲"]


def test_单机时没预告过的一律不删():
    # §4.6 硬约束:删之前必须先预告。台账里没有它,就是没说过。
    过期了 = _info(200.0)
    sweep = plan_sweep([过期了], now=NOW, has_upload=False, need_bytes=5000,
                       noticed={})
    assert sweep.delete == ()
    assert sweep.short_bytes == 5000


def test_单机时预告不满七天的不删():
    过期了 = _info(200.0)
    sweep = plan_sweep([过期了], now=NOW, has_upload=False, need_bytes=5000,
                       noticed=_noticed(过期了, days_ago=MIN_NOTICE_DAYS - 0.5))
    assert sweep.delete == ()


def test_单机时预告刚满七天就可以删():
    过期了 = _info(200.0)
    sweep = plan_sweep([过期了], now=NOW, has_upload=False, need_bytes=5000,
                       noticed=_noticed(过期了, days_ago=MIN_NOTICE_DAYS))
    assert sweep.delete == (过期了,)


def test_单机时不看传没传():
    # 单机就没有上传对象,所有文件永远算"未传" —— 照 §2.5 那条规则狗迟早
    # 因为盘满而永久拒绝出任务。这个洞正是 §4.6 要补的。
    过期了 = _info(200.0, uploaded=False)
    sweep = plan_sweep([过期了], now=NOW, has_upload=False, need_bytes=5000,
                       noticed=_noticed(过期了))
    assert sweep.delete == (过期了,)


def test_单机时导出确认过的不必再等保留期():
    # §4.6 第 2 条「导出并释放」的出口:走过那条通道的,别处已经有一份了。
    # 打的是 ``.exported`` 不是 ``.uploaded`` —— 人工导出证明的是"客户手里
    # 有一份",不是"服务器上有一份"。单机档下这两件事等价。
    导出过 = _info(1.0, exported=True)
    sweep = plan_sweep([导出过], now=NOW, has_upload=False, need_bytes=500,
                       noticed={})
    assert sweep.delete == (导出过,)


def test_有服务器时导出确认过的不算数():
    """这一条钉的是两个标记不许混用。

    判据是**传到服务器了没有**,而一次人工导出证明不了这件事。混用的失败
    场景:联网的狗回传断了三天,操作员用导出通道把这几趟拉到手机上确认了;
    次日盘过水位,它们被删;回传恢复之后 uploader 找不到它们 —— 服务器端的
    档案里从此有一个三天的洞,**而服务器才是这一档下的权威副本**。
    """
    只导出过 = _info(200.0, exported=True, uploaded=False)
    sweep = plan_sweep([只导出过], now=NOW, has_upload=True, need_bytes=5000)
    assert sweep.delete == ()
    assert sweep.short_bytes == 5000


def test_有服务器时传走了的照删不误():
    # 反面那一半:``uploaded`` 在这一档下照旧算数,别把闸门关死了。
    传了 = _info(1.0, uploaded=True, exported=False)
    sweep = plan_sweep([传了], now=NOW, has_upload=True, need_bytes=500)
    assert sweep.delete == (传了,)


def test_正在写的那一趟两边都不碰():
    for has_upload in (True, False):
        跑着呢 = _info(200.0, settled=False, uploaded=True)
        sweep = plan_sweep([跑着呢], now=NOW, has_upload=has_upload,
                           need_bytes=5000, noticed=_noticed(跑着呢))
        assert sweep.delete == ()


def test_最老的先删():
    a = _info(300.0, mission="甲", uploaded=True)
    b = _info(200.0, mission="乙", uploaded=True)
    c = _info(100.0, mission="丙", uploaded=True)
    sweep = plan_sweep([c, a, b], now=NOW, has_upload=True, need_bytes=1500)
    assert [r.mission for r in sweep.delete] == ["甲", "乙"]


def test_腾够了就停手():
    runs = [_info(300.0 - i, mission=f"m{i}", uploaded=True) for i in range(5)]
    sweep = plan_sweep(runs, now=NOW, has_upload=True, need_bytes=1000)
    assert len(sweep.delete) == 1
    assert sweep.freed_bytes == 1000
    assert sweep.short_bytes == 0


def test_腾不够时文案说的是导出并释放而不是盘满了():
    # §4.6:「这个死锁是故意留的。它逼出一次人的确认,而这正是"删掉唯一副本"
    # 这件事应该有的门槛。」
    sweep = plan_sweep([], now=NOW, has_upload=False, need_bytes=9999)
    assert sweep.short_bytes == 9999
    assert "导出" in sweep.detail
    assert "释放" in sweep.detail


def test_没什么要删的时候文案也说得出话():
    sweep = plan_sweep([], now=NOW, has_upload=True, need_bytes=0)
    assert sweep.detail


def test_线格式带得走计划():
    a = _info(300.0, uploaded=True)
    wire = plan_sweep([a], now=NOW, has_upload=True, need_bytes=500).to_wire()
    assert wire["freed_bytes"] == 1000
    assert wire["short_bytes"] == 0
    assert wire["delete"][0]["mission"] == "巡检一号"


# --------------------------------------------------------------- 真删


def _on_disk(root: Path, info: RunInfo) -> Path:
    info.path.mkdir(parents=True, exist_ok=True)
    (info.path / "photos").mkdir(exist_ok=True)
    (info.path / "photos" / "a.jpg").write_bytes(b"x")
    return info.path


def test_真的把目录删掉了(tmp_path):
    a = _info(300.0, uploaded=True, root=tmp_path)
    _on_disk(tmp_path, a)
    sweep = plan_sweep([a], now=NOW, has_upload=True, need_bytes=500)
    assert apply_sweep(sweep, runs_root=tmp_path) == (a.path,)
    assert not a.path.exists()


def test_目录已经不在了不算错(tmp_path):
    a = _info(300.0, uploaded=True, root=tmp_path)
    sweep = plan_sweep([a], now=NOW, has_upload=True, need_bytes=500)
    assert apply_sweep(sweep, runs_root=tmp_path) == ()


def test_不许删runs根目录之外的东西(tmp_path):
    # `shutil.rmtree` 是全仓最危险的一行。没有这条护栏,日后任何一个"顺手把
    # path 拼错了"的改动都会安静地删掉别处的东西。
    outside = tmp_path / "别人的数据"
    outside.mkdir()
    (outside / "重要.txt").write_text("别删我", encoding="utf-8")
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    bad = RunInfo(path=outside, mission="伪造", started_at=NOW,
                  retention_days=90, size_bytes=1, settled=True, uploaded=True)
    sweep = plan_sweep([bad], now=NOW, has_upload=True, need_bytes=1)
    assert apply_sweep(sweep, runs_root=runs_root) == ()
    assert (outside / "重要.txt").is_file()


def test_一条删不掉不挡住别的(tmp_path, monkeypatch):
    import shutil as _shutil
    a = _info(300.0, mission="甲", uploaded=True, root=tmp_path)
    b = _info(299.0, mission="乙", uploaded=True, root=tmp_path)
    _on_disk(tmp_path, a)
    _on_disk(tmp_path, b)
    真删 = _shutil.rmtree

    def 第一条炸(path, *args, **kwargs):
        if Path(path) == a.path:
            raise OSError("盘只读")
        return 真删(path, *args, **kwargs)

    monkeypatch.setattr("d1max_patrol.engine.retention.shutil.rmtree", 第一条炸)
    sweep = plan_sweep([a, b], now=NOW, has_upload=True, need_bytes=2000)
    assert apply_sweep(sweep, runs_root=tmp_path) == (b.path,)
    assert a.path.exists()
