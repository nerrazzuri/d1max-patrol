"""W09h 时间权威(决策 19,改 W09d 的「没有估计就放行」):会让狗动、建任务、起新运行状态、依赖命令
有效期的命令要求钟差**已知且** |钟差| ≤ 30 s;停下、撤销、放掉、退回与只读照发。"""

from __future__ import annotations

import pytest
from test_site_dispatcher import target, 台子

from d1max_contract.messages import Telemetry
from d1max_contract.supervision import Supervise
from d1max_contract.teleop import TeleopLease
from d1max_contract.video import VideoRequest
from d1max_site.dispatcher import DispatchRefused
from d1max_site.temporal import GATED, READ, SAFE, temporal_class


@pytest.mark.parametrize("kind,payload,want", [
    ("goto", {}, GATED), ("patrol", {}, GATED), ("teleop", {}, GATED),
    ("teleop_lease", {"action": "renew"}, GATED), ("teleop_lease", {"action": "release"}, SAFE),
    ("supervise", {"action": "renew"}, GATED), ("supervise", {"action": "release"}, SAFE),
    ("video", {"camera": "front"}, GATED), ("video", {"stop": True}, SAFE),
    ("mapping", {"action": "start"}, GATED), ("mapping", {"action": "stop"}, SAFE),
    ("map_activate", {}, GATED), ("map_build", {}, GATED), ("release_install", {}, GATED),
    ("release_activate", {}, GATED), ("relocalize", {}, GATED), ("mark_home", {}, GATED),
    ("halt", {}, SAFE), ("abort", {}, SAFE), ("release_rollback", {}, SAFE),
    ("outbox_retry", {}, SAFE),
    ("proc_log", {}, READ), ("mapping_trail", {}, READ), ("mapping_preview", {}, READ),
    ("release_precheck", {}, READ),
    ("some_future_kind", {}, GATED), ("teleop_lease", {}, GATED), ("mapping", {}, GATED),
    ("video", {"stop": "yes"}, GATED),
])
def test_分类_按种类和动作_没列出的一律收紧(kind, payload, want):
    assert temporal_class(kind, payload) == want


def _tele(t, ahead_ms):
    t.site._on_telemetry("A", Telemetry(stamp=t.clock.ms + ahead_ms, pose=None, battery_pct=80,
                                        task_state=None, loc_quality=1.0))


@pytest.fixture
async def 台(tmp_path):
    from conftest import 真钟差
    t = 台子(tmp_path)
    真钟差(t.site)                                         # 测的就是真的估计器
    await t.start()
    yield t
    await t.agent.close()
    await t.site.close()


def _no_samples(t):
    """站点刚起来、还没收到遥测:清掉这台狗的样本(台子起来时狗已经发过几条)。"""
    t.site._skew._samples.pop("A", None)


async def test_零样本_不派_goto_巡检_遥控_视频_监护_换图(台):
    """外审 1:站点刚起来、零样本。"""
    t = 台
    await t.run(3)
    _no_samples(t)
    assert t.site.clock_skew_s("A") is None
    with pytest.raises(DispatchRefused, match="钟差还不知道"):
        await t.site.goto("A", target(1.0), 0.8, issued_by="alice")
    assert "钟差还不知道" in t.site.dispatchable("A", "patrol")
    with pytest.raises(DispatchRefused, match="钟差还不知道"):
        await t.site.teleop_grant("A", lease_epoch=1, operator="gina", lease_ttl_ms=1500,
                                  issued_by="gina")
    with pytest.raises(DispatchRefused, match="钟差还不知道"):
        await t.site.teleop_lease("A", TeleopLease(action="renew", lease_epoch=1,
                                                   lease_ttl_ms=1500), timeout_s=1.0)
    with pytest.raises(DispatchRefused, match="钟差还不知道"):
        await t.site.video("A", VideoRequest(camera="front", url="srt://x", passphrase="p" * 16,
                                             ttl_ms=10_000), timeout_s=1.0)
    with pytest.raises(DispatchRefused, match="钟差还不知道"):
        await t.site.supervise("A", Supervise(action="renew", ttl_ms=3000, operator="gina",
                                              session="s", seq=1), timeout_s=1.0)
    caps = t.site.clients["A"].capabilities
    for kind in ("map_activate", "mapping", "release_install", "relocalize", "mark_home"):
        caps.tasks.setdefault(kind, {})
    for kind, payload in (("map_activate", {}), ("mapping", {"action": "start"}),
                          ("release_install", {}), ("relocalize", {}), ("mark_home", {})):
        with pytest.raises(DispatchRefused, match="钟差还不知道"):
            await t.site.map_command("A", kind, payload, issued_by="alice")


