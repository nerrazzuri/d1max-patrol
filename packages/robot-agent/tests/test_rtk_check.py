"""W09e RTK 核对定位器(纯逻辑)。"""

from __future__ import annotations

import pytest

from d1max_agent.rtk_check import DIFF_M, HOLD_S, RELOC_COOLDOWN_S, RtkCheck
from d1max_contract.geo import GeoRef, enu_to_llh

GEO = GeoRef(5.41, 100.32, 10.0, yaw_deg=30.0, tx=2.0, ty=-1.0, antenna_in_base=(0.3, 0.0))


def _fix(x, y, yaw=0.0, **kw):
    """狗身中心在地图 (x, y)、朝向 yaw 时,天线处的 RTK 固定解。"""
    import math
    ax, ay = x + 0.3 * math.cos(yaw), y + 0.3 * math.sin(yaw)
    lat, lon = enu_to_llh(*GEO.map_to_enu(ax, ay), GEO.lat0, GEO.lon0)
    return {"fix": "fixed", "lat": lat, "lon": lon, "std_h_m": 0.02, "age_s": 1.0,
            "stale": False} | kw


def _check():
    c = RtkCheck()
    c.on_map(GEO)
    return c


def test_对得上什么都不做():
    c = _check()
    for t in range(10):
        v = c.step(_fix(10, 5, 0.5), (10.1, 5.05, 0.5), float(t))
        assert v.reason == "" and v.reloc is None and v.gap_m == pytest.approx(0.11, abs=0.01)


def test_连续差一米以上三秒_不可信_在RTK的位置重定位_冷却期内不再请():
    c = _check()
    got = [c.step(_fix(10, 5, 0.5), (14, 5, 0.5), t * 0.5) for t in range(int(HOLD_S / 0.5) + 1)]
    assert all(v.reason == "" for v in got[:-1])
    last = got[-1]
    assert "RTK 说位置差 4.0 m" in last.reason
    assert last.reloc == pytest.approx((10, 5, 0.5), abs=1e-6), "朝向用定位器的"
    v = c.step(_fix(10, 5, 0.5), (14, 5, 0.5), HOLD_S + 1)
    assert v.reason and v.reloc is None, "冷却期内不再请"
    v = c.step(_fix(10, 5, 0.5), (14, 5, 0.5), HOLD_S + RELOC_COOLDOWN_S + 0.1)
    assert v.reloc is not None


def test_单帧跳一下不触发():
    c = _check()
    c.step(_fix(10, 5), (14, 5, 0.0), 0.0)
    c.step(_fix(10, 5), (10, 5, 0.0), 1.0)
    v = c.step(_fix(10, 5), (14, 5, 0.0), HOLD_S + 0.5)
    assert v.reason == "", "中间对上过一次:重新数"


def test_对回来连续三秒才解除():
    c = _check()
    for t in range(8):
        c.step(_fix(10, 5), (14, 5, 0.0), float(t))
    assert c.step(_fix(10, 5), (10.1, 5, 0.0), 10.0).reason
    assert c.step(_fix(10, 5), (10.1, 5, 0.0), 12.0).reason
    assert c.step(_fix(10, 5), (10.1, 5, 0.0), 13.0).reason == ""


@pytest.mark.parametrize("kw", [{"fix": "float"}, {"fix": "single"}, {"std_h_m": 0.3},
                                {"age_s": 9.0}, {"stale": True}, {"lat": None}])
def test_不够好的RTK不参与(kw):
    c = _check()
    for t in range(10):
        v = c.step(_fix(10, 5, **kw), (14, 5, 0.0), float(t))
    assert v.reason == "" and v.reloc is None and v.gap_m is None


def test_没有配准不参与_换图作废():
    c = RtkCheck()
    for t in range(10):
        assert c.step(_fix(10, 5), (14, 5, 0.0), float(t)).reason == ""
    c = _check()
    for t in range(8):
        c.step(_fix(10, 5), (14, 5, 0.0), float(t))
    assert c.step(_fix(10, 5), (14, 5, 0.0), 8.0).reason
    c.on_map(None)
    assert c.step(_fix(10, 5), (14, 5, 0.0), 9.0).reason == ""


def test_差在一米以内不算():
    c = _check()
    for t in range(10):
        v = c.step(_fix(10, 5), (10 + DIFF_M * 0.9, 5, 0.0), float(t))
    assert v.reason == ""


def _flag(c, t0=0.0):
    """让它判不可信(连续 4 s 差 4 m),回最后一拍的结论。"""
    v = None
    for i in range(5):
        v = c.step(_fix(10, 5), (14, 5, 0.0), t0 + i)
    return v


