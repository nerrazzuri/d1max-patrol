"""起飞前五项检查:不许短路,不确定不放行。"""

from __future__ import annotations

import pytest

from d1max_patrol.engine.preflight import BATTERY_MARGIN_PCT, run_preflight
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus

NAMES = ["nav_ready", "device_ready", "localized", "battery", "storage"]


async def test_全都好的时候五项全过(tmp_path, sample_mission, fake_nav, fake_device):
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert r.ok
    assert [c.name for c in r.checks] == NAMES


async def test_导航不在待机就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    fake_nav.nav = NavStatus.ACTIVE
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert not r.ok
    assert [c.name for c in r.failures] == ["nav_ready"]
    assert "Active" in r.failures[0].detail, "得说清楚当前是什么状态"


@pytest.mark.parametrize("bad", [LocStatus.LOC_LOST, LocStatus.INIT,
                                 LocStatus.MAP_LOADING, LocStatus.ERROR])
async def test_定位没收敛就不让起飞(tmp_path, sample_mission, fake_nav, fake_device, bad):
    fake_nav.loc = bad
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["localized"]
    assert bad.value in r.failures[0].detail


async def test_电量低于返航线加余量就不让起飞(tmp_path, sample_mission, fake_nav,
                                              fake_device):
    """刚好卡在返航线上就出发,等于第一个点还没到就该返航了。"""
    fake_device.batt = sample_mission.policy.battery_return_pct + 1.0
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["battery"]


async def test_电量高出余量就放行(tmp_path, sample_mission, fake_nav, fake_device):
    fake_device.batt = (sample_mission.policy.battery_return_pct
                        + BATTERY_MARGIN_PCT + 0.1)
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert r.ok


async def test_余量是可以调的(tmp_path, sample_mission, fake_nav, fake_device):
    fake_device.batt = sample_mission.policy.battery_return_pct + 1.0
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path,
                            battery_margin_pct=0.5)
    assert r.ok


async def test_急停按下去了就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    fake_device.estop = True
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["device_ready"]
    assert "急停" in r.failures[0].detail


async def test_控制权不在手里就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    """控制权被上装抢走了,指令发出去石沉大海(清单 #46/#47)。"""
    fake_device.control = False
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["device_ready"]
    assert "控制权" in r.failures[0].detail


async def test_归档目录写不了就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    target = tmp_path / "占位"
    target.write_text("我是个文件不是目录", encoding="utf-8")
    r = await run_preflight(fake_nav, fake_device, sample_mission, target)
    assert [c.name for c in r.failures] == ["storage"]


async def test_归档目录不存在就当场建出来(tmp_path, sample_mission, fake_nav, fake_device):
    target = tmp_path / "runs" / "深" / "一点"
    r = await run_preflight(fake_nav, fake_device, sample_mission, target)
    assert r.ok
    assert target.is_dir()


async def test_探针文件不会留在归档目录里(tmp_path, sample_mission, fake_nav, fake_device):
    await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_空间不够就不让起飞(tmp_path, sample_mission, fake_nav, fake_device):
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path,
                            min_free_mb=1e12)
    assert [c.name for c in r.failures] == ["storage"]


async def test_不管前面哪项挂了后面几项照样查(tmp_path, sample_mission, fake_nav,
                                              fake_device):
    """一次把所有毛病都说完 —— 现场最烦的是修一个又冒一个。"""
    fake_nav.nav = NavStatus.ACTIVE
    fake_device.estop = True
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert len(r.checks) == 5
    assert {c.name for c in r.failures} == {"nav_ready", "device_ready"}


async def test_后端抛异常算这一项没过而不是整个炸掉(tmp_path, sample_mission,
                                                    fake_nav, fake_device):
    async def boom():
        raise RuntimeError("连不上")
    fake_nav.nav_status = boom
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["nav_ready"]
    assert "连不上" in r.failures[0].detail
    assert len(r.checks) == 5, "一项炸了不该让后面几项不查"


async def test_读不到状态算没过而不是当成好的(tmp_path, sample_mission, fake_nav,
                                              fake_device):
    """"不知道有没有急停"和"确认没有急停"是两回事,前者不该放行。"""
    async def boom():
        raise RuntimeError("还没收到过状态帧")
    fake_device.emergency = boom
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["device_ready"]


async def test_通过的项也说得出理由(tmp_path, sample_mission, fake_nav, fake_device):
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path)
    assert all(c.detail for c in r.checks), "过了也要给人看,别留空串"
