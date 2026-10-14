"""定位器经本机桥给的位姿,代理这边怎么判能不能信(W09a,W08 决定 3、4)。单元测试:假桥、手拨的
单调钟。"""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from d1max_agent.bridge_localizer import (
    AUTO_RELOC_SIGMA_M,
    DR_MAX_M,
    FRESH_S,
    SETTLE_FIXES,
    SIGMA_LOST_M,
    BridgeLocalizer,
)
from d1max_agent.localization import compose, inverse
from d1max_contract.locbridge import Pose, Relocalize, Reply, SetPrior, State

M = ("estate-1", "7")


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class 假桥:
    """记下发过的请求;按类型回。``down`` 为真时像没连上;``hang`` 是还要「不回」几次;
    ``before_reply`` 在回复之前调(模拟那一刻定位器那边又进来了别的报文)。"""

    def __init__(self):
        self.sent = []
        self.answer = {"set_prior": (True, ""), "relocalize": (True, "")}
        self.hang = 0
        self.down = False
        self.before_reply = None

    async def request(self, make, timeout_s):
        from d1max_agent.bridge_localizer import LocalizerUnavailable
        if self.down:
            raise LocalizerUnavailable("定位器没连上")
        req = len(self.sent) + 1
        msg = make(req)
        self.sent.append(msg)
        if self.hang:
            self.hang -= 1
            raise asyncio.TimeoutError()
        if self.before_reply is not None:
            self.before_reply(msg)
        ok, reason = self.answer[msg.T]
        return Reply(req=req, ok=ok, reason=reason)


_seq = [0]
_钟 = [None]


def _位姿(x, y, yaw=0.0, *, sigma=0.1, jump=False, m=M, source="scan_match", reloc_id=None,
         stamp=None, meas_age_ms=0):
    """时间戳默认按手拨的钟(再加序号个纳秒,钟没拨的时候也严格递增)。"""
    _seq[0] += 1
    if stamp is None:
        stamp = round(_钟[0].t * 1e9) + _seq[0]
    return Pose(seq=_seq[0], stamp_ns=stamp, map_id=m[0], map_version=m[1], x=x, y=y, yaw=yaw,
                sigma_xy=sigma, sigma_yaw=0.02, source=source, jump=jump, reloc_id=reloc_id,
                meas_age_ms=meas_age_ms)


def _稳(loc, x, y, yaw=0.0, **kw):
    """连上(或跳过)之后的第一帧要稳 SETTLE_FIXES 帧:一口气给够。"""
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(x, y, yaw, **kw))
    assert loc.ok(True), loc.why_not(True)


async def _就绪(odom=(0.0, 0.0, 0.0)):
    """连上、换好先验、里程在 ``odom``,还没有位姿。"""
    c, link = 钟(), 假桥()
    _钟[0] = c
    loc = BridgeLocalizer(monotonic=c)
    loc.link = link
    loc.on_map(M, "/maps/estate-1/7")
    loc.update(odom, True)
    loc.on_connect()
    await loc.prior_task
    return c, link, loc


def _近(a, b, tol=1e-6):
    return all(math.isclose(u, v, abs_tol=tol) for u, v in zip(a, b, strict=True))


async def test_没连上_在换图_还没位置_都不可信_换好先验给了位置稳下来就可信():
    c, link = 钟(), 假桥()
    _钟[0] = c
    loc = BridgeLocalizer(monotonic=c)
    loc.link = link
    assert "没连上" in loc.why_not(True)
    loc.on_map(M, "/maps/estate-1/7")
    loc.on_connect()
    assert "换图" in loc.why_not(True), "换先验回 ok 之前"
    await loc.prior_task
    assert link.sent == [SetPrior(req=1, map_id="estate-1", map_version="7",
                                  dir="/maps/estate-1/7")]
    assert "还没给出位置" in loc.why_not(True)
    loc.update((0.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(3.0, 4.0, 0.5))
    # 第一帧不直接信(内审应修 6):初始化错了还报 tracking 的,不然抓不住。
    assert "刚连上" in loc.why_not(True)
    assert loc.anchored
    _稳(loc, 3.0, 4.0, 0.5)
    e = loc.estimate((0.0, 0.0, 0.0))
    assert (e.x, e.y, e.yaw, e.source) == (3.0, 4.0, 0.5, "scan_match")
    assert (e.map_id, e.map_version) == M
    w = loc.to_wire(True)
    assert w["localizer"] == "bridge" and w["anchored"] is True and w["reason"] == ""
    assert w["source"] == "scan_match" and w["sigma_m"] == 0.1


async def test_换不了先验_说清楚():
    c, link = 钟(), 假桥()
    link.answer["set_prior"] = (False, "没有这张图的点云")
    loc = BridgeLocalizer(monotonic=c)
    loc.link = link
    loc.on_map(M, "")
    loc.on_connect()
    await loc.prior_task
    assert "没有这张图的点云" in loc.why_not(True)


async def test_换先验没回_再发_回了就好():
    """三维先验载入可能很久(W09c):没回不能就此放弃、一直丢定位。"""
    c, link = 钟(), 假桥()
    link.hang = 2
    loc = BridgeLocalizer(monotonic=c)
    loc.link = link
    loc.on_map(M, "")
    loc.on_connect()
    await loc.prior_task
    assert [type(m) for m in link.sent] == [SetPrior] * 3
    assert "换图" not in loc.why_not(True) and "先验" not in loc.why_not(True)


async def test_两次定位之间用里程推算_有上限():
    c, link, loc = await _就绪()
    _稳(loc, 10.0, 5.0, math.pi / 2)                     # 此刻里程在原点、朝东;地图上朝北
    c.t += 0.3
    loc.update((0.4, 0.0, 0.0), True)                   # 往前 0.4 m
    e = loc.estimate((0.4, 0.0, 0.0))
    assert (round(e.x, 3), round(e.y, 3)) == (10.0, 5.4) and loc.ok(True)
    c.t += 0.5                                           # 0.8 s 没来:推算,照用
    loc.update((0.8, 0.0, 0.0), True)
    assert loc.ok(True) and loc.estimate((0.8, 0.0, 0.0)).source == "dead_reckoning"
    assert loc.sigma_xy > 0.1, "推算的时候 σ 往上涨"
    loc.update((0.8 + DR_MAX_M, 0.0, 0.0), True)         # 推算超过 1 m
    assert "没来位姿" in loc.why_not(True)


async def test_推算有时间上限_里程不新鲜不推算():
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    c.t += 2.1
    assert "没来位姿" in loc.why_not(True), "2 s 没来:不再推"
    loc.on_pose(_位姿(1.0, 1.0))
    loc.update((0.0, 0.0, 0.0), False)                   # 里程断了
    c.t += FRESH_S - 0.1
    assert loc.ok(False), "0.5 s 内那一帧照用"
    assert loc.estimate((5.0, 5.0, 0.0)).x == 1.0, "里程不新鲜:不拿它推"
    c.t += 0.2
    assert "没来位姿" in loc.why_not(False)


async def test_调用方说里程不新鲜_也不推算():
    """导航桥每拍喂的里程还新鲜,可调用方(设位置、标原点)此刻读到的里程不新鲜:按调用方说的,不推。"""
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    c.t += 0.8
    loc.update((0.1, 0.0, 0.0), True)
    assert loc.ok(True)
    assert "没来位姿" in loc.why_not(False)


async def test_里程一拍只转了个大角度_也换段_不再推算():
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    c.t += 0.7
    loc.update((0.1, 0.0, 1.5), True)                    # 挪得不多、一拍转了 1.5 rad:里程跳了
    assert "没来位姿" in loc.why_not(True)


async def test_里程一拍跳了_不再推算():
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    c.t += 0.7
    loc.update((3.0, 0.0, 0.0), True)                    # 一拍 3 m:里程跳了
    assert "没来位姿" in loc.why_not(True)


async def test_σ过线_别的图_都不可信():
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    loc.on_pose(_位姿(1.0, 1.0, sigma=SIGMA_LOST_M + 0.2))
    assert "偏差" in loc.why_not(True)
    loc.on_pose(_位姿(1.0, 1.0, m=("other", "1")))
    assert "别的图" in loc.why_not(True)


async def test_跳变之后要连着几帧稳下来_恢复时把修正量交给引擎():
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)                                   # 里程原点 ↔ 地图 (0, 0)
    loc.on_corrected = lambda d, human: got.append((d, human))
    before = compose((0.0, 0.0, 0.0), inverse((0.0, 0.0, 0.0)))
    loc.on_pose(_位姿(0.5, 0.2, 0.1, jump=True))         # 定位器自己跳了(里程没动)
    assert "跳" in loc.why_not(True)
    for _ in range(SETTLE_FIXES - 1):
        loc.on_pose(_位姿(0.5, 0.2, 0.1))
        assert not loc.ok(True)
    assert got == []
    loc.on_pose(_位姿(0.5, 0.2, 0.1))
    assert loc.ok(True)
    after = compose((0.5, 0.2, 0.1), inverse((0.0, 0.0, 0.0)))
    [(d, human)] = got
    assert _近(d, compose(after, inverse(before)), 1e-9)
    assert human is False, "定位器自己跳、自己稳:不是人给的位置(丢定位的次数不从头算,内审应修 4)"


