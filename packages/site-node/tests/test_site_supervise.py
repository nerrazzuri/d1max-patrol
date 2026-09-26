"""过渡期的监护租约,站点这一侧(W00c6i)。

手机上「我在现场监护」开着时每 1 s 心跳一次(``POST /api/robots/<id>/supervise``),
站点转成一条有效期很短的 ``supervise`` 命令;``supervised`` 的狗没人监护就不收 goto/巡检。
排程与事件派遣只派给 ``autonomous`` 的狗。
"""

from __future__ import annotations

import pytest
from test_site_api import PW, _等, target, 站


@pytest.fixture
def 监护站(tmp_path):
    s = 站(tmp_path, autonomy="supervised")
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


_seq = {"n": 0}


def _心跳(s, tok, action="renew", session="p-gina"):
    _seq["n"] += 1
    return s.req("POST", "/api/robots/A/supervise",
                 {"action": action, "session": session, "seq": _seq["n"]}, token=tok)


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _新鲜(s, tok):
    _等(lambda: s.req("GET", "/api/robots/A", token=tok)[1].get("fresh"))
    _等(lambda: s.disp.clients["A"].capabilities is not None
        and "goto" in s.disp.clients["A"].capabilities.tasks)


def test_狗在能力里报要人监护(监护站):
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    assert s.disp.clients["A"].capabilities.tasks["goto"]["autonomy"] == "supervised"


def test_没人监护派goto_站点先挡_说清楚为什么(监护站):
    """站点这一道关(W00c6i 内审):回滚到旧版代理时狗自己不查,站点照样挡。"""
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert code == 409 and "监护" in d["error"], d
    code, d = s.req("POST", "/api/robots/A/standby/return", {}, token=tok)
    assert code in (404, 409), d


def test_旧版代理不报级别_站点照样按要人监护挡(监护站):
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    for task in ("goto", "patrol"):
        s.disp.clients["A"].capabilities.tasks[task].pop("autonomy", None)
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert code == 409, d


def test_保安开监护_心跳转到狗上_之后goto收下_放了又不收(监护站):
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    code, d = _心跳(s, tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert d["ack"]["result"] == "accepted", d
    code, d = _心跳(s, tok, "release")
    assert code == 200 and d["ack"]["result"] == "accepted", d
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert code == 409, d


def test_业主不能开监护_动作不对400(监护站):
    s = 监护站
    olga = _登(s, "olga")
    code, _ = _心跳(s, olga)
    assert code == 403
    gina = _登(s, "gina")
    for bad in ({"action": "forever", "session": "x", "seq": 1}, {"action": "renew", "seq": 1},
                {"action": "renew", "session": "x", "seq": 0}):
        code, _ = s.req("POST", "/api/robots/A/supervise", bad, token=gina)
        assert code == 400, bad
    code, _ = s.req("POST", "/api/robots/ghost/supervise",
                    {"action": "renew", "session": "x", "seq": 1}, token=gina)
    assert code in (404, 409)


def test_心跳不刷审计_只记开始和结束(监护站):
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    for _ in range(5):
        _心跳(s, tok)
    _心跳(s, tok, "release")
    rows = [r for r in s.api.audit.list() if "/supervise" in r["action"]]
    assert len(rows) == 2, rows


def test_两个人交替续_审计各记一次开始(监护站):
    s = 监护站
    s.accounts.add("bob", PW, role="guard")
    gina, bob = _登(s, "gina"), _登(s, "bob")
    _新鲜(s, gina)
    for _ in range(3):
        _心跳(s, gina, session="p-gina")
        _心跳(s, bob, session="p-bob")
    rows = [r for r in s.api.audit.list() if "/supervise" in r["action"]]
    assert sorted(r["actor"] for r in rows) == ["bob", "gina"], rows


def test_心跳命令有效期是30秒_租约3秒(监护站):
    """命令有效期按狗的墙钟判:给 30 s(同遥控续租),不然狗钟快几秒监护就不能用(内审)。"""
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    c = s.disp.clients["A"]
    seen = []
    real = c.new_command

    def 记(kind, payload, **k):
        if kind == "supervise":
            seen.append((k.get("ttl_ms"), payload.get("ttl_ms")))
        return real(kind, payload, **k)
    c.new_command = 记
    try:
        _心跳(s, tok)
    finally:
        c.new_command = real
    assert seen == [(30_000, 3_000)]


def test_过期的会话不留在站点的内存里():
    """外审(Codex)建议修 2:会话只在手机正常放开时删;手机崩了、断网了,过期的会话一直留着,
    长期运行、会话越开越多就慢慢涨。续约、查谁在监护的时候顺手清掉过期的。"""
    from d1max_site.supervision import SupervisionDesk
    t = [0]
    desk = SupervisionDesk(dispatcher=None, loop=None, now_ms=lambda: t[0], ttl_ms=3000)
    desk._send = lambda robot_id, sup: {"result": "accepted", "reason": ""}
    for i in range(500):
        desk.renew("A", "gina", f"p-{i}", 1)
    t[0] = 10_000
    assert desk.active("A") is None
    desk.renew("A", "gina", "p-new", 1)
    assert list(desk._active["A"]) == ["p-new"], "过期的都清了"
    assert desk.active("A") == "gina"
    t[0] = 20_000
    assert desk.active("A") is None and not desk._active.get("A")
