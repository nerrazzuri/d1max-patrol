"""备份盘:认盘、同步状态、规划、执行。

**这一层不碰引擎状态,只碰文件。** 所以每一条都能在临时目录里跑完,
而备份这件事在真机上恰恰是最难复现的——盘要插着、要挂上、要有空间。
"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.engine.backup import (
    BackupError,
    Target,
    init_target,
    marker_path,
    read_marker,
)
from d1max_patrol.engine.removable import DiskRole, read_role


def test_写下的标记能被第一卷那个读取器读出来(tmp_path):
    # 一个模块写、另一个模块读同一个文件。这条测试是那道缝的唯一守卫。
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    role, sn = read_role(tmp_path)
    assert role is DiskRole.MIRROR
    assert sn == "D1M-0007"


def test_标记落在第一卷约定的那个路径上(tmp_path):
    assert marker_path(tmp_path) == tmp_path / ".d1max-backup" / "target.json"


def test_读得回完整的标记(tmp_path):
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.TRANSFER,
                label="一号厂房交付盘", now_ms=1_757_000_000_000)
    assert read_marker(tmp_path) == Target(
        mount=tmp_path, role=DiskRole.TRANSFER, sn="D1M-0007",
        label="一号厂房交付盘", created_at_ms=1_757_000_000_000)


def test_没有标记的盘读出来是空(tmp_path):
    assert read_marker(tmp_path) is None


def test_标记文件坏了当成没有标记而不是抛(tmp_path):
    # 抛出去的那一边,一块坏盘会把整趟"认盘"炸掉,连边上那几块好盘都列不出来,
    # 而人正等着看那份列表决定往哪块盘上拷。
    marker_path(tmp_path).parent.mkdir(parents=True)
    marker_path(tmp_path).write_text("{不是 json", encoding="utf-8")
    assert read_marker(tmp_path) is None


def test_盘上已经有别的狗的标记要拒绝而不是覆盖(tmp_path):
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    with pytest.raises(BackupError) as e:
        init_target(tmp_path, robot_sn="D1M-0008", role=DiskRole.MIRROR,
                    now_ms=1_757_000_100_000)
    assert "D1M-0007" in str(e.value)
    assert "D1M-0008" in str(e.value)
    # 原来那份原封不动。
    assert read_marker(tmp_path).sn == "D1M-0007"


def test_同一台狗重新初始化是允许的(tmp_path):
    # 换角色(镜像盘改成交付盘)是个正当操作,不该被"已有标记"挡住。
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    got = init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.TRANSFER,
                      now_ms=1_757_000_100_000)
    assert got.role is DiskRole.TRANSFER
    assert read_marker(tmp_path).role is DiskRole.TRANSFER


def test_角色只许写镜像或取走(tmp_path):
    with pytest.raises(BackupError) as e:
        init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.UNKNOWN,
                    now_ms=1_757_000_000_000)
    assert "unknown" in str(e.value)
    assert not marker_path(tmp_path).exists()


def test_写标记是原子的_不留临时文件(tmp_path):
    init_target(tmp_path, robot_sn="D1M-0007", role=DiskRole.MIRROR,
                now_ms=1_757_000_000_000)
    assert not list(marker_path(tmp_path).parent.glob("*.tmp"))
    raw = json.loads(marker_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["role"] == "mirror"
    assert raw["sn"] == "D1M-0007"
