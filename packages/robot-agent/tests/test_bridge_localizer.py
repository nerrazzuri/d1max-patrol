"""定位器经本机桥给的位姿,代理这边怎么判能不能信(W09a,W08 决定 3、4)。单元测试:假桥、手拨的
单调钟。"""

from __future__ import annotations

import asyncio
import math

import pytest

from d1max_agent.bridge_localizer import (
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
    """记下发过的请求;按类型回。``down`` 为真时像没连上。"""

    def __init__(self):
        self.sent = []
        self.answer = {"set_prior": (True, ""), "relocalize": (True, "")}
        self.hang = False
        self.down = False

    async def request(self, make, timeout_s):
        from d1max_agent.bridge_localizer import LocalizerUnavailable
        if self.down:
            raise LocalizerUnavailable("定位器没连上")
        req = len(self.sent) + 1
        msg = make(req)
        self.sent.append(msg)
        if self.hang:
            raise asyncio.TimeoutError()
        ok, reason = self.answer[msg.T]
        return Reply(req=req, ok=ok, reason=reason)


_seq = [0]


def _位姿(x, y, yaw=0.0, *, sigma=0.1, jump=False, m=M, source="scan_match", reloc_id=None):
    _seq[0] += 1
    return Pose(seq=_seq[0], stamp_ns=0, map_id=m[0], map_version=m[1], x=x, y=y, yaw=yaw,
                sigma_xy=sigma, sigma_yaw=0.02, source=source, jump=jump, reloc_id=reloc_id)


async def _就绪(odom=(0.0, 0.0, 0.0)):
    """连上、换好先验、里程在 ``odom``,还没有位姿。"""
    c, link = 钟(), 假桥()
    loc = BridgeLocalizer(monotonic=c)
    loc.link = link
    loc.on_map(M, "/maps/estate-1/7")
    loc.update(odom, True)
    loc.on_connect()
    await loc.prior_task
    return c, link, loc


async def test_没连上_在换图_还没位置_都不可信_换好先验给了位置就可信():
    c, link = 钟(), 假桥()
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
    assert loc.ok(True) and loc.anchored
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


async def test_两次定位之间用里程推算_有上限():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(10.0, 5.0, math.pi / 2))          # 此刻里程在原点、朝东;地图上朝北
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
    loc.on_pose(_位姿(1.0, 1.0))
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
    loc.on_pose(_位姿(1.0, 1.0))
    c.t += 0.8
    loc.update((0.1, 0.0, 0.0), True)
    assert loc.ok(True)
    assert "没来位姿" in loc.why_not(False)


async def test_里程一拍只转了个大角度_也换段_不再推算():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(1.0, 1.0))
    c.t += 0.7
    loc.update((0.1, 0.0, 1.5), True)                    # 挪得不多、一拍转了 1.5 rad:里程跳了
    assert "没来位姿" in loc.why_not(True)


async def test_里程一拍跳了_不再推算():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(1.0, 1.0))
    c.t += 0.7
    loc.update((3.0, 0.0, 0.0), True)                    # 一拍 3 m:里程跳了
    assert "没来位姿" in loc.why_not(True)


async def test_σ过线_别的图_都不可信():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(1.0, 1.0, sigma=SIGMA_LOST_M + 0.2))
    assert "偏差" in loc.why_not(True)
    loc.on_pose(_位姿(1.0, 1.0, m=("other", "1")))
    assert "别的图" in loc.why_not(True)


async def test_跳变之后要连着几帧稳下来_恢复时把修正量交给引擎():
    got = []
    c, link, loc = await _就绪()
    loc.on_corrected = got.append
    loc.on_pose(_位姿(0.0, 0.0))                         # 里程原点 ↔ 地图 (0, 0)
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
    [d] = got
    want = compose(after, inverse(before))
    assert all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(d, want, strict=True))


async def test_跟里程对不上也当跳_里程不新鲜时修正量给不出():
    got = []
    c, link, loc = await _就绪()
    loc.on_corrected = got.append
    loc.on_pose(_位姿(0.0, 0.0))
    loc.on_pose(_位姿(1.0, 0.0))                          # 定位器挪了 1 m,里程没动
    assert "对不上" in loc.why_not(True)
    loc.update((0.0, 0.0, 0.0), False)                   # 这期间里程断了
    loc.update((0.0, 0.0, 0.0), True)
    for _ in range(SETTLE_FIXES):
        loc.on_pose(_位姿(1.0, 0.0))
    assert loc.ok(True) and got == [None], "里程断过:旧坐标换不过来,引擎把来路作废"


async def test_只转了朝向_跟里程对不上也当跳():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(0.0, 0.0, 0.0))
    loc.on_pose(_位姿(0.0, 0.0, 0.6))                     # 定位器转了 0.6 rad,里程没转
    assert "朝向差" in loc.why_not(True)


async def test_小的出入不算跳():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(0.0, 0.0))
    loc.update((1.0, 0.0, 0.0), True)
    loc.on_pose(_位姿(1.15, 0.0))                         # 里程 1 m、定位器 1.15 m:在线内
    assert loc.ok(True)


async def test_定位器说丢了_断了_都不可信_连回来重新换先验要新位置():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(1.0, 1.0))
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


async def test_换图_旧图的位置作废_新图换好先验给了位置才可信():
    c, link, loc = await _就绪()
    loc.on_pose(_位姿(1.0, 1.0))
    loc.on_map(("estate-1", "8"), "/maps/estate-1/8")
    assert "换图" in loc.why_not(True)
    await loc.prior_task
    assert link.sent[-1].map_version == "8"
    loc.on_pose(_位姿(1.0, 1.0))                          # 还是旧图的
    assert "别的图" in loc.why_not(True)
    loc.on_pose(_位姿(2.0, 2.0, m=("estate-1", "8")))
    assert loc.ok(True)


@pytest.mark.parametrize("how,want", [("down", "localizer_unavailable"),
                                      ("hang", "localizer_unavailable"),
                                      ("refuse", "localizer_refused: 初值离地图太远")])
async def test_重定位_不在线_没回_拒了(how, want):
    c, link, loc = await _就绪()
    if how == "down":
        link.down = True
    elif how == "hang":
        link.hang = True
    else:
        link.answer["relocalize"] = (False, "初值离地图太远")
    assert (await loc.relocalize(M, (1.0, 2.0, 0.3))).startswith(want)


async def test_重定位_发初值_收下之后下一帧按跳变稳下来():
    got = []
    c, link, loc = await _就绪()
    loc.on_corrected = got.append
    loc.on_pose(_位姿(0.0, 0.0))
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


async def test_质量_越不确定越低_丢了是零():
    c, link, loc = await _就绪()
    assert loc.quality(True) == 0.0
    loc.on_pose(_位姿(0.0, 0.0, sigma=0.1))
    q1 = loc.quality(True)
    loc.on_pose(_位姿(0.0, 0.0, sigma=0.5))
    assert 0.0 < loc.quality(True) < q1 < 1.0
