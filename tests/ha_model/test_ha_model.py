"""站点热备:控制权的不变式和脑裂场景(docs/热备与HA设计.md 第九节)。纯模型,不碰产品代码。"""

from __future__ import annotations

import pytest

from tests.ha_model.model import KEEP_S, PromoteRefused, Robot, World


def _failover(w: World, **kw) -> None:
    w.b.promote(old_leader_is_off=True, **kw)


def test_平时_只有一个主_备机不发命令不开_broker():
    w = World.fresh("r1")
    assert w.leaders() == ["a"]
    assert w.send("a", "r1", "move").accepted
    assert w.send("b", "r1", "move") is None and w.b.issue("move") is None
    assert not w.b.broker_open() and w.b.sent == []


def test_主坏了_人确认后升备机_任期加一_狗改听新主():
    w = World.fresh("r1", "r2")
    w.send("a", "r1", "move")
    w.power_off("a")
    _failover(w)
    assert w.leaders() == ["a", "b"], "断了电的旧主自己还以为是主:靠任期号和下台来收"
    assert (w.a.epoch, w.b.epoch) == (1, 2)
    assert w.send("b", "r1", "move").accepted and w.robots["r1"].disk_epoch == 2
    assert w.send("a", "r1", "move") is None, "断着电,发不出来"


def test_不变式_任期只增_不听旧的_狗重启也一样():
    w = World.fresh("r1")
    w.power_off("a")
    _failover(w)
    assert w.send("b", "r1", "move").accepted
    w.heal()                                    # 旧主又通了,还没发现自己被换了
    r = w.robots["r1"]
    r.reboot()
    ack = w.send("a", "r1", "move")
    assert not ack.accepted and ack.why == "stale_site" and ack.seen_epoch == 2
    assert [c.leader_id for c in r.executed] == ["b"] and r.disk_epoch == 2


def test_不变式_先记再做_反过来的话狗重启后会听旧主的():
    w = World.fresh("r1")
    w.robots["r1"] = Robot("r1", persist_before_execute=False)      # 故意做错
    w.power_off("a")
    _failover(w)
    # 直接收命令(不经过连 broker 时看标记那一道):只演「执行前落不落盘」这一条
    assert w.robots["r1"].receive(w.b.issue("move"), w.now).accepted
    w.heal()
    w.robots["r1"].reboot()                     # 新任期没落盘就重启
    wrong = w.robots["r1"].receive(w.a.issue("move"), w.now)
    assert wrong.accepted, "这就是为什么要先落盘:做错了旧主的命令会被执行"
    good = World.fresh("r1")
    good.power_off("a")
    _failover(good)
    good.send("b", "r1", "move")
    good.heal()
    good.robots["r1"].reboot()
    assert not good.send("a", "r1", "move").accepted


def test_不变式_旧主收到一条站点过期的回执就下台_重启也不回来():
    w = World.fresh("r1", "r2")
    w.power_off("a")
    _failover(w)
    w.send("b", "r1", "move")
    w.heal()
    w.partition("a", "b")                       # 两台主机互相够不着,旧主够得着狗
    w.send("a", "r1", "move")                   # r1 见过新任期:拒
    assert w.a.state == "deposed" and w.leaders() == ["b"]
    assert w.send("a", "r2", "move") is None, "下台以后对没见过新任期的狗也不发"
    w.a.restart()
    assert w.a.state == "deposed" and w.a.issue("move") is None


def test_不变式_旧主回来先问对方_发现任期大就下台_一条命令都不发():
    w = World.fresh("r1")
    w.power_off("a")
    _failover(w)
    w.heal()
    w.gossip()
    assert w.a.state == "deposed" and w.b.state == "leader"
    assert w.send("a", "r1", "move") is None and w.a.sent == []


def test_升主的规矩_没确认旧主断开不升_下台的不能直接升_落后要加确认():
    w = World.fresh("r1")
    with pytest.raises(PromoteRefused, match="没确认"):
        w.b.promote(old_leader_is_off=False)
    w.write("a", "告警1")                        # 异步的:还没到备机
    with pytest.raises(PromoteRefused, match="落后"):
        w.b.promote(old_leader_is_off=True, peer_position=len(w.a.log))
    w.b.promote(old_leader_is_off=True, peer_position=len(w.a.log), accept_stale_data=True)
    w.gossip()
    assert w.a.state == "deposed"
    with pytest.raises(PromoteRefused, match="重新同步"):
        w.a.promote(old_leader_is_off=True)
    with pytest.raises(PromoteRefused, match="已经是主"):
        w.b.promote(old_leader_is_off=True)


