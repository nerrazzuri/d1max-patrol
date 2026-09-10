"""值守屏那六项(§5.1)。

**这一组防的是「屏上有个数,但那个数是编的」。** 六项的价值全在「静默失败
变可见」—— 一旦某一项在没有事实的时候报了个看着正常的数(``0``、``-1``、
空串),它就从「让人发现问题」翻面成了「让人放心地忽略问题」,比不显示更坏。
所以下面每一条断言的形状都是「不知道的时候必须是 ``None``」。

「这个模块只读」这条纪律在本文件里有**两道**守卫,值钱的是第一道:

* ``test_只读这条纪律_拿探针钉住`` / ``..._那条路由的处理器也罩在里面`` ——
  塞一个只放行白名单属性的假引擎进去,别的属性访问一律当场炸。它认的是
  **运行时的属性访问**,所以取别名、改方法名、把动作挪进 ``server.py`` 的
  处理器,全都当场炸。**这才是真正的钉子。**
* ``test_盘况屏那条规矩_这个模块只读`` —— 读源码里那几个字面量。它降级成了
  一条便宜的绊线:抓得住手滑,挡不住换个名字(挂账 66 说的正是这种名单)。
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import time
from pathlib import Path

import pytest

from d1max_patrol.app import watch
from d1max_patrol.app.alert_sources import _落了但没生效
from d1max_patrol.app.control import needs_lease
from d1max_patrol.app.server import AppServer, Request
from d1max_patrol.app.watch import watch_summary
from d1max_patrol.backends.base import BatteryEvent
from d1max_patrol.engine.backup import (
    SyncState,
    init_target,
    resolve_targets,
    write_sync_state,
)
from d1max_patrol.engine.removable import DiskRole, Removable
from tests.app import conftest as C
from tests.conftest import SomeDisks

WATCH = "/api/watch/summary"

#: 这一组测试统一用的那一刻(UTC 毫秒)。**写死**,不读真钟 —— 时间是这一卷
#: 的被测对象本身(§8.5 第 2 条)。
NOW_MS = 1_757_000_000_000


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


def 摆一趟(runs_root: Path, mission: str, stamp: str, *, size: int = 4096) -> Path:
    """在 ``runs_root`` 下摆一趟**跑完了**的归档。

    ``manifest.json`` 里必须有 ``summary`` —— ``scan_runs`` 靠它判「没人在写
    这一趟了」,而 ``plan_sync`` 会把没安定的整趟跳过。跳过了 ``behind`` 就恒
    为 0,于是这一组测试断的全是平凡不动点,把整个聚合循环删掉也不会红。
    """
    run = runs_root / mission / stamp
    (run / "photos").mkdir(parents=True)
    (run / "photos" / "a.jpg").write_bytes(b"x" * size)
    (run / "manifest.json").write_text(json.dumps(
        {"mission": {"name": mission}, "summary": {"photos": 1}},
        ensure_ascii=False), encoding="utf-8")
    return run


def 摆镜像盘(ctx, tmp_path: Path, 名: str, *, last_sync_ms: int | None = None,
          done: tuple[str, ...] = ()) -> Removable:
    """摆一块认成这台狗镜像盘的外插盘。

    ``last_sync_ms=None`` 就是「这块盘上一趟也没同步过」—— 那时候盘上根本没有
    进度文件,``read_sync_state`` 回的是默认值 ``0``,而 ``0`` 摆到屏上是
    1970-01-01。
    """
    mount = tmp_path / "media" / 名
    mount.mkdir(parents=True)
    init_target(mount, robot_sn=ctx.identity.sn, role=DiskRole.MIRROR,
                now_ms=NOW_MS)
    if last_sync_ms is not None:
        write_sync_state(mount, SyncState(robot_sn=ctx.identity.sn,
                                          last_sync_ms=last_sync_ms,
                                          done=frozenset(done)))
    return Removable(mount=mount, role=DiskRole.UNKNOWN, sn="")


def 认盘(ctx, *盘: Removable):
    return resolve_targets(list(盘), robot_sn=ctx.identity.sn)


class 只读引擎探针:
    """只放行白名单里那几个只读属性的假引擎,**别的属性访问一律当场炸**。

    这是「这个模块只读」那条纪律真正的钉子。守源码字面量的那条(下面那个
    ``test_盘况屏那条规矩_这个模块只读``)挡不住 ``eng = ctx.engine`` 换一行
    再调、挡不住换个方法名、更挡不住把动作写进 ``server.py`` 那个处理器 ——
    而那才是最顺手加动作的地方,处理器手上握着整个 ``self``,而文本扫描根本
    照不到那个文件(``server.py`` 里当然到处都是引擎动作)。

    这个探针三种全挡得住:它认的是**运行时的属性访问**,不是文本。
    """

    #: 值守屏这条路径正当读得到的引擎属性。``removable`` 是 ``ctx.removable``
    #: 这个只读属性转手要的(``_scan_targets`` 扫盘要它);除此之外这条路径
    #: 不该碰引擎上的任何东西。**白名单越短,这颗钉子越硬。**
    放行 = frozenset({"removable"})

    def __init__(self, 真引擎) -> None:
        self._真引擎 = 真引擎
        self._严格 = False

    @contextlib.contextmanager
    def 上膛(self):
        """**只在这一段里严格**,外面照常转手给真引擎。

        造一个 ``AppServer`` 本身就要读引擎快照(``AlertSources`` 起手要判
        「现在是不是正跑着一趟」),那是它的本分,不归这条纪律管。这条纪律
        管的是**值守屏那一次取数**,所以只罩那一段。
        """
        self._严格 = True
        try:
            yield
        finally:
            self._严格 = False

    def __getattr__(self, 名: str):
        if self._严格 and 名 not in 只读引擎探针.放行:
            raise AssertionError(
                f"值守屏这条路径碰了 ctx.engine.{名} —— 这个模块只读,一个会"
                f"改狗的动作都不许有(白名单:{sorted(只读引擎探针.放行)})")
        return getattr(self._真引擎, 名)


@pytest.fixture
def 探针ctx(bridge, tmp_path):
    """一份 ``engine`` 被换成 :class:`只读引擎探针` 的上下文。"""
    ctx = C.make_ctx(bridge, tmp_path)
    return dataclasses.replace(ctx, engine=只读引擎探针(ctx.engine))


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
    #
    # 断的是**承载语义的那半句**,不是措辞。原来断的是「第 9 卷」——
    # 一句排期黑话(站在屏前面的人不知道那是什么),而且把它锁死了:想把这
    # 句话润色成人话,得先改测试,而改测试是有心理阻力的。
    assert "不是「没有积压」" in got["detail"]["upload_backlog"]


def test_每一项都指得出出处(装好的ctx):
    """六项的价值全在「静默失败变可见」,所以每一项都要能说出它从哪儿来。"""
    got = watch_summary(装好的ctx(), now_ms=0, battery_pct=31,
                        disk=lambda: (83, 100))
    assert got["battery_pct"] == 31
    assert got["disk_pct"] == pytest.approx(0.83)


def test_盘况屏那条规矩_这个模块只读():
    """**这条只是一根便宜的绊线,真正的钉子是下面那两条探针测试。**

    绊线的价值就一样:手滑的时候当场绊一下,比评审早。它守不住的东西是明摆
    着的 —— ``eng = ctx.engine`` 换一行再调、换一个不在这份名单上的方法名、
    把动作挪去 ``server.py`` 那个处理器(这个文件根本不在扫描范围里),
    每一条都是一行就溜得过去。挂账 66 说的正是这种「换了皮的名单」。

    所以别把它当成保障:保障在 ``test_只读这条纪律_拿探针钉住`` 和
    ``test_只读这条纪律_那条路由的处理器也罩在里面``,那两条认的是运行时的
    属性访问,不是文本。

    这条绊线的副作用是 ``watch.py`` 的散文里不能出现下面这几个字面量 ——
    要在那份 docstring 里解释这条纪律,只能绕着说。
    """
    src = Path(watch.__file__).read_text(encoding="utf-8")
    assert "POST" not in src
    for 会改狗的 in ("engine.start", "engine.abort", "engine.suspend",
                    "engine.resume", "engine.pause", "walk("):
        assert 会改狗的 not in src


def test_只读这条纪律_拿探针钉住(探针ctx):
    """**塞一个只放行只读属性的假引擎进去,别的属性访问一律当场炸。**

    上面那条绊线守的是六个字面量,任务书要的是「不许调任何 ``ctx.engine.*``
    的非属性方法」—— 两件事差着一整类绕法。这条守的是后者:取别名、改名、
    换方法,全部在运行时炸,因为炸的是属性访问本身。
    """
    with 探针ctx.engine.上膛():
        got = watch_summary(探针ctx, now_ms=NOW_MS, targets=())
    assert set(got) >= 六项


def test_只读这条纪律_那条路由的处理器也罩在里面(探针ctx):
    """**同一条纪律要盖到路由处理器上** —— 那才是最顺手加动作的地方。

    处理器手上握着整个 ``self``,而读源码那条绊线照不到 ``server.py``
    (那个文件里当然到处都是引擎动作)。探针照得到。

    这里**不 start 服务,直接调处理器**:``_StateHub`` 起来之后会在循环线程
    上反复读引擎快照 —— 那是它的本分,不归这条纪律管,让探针在那儿炸只会
    得到一条读不懂的后台报错。要罩住的是 ``_watch_summary`` 这一段。
    """
    s = AppServer(探针ctx, port=0)
    with 探针ctx.engine.上膛():
        resp = s._watch_summary(Request(method="GET", path=WATCH, params={},
                                        query={}, body=b""))
    got = json.loads(resp.body)
    assert set(got) >= 六项
    # 扫盘那一跳真的走通了(它过 ``ctx.removable`` -> ``engine.removable``),
    # 否则这条测试会退化成"处理器压根没碰引擎"的空断言。
    assert got["mirror"] is not None


def test_报不出来的每一项都得有一句为什么(装好的ctx):
    """挂账 79 的不变量:私有取数函数用**空串**表示「这一项没话说」。

    谁不小心返回了 ``(None, "")`` —— 值是不知道但话是空的 —— 屏上就多一个
    没有解释的 ``null``,而那正是模块 docstring 明令禁止的东西:光秃秃一个
    null 摆在屏上,人分不清是没查还是查了没事。

    这条罩住所有字段,比逐字段断言耐久:将来新加一格也自动被它守着。
    """
    got = watch_summary(装好的ctx(), now_ms=0)   # 什么都不注入 = 最多的 None
    for 名 in 六项:
        if got[名] is None:
            assert got["detail"].get(名), f"{名} 是 null 却没有一句解释"


def test_屏上带一个未处理告警的计数(装好的ctx):
    """**「六项全绿 + 屏上没有告警入口」会被读成「这台狗没事」。**

    详情不并进这条只读汇总:告警簿有自己的生命周期(挂起、确认、清除),
    并进来就把一条只读汇总绑上了那台状态机。但一个数要带上,而计数是
    O(1) 的,不带来性能账也不带来耦合;详情走 ``/api/alerts`` 那几条路由。
    """
    ctx = 装好的ctx()
    assert watch_summary(ctx, now_ms=0)["alerts_open"] == 0
    ctx.alerts.raise_alert(kind="disk_80", robot=ctx.identity.sn,
                           title="盘快满了", now_ms=0)
    assert watch_summary(ctx, now_ms=0)["alerts_open"] == 1


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
    # 没收到过遥测,那「什么时候收到的」当然也不知道 —— 不是 0(1970 年)。
    assert got["battery_as_of_ms"] is None


def test_电量真的是0的时候报的就是0不是不知道(装好的ctx):
    """挂账 80:这一档判的是 ``is None``,**不是** truthiness。

    写成 truthiness 的那一边,一台真的趴在地上没电的狗会被翻译成「还没收到
    电量遥测」—— 人会去查链路,而该做的事是去把狗抱回来充电。这两句话在现场
    差着一次事故,而这个分支上一轮零测试零裁剪。
    """
    got = watch_summary(装好的ctx(), now_ms=0, battery_pct=0,
                        battery_as_of_ms=NOW_MS)
    assert got["battery_pct"] == 0
    assert "battery_pct" not in got["detail"]


def test_电量带着它是什么时候收到的(装好的ctx):
    """**一个不再更新的数比 ``None`` 更危险,因为它看起来像在更新。**

    电量是事件推出来的,链路断了它不会自己变回 ``None``,只会一直停在最后
    一个读数上:狗在地下室断链 40 分钟,屏上稳稳写着 31%。值班的人看到 31%
    的判断是「还能撑一会儿」,而正确的判断是「我已经 40 分钟不知道它的电量
    了,而它上次报的时候只剩 31%」。

    这一档**不设阈值、不替人判「多久算旧」**,只把时刻摆出来 —— 带
    ``as_of`` 的不是撒谎,不带的才是。
    """
    got = watch_summary(装好的ctx(), now_ms=NOW_MS + 40 * 60 * 1000,
                        battery_pct=31, battery_as_of_ms=NOW_MS)
    assert got["battery_pct"] == 31
    assert got["battery_as_of_ms"] == NOW_MS


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
    """§7.6 那两件事:盘还在不在,以及它落后多少。

    **归档必须真的摆几趟进去。** 上一轮这条测试跑在一个空 ``runs_root`` 上,
    四个字段天然全 0 —— 把 ``_镜像`` 里那整个聚合循环删掉、直接写死四个 0,
    本文件一条测试都不会红。那正是上一轮那个「上次同步时刻取最新的」的
    bug 能活到评审的直接原因:断的不是判据的输出,是这个场景下判据的**平凡
    不动点**。
    """
    ctx = C.make_ctx(bridge, tmp_path)
    for stamp in ("20250901T010203Z", "20250902T010203Z", "20250903T010203Z"):
        摆一趟(ctx.runs_root, "一号厂房", stamp)
    targets = 认盘(ctx, 摆镜像盘(ctx, tmp_path, "u1", last_sync_ms=NOW_MS - 3600_000))
    got = watch_summary(ctx, now_ms=NOW_MS, targets=targets)
    assert got["mirror"]["disks"] == 1
    assert got["mirror"]["usable"] == 1
    assert got["mirror"]["measured"] == 1
    # 摆了 3 趟,一趟也没拷过去 —— 这个 3 必须是真算出来的。
    assert got["mirror"]["behind"] == 3
    assert got["mirror"]["behind_bytes"] >= 3 * 4096
    assert got["mirror"]["full"] is False
    assert got["mirror"]["last_sync_ms"] == NOW_MS - 3600_000
    assert "mirror" not in got["detail"]


def test_已经拷过去的那几趟不再算进落后量(bridge, tmp_path):
    """落后量得跟着盘上的进度走,**不是「归档有几趟」的另一种写法**。

    没有这一条,``behind`` 报成 ``len(scan_runs(...))`` 也一样绿。
    """
    ctx = C.make_ctx(bridge, tmp_path)
    for stamp in ("20250901T010203Z", "20250902T010203Z", "20250903T010203Z"):
        摆一趟(ctx.runs_root, "一号厂房", stamp)
    targets = 认盘(ctx, 摆镜像盘(ctx, tmp_path, "u1", last_sync_ms=NOW_MS,
                            done=("一号厂房/20250901T010203Z",
                                  "一号厂房/20250902T010203Z")))
    got = watch_summary(ctx, now_ms=NOW_MS, targets=targets)
    assert got["mirror"]["behind"] == 1


def test_两块盘的落后量报的是最落后的那一块(bridge, tmp_path):
    """悲观聚合:**只要有一块盘落后,这台狗的备份就是落后的。**"""
    ctx = C.make_ctx(bridge, tmp_path)
    全部 = ["一号厂房/20250901T010203Z", "一号厂房/20250902T010203Z",
          "一号厂房/20250903T010203Z"]
    for stamp in ("20250901T010203Z", "20250902T010203Z", "20250903T010203Z"):
        摆一趟(ctx.runs_root, "一号厂房", stamp)
    targets = 认盘(
        ctx,
        # 勤快的那块:三趟全拷过去了,自己算落后 0 趟。
        摆镜像盘(ctx, tmp_path, "u1", last_sync_ms=NOW_MS, done=tuple(全部)),
        # 掉队的那块:一趟也没拷。
        摆镜像盘(ctx, tmp_path, "u2", last_sync_ms=NOW_MS))
    got = watch_summary(ctx, now_ms=NOW_MS, targets=targets)
    assert got["mirror"]["measured"] == 2
    assert got["mirror"]["behind"] == 3


def test_两块盘的上次同步时刻报的是最旧的那一块(bridge, tmp_path):
    """**取最新的那一边,一块死掉的备份盘会被边上的好盘整个盖住。**

    一台狗插两块镜像盘:U1 每晚正常同步,U2 三周前就被人拔去当 U 盘格式化
    了 —— 重新插回来标记还在,``usable`` 为真,读回来的是三周前那个时间戳。
    取最新的那一边,屏上写着「两块盘都在,昨晚刚同步过」,值班的人放心地
    走开。§7.6 那一行原话要拦的就是这件事:一块坏掉或者被刷没了的备份盘,
    一切看起来都正常,直到你需要它。

    ``behind`` 拦不住它:U2 只要归档还在盘上,落后量可能只有个位数甚至 0,
    那时候**唯一的信号就是这个时间戳**。
    """
    ctx = C.make_ctx(bridge, tmp_path)
    三周前, 昨晚 = NOW_MS - 21 * 86400_000, NOW_MS - 12 * 3600_000
    targets = 认盘(ctx,
                 摆镜像盘(ctx, tmp_path, "u1", last_sync_ms=昨晚),
                 摆镜像盘(ctx, tmp_path, "u2", last_sync_ms=三周前))
    got = watch_summary(ctx, now_ms=NOW_MS, targets=targets)
    assert got["mirror"]["last_sync_ms"] == 三周前


def test_一趟也没同步过的盘不许报成1970年(bridge, tmp_path):
    """``read_sync_state`` 拼不出进度时给的是 ``0``,而 ``0`` 摆到屏上是
    1970-01-01 —— **一个看着像真事的假时刻,比 ``null`` 更坏**。

    这跟上面那条是同一个 bug 的两半:聚合改成取最旧的之后,一块从来没同步过
    的盘会把整档拉到 ``0``。只改方向不翻 ``0``,等于把「从来没备份过」显示成
    「1970 年备份过」。
    """
    ctx = C.make_ctx(bridge, tmp_path)
    targets = 认盘(ctx, 摆镜像盘(ctx, tmp_path, "u1"))    # 不写进度文件
    got = watch_summary(ctx, now_ms=NOW_MS, targets=targets)
    assert got["mirror"]["last_sync_ms"] is None
    assert "1970" in got["detail"]["mirror"]


def test_有镜像盘这一拍读不了的时候屏上必须说出来(bridge, tmp_path, monkeypatch):
    """**在一块以消灭静默失败为职责的屏上,不许自己造一个静默失败。**

    三块盘坏两块的时候,``usable`` 照报 3、``behind`` 照报 0、``detail`` 里
    一个字没有 —— 值班的人得到的信息是「三块备份盘都在,一趟都不落后」,
    而事实是三块里只有一块被真正看过。这比报 ``null`` 更糟:它连 ``null``
    都不给,给的是一个看着正常的数。
    """
    ctx = C.make_ctx(bridge, tmp_path)
    摆一趟(ctx.runs_root, "一号厂房", "20250901T010203Z")
    targets = 认盘(ctx,
                 摆镜像盘(ctx, tmp_path, "u1", last_sync_ms=NOW_MS),
                 摆镜像盘(ctx, tmp_path, "u2", last_sync_ms=NOW_MS))
    坏的 = tmp_path / "media" / "u2"
    真的 = watch.plan_sync

    def 半路炸(runs_root, mount, **kw):
        if Path(mount) == 坏的:
            raise OSError("stale NFS file handle")
        return 真的(runs_root, mount, **kw)

    monkeypatch.setattr(watch, "plan_sync", 半路炸)
    got = watch_summary(ctx, now_ms=NOW_MS, targets=targets)
    # 盘上的标记**声称**两块能用,这一拍真读出来的只有一块 —— 两个数都要报。
    assert got["mirror"]["usable"] == 2
    assert got["mirror"]["measured"] == 1
    assert "读不了" in got["detail"]["mirror"]


def test_只要有一块镜像盘满了整台狗就报满(bridge, tmp_path, monkeypatch):
    """满盘是悲观聚合里最容易被乐观掉的那一格。

    满盘的意思是「同步从这一刻起停住了」,而屏上只看得到 ``behind`` 在涨。
    两块盘里满了一块,报的必须是满 —— 拿边上那块还有空间的盘把它盖掉,等于
    帮着把一条已经断掉的备份链藏起来,跟 ``last_sync_ms`` 取最新的那一半是
    同一个错。

    这一格没法靠真造一块满盘来钉(得占满一整块真盘),所以在 ``plan_sync``
    这一跳上换掉其中一块的结论。
    """
    ctx = C.make_ctx(bridge, tmp_path)
    摆一趟(ctx.runs_root, "一号厂房", "20250901T010203Z")
    targets = 认盘(ctx,
                 摆镜像盘(ctx, tmp_path, "u1", last_sync_ms=NOW_MS),
                 摆镜像盘(ctx, tmp_path, "u2", last_sync_ms=NOW_MS))
    满的 = tmp_path / "media" / "u2"
    真的 = watch.plan_sync

    def 一块报满(runs_root, mount, **kw):
        plan = 真的(runs_root, mount, **kw)
        return dataclasses.replace(plan, full=True) if Path(mount) == 满的 else plan

    monkeypatch.setattr(watch, "plan_sync", 一块报满)
    got = watch_summary(ctx, now_ms=NOW_MS, targets=targets)
    assert got["mirror"]["measured"] == 2
    assert got["mirror"]["full"] is True


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
    # 电量和「这个数什么时候收到的」是**配对的一次读**:``_StateHub`` 把它们
    # 存在同一个字段里,不许分两次读 —— 两次读之间夹进一个新事件,屏上就会
    # 出现「刚刚收到的」配着上一拍的数值。
    assert got["battery_as_of_ms"] is not None
    assert got["battery_as_of_ms"] > 0


def test_路由上一个电量事件都没来过的时候接收时刻是不知道(值守服务):
    """没收到过遥测,那「什么时候收到的」也是不知道 —— **不是 0**(1970 年)。"""
    _, s = 值守服务
    got = C.get_json(s, WATCH)
    assert got["battery_pct"] is None
    assert got["battery_as_of_ms"] is None


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
