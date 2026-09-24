"""起飞前检查:不许短路,不确定不放行。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from d1max_agent.engine.form import STANDALONE, Form
from d1max_agent.engine.homing import HomePoint, ReturnParams
from d1max_agent.engine.mission import MissionWaypoint, Policy
from d1max_agent.engine.preflight import departure_line_pct, run_preflight
from d1max_agent.engine.removable import DiskRole, Removable
from d1max_agent.engine.safety import (
    Decision,
    SafetyContext,
    battery_ruling,
    return_line_pct,
)
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose

from .conftest import make_mission

NAMES = ["nav_ready", "device_ready", "localized", "home", "battery",
         "storage", "removable"]

_HOME = HomePoint(map_id="map_test", pose=Pose.from_xy_yaw(0.0, 0.0),
                  marked_at_ms=1_757_000_000_000)


async def _run(nav, device, mission, root, **kw):
    """跑一遍起飞检查,原点和"扫过的盘"默认都给一个正常的。

    绝大多数用例关心的不是原点,给它一个正常值,免得每条都写一遍;
    真要测原点的那几条显式传 ``home=``。

    ``removable`` 同理,但默认值的含义要紧:``()`` 是"扫过了,一块都没有",
    ``run_preflight`` 自己的默认 ``None`` 是"没扫过",判没过。这里显式传
    ``()`` 而不是省略,是因为这些用例测的都不是"没扫过"那条路。
    """
    kw.setdefault("home", _HOME)
    kw.setdefault("removable", ())
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


async def test_预计耗电的安全系数是可以调的(sample_mission):
    """1.5 是"预计耗电本身不准"的赔率。场地摸熟了可以往下调。"""
    assert (departure_line_pct(sample_mission, _HOME, slack=3.0)
            > departure_line_pct(sample_mission, _HOME, slack=1.5))


async def test_run_preflight的安全系数和标定系数真的传到判决里(
        tmp_path, sample_mission, fake_nav, fake_device):
    """`departure_line_pct` 能调不等于 `run_preflight` 真把参数往下传了 ——
    这条从 `_run` 一路查到 battery 项的判决,不是关起门来测那个纯函数。"""
    line = departure_line_pct(sample_mission, _HOME)
    fake_device.batt = line + 0.1
    ok = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert ok.ok, "刚好卡在默认参数算出的线上方,默认情况下该放行"

    steeper = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                         estimate_slack=10.0)
    assert [c.name for c in steeper.failures] == ["battery"], (
        "安全系数调大之后,同样的电量应该不够了")

    thirsty = ReturnParams(drain_pct_per_hour=2230.0)
    thirstier = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                           return_params=thirsty)
    assert [c.name for c in thirstier.failures] == ["battery"], (
        "标定系数换成耗电快得多的一套,同样的电量应该不够了")


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
    assert len(r.checks) == 7
    assert {c.name for c in r.failures} == {"nav_ready", "device_ready"}


async def test_后端抛异常算这一项没过而不是整个炸掉(tmp_path, sample_mission,
                                                    fake_nav, fake_device):
    async def boom():
        raise RuntimeError("连不上")
    fake_nav.nav_status = boom
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.failures] == ["nav_ready"]
    assert "连不上" in r.failures[0].detail
    assert len(r.checks) == 7, "一项炸了不该让后面几项不查"


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
    assert [c.name for c in r.failures] == ["home", "battery"]
    assert "别的图" in r.failures[0].detail


async def test_出发线随着路线变长而升高(sample_mission):
    far = make_mission(waypoints=(
        MissionWaypoint(name="远", pose=Pose.from_xy_yaw(300.0, 0.0)),
    ))
    assert departure_line_pct(far, _HOME) > departure_line_pct(sample_mission, _HOME)


async def test_出发线不会低于返航线的静态下限():
    """**刚起飞就该返航**是这条线最难看的一种失败。

    ``abort=25 / return=60`` 过得了 ``Mission`` 的校验(它只查
    ``abort <= return``)。只算"中止线 + 全程预计 × 1.5"的话出发线是 29.5%,
    而返航线是 60% —— 30% 的电能过起飞门槛,第一帧电量遥测到达就 RETURN_HOME。
    """
    m = make_mission(policy=Policy(battery_abort_pct=25.0,
                                   battery_return_pct=60.0))
    assert departure_line_pct(m, _HOME) == pytest.approx(60.0)


async def test_静态下限低的时候还是动态那一支说了算():
    """两支取 max —— 加上静态那一支不能把动态那一支盖掉。"""
    m = make_mission(policy=Policy(battery_abort_pct=25.0,
                                   battery_return_pct=26.0))
    assert departure_line_pct(m, _HOME) > 26.0


async def test_电量卡在静态下限上就不让起飞(tmp_path, fake_nav, fake_device):
    """从 ``run_preflight`` 那一头查:这条线得真拦住,不只是纯函数算得对。"""
    m = make_mission(policy=Policy(battery_abort_pct=25.0,
                                   battery_return_pct=60.0))
    fake_device.batt = 30.0
    r = await _run(fake_nav, fake_device, m, tmp_path)
    assert [c.name for c in r.failures] == ["battery"]
    assert "60" in r.failures[0].detail


async def test_出发线里带得出中止线那一份(sample_mission):
    # 走完全程回到家的那一刻,手上还得高于中止线 —— 这是出发线的地板。
    assert (departure_line_pct(sample_mission, _HOME)
            > sample_mission.policy.battery_abort_pct)


async def test_通过时也说得出出发线是多少(tmp_path, sample_mission, fake_nav,
                                          fake_device):
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    battery = next(c for c in r.checks if c.name == "battery")
    assert "出发线" in battery.detail, "过了也要让人看见这条线画在哪儿"


class _FakeUpload:
    async def describe(self) -> str:
        return "控制台 https://console.example/upload"


async def test_单机档盘满时说的是保留期而不是回传(tmp_path, sample_mission,
                                                fake_nav, fake_device):
    # 把 min_free_mb 顶到天上,借它触发拦停 —— 我们要看的是文案,不是水位怎么来的。
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                   min_free_mb=1e12, form=STANDALONE)
    detail = next(c.detail for c in r.checks if c.name == "storage")
    assert "保留期" in detail and "回传" not in detail


async def test_联网档盘满时说的是回传中断(tmp_path, sample_mission, fake_nav,
                                          fake_device):
    connected = Form(name="connected", upload=_FakeUpload())
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                   min_free_mb=1e12, form=connected, last_upload_age_days=9.0)
    detail = next(c.detail for c in r.checks if c.name == "storage")
    assert "回传" in detail and "9 天" in detail


def _盘用到(ratio: float):
    """伪造一个水位。总量 100GB,按比例分 used/free。"""
    total = 100 * 1024 ** 3

    def fake(_path):
        used = int(total * ratio)
        return SimpleNamespace(total=total, used=used, free=total - used)
    return fake


async def test_盘过了报警线但还没到拦停线时这一项要把话说重(
        tmp_path, sample_mission, fake_nav, fake_device, monkeypatch):
    # 85%:能起飞,但已经过了 §4.7 那条 80% 报警线。这一卷还没有告警通道,
    # 只印一句"剩余 15360MB,已用 85%"的话,这一项在页面上纯绿 —— 人第一次
    # 知道盘要满,会是它满到 90% 拦停的那一天。
    monkeypatch.setattr("shutil.disk_usage", _盘用到(0.85))
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    storage = next(c for c in r.checks if c.name == "storage")
    assert storage.ok, "85% 还没到拦停线,该放行"
    assert "已用 85%" in storage.detail
    assert "80% 报警线" in storage.detail
    assert "清盘" in storage.detail
    assert "90%" in storage.detail, "还要告诉人下一道线画在哪儿"


async def test_水位正常的时候不说那些重话(tmp_path, sample_mission, fake_nav,
                                          fake_device, monkeypatch):
    monkeypatch.setattr("shutil.disk_usage", _盘用到(0.50))
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    storage = next(c for c in r.checks if c.name == "storage")
    assert storage.ok
    assert "报警线" not in storage.detail and "清盘" not in storage.detail


@pytest.mark.parametrize("name", NAMES)
async def test_七项里随便哪一项炸了都只算那一项没过(tmp_path, sample_mission,
                                                  fake_nav, fake_device,
                                                  monkeypatch, name):
    """保护圈得罩住七项,一项都不能漏在外面。

    漏在外面的那一项炸掉时,掀掉的是整份报告 —— 现场拿到的是一个
    traceback,而不是"还差哪几项"。哪几项是"纯算的、不会炸"这件事以后会变,
    所以这条按名字全量参数化:以后加了第八项,NAMES 一改这里自动跟上。
    """
    async def boom(*args, **kw):
        raise RuntimeError(f"{name} 这一项炸了")
    monkeypatch.setattr(f"d1max_agent.engine.preflight._check_{name}", boom)
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    assert [c.name for c in r.checks] == NAMES, "炸掉一项不许少报另外六项"
    assert [c.name for c in r.failures] == [name]
    assert f"{name} 这一项炸了" in r.failures[0].detail, "异常本身就是没过的理由"


async def test_还插着取走盘就不让起飞(tmp_path, sample_mission, fake_nav,
                                      fake_device):
    disks = [Removable(mount=Path("/media/u1"), role=DiskRole.TRANSFER)]
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                   removable=disks)
    assert [c.name for c in r.failures] == ["removable"]
    assert "/media/u1" in r.failures[0].detail


async def test_只插着镜像盘照样起飞(tmp_path, sample_mission, fake_nav,
                                    fake_device):
    disks = [Removable(mount=Path("/media/mirror"), role=DiskRole.MIRROR)]
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                   removable=disks)
    assert r.ok


async def test_没插盘的时候这一项也说得出话(tmp_path, sample_mission, fake_nav,
                                            fake_device):
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path)
    detail = next(c.detail for c in r.checks if c.name == "removable")
    assert "共认到 0 块" in detail


async def test_别的狗的镜像盘也拦得住(tmp_path, sample_mission, fake_nav,
                                      fake_device):
    """``robot_sn`` 要真传到 ``blocks_takeoff`` 那儿去,不是关起门来测纯函数。"""
    disks = [Removable(mount=Path("/media/mirror"), role=DiskRole.MIRROR,
                       sn="D1MAX-0002")]
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                   removable=disks, robot_sn="D1MAX-0001")
    assert [c.name for c in r.failures] == ["removable"]
    assert "别的狗" in r.failures[0].detail
    assert "D1MAX-0002" in r.failures[0].detail, "得说清是哪块盘"


async def test_自己的镜像盘照样放行(tmp_path, sample_mission, fake_nav,
                                    fake_device):
    disks = [Removable(mount=Path("/media/mirror"), role=DiskRole.MIRROR,
                       sn="D1MAX-0001")]
    r = await _run(fake_nav, fake_device, sample_mission, tmp_path,
                   removable=disks, robot_sn="D1MAX-0001")
    assert r.ok


async def test_没扫过盘跟没插盘不是一回事(tmp_path, sample_mission, fake_nav,
                                          fake_device):
    """``None`` 是"没扫过",判没过 —— 漏传参数不该白得一项绿的。"""
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path,
                            home=_HOME, removable=None)
    assert [c.name for c in r.failures] == ["removable"]
    assert "没扫过" in r.failures[0].detail


async def test_默认就是没扫过而不是没有盘(tmp_path, sample_mission, fake_nav,
                                          fake_device):
    """默认值的方向必须跟 ``home`` 一致 —— 同一个函数里不能有两种哲学。"""
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path,
                            home=_HOME)
    assert [c.name for c in r.failures] == ["removable"]


# ---------------------------------------------------------- 两条线用同一份系数
@pytest.mark.parametrize("floor", [0.0, 0.5, 1.2, 2.9, 3.0, 5.0, 12.0])
def test_出发线永远不低于同一刻的返航线(sample_mission, floor):
    """**放行了就不该立刻掉头。**

    出发线按引擎自己那份 ``ReturnParams`` 算,返航线一度按模块默认那份算 ——
    两支各按各的地板。``floor_pct`` 一旦标定到 3.0 以下,出发线就会低于返航线:
    起飞门槛放行,第一帧电量遥测到达就判 ``RETURN_HOME``,狗一起飞就掉头,
    而没有一条测试会红。这条把两支钉在一起。
    """
    params = ReturnParams(floor_pct=floor)
    line = departure_line_pct(sample_mission, _HOME, params=params)
    # 刚起飞那一刻站在原点上,回家成本是 0 —— 返航线取的正是地板那一支。
    ctx = SafetyContext(policy=sample_mission.policy, battery_pct=line,
                        return_cost_pct=0.0, floor_pct=floor)
    assert return_line_pct(ctx) <= line
    assert battery_ruling(ctx).decision is not Decision.RETURN_HOME
