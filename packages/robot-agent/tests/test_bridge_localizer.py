"""定位器经本机桥给的位姿,代理这边怎么判能不能信(W09a,W08 决定 3、4)。单元测试:假桥、手拨的
单调钟。"""

from __future__ import annotations

import asyncio
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
    assert loc.ok(True) and got == [None], "里程断过:旧坐标换不过来,引擎把来路作废"


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
    loc.on_pose(_位姿(0.0, 0.0, sigma=0.5))
    assert 0.0 < loc.quality(True) < q1 < 1.0
