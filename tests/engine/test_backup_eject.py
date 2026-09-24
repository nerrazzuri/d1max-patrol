"""安全弹出:停同步 -> 把缓冲刷到盘上 -> 才说可以拔了。

Linux 上写文件先进页缓存。人看到"同步完成"就伸手拔盘,而那几百兆可能还有
一部分在内存里 —— 盘上那趟归档因此是残的,**而我们已经把它记成拷完了**。
"""

from __future__ import annotations

from d1max_agent.engine.backup import DEFAULT_SYNC, eject


def test_弹出会把缓冲刷到盘上_然后说可以拔了(tmp_path):
    calls = []
    got = eject(tmp_path, sync_fs=lambda: calls.append(1))
    assert calls == [1]
    assert got.ok is True
    assert "可以拔" in got.detail


def test_正在同步的时候不许弹出_而且说清楚在同步(tmp_path):
    # 拔一块正在写的盘,坏的不只是这一趟:元数据写到一半,整块盘可能挂不上,
    # 而它是备份盘,挂不上的那天正好是需要它的那天。
    calls = []
    got = eject(tmp_path, busy=True, sync_fs=lambda: calls.append(1))
    assert got.ok is False
    assert "正在同步" in got.detail
    assert calls == []


def test_刷盘失败要说清楚而不是炸(tmp_path):
    def _炸():
        raise OSError("盘已经被拔了")

    got = eject(tmp_path, sync_fs=_炸)
    assert got.ok is False
    assert "盘已经被拔了" in got.detail
    assert "别拔" in got.detail


def test_弹出不动盘上的任何文件(tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "x.txt").write_text("在", encoding="utf-8")
    eject(tmp_path, sync_fs=lambda: None)
    assert (tmp_path / "runs" / "x.txt").read_text(encoding="utf-8") == "在"


def test_默认的刷盘函数在这台机器上取得到():
    # os.sync 是 Unix 独有的,Windows 开发机上没有这个名字。直接写 os.sync()
    # 会在 import 阶段就把整个模块炸掉。
    assert callable(DEFAULT_SYNC)
    DEFAULT_SYNC()
