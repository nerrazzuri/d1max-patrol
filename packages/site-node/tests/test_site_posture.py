"""站立、趴下、蹲下(POC 第 1 项):网页 / 手机 → 站点 → 代理 → 仿真狗。真 HTTP、真代理。
保安能按(跟遥控同一个权限),业主不能;不认识的姿势 400;有任务在跑 409、原因原样带回;
狗没报这种姿势 409 并标 unsupported;进审计。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站

from nexwar_contract.hal import MotionStatus


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _新鲜(s, tok):
    _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    _等(lambda: "posture" in ((s.req("GET", "/api/robots/A", token=tok)[1].get("capabilities")
                              or {}).get("tasks") or {}))


def _姿态(s):
    return s.loop.call(lambda: s.dog.motion_status())


def test_保安按趴下_狗趴了_再站起_进审计_业主不能(站点):
    s = 站点
    gina, olga = _登(s, "gina"), _登(s, "olga")
    _新鲜(s, gina)
    assert s.req("POST", "/api/robots/A/posture", {"mode": "lie"}, token=olga)[0] == 403
    code, d = s.req("POST", "/api/robots/A/posture", {"mode": "lie"}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted" and d["mode"] == "lie", d
    assert _姿态(s) is MotionStatus.LYING
    code, d = s.req("POST", "/api/robots/A/posture", {"mode": "stand"}, token=gina)
    assert code == 200, d
    assert _姿态(s) is MotionStatus.READY
    rows = [r for r in s.api.audit.list() if r["action"] == "POST /api/robots/A/posture"]
    assert [r["detail"]["mode"] for r in rows if r["detail"].get("mode")] == ["stand", "lie"]
    assert len(rows) == 3, "业主那次被拒也记了"


def test_不认识的姿势_400_有任务在跑_409带原因(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    for bad in ({"mode": "sit"}, {}, {"mode": 1}):
        assert s.req("POST", "/api/robots/A/posture", bad, token=gina)[0] == 400, bad
    target = {"map_id": "estate-1", "map_version": "7", "frame_id": "map", "x": 30.0, "y": 0.0,
              "yaw": 0.0}
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target}, token=gina)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    code, d = s.req("POST", "/api/robots/A/posture", {"mode": "lie"}, token=gina)
    assert code == 409 and "正在执行任务" in d["error"], d
    assert _姿态(s) is MotionStatus.READY


def test_狗没报这种姿势_409_标不支持(站点):
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)
    c = s.disp.clients["A"]
    tasks = dict(c.capabilities.tasks)
    tasks["posture"] = {"modes": ["stand", "lie"]}
    c.capabilities = type(c.capabilities)(**{**c.capabilities.__dict__, "tasks": tasks})
    code, d = s.req("POST", "/api/robots/A/posture", {"mode": "crawl"}, token=gina)
    assert code == 409 and d.get("unsupported") is True, d


def test_概览卡片带能做哪几种姿势_和正在跑的任务(站点):
    """网页、手机按它显示三个按钮、有任务在跑时按钮灰掉写原因。"""
    s = 站点
    gina = _登(s, "gina")
    _新鲜(s, gina)

    alice = _登(s, "alice")

    def card():
        return [r for r in s.req("GET", "/api/robots/overview", token=alice)[1]["robots"]
                if r["robot_id"] == "A"][0]
    assert card()["postures"] == ["stand", "lie", "crawl"] and card()["task"] is None
    target = {"map_id": "estate-1", "map_version": "7", "frame_id": "map", "x": 30.0, "y": 0.0,
              "yaw": 0.0}
    tid = s.req("POST", "/api/robots/A/goto", {"target": target}, token=gina)[1]["task_id"]
    _等(lambda: card()["task"] == tid)
