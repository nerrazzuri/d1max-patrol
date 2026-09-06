"""原点：换电位、待命位、返航目标。存得下、读得回、重建图会作废。"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.engine.homing import (
    HomeError,
    HomePoint,
    forget_home,
    home_path,
    load_home,
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