async def test_修正量_旧的地图系跟里程系不重合时也对():
    """内审应修 8(突变 M10 活):以前的测试 ``T`` 都是单位变换,修正量里 compose 的先后反了也看不出。
    修正量的意思:旧坐标系里记下的点,按它挪过去 = 新坐标系里同一个地方。"""
    got = []
    c, link, loc = await _就绪(odom=(2.0, 1.0, 0.5))
    _稳(loc, 10.0, 5.0, 1.2)
    loc.on_corrected = lambda d, human: got.append(d)
    T_old = compose((10.0, 5.0, 1.2), inverse((2.0, 1.0, 0.5)))
    loc.on_pose(_位姿(10.6, 4.7, 1.35, jump=True))
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(10.6, 4.7, 1.35))
    [d] = got
    T_new = compose((10.6, 4.7, 1.35), inverse((2.0, 1.0, 0.5)))
    for o in ((2.0, 1.0, 0.5), (0.0, 0.0, 0.0), (-3.0, 4.0, 2.0)):  # 来路上随便哪一点
        assert _近(compose(d, compose(T_old, o)), compose(T_new, o), 1e-9), o


async def test_稳定期里又跳一次_修正量还是从第一次跳之前算():
    got = []
    c, link, loc = await _就绪(odom=(1.0, 0.0, 0.3))
    _稳(loc, 5.0, 5.0, 0.3)
    loc.on_corrected = lambda d, human: got.append(d)
    loc.on_pose(_位姿(5.5, 5.0, 0.3, jump=True))
    loc.on_pose(_位姿(5.5, 5.0, 0.3))
    loc.on_pose(_位姿(6.0, 5.5, 0.4, jump=True))          # 还没稳又跳
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(6.0, 5.5, 0.4))
    [d] = got
    assert _近(compose(d, (5.0, 5.0, 0.3)), (6.0, 5.5, 0.4), 1e-9), "从跳之前可信的那一帧算"


async def test_稳下来之后再跳_修正量从稳下来那一帧算_不重复挪():
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_corrected = lambda d, human: got.append(d)
    loc.on_pose(_位姿(0.5, 0.0, jump=True))
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(0.5, 0.0, sigma=SIGMA_LOST_M + 0.5))   # 稳下来了、只是 σ 大
    loc.on_pose(_位姿(0.8, 0.0, jump=True))
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(0.8, 0.0))
    assert [round(d[0], 6) for d in got] == [0.5, 0.3]


async def test_跟里程对不上也当跳_里程不新鲜时修正量给不出():
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_corrected = lambda d, human: got.append(d)
    loc.on_pose(_位姿(1.0, 0.0))                          # 定位器挪了 1 m,里程没动
    assert "对不上" in loc.why_not(True)
    loc.update((0.0, 0.0, 0.0), False)                   # 这期间里程断了
    loc.update((0.0, 0.0, 0.0), True)
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(1.0, 0.0))
    # 对账 S1:跳过、里程又断过 —— 核不了稳下来的地方,不再照旧认(原来这里直接认了)
    assert not loc.ok(True) and "里程断过" in loc.why_not(True) and got == []
    assert await loc.relocalize(M, (1.0, 0.0, 0.0)) == ""   # 人给位置
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(1.0, 0.0, jump=True, reloc_id=r.req))
    _稳(loc, 1.0, 0.0)
    assert got == [None], "里程断过:旧坐标换不过来,引擎把来路作废"


async def test_只转了朝向_跟里程对不上也当跳():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0, 0.0)
    loc.on_pose(_位姿(0.0, 0.0, 0.6))                     # 定位器转了 0.6 rad,里程没转
    assert "朝向差" in loc.why_not(True)