def test_第一次判不可信发事件():
    c = _check()
    got = [c.step(_fix(10, 5), (14, 5, 0.0), float(t)) for t in range(5)]
    evs = [v.event for v in got if v.event]
    assert evs == ["rtk_disagree"]
    first = next(v for v in got if v.event)
    assert first.data["gap_m"] == pytest.approx(4.0, abs=0.01)


def test_按RTK请了两次还对不上_不再请_发事件交给人():
    """W09e 内审再议 1:RTK 的「假固定」时地图匹配很坚定,按 RTK 重定位完又回原处,来回拉扯。"""
    c = _check()
    relocs, events = [], []
    t = 0.0
    while t < 60:
        v = c.step(_fix(10, 5), (14, 5, 0.0), t)
        if v.reloc:
            relocs.append(t)
        if v.event:
            events.append(v.event)
        t += 1.0
    assert len(relocs) == 2 and events == ["rtk_disagree", "rtk_gave_up"]
    assert c.step(_fix(10, 5), (14, 5, 0.0), 61.0).reason, "还是不可信,只是不再自动请"


def test_人给了位置_之前的结论作废_一分钟内不按RTK自动请():
    c = _check()
    assert _flag(c).reason
    c.human_override(10.0)
    assert c.step(_fix(10, 5), (14, 5, 0.0), 10.5).reason == ""
    vs = [c.step(_fix(10, 5), (14, 5, 0.0), 10.0 + i) for i in range(1, 50)]
    assert any(v.reason for v in vs), "还是会标不可信"
    assert not any(v.reloc for v in vs), "但人刚给过位置:不自动请"
    assert c.step(_fix(10, 5), (14, 5, 0.0), 71.0).reloc is not None


def test_标了之后RTK一直用不上_半分钟后降级解除():
    c = _check()
    assert _flag(c).reason
    for t in range(5, 30):
        assert c.step(_fix(10, 5, fix="float"), (14, 5, 0.0), float(t)).reason
    assert c.step(_fix(10, 5, fix="float"), (14, 5, 0.0), 36.0).reason == ""


def test_定位器正在重定位时不算RTK用不上():
    c = _check()
    assert _flag(c).reason
    for t in range(5, 60):
        v = c.step(_fix(10, 5), None, float(t))              # est None:定位器在重定位
    assert v.reason, "RTK 还好好的:不降级"


def test_基站挪了_不核对_发一次事件():
    import dataclasses
    geo = dataclasses.replace(GEO, base_ecef=(-1.0e6, 6.2e6, 6.0e5))
    c = RtkCheck()
    c.on_map(geo)
    assert c.on_base((-1.0e6, 6.2e6, 6.0e5 + 0.01)) is None, "差 1 cm:没挪"
    ev = c.on_base((-1.0e6 + 2.0, 6.2e6, 6.0e5))
    assert ev.event == "rtk_base_moved" and ev.data["moved_m"] == pytest.approx(2.0)
    assert c.on_base((-1.0e6 + 2.0, 6.2e6, 6.0e5)) is None, "只发一次"
    for t in range(10):
        v = c.step(_fix(10, 5), (14, 5, 0.0), float(t))
    assert v.reason == "" and v.reloc is None
    c.on_base(None)
    assert c.base_moved, "不知道的时候保持原判断"


def test_标准差是NaN_不参与():
    c = _check()
    for t in range(10):
        v = c.step(_fix(10, 5, std_h_m=float("nan")), (14, 5, 0.0), float(t))
    assert v.reason == ""


async def test_定位桥_RTK说不对时_最后可信的位置不跟着走_丢定位重置不按它请():
    """W09e 内审应修 2:原来 _good 一路跟着错的位置走,引擎丢定位后的 reset() 又把它拉回错处。"""
    from d1max_agent.bridge_localizer import BridgeLocalizer
    b = BridgeLocalizer(monotonic=lambda: 0.0)
    asked = []

    async def relocalize(*a, **k):
        asked.append(a)
        return ""
    b.relocalize = relocalize
    b._good = object()
    b._map = ("m", "1")
    b.rtk_disagree = "RTK 说位置差 4.0 m,定位器不可信"
    await b.reset()
    assert asked == [], "RTK 那一路负责;这里不按最后可信位置再请"

    class F:
        sigma_xy = 0.1
    assert b._trusted(F()) is False
    b.rtk_disagree = ""
    assert b._trusted(F()) is True
