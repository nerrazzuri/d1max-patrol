"""意图与执行的落差(§3.4)。

**狗手上那一版是「执行」的唯一真理源;服务器那一版是「意图」的唯一真理源。**
两边不一致是常态,不是异常 —— 狗断网了、推送还没到、包落好了还没生效。
这个模块判断的就是「差在哪儿」。

时刻全是传进来的,一个 ``sleep`` 都没有(§8.5 第 2 条)。
"""

from __future__ import annotations

import pytest

from d1max_agent.engine.bundle import (
    BundleRef,
    Divergence,
    DivergenceKind,
    divergence,
)

现在 = 1_757_000_000_000          # 随便一个 UTC 毫秒
四小时 = 4 * 3600 * 1000


def 版(version: int, *, bid: str = "site-kl", sha: str | None = None):
    return BundleRef(bid, version, sha or f"{version:064d}")


def 判(local, intended, *, last_sync_ms=现在, now_ms=现在) -> Divergence:
    return divergence(local=local, intended=intended,
                      last_sync_ms=last_sync_ms, now_ms=now_ms)


def test_两边一样就是一致():
    got = 判(版(7), 版(7))
    assert got.kind is DivergenceKind.IN_SYNC
    assert got.gap == 0


def test_两边都是空的也是一致():
    """服务器什么都没派,狗手上什么都没有 —— 这两边是同意的。"""
    assert 判(None, None).kind is DivergenceKind.IN_SYNC


def test_狗落后():
    got = 判(版(7), 版(9))
    assert got.kind is DivergenceKind.BEHIND
    assert got.gap == 2


def test_狗上一个包都没有也是落后():
    got = 判(None, 版(9))
    assert got.kind is DivergenceKind.BEHIND
    assert got.gap == 9


def test_狗超前():
    """**这一条不报出来,人会以为下发成功了。**

    服务器被回滚到 v7、狗手上还是 v9 —— 值守屏上要是只显示「已同步」,
    那台狗会带着一份已经被撤回的任务继续上岗,而且没有任何人知道。
    """
    got = 判(版(9), 版(7))
    assert got.kind is DivergenceKind.AHEAD
    assert got.gap == 2


def test_服务器那边什么都没有狗手上却有也是超前():
    got = 判(版(9), None)
    assert got.kind is DivergenceKind.AHEAD
    assert got.gap == 9


def test_从没同步过():
    """``last_sync_ms is None``。**这个优先于版本号的比较。**

    两边碰巧都是 v7 也不算「一致」—— 那是两个从没对过话的人报了同一个数字,
    而不是一次成功的同步。
    """
    got = 判(版(7), 版(7), last_sync_ms=None)
    assert got.kind is DivergenceKind.NEVER_SYNCED
    assert got.since_sync_s is None


def test_同一个版本号内容却不一样是冲突():
    """**这是 ``content_sha256`` 唯一要防的那件事。**

    §3.2 定死了版本号单调递增、同号不许换内容。真出现同号不同内容,说明
    包在路上被改过、或者有人手工动过盘 —— 报「一致」就等于把这道闸白设了。
    """
    got = 判(版(7, sha="a" * 64), 版(7, sha="b" * 64))
    assert got.kind is DivergenceKind.CONFLICT
    assert got.gap == 0


def test_两边根本不是同一个包也是冲突():
    """狗手上跑着**别的站点**的包。这比落后几版严重得多。"""
    got = 判(版(7, bid="site-kl"), 版(7, bid="site-jb"))
    assert got.kind is DivergenceKind.CONFLICT


def test_多久没同步了():
    got = 判(版(7), 版(7), last_sync_ms=现在 - 四小时)
    assert got.since_sync_s == pytest.approx(4 * 3600.0)


def test_最后同步的时刻在未来会报成负数():
    """**不夹到 0。**

    夹了就把「这台的钟是歪的」这条线索抹掉了,而那条线索恰好是
    ``clock_skew()`` 在另一头报的同一件事。
    """
    assert 判(版(7), 版(7), last_sync_ms=现在 + 1000).since_sync_s < 0


def test_上线的形状():
    got = 判(版(7), 版(9), last_sync_ms=现在 - 四小时)
    assert got.to_wire() == {
        "kind": "behind",
        "local": {"bundle_id": "site-kl", "version": 7,
                  "content_sha256": f"{7:064d}"},
        "intended": {"bundle_id": "site-kl", "version": 9,
                     "content_sha256": f"{9:064d}"},
        "gap": 2,
        "since_sync_s": 4 * 3600.0,
    }


def test_空的那一边上线是null():
    got = 判(None, 版(9)).to_wire()
    assert got["local"] is None


def test_从一份自述直接构一个ref():
    """服务器和狗两头都是从 ``bundle.yaml`` 拿这三个字段的。"""
    from d1max_agent.engine.bundle import parse_manifest
    m = parse_manifest({"bundle_id": "site-kl", "version": 7, "schema": 1,
                        "content_sha256": "a" * 64,
                        "built_at": "2026-09-07T14:03:00+08:00"})
    assert BundleRef.of(m) == BundleRef("site-kl", 7, "a" * 64)