async def test_小的出入不算跳():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    c.t += 1.0
    loc.update((1.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(1.15, 0.0))                         # 里程 1 m、定位器 1.15 m:在线内
    assert loc.ok(True)
    c.t += 1.0
    loc.update((2.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(2.4, 0.0))                          # 里程 2 m、定位器 2.4 m:走得远允许差得多
    assert loc.ok(True), loc.why_not(True)


async def test_走的一样远_方向不对_也当跳():
    """里程往前 1 m、定位器往侧面 1 m(比如匹配把地图转错了):光比走了多远看不出来,要比相对位移。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    for i in range(1, 11):
        c.t += 0.1
        loc.update((0.1 * i, 0.0, 0.0), True)
        loc.on_pose(_位姿(0.0, 0.1 * i))
    assert "对不上" in loc.why_not(True)


async def test_里程朝向正常地慢慢漂_走得再远也不当跳():
    """里程的朝向每米漂 0.02 rad(W00c6e 取的漂移率),走 30 m 累计 0.6 rad,过了朝向差的线 —— 交叉
    校验只比窗口里(几秒内)的,不跟很久以前的比。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    ox = oy = 0.0
    for i in range(1, 301):                              # 1 m/s 直走 30 m
        c.t += 0.1
        yaw = 0.002 * i                                  # 里程以为自己在慢慢左转,按这个朝向积分
        ox, oy = ox + 0.1 * math.cos(yaw), oy + 0.1 * math.sin(yaw)
        loc.update((ox, oy, yaw), True)
        loc.on_pose(_位姿(0.1 * i, 0.0))
        assert loc.ok(True), (i, loc.why_not(True))


async def test_定位器冻住_一直报同一个位置_狗在走_抓得住也稳不下来():
    """内审阻断 1:定位器匹配线程死了、定时器还在重发最后一个位置。相邻两帧只差 0.1 m(在线内),
    按窗口累计比才抓得住;冻着的时候一直不可信。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    bad = 0
    for i in range(1, 41):                               # 1 m/s 走 4 m
        c.t += 0.1
        loc.update((0.1 * i, 0.0, 0.0), True)
        loc.on_pose(_位姿(0.0, 0.0))
        if not loc.ok(True):
            bad = bad or i
        if i >= 8:
            assert not loc.ok(True), (i, loc.why_not(True))
    assert bad <= 4 and "对不上" in loc.why_not(True)


@pytest.mark.parametrize("per_frame", [0.25, 0.1])
async def test_定位器慢慢漂走_狗没动_抓得住也稳不下来(per_frame):
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    for i in range(1, 31):
        c.t += 0.1
        loc.update((0.0, 0.0, 0.0), True)
        loc.on_pose(_位姿(per_frame * i, 0.0))
        if i >= 5:
            assert not loc.ok(True), (i, loc.why_not(True))


async def test_里程归零换了段_交叉校验不跨段比():
    """内审应修 8(突变 M18 活):里程归零(换段)之后,不能拿旧段的里程跟新段的比 —— 定位器没动、
    里程坐标一下子变了,不是定位器跳了。"""
    c, link, loc = await _就绪(odom=(5.0, 0.0, 0.0))
    _稳(loc, 5.0, 0.0)
    c.t += 0.1
    loc.update((0.0, 0.0, 0.0), True)                    # 旁路重启、里程归零
    loc.on_pose(_位姿(5.0, 0.0))
    assert loc.ok(True), loc.why_not(True)
    e = loc.estimate((0.0, 0.0, 0.0))
    assert (round(e.x, 6), round(e.y, 6)) == (5.0, 0.0)


async def test_卡了一下一批一起到_各配各的里程_不当跳也不给修正量():
    """内审应修 5:定位器卡 1 s,十帧一起到。以前都配此刻的里程:第一帧当跳、稳下来那一帧拿半秒前的
    位置配此刻的里程,修正量错。按时间戳算晚到多少、配那一刻的里程。"""
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_corrected = lambda d, human: got.append(d)
    for i in range(1, 6):                                # 0.5 m/s,按时到
        c.t += 0.1
        loc.update((0.05 * i, 0.0, 0.0), True)
        loc.on_pose(_位姿(0.05 * i, 0.0))
    held = []
    for i in range(6, 16):                               # 卡住 1 s:帧攒着
        c.t += 0.1
        loc.update((0.05 * i, 0.0, 0.0), True)
        held.append(_位姿(0.05 * i, 0.0))
    for p in held:                                       # 一起到
        loc.on_pose(p)
        assert loc.ok(True), loc.why_not(True)
    assert got == []
    e = loc.estimate((0.75, 0.0, 0.0))
    assert (round(e.x, 3), round(e.y, 3), e.source) == (0.75, 0.0, "scan_match")


async def test_刚连上那几帧晚到_之后按时到的照样配此刻的里程():
    """「晚到多少」按近 10 s 里最小的那个延迟算,不是按窗口里最早那一帧:刚连上那几帧恰好晚到的话,
    之后按时到的会被当成「早到」,配不上里程,推算也用不了。"""
    c, link, loc = await _就绪()
    for _ in range(SETTLE_FIXES + 1):                    # 刚连上:晚到 0.6 s(狗停着)
        loc.on_pose(_位姿(0.0, 0.0, stamp=round((c.t - 0.6) * 1e9) + _seq[0]))
    assert loc.ok(True), loc.why_not(True)
    for i in range(1, 6):                                # 之后按时到,0.5 m/s
        c.t += 0.1
        loc.update((0.05 * i, 0.0, 0.0), True)
        loc.on_pose(_位姿(0.05 * i, 0.0))
    c.t += 0.3
    loc.update((0.4, 0.0, 0.0), True)
    e = loc.estimate((0.4, 0.0, 0.0))
    assert round(e.x, 3) == 0.4, "按时到的那一帧配上了里程,能往前推"


async def test_时间戳不增的当重发丢掉_往回跳一大截的当钟拨过():
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    last = _位姿(1.0, 1.0)
    loc.on_pose(last)
    loc.on_pose(_位姿(1.1, 1.0, stamp=last.stamp_ns))       # 同一个时间戳:重发的
    assert loc.estimate((0.0, 0.0, 0.0)).x == 1.0
    c.t += 1.5
    loc.update((0.0, 0.0, 0.0), True)
    assert loc.source == "dead_reckoning"
    loc.on_pose(_位姿(1.0, 1.0, stamp=last.stamp_ns - 5 * 10**9))   # 往回 5 s:它对了时
    assert loc.source == "scan_match" and loc.ok(True), loc.why_not(True)


async def test_定位器自己推算的也算在新鲜和推算上限里():
    """内审阻断 1 的另一半:定位器匹配不上、拿里程自己推着报(帧照常来),``meas_age_ms`` 说推了
    多久。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    for k in range(1, 13):                               # 1 m/s;它从 k=0 起就没真匹配上
        c.t += 0.1
        loc.update((0.1 * k, 0.0, 0.0), True)
        loc.on_pose(_位姿(0.1 * k, 0.0, meas_age_ms=100 * k))
        if k <= 5:
            assert loc.ok(True) and loc.source == "scan_match", k
        elif k <= 9:
            assert loc.ok(True) and loc.source == "dead_reckoning", k
        elif k >= 11:                                    # 推了 1 m 以上
            assert not loc.ok(True), k
    assert "秒前的" in loc.why_not(True)
    loc.on_pose(_位姿(1.2, 0.0, meas_age_ms=2100))
    assert "秒前的" in loc.why_not(True)


async def test_定位器说丢了_断了_都不可信_连回来重新换先验要新位置():
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    loc.on_state(State(seq=99, state="lost", reason="匹配不上"))
    assert "匹配不上" in loc.why_not(True)
    loc.on_state(State(seq=100, state="tracking"))
    assert loc.ok(True)
    loc.on_disconnect()
    assert "没连上" in loc.why_not(True)
    loc.on_connect()
    await loc.prior_task
    assert len([m for m in link.sent if isinstance(m, SetPrior)]) == 2
    assert "还没给出位置" in loc.why_not(True), "断过:以前那一帧不算"


async def test_断了连回来_第一帧要稳下来_修正量按断之前那一帧算():
    """内审应修 6:连回来的第一帧不直接信;断之前那一帧(同一段里程)留着当修正量的参照。"""
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    loc.on_corrected = lambda d, human: got.append((d, human))
    loc.on_disconnect()
    c.t += 3.0
    loc.update((0.5, 0.0, 0.0), True)                    # 断着的时候又挪了 0.5 m 才停
    loc.on_connect()
    await loc.prior_task
    loc.on_pose(_位姿(1.5, 1.4))                          # 回来的时候差了 0.4 m
    assert "刚连上" in loc.why_not(True)
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(1.5, 1.4))
    [(d, human)] = got
    assert _近(d, (0.0, 0.4, 0.0)) and human is False


async def test_换图_旧图的位置作废_新图换好先验给了位置稳下来才可信():
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    loc.on_corrected = lambda d, human: got.append(d)
    loc.on_map(("estate-1", "8"), "/maps/estate-1/8")
    assert "换图" in loc.why_not(True)
    await loc.prior_task
    assert link.sent[-1].map_version == "8"
    assert "还没给出位置" in loc.why_not(True), "换好先验、新图上还没来帧:旧图的位置不能当新图的"
    assert loc.estimate((0.0, 0.0, 0.0)) is None
    loc.on_pose(_位姿(1.0, 1.0))                          # 还是旧图的
    assert "别的图" in loc.why_not(True)
    _稳(loc, 2.0, 2.0, m=("estate-1", "8"))
    assert got == [None], "别的图:旧图上的位置不能当参照"


@pytest.mark.parametrize("how,want", [("down", "localizer_unavailable"),
                                      ("hang", "localizer_unavailable"),
                                      ("refuse", "localizer_refused: 初值离地图太远")])
async def test_重定位_不在线_没回_拒了(how, want):
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    if how == "down":
        link.down = True
    elif how == "hang":
        link.hang = 1
    else:
        link.answer["relocalize"] = (False, "初值离地图太远")
    assert (await loc.relocalize(M, (1.0, 2.0, 0.3))).startswith(want)
    assert loc.ok(True), "没成:不留着「在重定位」"


async def test_重定位_发初值_收下之后下一帧按跳变稳下来():
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_corrected = lambda d, human: got.append((d, human))
    assert await loc.relocalize(M, (1.0, 2.0, 0.3)) == ""
    assert "重定位" in loc.why_not(True), "收下了、还没回出那一帧:不拿旧位置走"
    loc.on_pose(_位姿(0.0, 0.0))                          # 定位器还在报旧的
    assert not loc.ok(True)
    r = link.sent[-1]
    assert isinstance(r, Relocalize)
    assert (r.map_id, r.map_version, r.x, r.y, r.yaw, r.sigma_xy) == (*M, 1.0, 2.0, 0.3, 0.5)
    loc.on_pose(_位姿(1.0, 2.0, 0.3, jump=True, reloc_id=r.req))
    assert not loc.ok(True)
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(1.0, 2.0, 0.3))
    assert loc.ok(True) and len(got) == 1
    assert got[0][1] is True, "人给的位置:引擎把丢定位的次数从头算"


