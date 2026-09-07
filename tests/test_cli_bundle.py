"""任务包守卫在命令行上的那一条 —— **它是这条修复路径唯一的生产入口。**

``engine/bundle.py`` 的 ``guard_bundle()`` 修的是「apply 连着换两条链,两次
之间断电」留下的局面。评审复评 finding 4 逮到的是:那段代码写好了、测了,
**却没有任何一个地方调它** —— systemd 单元里只挂着版本守卫,而
``docs/任务包格式.md`` 已经对客户写着「开机时照着它把链修回一个能用的样子」。
一条永远不会被执行的修复路径等于没有,写在文档里就是一句假话。

所以这个文件盯的不是 ``guard_bundle`` 的语义(那在
``tests/engine/test_bundle_store.py``),而是**这条命令真的接得上**:
``main(["bundle", "guard"])`` 走得通、退出码对、真的走到了 ``guard_bundle``。
形状是照 ``tests/test_cli_release.py`` 抄的 —— 那边是同一件事的版本目录版。

**这个文件里没有一个 sleep。**
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from d1max_patrol.cli import _bundles_root, main
from d1max_patrol.engine import bundle as bundle_mod
from d1max_patrol.engine.bundle import (
    CURRENT_LINK,
    LANDED,
    MAX_ROLLBACKS,
    PREVIOUS_LINK,
    apply_bundle,
    build_bundle,
    land,
    mark_proven,
    read_state,
)

时刻 = "2026-09-07T14:03:00+08:00"


def 打包并落(tmp_path: Path, root: Path, version: int) -> str:
    """打一个包,落到 ``root``。返回槽名。**只落盘,不换链。**"""
    src = tmp_path / f"src-v{version}"
    (src / "missions").mkdir(parents=True, exist_ok=True)
    (src / "schedule.yaml").write_text(
        f"timezone: Asia/Kuala_Lumpur\nentries: []\n# v{version}\n",
        encoding="utf-8")
    staged = build_bundle(src, tmp_path / f"staging-v{version}",
                          bundle_id="site-kl", version=version, built_at=时刻)
    return land(root, staged).slot_name


def 断电装二(tmp_path, root, monkeypatch):
    """一台装着 v1 的狗,装 v2 装到两次换链之间断了电。返回 ``(一, 二)``。

    跟 ``tests/engine/test_bundle_store.py`` 里那个同名助手一样:§8.5 禁
    sleep,开发机上也没法真断电,所以直接把那个中间态造出来。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    mark_proven(root)
    二 = 打包并落(tmp_path, root, 2)

    真 = bundle_mod.point_link
    次: list[str] = []

    def 假(link, target):
        次.append(link.name)
        if len(次) >= 2:
            raise OSError("断电")
        真(link, target)

    monkeypatch.setattr(bundle_mod, "point_link", 假)
    with pytest.raises(OSError):
        apply_bundle(root, 二)
    monkeypatch.undo()                      # 电来了
    return 一, 二


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "bundles"
    d.mkdir()
    return d


def test_守卫在没有半成品时放行(tmp_path, root, capsys):
    """绝大多数开机走这条。它必须便宜、安静、退 0,而且不能碰链。"""
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    capsys.readouterr()

    assert main(["bundle", "guard", "--root", str(root)]) == 0

    assert read_state(root).current == 一
    out = capsys.readouterr()
    assert 一 in out.out
    assert out.err == ""


