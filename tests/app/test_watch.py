"""值守屏那六项(§5.1)。

**这一组防的是「屏上有个数,但那个数是编的」。** 六项的价值全在「静默失败
变可见」—— 一旦某一项在没有事实的时候报了个看着正常的数(``0``、``-1``、
空串),它就从「让人发现问题」翻面成了「让人放心地忽略问题」,比不显示更坏。
所以下面每一条断言的形状都是「不知道的时候必须是 ``None``」。

最后那条 ``test_盘况屏那条规矩_这个模块只读`` 是本文件里最值钱的一条:它
**读源码**,不认名单。挂账 66 的教训是盘况屏的例外名单按 ``refreshKey``
认,而名单挡不住有人换个 key 把功能挂进去 —— 名字是可以换的,源码里那几个
字面量不是。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from d1max_patrol.app import watch
from d1max_patrol.app.alert_sources import _落了但没生效
from d1max_patrol.app.control import needs_lease
from d1max_patrol.app.server import AppServer
from d1max_patrol.app.watch import watch_summary
from d1max_patrol.backends.base import BatteryEvent
from d1max_patrol.engine.backup import init_target, resolve_targets
from d1max_patrol.engine.removable import DiskRole, Removable
from tests.app import conftest as C
from tests.conftest import SomeDisks

WATCH = "/api/watch/summary"


async def _发(emitter, event) -> None:
    """在循环线程里推一个事件出去。``EventEmitter`` 的订阅者列表不带锁。"""
    emitter.emit(event)

#: 六项。**一个不少** —— 少一项就是屏上少一块玻璃。
六项 = {"bundle_lag", "clock_skew_s", "upload_backlog",
        "disk_pct", "battery_pct", "mirror"}


@pytest.fixture
def 装好的ctx(bridge, tmp_path):
    """造一份上下文的工厂。``盘`` 是已用比例,``电量`` 是 0-100 的百分数。

    ``盘`` 走的是注入口(``disk=``)而不是真去量临时目录所在的那块盘:量真盘
    的那一边,这条测试的结果取决于跑测试的这台机器上 D 盘还剩多少。
    """
    def 造(**kw):
        return C.make_ctx(bridge, tmp_path, **kw)
    return 造


def 摆包(root: Path, 槽名: list[str], *, current: str = "") -> Path:
    """在 ``root`` 里摆几个槽,可选地把 ``current`` 链指到其中一个。

    跟 ``tests/app/test_alert_sources.py`` 里那个同名助手是一回事:这一组测
    的是「盘上是什么局面 -> 屏上报什么」,不是打包和换链本身。
    """
    root.mkdir(parents=True, exist_ok=True)
    for 名 in 槽名:
        (root / 名).mkdir(exist_ok=True)
    if current:
        os.symlink(root / current, root / "current", target_is_directory=True)
    return root


# ------------------------------------------------------------------ 六项本身


def test_六项一个不少(装好的ctx):
    got = watch_summary(装好的ctx(), now_ms=0)
    assert set(got) >= 六项


def test_回传积压这一档还没有_是None不是0(装好的ctx):
    """``0`` 是「查过了没积压」,``None`` 是「这一档没这个能力」。

    拿 ``0`` 冒充,人会以为证据都传上去了 —— 而它们全在狗上堆着。
    """
    got = watch_summary(装好的ctx(), now_ms=0)
    assert got["upload_backlog"] is None
    # 光秃秃一个 null 摆在屏上,人分不清是没查还是查了没事 —— 那本身就是
    # 一次新的静默失败。
    assert "第 9 卷" in got["detail"]["upload_backlog"]


def test_每一项都指得出出处(装好的ctx):
    """六项的价值全在「静默失败变可见」,所以每一项都要能说出它从哪儿来。"""
    got = watch_summary(装好的ctx(), now_ms=0, battery_pct=31,
                        disk=lambda: (83, 100))
    assert got["battery_pct"] == 31
    assert got["disk_pct"] == pytest.approx(0.83)


def test_盘况屏那条规矩_这个模块只读():
    """挂账 66 的同一根:例外名单按 key 认,挡不住换个 key 挂功能。

    这里换一种守法 —— 守**源码**,不守名单。
    """
    src = Path(watch.__file__).read_text(encoding="utf-8")
    assert "POST" not in src
    for 会改狗的 in ("engine.start", "engine.abort", "engine.suspend",
                    "engine.resume", "engine.pause", "walk("):
        assert 会改狗的 not in src


# ------------------------------------------------------------------ 钟偏


def test_断网的时候钟偏是不知道不是0(装好的ctx):
    """**报 0 等于说「钟是准的」**,而那正是断网久了之后最不可能成立的一句话。

    ``AppContext.time_reference`` 拿不到参照时回的就是 ``None``(单机档一直
    是这样),这一档必须照实报「不知道」。
    """
    got = watch_summary(装好的ctx(time_reference=lambda: None), now_ms=1000)
    assert got["clock_skew_s"] is None
    assert "不知道" in got["detail"]["clock_skew_s"]


def test_有参照的时候钟偏算得出来(装好的ctx):
    ctx = 装好的ctx(time_reference=lambda: (95_000, "ntp"))
    got = watch_summary(ctx, now_ms=100_000)
    assert got["clock_skew_s"] == pytest.approx(5.0)


def test_钟偏用的是传进来的那个时刻不是现读的钟(装好的ctx):
    """时间必须可注入(§8.5 第 2 条):同一份 ctx,``now_ms`` 变了结果就得变。"""
    ctx = 装好的ctx(time_reference=lambda: (0, "ntp"))
    assert watch_summary(ctx, now_ms=7_000)["clock_skew_s"] == pytest.approx(7.0)
    assert watch_summary(ctx, now_ms=9_000)["clock_skew_s"] == pytest.approx(9.0)


# ------------------------------------------------------------------ 任务包落差


def test_任务包落了但没生效_报的是告警簿那一份判据(装好的ctx, tmp_path):
    """§3.4 那条失效模式:**人以为改生效了,其实没有。**

    判据必须跟 ``bundle_lag`` 那条 P2 告警同一份(``_落了但没生效``)。在这
    儿另写一份的话,同一件事会有两套说法 —— 屏上写着「没落差」,告警栏里挂
    着一条 ``bundle_lag``,现场没人知道该信哪个。
    """
    root = 摆包(tmp_path / "bundles", ["site-kl-3", "site-kl-4"],
              current="site-kl-3")
    got = watch_summary(装好的ctx(bundles_root=root), now_ms=0)
    assert got["bundle_lag"] == ["site-kl-4"]
    assert tuple(got["bundle_lag"]) == _落了但没生效(root)


def test_没有落差的时候是空的不是不知道(装好的ctx, tmp_path):
    """空列表是「翻过盘了,没落差」;``None`` 是「读不出来」。两件事。"""
    root = 摆包(tmp_path / "bundles", ["site-kl-4"], current="site-kl-4")
    got = watch_summary(装好的ctx(bundles_root=root), now_ms=0)
    assert got["bundle_lag"] == []
    assert "bundle_lag" not in got["detail"]


def test_读不出盘上局面的时候是不知道(装好的ctx, monkeypatch):
    """读不出来就说读不出来。**不许翻成「没有落差」** —— 那是一句假的好消息。"""
    def 炸(_root):
        raise OSError("stale NFS file handle")

    monkeypatch.setattr(watch, "_落了但没生效", 炸)
    got = watch_summary(装好的ctx(), now_ms=0)
    assert got["bundle_lag"] is None
    assert "读不出" in got["detail"]["bundle_lag"]


# ------------------------------------------------------------------ 盘水位与电量


def test_盘水位量不出来的时候是不知道不是0(装好的ctx):
    """报 0 等于说「盘是空的」—— 而这一档存在的理由正是「盘快满了」。"""
    got = watch_summary(装好的ctx(), now_ms=0, disk=lambda: (0, 0))
    assert got["disk_pct"] is None


def test_还没收到电量遥测的时候是不知道不是0(装好的ctx):
    """``0`` 在电量这一档上尤其毒:它看着像「快没电了」,人会去救一台好狗,
    也会在真没电的那次以为又是这个假数。"""
    got = watch_summary(装好的ctx(), now_ms=0)
    assert got["battery_pct"] is None
    assert "不知道" in got["detail"]["battery_pct"]


# ------------------------------------------------------------------ 镜像盘


def test_一块镜像盘都没有的时候落后量是不知道不是0(装好的ctx):
    """**「落后 0 趟」是一句听着让人放心的假话** —— 一块盘都没有的时候,
    归档只有一份,那正是 §7.6 要人看见的事。"""
    got = watch_summary(装好的ctx(), now_ms=0, targets=())
    assert got["mirror"]["disks"] == 0
    assert got["mirror"]["behind"] is None
    assert "一块镜像盘都没认到" in got["detail"]["mirror"]


def test_扫不出盘的时候镜像这一档整个是不知道(装好的ctx):
    got = watch_summary(装好的ctx(), now_ms=0, targets=None)
    assert got["mirror"] is None
    assert "认不出" in got["detail"]["mirror"]


def test_认到镜像盘就报得出落后多少趟(bridge, tmp_path):
    """§7.6 那两件事:盘还在不在,以及它落后多少。"""
    mount = tmp_path / "media" / "u1"
    mount.mkdir(parents=True)
    ctx = C.make_ctx(bridge, tmp_path)
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    targets = resolve_targets(
        [Removable(mount=mount, role=DiskRole.UNKNOWN, sn="")],
        robot_sn=ctx.identity.sn)
    got = watch_summary(ctx, now_ms=1_757_000_000_000, targets=targets)
    assert got["mirror"] == {"disks": 1, "usable": 1, "behind": 0,
                             "behind_bytes": 0, "full": False,
                             "last_sync_ms": 0}
    assert "mirror" not in got["detail"]


# ------------------------------------------------------------------ 路由


@pytest.fixture
def 值守服务(bridge, tmp_path):
    ctx = C.make_ctx(bridge, tmp_path)
    s = AppServer(ctx, port=0)
    s.start()
    yield ctx, s
    s.stop()


def test_路由回的就是那六项(值守服务):
    _, s = 值守服务
    got = C.get_json(s, WATCH)
    assert set(got) >= 六项


def test_路由上的电量来自状态快照(值守服务):
    """**不现问后端。** 厂商后端上问一次电量是一次真实的链路往返,而这一屏
    是按秒刷的;快照是事件推出来的,读它不花一分钱。"""
    ctx, s = 值守服务
    ctx.bridge.spawn(lambda: _发(ctx.device, BatteryEvent(31.0)))
    截止 = time.monotonic() + 5.0
    while time.monotonic() < 截止:
        got = C.get_json(s, WATCH)
        if got["battery_pct"] == 31.0:
            break
        time.sleep(0.05)
    assert got["battery_pct"] == 31.0


def test_路由上的盘水位是真量出来的(值守服务):
    _, s = 值守服务
    got = C.get_json(s, WATCH)
    assert 0.0 < got["disk_pct"] <= 1.0


def test_扫盘炸了这一屏照样答完(bridge, tmp_path):
    """**值守屏是最后一块必须还能亮的玻璃。** 探针卡住不该把盘水位和电量
    一起带走 —— 那正是人在盘出事的时候要看的两项。"""
    class BoomProbe:
        async def scan(self):
            raise OSError("stale NFS file handle")

    ctx = C.make_ctx(bridge, tmp_path, removable=BoomProbe())
    s = AppServer(ctx, port=0)
    s.start()
    try:
        got = C.get_json(s, WATCH)
    finally:
        s.stop()
    assert got["mirror"] is None
    assert got["disk_pct"] is not None


def test_插着镜像盘的时候路由也报得出来(bridge, tmp_path):
    mount = tmp_path / "media" / "u1"
    mount.mkdir(parents=True)
    ctx = C.make_ctx(bridge, tmp_path, removable=SomeDisks(
        Removable(mount=mount, role=DiskRole.UNKNOWN, sn="")))
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    s = AppServer(ctx, port=0)
    s.start()
    try:
        got = C.get_json(s, WATCH)
    finally:
        s.stop()
    assert got["mirror"]["disks"] == 1
    assert got["mirror"]["behind"] == 0


def test_这条路由不要控制权(值守服务):
    """``CONTROLLED`` 那张表是「要控制权才能调」的**写**路由。只读的一屏
    进了那张表,就等于别人握着控制权的时候值守的人连看都看不了。"""
    assert not needs_lease("GET", WATCH)


def test_这条路由是只读的(值守服务):
    """挂个写方法上去要 405,不是 200。"""
    _, s = 值守服务
    code, body, _ = C.request(s, WATCH, method="PUT", payload={})
    assert code == 405, body
    assert json.loads(body)["error"]