async def test_重定位_那一帧比回复先读进来_也不卡住():
    """内审应修 1:回复跟那一帧挨着到,读任务不让出就接着读 —— 那一帧可能先到。以前收下之后才登记,
    带 ``reloc_id`` 的那一帧被当普通帧,之后一直「在按人给的位置重定位」。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    link.before_reply = lambda msg: loc.on_pose(_位姿(1.0, 2.0, 0.3, jump=True,
                                                       reloc_id=msg.req))
    assert await loc.relocalize(M, (1.0, 2.0, 0.3)) == ""
    assert "重定位了,等它稳下来" in loc.why_not(True), loc.why_not(True)
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(1.0, 2.0, 0.3))
    assert loc.ok(True)


async def test_重定位_等回复的时候换了图_不收():
    """内审应修 2:等回复的时候换了图,这次重定位是旧图上的,不收、也不留着「在重定位」。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    def 换图(msg):
        link.before_reply = None
        loc.on_map(("estate-1", "8"), "")
    link.before_reply = 换图
    assert (await loc.relocalize(M, (1.0, 2.0, 0.3))).startswith("busy")
    await loc.prior_task
    assert "重定位" not in loc.why_not(True)


async def test_丢定位后引擎要重置_请定位器在最后可信的位置附近重定位():
    """W08 决定 4:``reset_localization`` 的真实现。初值 = 最后可信的那一帧按里程推到此刻;σ 过线的帧
    不当初值。不是人给的:稳下来之后丢定位的次数不从头算。"""
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 1.0, 1.0)
    loc.on_corrected = lambda d, human: got.append(human)
    loc.on_pose(_位姿(1.2, 1.0, sigma=SIGMA_LOST_M + 0.5))   # 不可信的
    c.t += 0.3
    loc.update((0.3, 0.0, 0.0), True)
    await loc.reset()
    r = link.sent[-1]
    assert isinstance(r, Relocalize)
    assert (round(r.x, 6), round(r.y, 6), r.sigma_xy) == (1.3, 1.0, AUTO_RELOC_SIGMA_M)
    assert loc.why_not(True) == "定位器在重定位"
    loc.on_pose(_位姿(1.3, 1.0, jump=True, reloc_id=r.req))
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(1.3, 1.0))
    assert loc.ok(True) and got == [False]


async def test_丢定位后重置_没可信过的位置就不请():
    c, link, loc = await _就绪()
    await loc.reset()
    assert not [m for m in link.sent if isinstance(m, Relocalize)]