def test_守卫真的把换到一半的链换完(tmp_path, root, monkeypatch, capsys):
    """**这一条是 finding 4 的正身。**

    断电留下的是 ``current == previous == v1`` 且 ``proven=False`` —— 第 8 卷
    那条自动回退判据随即成立,把盘上唯一那份好包拉黑,而路由那头它再也装不
    回去。这条命令跑一次,局面就回到一个能用的终态。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    st = read_state(root)
    assert (st.current, st.previous) == (一, 一)      # 自相矛盾的中间态
    assert st.applying is not None
    capsys.readouterr()

    assert main(["bundle", "guard", "--root", str(root)]) == 0

    st = read_state(root)
    assert (st.current, st.previous) == (二, 一)      # 两条链各指各的
    assert st.applying is None
    assert st.denied == ()                           # 谁都没被拉黑
    assert 二 in capsys.readouterr().out


def test_两个方向都走不通时提示落在stderr而且仍然退0(tmp_path, root,
                                                     monkeypatch, capsys):
    """BROKEN 是「请人来看」那一种。

    **退出码仍然是 0。** 单元里那一行前缀虽然有 '-',但守卫自己也不该把
    「机器起不来」当成自己的表达方式 —— 一个把机器挡在启动之外的安全网,
    比它要防的问题更糟。而「请人来看」这句必须落在 stderr,不能被日常开机
    收 stdout 的脚本吞掉。
    """
    一, 二 = 断电装二(tmp_path, root, monkeypatch)
    shutil.rmtree(root / 二)
    shutil.rmtree(root / 一)
    capsys.readouterr()

    assert main(["bundle", "guard", "--root", str(root)]) == 0

    out = capsys.readouterr()
    assert "请人来看" in out.err
    assert "请人来看" not in out.out
    assert read_state(root).applying is not None     # 标记留着,现场要取证


def test_老盘开一次机就在这条路上被归一化(tmp_path, root, capsys):
    """评审复评 finding 6 靠的正是这条开机路 —— 截断只发生在写路径上,
    升级上来的机器不再回退就永远背着几百条历史(实测 489KB /
    ``GET /api/bundle`` 吐 177KB / 3.1s)。**``read_state`` 不改成写**,
    归一化挂在开机这一次。
    """
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    记 = json.loads((root / LANDED).read_text(encoding="utf-8"))
    退过的 = [f"site-kl-{900 + i}" for i in range(200)]
    记["rollbacks"] = [{"at": 时刻, "from": s, "to": 一, "reason": "崩" * 500}
                       for s in 退过的]
    记.pop("denied", None)                   # 老版本压根没有这个键
    (root / LANDED).write_text(json.dumps(记, ensure_ascii=False),
                               encoding="utf-8")
    大 = (root / LANDED).stat().st_size

    assert main(["bundle", "guard", "--root", str(root)]) == 0

    st = read_state(root)
    assert len(st.rollbacks) == MAX_ROLLBACKS
    assert set(退过的) <= set(st.denied)      # 一条拉黑事实都没丢
    assert (root / LANDED).stat().st_size < 大 // 3


def test_根路径也能从环境变量来(tmp_path, root, monkeypatch, capsys):
    """systemd 单元里写环境变量比写一长串参数干净 —— 跟 release 那条一样。"""
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    monkeypatch.setenv("D1MAX_BUNDLES_ROOT", str(root))
    capsys.readouterr()

    assert main(["bundle", "guard"]) == 0
    assert 一 in capsys.readouterr().out


def test_显式root赢过环境变量(tmp_path, monkeypatch):
    monkeypatch.setenv("D1MAX_BUNDLES_ROOT", "/环境变量/说的")
    assert _bundles_root("/命令行/说的") == Path("/命令行/说的")


def test_bundles_root默认跟着版本根走(monkeypatch):
    """**默认值必须跟 ``app/server.py`` 的 ``AppContext.bundles_root`` 对得上。**

    两边各说各话的话,守卫在一个根下修链、服务读的是另一个根 —— 那比守卫
    根本没挂上还糟:它每次开机都会报「链是好的」。
    这里只调纯函数,不经过 ``main()``,绝不能真的去碰 /opt/d1max。
    """
    monkeypatch.delenv("D1MAX_BUNDLES_ROOT", raising=False)
    monkeypatch.delenv("D1MAX_RELEASE_ROOT", raising=False)
    assert _bundles_root(None) == Path("/opt/d1max/bundles")


def test_根目录根本不存在也不炸(tmp_path, capsys):
    """装机第一次起服务时 ``/opt/d1max/bundles`` 可能还不在。
    守卫在那儿抛异常的话,ExecStartPre 那行的 '-' 之外还得再兜一层。
    """
    assert main(["bundle", "guard", "--root", str(tmp_path / "没有这个目录")]) == 0
    assert "(没有)" in capsys.readouterr().out


def test_链是悬空的也不炸(tmp_path, root, capsys):
    """``current`` 指着一个被删掉的目录 —— 正是守卫本该兜底的那种坏法。"""
    一 = 打包并落(tmp_path, root, 1)
    apply_bundle(root, 一)
    shutil.rmtree(root / 一)
    assert (root / CURRENT_LINK).is_symlink()
    assert not (root / PREVIOUS_LINK).exists()

    assert main(["bundle", "guard", "--root", str(root)]) == 0