async def test_狗慢样本不够10条_不派_够了恢复(台):
    """外审 2、4。"""
    t = 台
    await t.run(3)
    _no_samples(t)
    for _ in range(5):
        _tele(t, -500)                                   # 狗慢 0.5 s
    assert t.site.clock_skew_s("A") is None
    with pytest.raises(DispatchRefused, match="钟差还不知道"):
        await t.site.goto("A", target(1.0), 0.8, issued_by="alice")
    for _ in range(5):
        _tele(t, -500)
    assert t.site.clock_skew_s("A") == pytest.approx(-0.5)
    r = await t.send(t.site.goto("A", target(1.0), 0.8, issued_by="alice"))
    assert r["ack"]["result"] == "accepted", "钟差已知且合格:恢复派单"


async def test_样本超过60秒失效_状态还新鲜_不派(台):
    """外审 3:狗停发遥测(或遥测丢了),status 还没到算不新鲜。"""
    t = 台
    await t.run(3)
    _no_samples(t)
    _tele(t, 0)
    assert t.site.clock_skew_s("A") == pytest.approx(0.0)
    t.clock.advance(61)
    await t.agent._publish_status(force=True)            # 狗还在报状态:新鲜
    await t.broker.drain()
    assert t.site.is_stale("A") is False
    assert t.site.clock_skew_s("A") is None
    with pytest.raises(DispatchRefused, match="钟差还不知道"):
        await t.site.goto("A", target(1.0), 0.8, issued_by="alice")


async def test_钟差未知_停下撤销放掉退回照发(台):
    """外审 5。"""
    t = 台
    await t.run(3)
    _no_samples(t)
    r = await t.send(t.site.abort("A", "goto-x", issued_by="alice"))
    assert "ack" in r
    ack = await t.send(t.site.teleop_lease("A", TeleopLease(action="release", lease_epoch=1),
                                           timeout_s=2.0))
    assert ack.result is not None
    ack = await t.send(t.site.supervise("A", Supervise(action="release", ttl_ms=3000,
                                                       operator="gina", session="s", seq=1),
                                        timeout_s=2.0))
    assert ack.result is not None
    ack = await t.send(t.site.video("A", VideoRequest(camera="front", url="srt://x",
                                                      passphrase="p" * 16, ttl_ms=10_000,
                                                      stop=True), timeout_s=2.0))
    assert ack.result is not None
    caps = t.site.clients["A"].capabilities
    for kind in ("mapping", "release_rollback", "proc_log"):
        caps.tasks.setdefault(kind, {})
    for kind, payload in (("mapping", {"action": "stop"}), ("release_rollback", {}),
                          ("proc_log", {})):
        r = await t.send(t.site.map_command("A", kind, payload, issued_by="alice"))
        assert "ack" in r, kind
    r = await t.send(t.site.halt("A", issued_by="alice"))
    assert r["ack"]["result"] == "accepted"


async def test_钟差超限_遥控续租视频监护也拒_放掉照发(台):
    """W09d 原来遥控续租、视频、监护心跳直接发,不过钟差闸。"""
    t = 台
    await t.run(3)
    for _ in range(5):
        _tele(t, 40_000)
    for coro in (t.site.teleop_lease("A", TeleopLease(action="renew", lease_epoch=1,
                                                      lease_ttl_ms=1500), timeout_s=1.0),
                 t.site.supervise("A", Supervise(action="renew", ttl_ms=3000, operator="g",
                                                 session="s", seq=1), timeout_s=1.0),
                 t.site.video("A", VideoRequest(camera="front", url="srt://x",
                                                passphrase="p" * 16, ttl_ms=10_000),
                              timeout_s=1.0)):
        with pytest.raises(DispatchRefused, match=r"钟差 \+40 秒"):
            await coro
    ack = await t.send(t.site.teleop_lease("A", TeleopLease(action="release", lease_epoch=1),
                                           timeout_s=2.0))
    assert ack.result is not None