async def test_质量_越不确定越低_丢了是零():
    c, link, loc = await _就绪()
    assert loc.quality(True) == 0.0
    _稳(loc, 0.0, 0.0, sigma=0.1)
    q1 = loc.quality(True)
    loc.on_pose(_位姿(0.0, 0.0, sigma=0.3))
    assert 0.0 < loc.quality(True) < q1 < 1.0


async def test_W34_σ在0点8还不认_现场重定位时1点0就认了太松():
    """2026-10-08 C40221 3d.8:杀掉 MOLA 自己重定位时 σ 从 1.5 往下降,原来 1.0 就认。"""
    c, link, loc = await _就绪()
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(0.0, 0.0, sigma=0.8))
    assert not loc.ok(True) and "定位偏差可能到 0.8 m" in loc.why_not(True)
    _稳(loc, 0.0, 0.0, sigma=0.4)


# ---------------------------------------------------------------- W35:稳下来的地方跟锚对不对得上


async def test_人给了位置_定位器乱跳一阵稳在8米外_里程没动_不认():
    """2026-10-13 C40221 3d.9:给偏 2 m、45° 的初值,MOLA 来回跳了 32 s,稳在错 8 m、朝向差 160° 的
    地方还报 σ 0.1,代理认了。狗一直站着没动。"""
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_corrected = lambda d, human: got.append((d, human))
    assert await loc.relocalize(M, (2.0, 0.0, 0.8)) == ""
    [r] = [m for m in link.sent if isinstance(m, Relocalize)]
    loc.on_pose(_位姿(2.0, 0.0, 0.8, jump=True, reloc_id=r.req))
    for x in (3.0, 5.5, 4.0, 7.0):                       # 来回跳
        c.t += 0.1
        loc.update((0.0, 0.0, 0.0), True)
        loc.on_pose(_位姿(x, 1.0, 2.0, jump=True))
    for _ in range(SETTLE_FIXES + 3):                    # 在错的地方稳住了
        c.t += 0.1
        loc.update((0.0, 0.0, 0.0), True)
        loc.on_pose(_位姿(8.0, 0.0, -2.8))
    assert not loc.ok(True)
    assert "请重新设位置" in loc.why_not(True) and "人给的位置" in loc.why_not(True)
    assert got == [], "没稳下来:不给引擎修正量"
    assert not loc.busy, "卡在这儿不算「在等稳定」:RTK 核对还能来救"


async def test_人给的位置偏2米45度_稳在真实位置_认():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    assert await loc.relocalize(M, (2.0, 0.0, 0.8)) == ""
    [r] = [m for m in link.sent if isinstance(m, Relocalize)]
    loc.on_pose(_位姿(2.0, 0.0, 0.8, jump=True, reloc_id=r.req))
    _稳(loc, 0.1, 0.0, 0.02)


async def test_自己跳了之后稳在离最后可信位置很远的地方_不认_回来了就认():
    got = []
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_corrected = lambda d, human: got.append(d)
    loc.on_pose(_位姿(6.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(6.0, 0.0))
    assert "最后可信的位置" in loc.why_not(True) and got == []
    loc.on_pose(_位姿(0.4, 0.0, jump=True))               # MOLA 自己对回来了
    assert "跳" in loc.why_not(True) and loc.busy, "又跳了:从头等稳定,不再挂着上一次的说法"
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(0.4, 0.0))
    assert loc.ok(True) and len(got) == 1


