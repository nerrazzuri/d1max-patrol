"""录包时的轨迹(W00c6h):站点接口 → ``mapping_trail`` 命令,狗回执里的点原样给管理员;手机录包期间
每 2 s 查一次,所以这条回执不推事件流(不然所有手机每 2 s 刷一次狗的列表)。"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, 站


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path)
    s.accounts.add("gina", PW, role="guard")
    yield s
    s.close()


def _登(s, name):
    return s.req("POST", "/api/login", {"name": name, "password": PW})[1]["token"]


def test_管理员取轨迹_since进命令_保安不行_since不像话400(站点, monkeypatch):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    sent = []

    async def 回(robot_id, kind, payload, *, issued_by):
        sent.append((kind, payload))
        return {"ack": {"result": "accepted", "reason": "",
                        "data": {"points": [[0.3, 0.0]], "since": 4, "total": 5, "full": False,
                                 "recording": True}}}
    monkeypatch.setattr(s.disp, "map_command", 回)
    assert s.req("GET", "/api/robots/A/mapping/trail", token=gina)[0] == 403
    code, d = s.req("GET", "/api/robots/A/mapping/trail?since=4", token=alice)
    assert code == 200 and d["points"] == [[0.3, 0.0]] and d["recording"] is True, d
    assert sent[-1] == ("mapping_trail", {"since": 4})
    s.req("GET", "/api/robots/A/mapping/trail", token=alice)
    assert sent[-1] == ("mapping_trail", {"since": 0})
    for bad in ("x", "-1", "1.5"):
        assert s.req("GET", f"/api/robots/A/mapping/trail?since={bad}", token=alice)[0] == 400


def test_狗拒了409_老代理409(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: s.disp.clients["A"].capabilities is not None)
    code, d = s.req("GET", "/api/robots/A/mapping/trail", token=alice)
    assert code == 409 and "mapping_trail" in d["error"], d

    async def 拒(*a, **k):
        return {"ack": {"result": "rejected", "reason": "payload: since"}}
    monkeypatch.setattr(s.disp, "map_command", 拒)
    assert s.req("GET", "/api/robots/A/mapping/trail", token=alice)[0] == 409


class 假录包:
    def __init__(self):
        self.recording = True
        self.last_bag = ""


def test_真狗_录着包取得到轨迹_事件流里不推(tmp_path):
    s = 站(tmp_path, maps={"mapper": 假录包()})
    try:
        alice = _登(s, "alice")
        _等(lambda: s.disp.clients["A"].capabilities is not None
            and "mapping_trail" in s.disp.clients["A"].capabilities.tasks)
        sub = s.disp.feed.subscribe()
        code, d = _等(lambda: (lambda r: r if r[1].get("total") else None)(
            s.req("GET", "/api/robots/A/mapping/trail", token=alice)))
        assert code == 200 and d["recording"] is True and d["points"][0] == [0.0, 0.0], d
        frames = []
        for _ in range(20):                            # 有界
            f = sub.get(timeout=0.1)
            if f is None:
                break
            frames.append(f)
        assert not [f for f in frames if f.get("kind") == "ack"], frames
    finally:
        s.close()
