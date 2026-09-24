"""原点：换电位、待命位、返航目标。存得下、读得回、重建图会作废。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from d1max_agent.engine.homing import (
    DEFAULT_RETURN_PARAMS,
    HomeError,
    HomePoint,
    ReturnParams,
    estimate_cost_pct,
    forget_home,
    home_path,
    load_home,
    route_length_m,
    save_home,
)
from d1max_patrol.protocol.nav_types import Pose


def _home(map_id: str = "一号厂房") -> HomePoint:
    return HomePoint(map_id=map_id, pose=Pose.from_xy_yaw(3.5, -1.25, 1.57),
                     marked_at_ms=1_757_000_000_000, note="换电位,靠西墙")


def test_原点存下去再读回来还是同一个点(tmp_path):
    save_home(tmp_path, _home())
    got = load_home(tmp_path, "一号厂房")
    assert got.map_id == "一号厂房"
    assert got.pose.position.x == pytest.approx(3.5)
    assert got.pose.position.y == pytest.approx(-1.25)
    assert got.pose.yaw == pytest.approx(1.57, abs=1e-6)
    assert got.marked_at_ms == 1_757_000_000_000
    assert got.note == "换电位,靠西墙"


def test_没标过原点要抛错而不是回一个零点(tmp_path):
    # (0, 0) 在地图里是一个真实存在的点。拿它当"没标过"的返回值,等于把狗
    # 派往一个谁也没标过的地方,而调用方看不出区别。
    with pytest.raises(HomeError) as e:
        load_home(tmp_path, "一号厂房")
    assert "没标过" in str(e.value)


def test_原点文件坏了要抛错不能当成没标过(tmp_path):
    # 坏了和没标过是两回事:没标过是"去标一个",坏了是"盘上有东西不对"。
    home_path(tmp_path, "一号厂房").write_text("{不是 json", encoding="utf-8")
    with pytest.raises(HomeError) as e:
        load_home(tmp_path, "一号厂房")
    assert "读不出来" in str(e.value)


def test_原点里的地图名跟要读的对不上要抛错(tmp_path):
    save_home(tmp_path, _home("一号厂房"))
    path = home_path(tmp_path, "一号厂房")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["map_id"] = "二号厂房"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(HomeError):
        load_home(tmp_path, "一号厂房")


def test_重建图要把旧原点作废(tmp_path):
    # 原点是标在坐标系上的。坐标系重建了,它就是错的 —— 而一个错的原点比
    # 没有原点危险得多:狗不会拒绝起飞,它会一声不吭地走过去。
    save_home(tmp_path, _home())
    forget_home(tmp_path, "一号厂房")
    with pytest.raises(HomeError):
        load_home(tmp_path, "一号厂房")


def test_作废一个本来就没有的原点不算错(tmp_path):
    forget_home(tmp_path, "从来没有过的图")


def test_写原点是原子的_写坏了不会毁掉已有的那个(tmp_path):
    save_home(tmp_path, _home())
    before = home_path(tmp_path, "一号厂房").read_bytes()
    bad = HomePoint(map_id="一号厂房", pose=Pose.from_xy_yaw(1.0, 2.0),
                    marked_at_ms=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        save_home(tmp_path, bad)
    assert home_path(tmp_path, "一号厂房").read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_原点文件就放在地图旁边(tmp_path):
    assert home_path(tmp_path, "一号厂房") == tmp_path / "一号厂房.home.json"


def test_原点是先落盘再改名的(tmp_path, monkeypatch):
    """``replace`` 一个人兑现不了"原子写"这句承诺。

    ``write_text`` 返回时数据只到页缓存;rename 保证的是改名不早于写入落盘,
    不保证两者都落了盘。这台机器的日常工况就是热插拔换电池 —— 断电不是
    意外分支,是操作流程本身。顺序错了(先改名后 fsync)照样可能留下半个
    JSON,所以这里查的不只是"调过 fsync",还有它在 replace 之前。
    """
    发生了: list[str] = []
    真的 = os.fsync
    真的replace = Path.replace

    def 记一笔fsync(fd):
        发生了.append("fsync")
        return 真的(fd)

    def 记一笔replace(self, target):
        发生了.append("replace")
        return 真的replace(self, target)

    monkeypatch.setattr(os, "fsync", 记一笔fsync)
    monkeypatch.setattr(Path, "replace", 记一笔replace)
    save_home(tmp_path, _home())
    assert 发生了 == ["fsync", "replace"]


# ---------------------------------------------------------------- 距离换电量


def test_原点就在脚下也要留一点电():
    # 返航成本能取 0 的话,返航线就等于中止线,而中止先判 —— 返航永远轮不到。
    assert estimate_cost_pct(0.0) == pytest.approx(DEFAULT_RETURN_PARAMS.floor_pct)


def test_越远要的电越多():
    assert estimate_cost_pct(200.0) > estimate_cost_pct(50.0)


def test_按默认系数算一百米要多少电():
    # 100m × 1.4 绕路 = 140m;140 / 0.4 = 350s = 0.09722h;× 22.3 = 2.168%
    # 比下限 3.0 还小,所以取下限。这个例子本身就是"下限在起作用"的证据。
    assert estimate_cost_pct(100.0) == pytest.approx(3.0)


def test_按默认系数算五百米要多少电():
    # 500 × 1.4 = 700m;700 / 0.4 = 1750s = 0.48611h;× 22.3 = 10.84%
    assert estimate_cost_pct(500.0) == pytest.approx(10.840, abs=0.01)


def test_系数可以整组换掉():
    slow = ReturnParams(cruise_speed_mps=0.2, drain_pct_per_hour=22.3,
                        detour_factor=1.4, floor_pct=3.0)
    assert estimate_cost_pct(500.0, slow) > estimate_cost_pct(500.0)


def test_负距离是调用方的错要当场抛():
    with pytest.raises(ValueError):
        estimate_cost_pct(-1.0)


@pytest.mark.parametrize("bad", [
    ReturnParams(cruise_speed_mps=0.0),
    ReturnParams(cruise_speed_mps=-0.4),
    ReturnParams(drain_pct_per_hour=0.0),
    ReturnParams(detour_factor=0.9),
    ReturnParams(floor_pct=-1.0),
])
def test_系数不合法要当场抛而不是算出个荒唐的数(bad):
    with pytest.raises(ValueError):
        estimate_cost_pct(10.0, bad)


def test_全程是原点出发绕一圈再回来():
    home = Pose.from_xy_yaw(0.0, 0.0)
    wps = [Pose.from_xy_yaw(3.0, 0.0), Pose.from_xy_yaw(3.0, 4.0)]
    # 0->3 = 3;(3,0)->(3,4) = 4;(3,4)->0 = 5。合计 12。
    assert route_length_m(home, wps) == pytest.approx(12.0)


def test_没有点位的全程是零():
    assert route_length_m(Pose.from_xy_yaw(1.0, 1.0), []) == pytest.approx(0.0)


def test_只有一个点位也要算上回来那一段():
    # 一个点位的全程是"去了再回来",不是"走过去就完了"。这条盯着的是日后
    # 有人给最后一个点位加"到了就停"的特例 —— 那会让出发线少算一半的电。
    home = Pose.from_xy_yaw(0.0, 0.0)
    wp = Pose.from_xy_yaw(3.0, 4.0)
    assert route_length_m(home, [wp]) == pytest.approx(10.0)
