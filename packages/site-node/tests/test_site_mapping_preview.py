"""建图预览(W09f):站点接口 → ``mapping_preview`` 命令,狗回执里的快照原样给管理员;手机边走边建时
每 3 s 查一次,所以这条回执不推事件流、不记命令账(跟录包轨迹一样)。"""

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


def test_管理员取预览_since进命令_保安不行_since不像话400(站点, monkeypatch):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    sent = []

    async def 回(robot_id, kind, payload, *, issued_by):
        sent.append((kind, payload))
        return {"ack": {"result": "accepted", "reason": "",
                        "data": {"live": True, "seq": 7, "png": "iVBORw0KGgo=", "res": 0.1}}}
    monkeypatch.setattr(s.disp, "map_command", 回)
    assert s.req("GET", "/api/robots/A/mapping/preview", token=gina)[0] == 403
    code, d = s.req("GET", "/api/robots/A/mapping/preview?since=6", token=alice)
    assert code == 200 and d == {"live": True, "seq": 7, "png": "iVBORw0KGgo=", "res": 0.1}, d
    assert sent[-1] == ("mapping_preview", {"since": 6})
    s.req("GET", "/api/robots/A/mapping/preview", token=alice)
    assert sent[-1] == ("mapping_preview", {"since": 0})
    for bad in ("x", "-1", "1.5"):
        assert s.req("GET", f"/api/robots/A/mapping/preview?since={bad}", token=alice)[0] == 400
    assert s.req("GET", "/api/robots/A/mapping/nope", token=alice)[0] == 404


def test_狗拒了409_老代理409_回执里没数据502(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: s.disp.clients["A"].capabilities is not None)
    code, d = s.req("GET", "/api/robots/A/mapping/preview", token=alice)
    assert code == 409 and "mapping_preview" in d["error"] and d["unsupported"] is True, d

    async def 老狗(*a, **k):
        return {"ack": {"result": "rejected", "reason": "unsupported"}}
    monkeypatch.setattr(s.disp, "map_command", 老狗)
    code, d = s.req("GET", "/api/robots/A/mapping/preview", token=alice)
    assert code == 409 and d["unsupported"] is True

    async def 拒(*a, **k):
        return {"ack": {"result": "rejected", "reason": "read_failed: 盘坏了"}}
    monkeypatch.setattr(s.disp, "map_command", 拒)
    code, d = s.req("GET", "/api/robots/A/mapping/preview", token=alice)
    assert code == 409 and "read_failed" in d["error"] and "unsupported" not in d

    async def 空(*a, **k):
        return {"ack": {"result": "accepted", "reason": ""}}
    monkeypatch.setattr(s.disp, "map_command", 空)
    assert s.req("GET", "/api/robots/A/mapping/preview", token=alice)[0] == 502


class 假录包:
    def __init__(self):
        self.recording = True
        self.last_bag = ""

    def preview(self, since):
        return {"live": True, "seq": 2, "recording": True, "png": "QQ=="}


def test_真狗_取得到预览_事件流里不推_不记命令账(tmp_path):
    s = 站(tmp_path, maps={"mapper": 假录包()})
    try:
        alice = _登(s, "alice")
        _等(lambda: s.disp.clients["A"].capabilities is not None
            and "mapping_preview" in s.disp.clients["A"].capabilities.tasks)
        sub = s.disp.feed.subscribe()
        code, d = s.req("GET", "/api/robots/A/mapping/preview", token=alice)
        assert code == 200 and d["seq"] == 2 and d["png"] == "QQ==", d
        frames = []
        for _ in range(20):                            # 有界
            f = sub.get(timeout=0.1)
            if f is None:
                break
            frames.append(f)
        assert not [f for f in frames if f.get("kind") == "ack"], frames
        assert not s.db.query("SELECT 1 FROM commands WHERE kind='mapping_preview'")
    finally:
        s.close()



def test_狗不在线_409_不说不支持(站点):
    """内审应修 1:不在线也是 409,手机不能当成老狗退回轨迹。"""
    s = 站点
    alice = _登(s, "alice")
    code, d = s.req("GET", "/api/robots/NOPE/mapping/preview", token=alice)
    assert code in (404, 409) and not d.get("unsupported"), d
