"""外插盘:认到取走盘就不许起飞(spec §7.5)。"""

from __future__ import annotations

import json
from pathlib import Path

from d1max_patrol.engine.removable import (
    MARKER_REL,
    NO_IDENTITY,
    DiskRole,
    MountRootProbe,
    Removable,
    blocks_takeoff,
    read_role,
)


def _mark(mount: Path, role: str, sn: str = "D1MAX-0001") -> None:
    marker = mount / MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"role": role, "sn": sn}, ensure_ascii=False),
                      encoding="utf-8")


def test_没有标记文件的盘角色是认不出来(tmp_path):
    assert read_role(tmp_path) == (DiskRole.UNKNOWN, "")


def test_标记文件说是镜像盘就认镜像盘(tmp_path):
    _mark(tmp_path, "mirror")
    assert read_role(tmp_path) == (DiskRole.MIRROR, "D1MAX-0001")


def test_标记文件说是取走盘就认取走盘(tmp_path):
    _mark(tmp_path, "transfer")
    assert read_role(tmp_path)[0] is DiskRole.TRANSFER


def test_标记文件坏了按认不出来处理(tmp_path):
    marker = tmp_path / MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{不是 json", encoding="utf-8")
    assert read_role(tmp_path)[0] is DiskRole.UNKNOWN


def test_标记文件不是utf8也按认不出来处理(tmp_path):
    # read_text(encoding="utf-8") 遇到不是 UTF-8 的字节会抛 UnicodeDecodeError,
    # 那也是"读不出来",不该往外抛——抛出去会把整趟 preflight 掀翻,而不是
    # 让 removable 这一项干净地没过(最需要拦住的那块盘,反而把检查炸了)。
    marker = tmp_path / MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"\xff\xfe\x00\x81binary junk")
    assert read_role(tmp_path)[0] is DiskRole.UNKNOWN


def test_标记文件里写了个没见过的角色也按认不出来处理(tmp_path):
    _mark(tmp_path, "backup")
    assert read_role(tmp_path)[0] is DiskRole.UNKNOWN


def test_镜像盘不拦起飞():
    # 镜像盘是装机时装进机器内部、没人碰的 —— 它本来就该一直在。
    disks = [Removable(mount=Path("/media/mirror"), role=DiskRole.MIRROR)]
    assert blocks_takeoff(disks) == ()


def test_取走盘拦起飞():
    disks = [Removable(mount=Path("/media/u1"), role=DiskRole.TRANSFER)]
    assert len(blocks_takeoff(disks)) == 1


def test_认不出角色的盘按取走盘拦():
    # 一块认不出来的盘,我们唯一知道的事就是它现在插在外面。
    disks = [Removable(mount=Path("/media/u1"), role=DiskRole.UNKNOWN)]
    assert len(blocks_takeoff(disks)) == 1


def test_一块镜像一块取走只拦取走那块():
    disks = [Removable(mount=Path("/media/mirror"), role=DiskRole.MIRROR),
             Removable(mount=Path("/media/u1"), role=DiskRole.TRANSFER)]
    assert [d.mount for d in blocks_takeoff(disks)] == [Path("/media/u1")]


# ------------------------------------------------- 镜像盘要验 SN(spec §7.6)


def _mirror(sn: str = "") -> list[Removable]:
    return [Removable(mount=Path("/media/mirror"), role=DiskRole.MIRROR, sn=sn)]


def test_别的狗的镜像盘拦起飞():
    """SN 记下来就是为了这一条 —— 只看 role 的话,A 狗的备份盘插到 B 狗上
    一路绿灯,而写进去的数据从此串了台。"""
    assert len(blocks_takeoff(_mirror("D1MAX-0002"),
                              robot_sn="D1MAX-0001")) == 1


def test_自己的镜像盘照样不拦():
    assert blocks_takeoff(_mirror("D1MAX-0001"), robot_sn="D1MAX-0001") == ()


def test_盘上没写SN的镜像盘放行():
    """没写 SN 的那块盘不属于任何一只狗 —— 拦它防不住数据串台。"""
    assert blocks_takeoff(_mirror(""), robot_sn="D1MAX-0001") == ()


def test_本机没有身份时镜像盘一律放行():
    """``fingerprint`` 读不出 SN 是合法工况(见 app/identity.py)。把这种狗
    连自己的镜像盘都拦掉,是凭空造出来的一个新故障模式。"""
    assert blocks_takeoff(_mirror("D1MAX-0002"), robot_sn="") == ()


def test_不传robot_sn时跟以前一样只看角色():
    assert blocks_takeoff(_mirror("D1MAX-0002")) == ()


async def test_扫的是挂载点不是目录(tmp_path):
    # tmp_path 下造不出真挂载点,所以"是不是挂载点"这个判据必须可注入 ——
    # 那是这一层能被离机测到的唯一办法。
    root = tmp_path / "media"
    (root / "真盘").mkdir(parents=True)
    (root / "只是个目录").mkdir()
    probe = MountRootProbe(roots=(root,), is_mount=lambda p: Path(p).name == "真盘")
    got = await probe.scan()
    assert [d.mount.name for d in got] == ["真盘"]


async def test_扫的时候顺手把角色读出来(tmp_path):
    root = tmp_path / "media"
    (root / "盘").mkdir(parents=True)
    _mark(root / "盘", "mirror")
    probe = MountRootProbe(roots=(root,), is_mount=lambda p: True)
    got = await probe.scan()
    assert got[0].role is DiskRole.MIRROR
    assert got[0].sn == "D1MAX-0001"


async def test_挂载根目录压根不存在不算错(tmp_path):
    probe = MountRootProbe(roots=(tmp_path / "没有这个目录",))
    assert await probe.scan() == ()


async def test_扫出来的顺序是稳定的(tmp_path):
    # 挂载点的遍历顺序在不同文件系统上不一样。不排序,"认到几块盘"这类断言
    # 就会在某台机器上随机翻车 —— 而那种翻车最难查。
    # 用 ASCII 名字:中文的排序规则不是这条测试要考的。
    root = tmp_path / "media"
    for name in ("c", "a", "b"):
        (root / name).mkdir(parents=True)
    probe = MountRootProbe(roots=(root,), is_mount=lambda p: True)
    got = await probe.scan()
    assert [d.mount.name for d in got] == ["a", "b", "c"]


def test_读不出身份的狗报的是unknown不是空串():
    """``app/identity.py`` 查不到 SN 的时候明写 ``"unknown"``,从不留空 ——
    所以上面那条"本机没有身份就放行"只认空串的话,在真机上是一句死话:
    真正会出现的那个值(``"unknown"``)照样被当成一个正经 SN 去比对。
    """
    from d1max_patrol.app.identity import UNKNOWN_SN

    assert UNKNOWN_SN in NO_IDENTITY
    assert blocks_takeoff(_mirror("D1MAX-0002"), robot_sn=UNKNOWN_SN) == ()


def test_盘上写着unknown的镜像盘也放行():
    """那块盘是一只读不出身份的狗备份出来的,它同样不属于任何一只狗。"""
    assert blocks_takeoff(_mirror("unknown"), robot_sn="D1MAX-0001") == ()


def test_两边都知道且不同还是照拦():
    """放宽的只是"不知道"那一侧 —— 真串台的那条线一步没让。"""
    assert len(blocks_takeoff(_mirror("D1MAX-0002"),
                              robot_sn="D1MAX-0001")) == 1