async def test_狗真的走了那么远_稳在那儿_认():
    """丢定位期间被遥控开走 6 m:里程也走了 6 m,不算对不上。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_pose(_位姿(0.0, 0.0, jump=True))
    for i in range(1, 61):
        c.t += 0.1
        loc.update((0.1 * i, 0.0, 0.0), True)
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(6.0, 0.0, jump=True) if _ == 0 else _位姿(6.0, 0.0))
    assert loc.ok(True), loc.why_not(True)


async def test_稳在对的位置但朝向反了_不认():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_pose(_位姿(0.2, 0.0, 2.9, jump=True))
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(0.2, 0.0, 2.9))
    assert "朝向" in loc.why_not(True)


async def test_S1_里程断过_又跳了_核不了_不认_人给位置之后认():
    """原来叫「里程断过_没法比_照旧认」(W35 交接里点名的漏洞):里程换段之后定位器跳 6 m,照样认了。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.update((0.0, 0.0, 0.0), False)
    loc.update((0.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(6.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(6.0, 0.0))
    assert not loc.ok(True)
    assert "里程断过" in loc.why_not(True) and "请重新设位置" in loc.why_not(True)
    assert not loc.busy, "卡在这儿不算「在等稳定」:RTK 核对还能来救"
    assert await loc.relocalize(M, (6.1, 0.0, 0.0)) == ""
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(6.0, 0.0, jump=True, reloc_id=r.req))
    _稳(loc, 6.0, 0.0)


async def test_S1_里程断过之后来过可信的帧_锚换到新的一段_再跳照样能比():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.update((0.0, 0.0, 0.0), False)
    loc.update((0.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(0.0, 0.0))                          # 新的一段里可信的一帧:锚换到这儿
    loc.on_pose(_位姿(0.4, 0.0, jump=True))
    _稳(loc, 0.4, 0.0)
    loc.on_pose(_位姿(6.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(6.0, 0.0))
    assert "最后可信的位置差 5.6 m" in loc.why_not(True)


async def test_换了图_旧的锚不管新图():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    m2 = ("estate-1", "8")
    loc.on_map(m2, "/maps/estate-1/8")
    await loc.prior_task
    _稳(loc, 30.0, 30.0, m=m2)


async def test_卡住之后人再给一次位置_按新给的比():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_pose(_位姿(8.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(8.0, 0.0))
    assert not loc.ok(True)
    assert await loc.relocalize(M, (8.2, 0.0, 0.0)) == ""    # 人说:它其实就在那儿
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(8.2, 0.0, jump=True, reloc_id=r.req))
    _稳(loc, 8.0, 0.0)


async def test_走得远_里程自己也会偏_线按走过的路放宽():
    """丢定位期间走了 20 m:里程偏几米是正常的(线 = 3 m + 走过的路的两成);没走时差 5 m 不认。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_pose(_位姿(0.0, 0.0, jump=True))
    for i in range(1, 201):
        c.t += 0.1
        loc.update((0.1 * i, 0.0, 0.0), True)
    loc.on_pose(_位姿(20.0, 5.0, jump=True))
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(20.0, 5.0))
    assert loc.ok(True), loc.why_not(True)


async def test_人给的朝向偏45度_边走边收敛到真实位置_认():
    """2026-10-13 回放:拿给的(偏了的)朝向推「往哪走了」,走 10 m 就推偏 7 m,把对的冤枉成错的。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    assert await loc.relocalize(M, (2.0, 0.0, 0.8)) == ""    # 真实位置 (0, 0)、朝向 0
    [r] = [m for m in link.sent if isinstance(m, Relocalize)]
    loc.on_pose(_位姿(2.0, 0.0, 0.8, jump=True, reloc_id=r.req))
    for i in range(1, 101):                              # 往前(地图 +x)走 10 m
        c.t += 0.1
        loc.update((0.1 * i, 0.0, 0.0), True)
    loc.on_pose(_位姿(10.0, 0.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(10.0, 0.0, 0.0))
    assert loc.ok(True), loc.why_not(True)


async def test_人给了位置_走了5米却稳在12米外_不认():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    assert await loc.relocalize(M, (2.0, 0.0, 0.8)) == ""
    [r] = [m for m in link.sent if isinstance(m, Relocalize)]
    loc.on_pose(_位姿(2.0, 0.0, 0.8, jump=True, reloc_id=r.req))
    for i in range(1, 51):
        c.t += 0.1
        loc.update((0.1 * i, 0.0, 0.0), True)
    loc.on_pose(_位姿(-10.0, 0.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(-10.0, 0.0, 0.0))
    assert "人给的位置" in loc.why_not(True)


# ---------------------------------------------------------------- W35 外审 I1:位姿比回执先到


async def test_外审I1_头一回设位置_回执到之前位姿和稳定帧先到了_稳在8米外_不认():
    """锚原来是等回执回来才设:回执之前到的那几帧按「没有锚」放行,错解已经认下了。"""
    got = []
    c, link, loc = await _就绪()
    loc.on_corrected = lambda d, human: got.append((d, human))

    def 先到(msg):
        loc.on_pose(_位姿(8.0, 0.0, jump=True, reloc_id=msg.req))
        for _ in range(SETTLE_FIXES + 1):
            loc.on_pose(_位姿(8.0, 0.0))
    link.before_reply = 先到
    assert await loc.relocalize(M, (0.0, 0.0, 0.0)) == ""
    assert not loc.ok(True) and "人给的位置" in loc.why_not(True)
    assert got == []
    loc.on_pose(_位姿(8.0, 0.0))                          # 再来一帧:不能把锚改成 8 m 那儿
    assert not loc.ok(True)
    link.before_reply = None
    assert await loc.relocalize(M, (0.2, 0.0, 0.0)) == ""
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(0.2, 0.0, jump=True, reloc_id=r.req))
    _稳(loc, 0.1, 0.0)


async def test_外审I1_已经有锚_人重新给8米_回执之前稳在原地的错解_按新给的比_不认():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)

    def 先到(msg):
        loc.on_pose(_位姿(0.0, 0.0, jump=True, reloc_id=msg.req))
        for _ in range(SETTLE_FIXES + 1):
            loc.on_pose(_位姿(0.0, 0.0))
    link.before_reply = 先到
    assert await loc.relocalize(M, (8.0, 0.0, 0.0)) == ""
    assert not loc.ok(True) and "人给的位置" in loc.why_not(True)
    loc.on_pose(_位姿(0.0, 0.0))
    loc.on_pose(_位姿(0.0, 0.0))
    assert not loc.ok(True), "后面的普通帧不能把锚盖回 0 m"


async def test_外审I1_等回执的时候来的是普通帧_不许把刚给的锚盖掉():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    link.before_reply = lambda msg: [loc.on_pose(_位姿(0.0, 0.0)) for _ in range(3)]
    assert await loc.relocalize(M, (8.0, 0.0, 0.0)) == ""
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(0.0, 0.0, jump=True, reloc_id=r.req))   # 定位器没照办,稳在原地
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(0.0, 0.0))
    assert not loc.ok(True) and "人给的位置" in loc.why_not(True)


@pytest.mark.parametrize("坏法", ["拒了", "没回", "没连上"])
async def test_外审I1_重定位没成_锚退回原来那个(坏法):
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    if 坏法 == "拒了":
        link.answer["relocalize"] = (False, "初值离地图太远")
    elif 坏法 == "没回":
        link.hang = 1
    else:
        link.down = True
    assert await loc.relocalize(M, (8.0, 0.0, 0.0)) != ""
    link.down = False
    loc.on_pose(_位姿(8.0, 0.0, jump=True))               # 没成的那次不算数:8 m 那儿还是离锚太远
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(8.0, 0.0))
    assert "最后可信的位置" in loc.why_not(True)
    loc.on_pose(_位姿(0.1, 0.0, jump=True))
    _稳(loc, 0.1, 0.0)


async def test_外审I1_等回执的时候换了图_给的锚不留到新图上():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    m2 = ("estate-1", "8")
    link.before_reply = lambda msg: loc.on_map(m2, "/maps/estate-1/8") \
        if isinstance(msg, Relocalize) else None
    assert (await loc.relocalize(M, (8.0, 0.0, 0.0))).startswith("busy")
    link.before_reply = None
    await loc.prior_task
    _稳(loc, 30.0, 30.0, m=m2)


async def test_外审I1_按给的锚稳下来之后_锚跟着可信的帧走_不一直钉在给的那儿():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    assert await loc.relocalize(M, (0.0, 0.0, 0.0)) == ""
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(2.5, 0.0, jump=True, reloc_id=r.req))
    _稳(loc, 2.5, 0.0)                                    # 离给的 2.5 m:认
    loc.on_pose(_位姿(5.0, 0.0, jump=True))               # 以后再跳:跟 2.5 m 那儿比,不跟 0 比
    _稳(loc, 5.0, 0.0)


# ---------------------------------------------------------------- W35 复查 R1:别的稳定轮不许放手


async def test_复查R1_等这次重定位的那一帧时_先稳下来的是普通帧_不许放开刚给的锚():
    """头一回设位置 x=2:先到的是没有请求号的普通帧(x=0,够稳一轮,离给的 2 m 在线内);原来这一轮
    稳下来就把锚放开、盖成 0。随后这次请求的那一帧稳在 x=−2.5:离给的位置 4.5 m,却按 0 比成
    2.5 m 认了。"""
    c, link, loc = await _就绪()

    def 先到(msg):
        for _ in range(SETTLE_FIXES + 2):
            loc.on_pose(_位姿(0.0, 0.0))
    link.before_reply = 先到
    assert await loc.relocalize(M, (2.0, 0.0, 0.0)) == ""
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(-2.5, 0.0, jump=True, reloc_id=r.req))
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(-2.5, 0.0))
    assert not loc.ok(True) and "人给的位置差 4.5 m" in loc.why_not(True)


