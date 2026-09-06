"""起飞前检查:不许短路,不确定不放行。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.homing import HomePoint
from d1max_patrol.engine.mission import MissionWaypoint
from d1max_patrol.engine.preflight import departure_line_pct, run_preflight
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

from .conftest import make_mission

NAMES = ["nav_ready", "device_ready", "localized", "home", "battery", "storage"]

_HOME = HomePoint(map_id="map_test", pose=Pose.from_xy_yaw(0.0, 0.0),
                  marked_at_ms=1_757_000_000_000)


async def _run(nav, device, mission, root, **kw):
    """跑一遍起飞检查,原点默认给一个正常的。

    绝大多数用例关心的不是原点,给它一个正常值,免得每条都写一遍;
    真要测原点的那几条显式传 ``home=``。
    """
    kw.setdefault("home", _HOME)
    return await run_preflight(nav, device, mission, root, **kw)


async def test_全都好的时候每一项都过(tmp_path, sample_mission, fake_nav, fake_device):
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert r.ok
    assert [c.name for c in r.checks] == NAMES


async def test_导航不在待机就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    fake_nav.nav = NavStatus.ACTIVE
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert not r.ok
    assert [c.name for c in r.failures] == ["nav_ready"]
    assert "Active" in r.failures[0].detail, "得说清楚当前是什么状态"


@pytest.mark.parametrize("bad", [LocStatus.LOC_LOST, LocStatus.INIT,
                                 LocStatus.MAP_LOADING, LocStatus.ERROR])
async def test_定位没收敛就不让起飞(tmp_path, sample_mission, fake_nav, fake_device, bad):
    fake_nav.loc = bad
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["localized"]
    assert bad.value in r.failures[0].detail


async def test_电量不够走完全程回来还剩着中止线那份就不让起飞(
        tmp_path, sample_mission, fake_nav, fake_device):
    """刚好卡在线上就出发,等于第一个点还没到就该返航了。"""
    fake_device.batt = sample_mission.policy.battery_abort_pct + 1.0
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["battery"]
    assert "出发线" in r.failures[0].detail


async def test_电量够走完全程就放行(tmp_path, sample_mission, fake_nav, fake_device):
    line = departure_line_pct(sample_mission, _HOME)
    fake_device.batt = line + 0.1
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert r.ok


async def test_预计耗电的安全系数是可以调的(tmp_path, sample_mission, fake_nav,
                                            fake_device):
    """1.5 是"预计耗电本身不准"的赔率。场地摸熟了可以往下调。"""
    assert (departure_line_pct(sample_mission, _HOME, slack=3.0)
            > departure_line_pct(sample_mission, _HOME, slack=1.5))


async def test_急停按下去了就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    fake_device.estop = True
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["device_ready"]
    assert "急停" in r.failures[0].detail


async def test_控制权不在手里就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    """控制权被上装抢走了,指令发出去石沉大海(清单 #46/#47)。"""
    fake_device.control = False
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["device_ready"]
    assert "控制权" in r.failures[0].detail


async def test_归档目录写不了就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    target = tmp_path / "占位"
    target.write_text("我是个文件不是目录", encoding="utf-8")
    r = await _run(fake_nav, fake_device, sample_mission, target)
    assert [c.name for c in r.failures] == ["storage"]


async def test_归档目录不存在就当场建出来(tmp_path, sample_mission, fake_nav, fake_device):
    target = tmp_path / "runs" / "深" / "一点"
    r = await _run(fake_nav, fake_device, sample_mission, target)
    assert r.ok
    assert target.is_dir()


async def test_探针文件不会留在归档目录里(tmp_path, sample_mission, fake_nav, fake_device):
    await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_空间不够就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                            min_free_mb=1e12)
    assert [c.name for c in r.failures] == ["storage"]


async def test_不管前面哪项挂了后面几项照样查(tmp_path, sample_mission, fake_nav,
                                              fake_device):
    """一次把所有毛病都说完 —— 现场最烦的是修一个又冒一个。"""
    fake_nav.nav = NavStatus.ACTIVE
    fake_device.estop = True
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert len(r.checks) == 6
    assert {c.name for c in r.failures} == {"nav_ready", "device_ready"}


async def test_后端抛异常算这一项没过而不是整个炸掉(tmp_path, sample_mission,
                                                    fake_nav, fake_device):
    async def boom():
        raise RuntimeError("连不上")
    fake_nav.nav_status = boom
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["nav_ready"]
    assert "连不上" in r.failures[0].detail
    assert len(r.checks) == 6, "一项炸了不该让后面几项不查"


async def test_读不到状态算没过而不是当成好的(tmp_path, sample_mission, fake_nav,
                                              fake_device):
    """"不知道有没有急停"和"确认没有急停"是两回事,前者不该放行。"""
    async def boom():
        raise RuntimeError("还没收到过状态帧")
    fake_device.emergency = boom
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["device_ready"]


async def test_通过的项也说得出理由(tmp_path, sample_mission, fake_nav, fake_device):
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert all(c.detail for c in r.checks), "过了也要给人看,别留空串"


@pytest.mark.parametrize("field, failed", [("nav", "nav_ready"), ("loc", "localized")])
async def test_状态认不出来就不放行(tmp_path, sample_mission, fake_nav, fake_device,
                                    field, failed):
    """端口约定认不出的枚举值回 None(固件可能新增)——None 不是"好的"。"""
    setattr(fake_nav, field, None)
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == [failed]
    assert "认不出" in r.failures[0].detail


async def test_没标过原点就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    # 没有原点就没有返航目标,也算不出出发线。原点是一等公民(spec §1.3),
    # 不是"标了更好"。
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path, home=None)
    assert {c.name for c in r.failures} == {"home", "battery"}
    assert "原点" in dict((c.name, c.detail) for c in r.failures)["battery"]


async def test_原点是别的图上的就不让起飞(tmp_path, sample_mission, fake_nav,
                                          fake_device):
    # 换了张图,原点的坐标就是另一个坐标系里的数。走过去只会到一个错地方。
    wrong = HomePoint(map_id="别的图", pose=Pose.from_xy_yaw(0.0, 0.0),
                      marked_at_ms=1)
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path, home=wrong)
    assert [c.name for c in r.failures] == ["home"]
    assert "别的图" in r.failures[0].detail


async def test_出发线随着路线变长而升高(sample_mission):
    far = make_mission(waypoints=(
        MissionWaypoint(name="远", pose=Pose.from_xy_yaw(300.0, 0.0)),
    ))
    assert departure_line_pct(far, _HOME) > departure_line_pct(sample_mission, _HOME)


async def test_出发线里带得出中止线那一份(sample_mission):
    # 走完全程回到家的那一刻,手上还得高于中止线 —— 这是出发线的地板。
    assert (departure_line_pct(sample_mission, _HOME)
            > sample_mission.policy.battery_abort_pct)


async def test_通过时也说得出出发线是多少(tmp_path, sample_mission, fake_nav,
                                          fake_device):
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    battery = next(c for c in r.checks if c.name == "battery")
    assert "出发线" in battery.detail, "过了也要让人看见这条线画在哪儿"
