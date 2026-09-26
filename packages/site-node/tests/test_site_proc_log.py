"""建图进程日志(W00c6g):站点接口 → ``proc_log`` 命令,狗回执里的数据原样给管理员。站点不存日志。"""

from __future__ import annotations

import json

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


def _假狗(s, monkeypatch, reply):
    sent = []

    async def 回(robot_id, kind, payload, *, issued_by):
        sent.append((robot_id, kind, payload, issued_by))
        return {"ack": reply}
    monkeypatch.setattr(s.disp, "map_command", 回)
    return sent


def test_管理员列日志_取尾巴_保安不行(站点, monkeypatch):
    s = 站点
    alice, gina = _登(s, "alice"), _登(s, "gina")
    sent = _假狗(s, monkeypatch, {"result": "accepted", "reason": "",
                                "data": {"logs": [{"name": "bagrecord", "size": 3,
                                                   "mtime_ms": 1}]}})
    assert s.req("GET", "/api/robots/A/logs", token=gina)[0] == 403
    code, d = s.req("GET", "/api/robots/A/logs", token=alice)
    assert code == 200 and d["logs"][0]["name"] == "bagrecord", d
    assert sent[-1][1:3] == ("proc_log", {}) and sent[-1][3] == "alice"
    sent = _假狗(s, monkeypatch, {"result": "accepted", "reason": "",
                                "data": {"name": "bagrecord", "size": 3, "bytes": 3,
                                         "truncated": False, "text": "ok\n"}})
    code, d = s.req("GET", "/api/robots/A/logs/bagrecord?bytes=2000", token=alice)
    assert code == 200 and d["text"] == "ok\n", d
    assert sent[-1][2] == {"name": "bagrecord", "bytes": 2000}


def test_名字不像话_400_字节数不像话_400(站点):
    s = 站点
    alice = _登(s, "alice")
    assert s.req("GET", "/api/robots/A/logs/a%20b", token=alice)[0] == 400
    assert s.req("GET", "/api/robots/A/logs/a?bytes=x", token=alice)[0] == 400
    assert s.req("GET", "/api/robots/A/logs/a?bytes=0", token=alice)[0] == 400
    assert s.req("GET", "/api/robots/A/logs/a?bytes=-5", token=alice)[0] == 400


def test_狗说没有这个日志_404_别的拒_409(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _假狗(s, monkeypatch, {"result": "rejected", "reason": "no_such_log"})
    assert s.req("GET", "/api/robots/A/logs/nope", token=alice)[0] == 404
    _假狗(s, monkeypatch, {"result": "rejected", "reason": "payload: x"})
    assert s.req("GET", "/api/robots/A/logs/nope", token=alice)[0] == 409


def test_真狗没有录包能力_409(站点):
    s = 站点
    alice = _登(s, "alice")
    _等(lambda: s.disp.clients["A"].capabilities is not None)
    code, d = s.req("GET", "/api/robots/A/logs", token=alice)
    assert code == 409 and "proc_log" in d["error"], d


class 假录包:
    def __init__(self, log_dir):
        self.recording = False
        self.last_bag = ""
        self.log_dir = log_dir


def test_真狗_日志经站点给管理员_事件流里不推(tmp_path):
    """站点的事件流(SSE)谁登录了都收得到(保安、业主也是):查日志的回执**不推** —— 日志只给管理员,
    手机收到事件流的每一帧都会刷新狗的列表。"""
    logs = tmp_path / "dog-logs"
    logs.mkdir()
    (logs / "slam.log").write_text("启动参数 --secret-param=abc\n结束\n")
    s = 站(tmp_path, maps={"mapper": 假录包(logs)})
    try:
        alice = _登(s, "alice")
        _等(lambda: s.disp.clients["A"].capabilities is not None
            and "proc_log" in s.disp.clients["A"].capabilities.tasks)
        sub = s.disp.feed.subscribe()
        code, d = s.req("GET", "/api/robots/A/logs", token=alice)
        assert code == 200 and [x["name"] for x in d["logs"]] == ["slam"], d
        code, d = s.req("GET", "/api/robots/A/logs/slam?bytes=4096", token=alice)
        assert code == 200 and d["text"].startswith("启动参数") and d["truncated"] is False, d
        frames = []
        for _ in range(50):                            # 有界:最多等 50 × 0.1 s
            f = sub.get(timeout=0.1)
            if f is None:
                break
            frames.append(f)
        assert not [f for f in frames if f.get("kind") == "ack"], frames
        assert all("secret-param" not in json.dumps(f, ensure_ascii=False) for f in frames)
        code, d = s.req("POST", "/api/robots/A/abort", {"task_id": "nope"}, token=alice)
        assert s.disp.feed is not None and any(
            (f or {}).get("kind") == "ack" for f in [sub.get(timeout=2.0)]), "别的回执照推"
        assert s.req("GET", "/api/robots/A/logs/nope", token=alice)[0] == 404
        # 内审小问题 3:查询不记进命令账 —— 单狗视图里保安、业主看得到最近 50 条命令(日志名、
        # 狗上的路径),
        # 管理员一刷新还会把正经命令挤出去。
        assert not s.db.query("SELECT 1 FROM commands WHERE kind='proc_log'")
        gina_view = s.req("GET", "/api/robots/A", token=alice)[1]
        assert not [x for x in gina_view["commands"] if x["kind"] == "proc_log"]
    finally:
        s.close()


def test_狗收下了却没带数据_502(站点, monkeypatch):
    s = 站点
    alice = _登(s, "alice")
    _假狗(s, monkeypatch, {"result": "accepted", "reason": ""})
    assert s.req("GET", "/api/robots/A/logs", token=alice)[0] == 502