async def test_复查R1_回执已经回来了_普通帧稳一轮_也不放开_要等这次请求的那一帧():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    assert await loc.relocalize(M, (2.0, 0.0, 0.0)) == ""
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(0.5, 0.0, jump=True))               # 不是这次请求的:自己跳了一下,稳住
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(0.5, 0.0))
    loc.on_pose(_位姿(-2.5, 0.0, jump=True, reloc_id=r.req))
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(-2.5, 0.0))
    assert not loc.ok(True) and "人给的位置差 4.5 m" in loc.why_not(True)


async def test_复查R1_收下了但那一帧没来定位器就断了_连回来稳一轮_锚才放开():
    """请求跟着连接一起没了:不然锚一直钉在给的那儿,以后的跳变都拿它比。"""
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    assert await loc.relocalize(M, (2.0, 0.0, 0.0)) == ""
    loc.on_disconnect()
    loc.on_connect()
    await loc.prior_task
    _稳(loc, 2.2, 0.0)                                    # 连回来:按给的锚核过
    loc.on_pose(_位姿(4.9, 0.0, jump=True))               # 以后跟着可信的帧走(离 2.2 差 2.7)
    _稳(loc, 4.9, 0.0)
    loc.on_pose(_位姿(7.5, 0.0, jump=True))               # 离 4.9 差 2.6:认;还钉在 2 就差 5.5
    _稳(loc, 7.5, 0.0)


# ---------------------------------------------------------------- 对账 S1:没有能核的锚,跳过的不认


def _代理(c, keep=None, boot="boot-1"):
    """一个(重新)起来的代理这边:给了 ``keep`` 就把锚记在那儿、起来时读回。"""
    _钟[0] = c
    loc = BridgeLocalizer(monotonic=c, keep=keep, boot_id=boot)
    link = 假桥()
    loc.link = link
    return loc, link


async def _接上(loc, odom, m=M):
    loc.on_map(m, f"/maps/{m[0]}/{m[1]}")
    loc.update(odom, True)
    loc.on_connect()
    await loc.prior_task


async def test_S1_没有锚_一直连着的照旧认_连上之后跳过的稳下来不认_要人给位置():
    """代理刚起来、手里没有锚:定位器一直连续在跟的照旧认(W34;仿真定位器一连上就有位置);可只要它
    在代理眼前跳过一次(报 jump、重定位、跟里程对不上),稳在哪儿都核不了,原来照认。"""
    c, link, loc = await _就绪()
    _稳(loc, 3.0, 4.0)
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(0.0, 0.0))                          # 连上的头一帧
    loc.on_pose(_位姿(6.0, 0.0, jump=True))               # 还没认过任何位置,它自己跳了
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(6.0, 0.0))
    assert not loc.ok(True)
    assert "没有可信过的位置" in loc.why_not(True) and "请重新设位置" in loc.why_not(True)
    assert not loc.busy
    assert await loc.relocalize(M, (6.1, 0.0, 0.0)) == ""
    r = [m for m in link.sent if isinstance(m, Relocalize)][-1]
    loc.on_pose(_位姿(6.0, 0.0, jump=True, reloc_id=r.req))
    _稳(loc, 6.0, 0.0)


async def test_S1_没有锚_跟里程对不上也算跳过():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(0.0, 0.0))
    for _ in range(3):
        loc.on_pose(_位姿(0.0, 0.0))
    loc.on_pose(_位姿(2.0, 0.0))                          # 没报 jump,可里程没动:对不上
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(2.0, 0.0))
    assert not loc.ok(True) and "请重新设位置" in loc.why_not(True)


async def test_S1_没有锚_跳过卡住了_断开再连上_不洗白():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(0.0, 0.0))
    loc.on_pose(_位姿(6.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(6.0, 0.0))
    assert not loc.ok(True)
    loc.on_disconnect()
    loc.on_connect()
    await loc.prior_task
    for _ in range(SETTLE_FIXES + 2):                    # 连回来:没报 jump,一直在 6 m 那儿
        loc.on_pose(_位姿(6.0, 0.0))
    assert not loc.ok(True) and "请重新设位置" in loc.why_not(True)


async def test_S1_跳过之后按锚核过了_从头记_后来里程断了又断开重连_说的是刚连上():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_pose(_位姿(0.4, 0.0, jump=True))
    _稳(loc, 0.4, 0.0)                                    # 跳过、核过:不再挂着「跳过」
    loc.on_disconnect()
    loc.update((0.0, 0.0, 0.0), False)
    loc.update((0.0, 0.0, 0.0), True)
    loc.on_connect()
    await loc.prior_task
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(0.4, 0.0))
    why = loc.why_not(True)
    assert "定位器刚连上" in why and "里程断过" in why and "请重新设位置" in why, why


async def test_S1_没有锚_换了图从头算():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(0.0, 0.0))
    loc.on_pose(_位姿(6.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES + 2):
        loc.on_pose(_位姿(6.0, 0.0))
    m2 = ("estate-1", "8")
    loc.on_map(m2, "/maps/estate-1/8")
    await loc.prior_task
    _稳(loc, 30.0, 30.0, m=m2)


async def test_S1_稳下来那一帧没配上里程_等配上了再核_不直接认():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    loc.on_pose(_位姿(8.0, 0.0, jump=True))
    c.t += 1.0                                           # 这一秒里程没来新样本:帧配不上里程
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(8.0, 0.0))
    assert not loc.ok(True) and "没配上里程" in loc.why_not(True)
    assert "请重新设位置" not in loc.why_not(True), "一会儿就好:不叫人"
    loc.update((0.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(8.0, 0.0))
    assert "差 8.0 m" in loc.why_not(True) and "请重新设位置" in loc.why_not(True)


async def test_S1_可信的帧没配上里程_不把能推的锚换掉():
    c, link, loc = await _就绪()
    _稳(loc, 0.0, 0.0)
    c.t += 1.0
    loc.on_pose(_位姿(0.0, 0.0))                          # 可信,可没配上里程
    loc.update((0.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(6.0, 0.0, jump=True))
    for _ in range(SETTLE_FIXES + 1):
        loc.on_pose(_位姿(6.0, 0.0))
    assert "最后可信的位置差 6.0 m" in loc.why_not(True)


# ---------------------------------------------------------------- 对账 S1:锚记在盘上,代理重启照样核


async def _走一段(c, loc, x0, odo0, n, dx=0.1):
    for i in range(1, n + 1):
        c.t += 0.1
        loc.update((odo0 + dx * i, 0.0, 0.0), True)
        loc.on_pose(_位姿(x0 + dx * i, 5.0))
        assert loc.ok(True), loc.why_not(True)


async def test_S1_代理重启_定位器一直在跟_按记下的锚核过_认_不用人(tmp_path):
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, link = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0))
    _稳(loc, 10.0, 5.0)
    await _走一段(c, loc, 10.0, 0.0, 30)                 # 走 3 m
    assert keep.is_file()
    c.t += 20.0                                          # 代理重启;这期间狗又被遥控挪了 1 m
    loc2, _ = _代理(c, keep)
    await _接上(loc2, (4.0, 0.0, 0.0))
    _稳(loc2, 14.0, 5.0)