def test_不变式_不会同号_换回去要先同步_任期再加一():
    w = World.fresh("r1")
    w.power_off("a")
    _failover(w)
    w.heal()
    w.gossip()
    w.a.rejoin(w.b)
    assert (w.a.state, w.a.epoch) == ("standby", 2)
    w.power_off("b")
    w.a.promote(old_leader_is_off=True)
    assert w.a.epoch == 3 and w.b.epoch == 2
    epochs = [(s.leader_id, s.epoch) for s in (w.a, w.b) if s.state == "leader"]
    assert len({e for _, e in epochs}) == len(epochs), "两个自以为是主的,任期号不一样"


def test_脑裂_人没断开旧主就升了_碰过新主的狗不听旧的_没碰过的还听_直到旧主下台():
    w = World.fresh("near_a", "near_b")
    w.partition("a", "b")
    w.partition("a", "near_b")
    w.partition("b", "near_a")                  # 网分成两半,各带一台狗
    _failover(w)                                # 人以为旧主断了,其实没有
    assert sorted(w.leaders()) == ["a", "b"], "两个主:设计稿明说这种情况防不住"
    assert w.send("a", "near_a", "move").accepted, "只跟旧主连着的狗还听旧主的"
    assert w.send("b", "near_b", "move").accepted
    w.heal()
    assert not w.send("a", "near_b", "move").accepted
    assert w.a.state == "deposed" and w.leaders() == ["b"], "一碰到见过新任期的狗就下台"
    assert w.send("a", "near_a", "move") is None
    assert w.send("b", "near_a", "move").accepted and w.robots["near_a"].disk_epoch == 2
    assert [c.leader_id for c in w.robots["near_a"].executed] == ["a", "b"]


def test_狗连_broker_先看任期标记_比自己记的小就不连():
    w = World.fresh("r1")
    w.power_off("a")
    _failover(w)
    w.send("b", "r1", "stop")
    r = w.robots["r1"]
    assert r.see_leader_marker(1) is False and r.see_leader_marker(2) is True
    assert r.see_leader_marker(3) is True and r.disk_epoch == 3


def test_换主时驱离会话没复制过来_狗上的声光最迟45秒自己关_新主不替它续():
    w = World.fresh("r1")
    w.write("a", "驱离会话1")                    # 没来得及同步
    assert w.send("a", "r1", "outputs_on").accepted
    w.power_off("a")
    _failover(w)
    assert "驱离会话1" not in w.b.log, "新主不知道有这一场"
    r = w.robots["r1"]
    w.now = KEEP_S - 1
    assert r.outputs_on(w.now)
    w.now = KEEP_S
    assert not r.outputs_on(w.now), "没人续:自己关"
    assert [c.kind for c in w.b.sent] == []


def test_三样要紧的同步写_备机健康时一条不丢_备机够不着退成只写本机并说没同步到():
    w = World.fresh("r1")
    assert w.write("a", "P1告警", sync=True) is True
    assert w.write("a", "驱离会话", sync=True) is True
    w.power_off("a")
    _failover(w)
    assert w.b.log == ["P1告警", "驱离会话"]
    w2 = World.fresh("r1")
    w2.partition("a", "b")
    assert w2.write("a", "P1告警", sync=True) is False, "要报 P2「备机跟不上」"
    assert w2.a.log == ["P1告警"] and w2.b.log == []


def test_旧主回来当备机_分区期间自己记的不合并_导出给人看():
    w = World.fresh("r1")
    w.write("a", "共同的", sync=True)
    w.partition("a", "b")
    w.write("a", "旧主自己确认的告警")
    _failover(w)
    w.write("b", "新主记的")
    w.heal()
    w.gossip()
    diverged = w.a.rejoin(w.b)
    assert diverged == ["旧主自己确认的告警"]
    assert w.a.log == ["共同的", "新主记的"] and w.a.state == "standby"
    with pytest.raises(PromoteRefused):
        w.b.rejoin(w.a)
