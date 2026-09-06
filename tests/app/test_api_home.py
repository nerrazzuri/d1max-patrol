"""在图上标一个原点的那两个端点。

**这两个端点存在的全部理由是最后那条测试**:起飞门槛把原点当硬前置,
标不了原点的话,这台狗一趟都跑不起来。前面几条测的是这条写入路径本身
不会写出一个假的原点(缺字段不按 0 补、图名对不上不混用)。

这里起真服务、打真 HTTP,理由同 ``tests/app/conftest.py``。
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from d1max_patrol.engine.homing import home_path, load_home
from tests.app import conftest as C

_MISSION = {
    "mission": "巡检一号",
    "map_id": "map_test",
    "waypoints": [{
        "name": "P1_transformer",
        "pose": {"position": {"x": 1.0, "y": 0.0, "z": 0.0},
                 "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
        "check": "配电柜门是否关闭",
        "actions": [{"type": "dwell", "seconds": 0.1}],
    }],
}


def _put(server, path: str, payload):
    code, body, _ = C.request(server, quote(path, safe="/.%"), method="PUT",
                              payload=payload)
    return code, json.loads(body)


def _post(server, path: str, payload=None):
    code, body, _ = C.request(server, quote(path, safe="/.%"), method="POST",
                              payload=payload or {})
    return code, json.loads(body)


@pytest.fixture
def 没标过原点(ctx):
    """把 conftest 顺手标好的那个原点撤掉。

    ``make_ctx`` 默认给 ``map_test`` 标了一个,好让别处的接口测试不必每条
    都自己标一次;这个文件测的正是"标"这个动作,得从没标过开始。
    """
    home_path(ctx.mapping.maps_dir, "map_test").unlink(missing_ok=True)
    return ctx


def test_标一个原点再读回来是同一个(server, 没标过原点):
    code, body = _put(server, "/api/maps/map_test/home",
                      {"x": 1.5, "y": -2.5, "yaw": 0.0, "note": "换电位"})
    assert code == 200
    assert body["map_id"] == "map_test"
    assert body["pose"]["position"]["x"] == pytest.approx(1.5)
    assert body["note"] == "换电位"

    got = C.get_json(server, "/api/maps/map_test/home")
    assert got == body


def test_标下去的那个点真的落到了盘上(server, 没标过原点):
    """起飞门槛读的是盘上那份,不是内存里的 —— 没落盘等于没标。"""
    assert _put(server, "/api/maps/map_test/home",
                {"x": 3.0, "y": 4.0, "yaw": 0.0})[0] == 200
    home = load_home(没标过原点.mapping.maps_dir, "map_test")
    assert home.pose.position.y == pytest.approx(4.0)
    assert home.marked_at_ms > 0, "标的时刻要记下来"


def test_没标过原点的图读回来是404(server, 没标过原点):
    err = C.get_err(server, "/api/maps/map_test/home", 404)
    assert "原点" in json.dumps(err, ensure_ascii=False)


def test_原点少了字段就拒掉而不是按零补(server, 没标过原点):
    """(0, 0) 在地图里是一个真实存在的点。补出来的原点跟人标的看不出区别。"""
    code, body = _put(server, "/api/maps/map_test/home", {"x": 1.0})
    assert code == 400
    assert "y" in json.dumps(body, ensure_ascii=False)
    assert not home_path(没标过原点.mapping.maps_dir, "map_test").exists()


@pytest.mark.parametrize("bad", [
    {"x": "一点五", "y": 0.0, "yaw": 0.0},
    {"x": True, "y": 0.0, "yaw": 0.0},
    {"x": 0.0, "y": 0.0, "yaw": 0.0, "note": 42},
])
def test_字段类型不对就给400(server, 没标过原点, bad):
    assert _put(server, "/api/maps/map_test/home", bad)[0] == 400
    assert not home_path(没标过原点.mapping.maps_dir, "map_test").exists()


def test_请求体不是对象也给400(server, 没标过原点):
    code, _, _ = C.request(server, "/api/maps/map_test/home", method="PUT",
                           raw=b"[1, 2, 3]")
    assert code == 400


def test_图名不合法根本进不来(server, 没标过原点):
    code, _, _ = C.request(server, "/api/maps/..%2Fetc/home", method="PUT",
                           payload={"x": 0.0, "y": 0.0, "yaw": 0.0})
    assert code in (400, 404)


def test_标完之后起飞门槛的原点那一项就过了(server, 没标过原点):
    """**这一条是这两个端点存在的全部理由。**

    没有写入路径的话,``load_home`` 必然抛 ``HomeError`` → home 和 battery
    两项一起挂 → 起任务恒定 409,这一卷的门槛就成了一道谁也过不去的墙。
    """
    assert _put(server, "/api/missions/巡检一号", _MISSION)[0] == 200

    code, blocked = _post(server, "/api/missions/巡检一号/run")
    assert code == 409, "没标过原点就该拦住"
    assert not next(c for c in blocked["checks"] if c["name"] == "home")["ok"]

    assert _put(server, "/api/maps/map_test/home",
                {"x": 0.0, "y": 0.0, "yaw": 0.0})[0] == 200

    code, body = _post(server, "/api/missions/巡检一号/run")
    assert code == 200, f"标完了还起不来: {body!r}"
    assert next(c for c in body["checks"] if c["name"] == "home")["ok"]
