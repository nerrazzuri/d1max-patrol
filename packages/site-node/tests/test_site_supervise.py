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


def test_没人监护派goto_狗不收_说清楚为什么(监护站):
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert d["ack"]["result"] != "accepted" and d["ack"]["reason"] == "unsupervised", d


def test_保安开监护_心跳转到狗上_之后goto收下_放了又不收(监护站):
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    code, d = s.req("POST", "/api/robots/A/supervise", {"action": "renew"}, token=tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert d["ack"]["result"] == "accepted", d
    code, d = s.req("POST", "/api/robots/A/supervise", {"action": "release"}, token=tok)
    assert code == 200 and d["ack"]["result"] == "accepted", d
    code, d = s.req("POST", "/api/robots/A/goto", {"target": target(0.5)}, token=tok)
    assert d["ack"]["reason"] == "unsupervised", d


def test_业主不能开监护_动作不对400(监护站):
    s = 监护站
    olga = _登(s, "olga")
    code, _ = s.req("POST", "/api/robots/A/supervise", {"action": "renew"}, token=olga)
    assert code == 403
    gina = _登(s, "gina")
    code, _ = s.req("POST", "/api/robots/A/supervise", {"action": "forever"}, token=gina)
    assert code == 400
    code, _ = s.req("POST", "/api/robots/ghost/supervise", {"action": "renew"}, token=gina)
    assert code in (404, 409)


def test_心跳不刷审计_只记开始和结束(监护站):
    s = 监护站
    tok = _登(s, "gina")
    _新鲜(s, tok)
    for _ in range(5):
        s.req("POST", "/api/robots/A/supervise", {"action": "renew"}, token=tok)
    s.req("POST", "/api/robots/A/supervise", {"action": "release"}, token=tok)
    rows = [r for r in s.api.audit.list() if "/supervise" in r["action"]]
    assert len(rows) == 2, rows
