"""三条纪律。**这三条是跨模块的约束**(备份 x 起飞检查 x 盘况页),所以单独
一个文件 —— 将来谁想放松它们,得先删掉一个名字里写着"纪律"的文件。

来源:spec §7.6 那三段结尾。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from d1max_patrol.engine.backup import (
    BEHIND_PUSH_RUNS,
    NoticeLevel,
    TargetStatus,
    backup_notice,
)
from d1max_patrol.engine.homing import HomePoint
from d1max_patrol.engine.preflight import run_preflight
from d1max_patrol.engine.removable import DiskRole, Removable, blocks_takeoff
from d1max_patrol.protocol.nav_types import Pose

_HOME = HomePoint(map_id="map_test", pose=Pose.from_xy_yaw(0.0, 0.0),
                  marked_at_ms=1_757_000_000_000)


def _mirror(usable: bool = True) -> TargetStatus:
    return TargetStatus(mount=Path("/media/u1"), role=DiskRole.MIRROR,
                        sn="D1M-0007", label="内置镜像", usable=usable,
                        detail="")


def _transfer() -> TargetStatus:
    return TargetStatus(mount=Path("/media/u2"), role=DiskRole.TRANSFER,
                        sn="D1M-0007", label="交付盘", usable=True, detail="")


def test_起飞检查里没有备份这一项():
    # 写在文档里的"可选"会在半年内漂成"必选",而漂过去的那天没有任何测试会红。
    names = set(inspect.signature(run_preflight).parameters)
    assert not [n for n in names if "backup" in n]


async def test_起飞检查跑完一遍结论里也没有备份这一项(tmp_path, sample_mission,
                                                     fake_nav, fake_device):
    # 只扫签名挡不住"复用一个已有参数把备份校验夹带进去"。这里真跑一遍,
    # 断言的是**检查项的名字**,不是它过没过 —— 名字不受这台机器盘用了多少影响。
    r = await run_preflight(fake_nav, fake_device, sample_mission, tmp_path,
                            home=_HOME, removable=())
    assert [c.name for c in r.checks if "backup" in c.name] == []


def test_本狗的镜像盘不拦起飞():
    # §7.5 那道门槛拦的是**外插的临时盘**(凸出来的盘是杠杆),理由是机械安全,
    # 跟"备份做没做"无关。镜像盘装在机器内部,从来不拦。
    disk = Removable(mount=Path("/media/u1"), role=DiskRole.MIRROR,
                     sn="D1M-0007")
    assert blocks_takeoff([disk], robot_sn="D1M-0007") == ()


def test_没配镜像盘的时候是中性的不是红的():
    # 常年报警的东西等于没报警:人先学会忽略它,然后连真的那次也一起忽略。
    got = backup_notice([_transfer()])
    assert got.level is NoticeLevel.NEUTRAL
    assert "未配备份盘" in got.detail


def test_保留期快到期的时候才顶到人脸上():
    got = backup_notice([_transfer()], days_left=3.0)
    assert got.level is NoticeLevel.PUSH
    assert "永久删掉" in got.detail


def test_保留期还早的时候不顶():
    got = backup_notice([_transfer()], days_left=60.0)
    assert got.level is NoticeLevel.NEUTRAL


def test_授权_OTA_之前也顶一次():
    # OTA 会刷掉启动盘。这是"没有第二份"这件事代价最大的一个时刻。
    got = backup_notice([_transfer()], ota_pending=True)
    assert got.level is NoticeLevel.PUSH
    assert "升级" in got.detail


def test_配了镜像盘而且跟得上的时候什么也不说():
    got = backup_notice([_mirror()], behind=1, days_left=3.0,
                        ota_pending=True)
    assert got.level is NoticeLevel.OK
    assert got.detail == ""


def test_镜像盘落后太多要顶出来_哪怕配着():
    # 一块不再同步的镜像盘比没有镜像盘更危险:它是个完美的静默故障 ——
    # 所有人都以为有第二份。
    got = backup_notice([_mirror()], behind=BEHIND_PUSH_RUNS)
    assert got.level is NoticeLevel.PUSH
    assert "落后" in got.detail


def test_不可用的镜像盘不算配了():
    # 插着一块别的狗的镜像盘 —— role 上写着 mirror,但我们一个字节也不会往里写。
    # 把它算成"配了",等于用最像"有备份"的样子盖住了"没有备份"。
    got = backup_notice([_mirror(usable=False)])
    assert got.level is NoticeLevel.NEUTRAL
    assert "未配备份盘" in got.detail
    # 而且该顶的时候照顶 —— 它等于没配,不是等于配好了。
    urgent = backup_notice([_mirror(usable=False)], days_left=3.0)
    assert urgent.level is NoticeLevel.PUSH
