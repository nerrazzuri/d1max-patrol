"""W44:报警结论分开记(确认有人 / 误报 / 处理好了)、交班留言(设计 §4.1、§4.5、§8)。"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from test_site_api import PW, 站

from d1max_site.alert_store import AlertDesk
from d1max_site.alerts import VERDICTS, AlertBook, Level
from d1max_site.db import SiteDB
from d1max_site.shift_notes import MAX_CHARS, ShiftNotes


def test_结论只有三种():
    assert VERDICTS == ("intruder", "false_alarm", "handled")


def test_解决时记下结论_不给就是空():
    book = AlertBook()
    a = book.raise_alert(level=Level.P1, kind="intrusion", robot="A", title="有人", now_ms=1)
    got = book.resolve(a.key, who="wang", verdict="false_alarm", now_ms=2)
    assert got.verdict == "false_alarm" and got.to_wire()["verdict"] == "false_alarm"
    b = book.raise_alert(level=Level.P1, kind="stuck", robot="A", title="卡住", now_ms=3)
    assert book.resolve(b.key, who="wang", now_ms=4).verdict == ""


def test_结论不认识的不收():
    book = AlertBook()
    a = book.raise_alert(level=Level.P1, kind="intrusion", robot="A", title="有人", now_ms=1)
    with pytest.raises(ValueError):
        book.resolve(a.key, who="wang", verdict="maybe", now_ms=2)


def test_结论落库_重启读回来还在(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    desk = AlertDesk(db, now_ms=lambda: 1000)
    a = desk.raise_alert(kind="intrusion", robot="A", title="有人")
    desk.resolve(a.key, who="wang", verdict="intruder")
    again = AlertDesk(db, now_ms=lambda: 2000)
    got = next(x for x in again.recent(10) if x["key"] == a.key)
    assert got["verdict"] == "intruder" and got["resolved_by"] == "wang"


def test_交班留言_最新一条(tmp_path):
    t = [1000]
    notes = ShiftNotes(SiteDB(tmp_path / "s.db"), now_ms=lambda: t[0])
    assert notes.latest() is None
    notes.add("li", "后花园东边的灯坏了")
    t[0] = 2000
    notes.add("wang", "  3 号摄像头误报多  ")
    assert notes.latest() == {"id": 2, "at_ms": 2000, "author": "wang", "text": "3 号摄像头误报多"}


def test_交班留言_长度(tmp_path):
    notes = ShiftNotes(SiteDB(tmp_path / "s.db"), now_ms=lambda: 1)
    assert MAX_CHARS == 500
    for bad in ("", "   ", "字" * 501):
        with pytest.raises(ValueError):
            notes.add("wang", bad)
    assert notes.add("wang", "字" * 500)["text"] == "字" * 500


# ------------------------------------------------------------ 接口


@pytest.fixture
def 站点(tmp_path):
    s = 站(tmp_path, alerts=True)
    s.accounts.add("gina", PW, role="guard")
    s.accounts.add("olga", PW, role="owner")
    yield s
    s.close()


def _登(s, name):
    code, d = s.req("POST", "/api/login", {"name": name, "password": PW})
    assert code == 200, d
    return d["token"]


def _一条(s, kind="intrusion"):
    a = s.loop.call(lambda: _sync(s.desk.raise_alert, kind=kind, robot="A", title="后花园有人闯入"))
    return quote(a.key, safe="")


async def _sync(fn, *a, **kw):
    return fn(*a, **kw)


def test_误报要记下结论和谁(站点):
    s = 站点
    gina = _登(s, "gina")
    k = _一条(s)
    code, d = s.req("POST", f"/api/alerts/{k}/resolve", {"verdict": "false_alarm", "who": "x"},
                    token=gina)
    assert code == 200, d
    assert d["alert"]["verdict"] == "false_alarm" and d["alert"]["resolved_by"] == "gina"
    code, d = s.req("GET", "/api/alerts?all=1", token=gina)
    assert any(a["verdict"] == "false_alarm" for a in d["alerts"])


def test_结论只认三种_不带也行(站点):
    s = 站点
    gina = _登(s, "gina")
    code, _ = s.req("POST", f"/api/alerts/{_一条(s)}/resolve", {"verdict": "x"}, token=gina)
    assert code == 400
    code, d = s.req("POST", f"/api/alerts/{_一条(s, 'stuck')}/resolve", {}, token=gina)
    assert code == 200 and d["alert"]["verdict"] == ""


def test_家人账号也能下结论(站点):
    s = 站点
    olga = _登(s, "olga")
    code, d = s.req("POST", f"/api/alerts/{_一条(s)}/resolve", {"verdict": "intruder"}, token=olga)
    assert code == 200 and d["alert"]["verdict"] == "intruder"


def test_交班留言接口_作者取登录账号(站点):
    s = 站点
    gina, alice = _登(s, "gina"), _登(s, "alice")
    assert s.req("GET", "/api/shift-notes", token=alice) == (200, {"latest": None})
    code, d = s.req("POST", "/api/shift-notes", {"text": "后花园东边的灯坏了", "author": "mallory"},
                    token=gina)
    assert code == 200 and d["note"]["author"] == "gina"
    code, d = s.req("GET", "/api/shift-notes", token=alice)
    assert d["latest"]["text"] == "后花园东边的灯坏了" and d["latest"]["author"] == "gina"
    audit = s.req("GET", "/api/audit", token=alice)[1]["audit"]
    assert any(r["actor"] == "gina" and "/api/shift-notes" in r["action"] for r in audit)


def test_交班留言接口_长度和没登录(站点):
    s = 站点
    gina = _登(s, "gina")
    assert s.req("POST", "/api/shift-notes", {"text": " "}, token=gina)[0] == 400
    assert s.req("POST", "/api/shift-notes", {"text": "字" * 501}, token=gina)[0] == 400
    assert s.req("POST", "/api/shift-notes", {"text": 3}, token=gina)[0] == 400
    assert s.req("GET", "/api/shift-notes")[0] == 401