async def test_S1_代理重启_这期间定位器跳到远处_一直报着没跳过_不认(tmp_path):
    """代理重启的时候定位器在别处重定位(或稳在了错解上),代理连上时它已经「连续」地报着错的位置:
    原来没有锚、也没看见跳,直接认。"""
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, link = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0))
    _稳(loc, 10.0, 5.0)
    await _走一段(c, loc, 10.0, 0.0, 30)
    c.t += 20.0
    loc2, link2 = _代理(c, keep)
    await _接上(loc2, (3.0, 0.0, 0.0))
    for _ in range(SETTLE_FIXES + 2):
        loc2.on_pose(_位姿(22.0, 5.0))
    assert not loc2.ok(True)
    why = loc2.why_not(True)
    assert "代理重启前最后可信的位置差 9.0 m" in why and "请重新设位置" in why, why
    assert await loc2.relocalize(M, (22.0, 5.0, 0.0)) == ""   # 人到场说「它就在这儿」
    r = [m for m in link2.sent if isinstance(m, Relocalize)][-1]
    loc2.on_pose(_位姿(22.0, 5.0, jump=True, reloc_id=r.req))
    _稳(loc2, 22.0, 5.0)


async def test_S1_代理重启_人刚给了位置还没稳下来_按记下的给的位置核(tmp_path):
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, link = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0))
    assert await loc.relocalize(M, (2.0, 0.0, 0.8)) == ""
    c.t += 5.0
    loc2, _ = _代理(c, keep)                              # 定位器按旧代理的请求重定位完了
    await _接上(loc2, (0.0, 0.0, 0.0))
    loc2.on_pose(_位姿(8.0, 0.0, -2.8, jump=True, reloc_id=1))
    for _ in range(SETTLE_FIXES + 2):
        loc2.on_pose(_位姿(8.0, 0.0, -2.8))
    assert "代理重启前人给的位置差" in loc2.why_not(True)
    loc3, _ = _代理(c, keep)
    await _接上(loc3, (0.0, 0.0, 0.0))
    loc3.on_pose(_位姿(0.1, 0.0, 0.02, jump=True, reloc_id=1))
    _稳(loc3, 0.1, 0.0, 0.02)


async def test_S1_重定位没成_盘上的锚也退回原来那个(tmp_path):
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, link = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0))
    _稳(loc, 10.0, 5.0)
    link.answer["relocalize"] = (False, "初值离地图太远")
    assert await loc.relocalize(M, (30.0, 30.0, 0.0)) != ""
    loc2, _ = _代理(c, keep)
    await _接上(loc2, (0.0, 0.0, 0.0))
    _稳(loc2, 10.0, 5.0)


async def test_S1_代理重启_记下的锚里程断过_核不了_不认(tmp_path):
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, _ = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0))
    _稳(loc, 10.0, 5.0)
    loc2, _ = _代理(c, keep)
    await _接上(loc2, (0.0, 0.0, 0.0))
    loc2.update((0.0, 0.0, 0.0), False)                  # 连上之前里程断了一下
    loc2.update((0.0, 0.0, 0.0), True)
    for _ in range(SETTLE_FIXES + 2):
        loc2.on_pose(_位姿(10.0, 5.0))
    assert not loc2.ok(True) and "里程断过" in loc2.why_not(True)


@pytest.mark.parametrize("坏法", ["别的开机", "别的图", "坏文件", "没有文件", "钟在后面"])
async def test_S1_代理重启_记下的锚用不上_当没有锚(tmp_path, 坏法):
    """用不上的一律当没有:定位器一直连续的照旧认(跟原来一样),起来不报错。"""
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, _ = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0), m=("estate-1", "6") if 坏法 == "别的图" else M)
    _稳(loc, 50.0, 50.0, m=("estate-1", "6") if 坏法 == "别的图" else M)
    if 坏法 == "坏文件":
        keep.write_text("{坏的")
    elif 坏法 == "没有文件":
        keep.unlink()
    elif 坏法 == "钟在后面":
        c.t -= 50.0                                      # 不是同一个钟记的:不信
    loc2, _ = _代理(c, keep, boot="boot-2" if 坏法 == "别的开机" else "boot-1")
    await _接上(loc2, (0.0, 0.0, 0.0))
    _稳(loc2, 10.0, 5.0)
    if 坏法 in ("别的开机", "别的图", "坏文件"):
        assert json.loads(keep.read_text())["pose"][:2] == [10.0, 5.0], "用不上的那份换成新的"


async def test_S1_换图把记下的锚删掉_写不进盘不出错(tmp_path):
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, _ = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0))
    _稳(loc, 10.0, 5.0)
    assert keep.is_file()
    loc.on_map(("estate-1", "8"), "/maps/estate-1/8")
    assert not keep.exists(), "别的图:记下的那份作废"
    loc2, _ = _代理(c, tmp_path / "没有这个目录" / "loc_anchor.json")
    await _接上(loc2, (0.0, 0.0, 0.0))
    _稳(loc2, 1.0, 1.0)                                   # 写不进去只记日志


async def test_S1_可信的帧攒着写_挪够了或隔够久才写(tmp_path, monkeypatch):
    import d1max_agent.bridge_localizer as BL
    keep = tmp_path / "loc_anchor.json"
    c = 钟()
    loc, _ = _代理(c, keep)
    await _接上(loc, (0.0, 0.0, 0.0))
    writes = []
    real = BL.os.replace
    monkeypatch.setattr(BL.os, "replace", lambda a, b: (writes.append(c.t), real(a, b)))
    _稳(loc, 0.0, 5.0)
    assert len(writes) == 1
    await _走一段(c, loc, 0.0, 0.0, 30)                  # 1 m/s 走 3 s:一秒最多一回
    assert len(writes) == 3, writes
    for _ in range(20):                                  # 停下:最后挪的那一截隔够 1 s 补写
        c.t += 0.1
        loc.update((3.0, 0.0, 0.0), True)
        loc.on_pose(_位姿(3.0, 5.0))
    assert len(writes) == 4, writes
    n = len(writes)
    for _ in range(100):                                 # 站着 10 s:不写
        c.t += 0.1
        loc.update((3.0, 0.0, 0.0), True)
        loc.on_pose(_位姿(3.0, 5.0))
    assert len(writes) == n
    for _ in range(250):                                 # 再站 25 s:到 30 s 写一回
        c.t += 0.1
        loc.update((3.0, 0.0, 0.0), True)
        loc.on_pose(_位姿(3.0, 5.0))
    assert len(writes) == n + 1
