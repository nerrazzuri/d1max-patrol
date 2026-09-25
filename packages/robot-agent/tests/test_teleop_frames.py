"""W00c5c:代理判一帧遥控能不能执行(决策 7:规矩由代理执行)。租约代次对得上、序号严格增、在途时间
不比基线多出 ``max_transit_ms``。狗与站点的钟不必对准:基线是「到达(狗钟)− 发出(站点钟)」的
滑动最小值。"""

from __future__ import annotations

from d1max_agent.teleop_frames import FrameGate
from d1max_contract.teleop import TeleopFrame

SKEW = 7_654_321                    # 狗钟比站点钟快这么多(任意),判定不受影响


def _f(seq, sent, epoch=2, vx=0.2):
    return TeleopFrame(lease_epoch=epoch, seq=seq, sent_at=sent, ttl_ms=300, vx=vx, wz=0.0)


def test_正常的帧收下():
    g = FrameGate(lease_epoch=2, max_transit_ms=250)
    for i in range(1, 6):
        assert g.accept(_f(i, 1000 + i * 100), rx_ms=1000 + i * 100 + 40 + SKEW) == ""


def test_别的代次不收():
    g = FrameGate(lease_epoch=2)
    assert g.accept(_f(1, 1000, epoch=1), rx_ms=1000 + SKEW) == "epoch"
    assert g.accept(_f(1, 1000, epoch=3), rx_ms=1000 + SKEW) == "epoch"


def test_序号不增不收_重复的不收_回退的不收():
    g = FrameGate(lease_epoch=2)
    assert g.accept(_f(5, 1000), rx_ms=1000 + SKEW) == ""
    assert g.accept(_f(5, 1100), rx_ms=1100 + SKEW) == "seq"
    assert g.accept(_f(3, 1200), rx_ms=1200 + SKEW) == "seq"
    assert g.accept(_f(6, 1300), rx_ms=1300 + SKEW) == ""


def test_在途积压的不收_但序号照样往前走():
    g = FrameGate(lease_epoch=2, max_transit_ms=250)
    for i in range(1, 11):                            # 基线:在途 40 ms
        assert g.accept(_f(i, i * 100), rx_ms=i * 100 + 40 + SKEW) == ""
    # 网络憋了一下:接下来几帧晚到 600 ms
    assert g.accept(_f(11, 1100), rx_ms=1100 + 640 + SKEW) == "late"
    assert g.accept(_f(12, 1200), rx_ms=1200 + 640 + SKEW) == "late"
    # 积压帧的重复(同一个序号)准时到了也不收:序号已经往前走过了
    assert g.accept(_f(12, 1200), rx_ms=1200 + 40 + SKEW) == "seq"
    # 恢复:又准时了
    assert g.accept(_f(13, 1300), rx_ms=1300 + 50 + SKEW) == ""
    # 憋着的那几帧里序号更小的,就算这时才到也不收
    assert g.accept(_f(12, 1200), rx_ms=1400 + SKEW) == "seq"


def test_基线会跟着真实的最小在途往下走():
    """第一帧就晚到的话,基线一开始偏大;之后来了更快的帧,基线跟着往下走。"""
    g = FrameGate(lease_epoch=2, max_transit_ms=250)
    assert g.accept(_f(1, 100), rx_ms=100 + 500 + SKEW) == ""
    assert g.accept(_f(2, 200), rx_ms=200 + 40 + SKEW) == ""
    assert g.accept(_f(3, 300), rx_ms=300 + 500 + SKEW) == "late"
