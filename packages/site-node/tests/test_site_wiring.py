"""站点主程序的接线:「没办成」的几件事接到告警上(W00c6b、W00c6c)。

``Server`` 在构造时把各块拼起来;接线漏一行的话,各块自己的测试照样绿,值守屏上却什么都看不见
(W00c6b 内审之后发现的:``standby_failed`` 一直只进事件流,没人收)。这里只构造、不起(不连 broker、
不开端口),看线接没接上。
"""

from __future__ import annotations

import pytest

from d1max_site import main as site_main


@pytest.fixture
def home(tmp_path):
    import json
    h = tmp_path / "site"
    assert site_main.main(["--home", str(h), "init", "--site-id", "estate-1",
                           "--hostname", "localhost", "--broker-port", "18883"]) == 0
    # 狗专用的接收口一构造就绑端口:测试给 0(随便一个空闲的),别跟同时在跑的别的站点撞 8444。
    cfg = json.loads((h / "site.json").read_text(encoding="utf-8"))
    cfg["intake"] = {"host": "127.0.0.1", "port": 0}
    (h / "site.json").write_text(json.dumps(cfg), encoding="utf-8")
    return h


@pytest.fixture
def srv(home):
    s = site_main.Server(home, api_host="127.0.0.1", api_port=0,
                         broker_url="mqtts://127.0.0.1:18883")
    yield s
    s.stop()


def test_没回待命点接到告警上(srv):
    assert srv.alert_sources.on_feed in srv.dispatcher.feed._listeners


def test_排程这一轮没跑接到告警上(srv):
    assert srv.scheduler.on_outcome == srv.alert_sources.on_schedule_outcome


def test_派单前查哪里有图_接的是站点的地图目录(srv):
    """W09c 决定 5。"""
    assert srv.dispatcher.maps is srv.maps and srv.maps is not None



def test_告警用派遣器的钟差估计_站点只有一份(srv):
    """W09d 内审:告警源、派遣器原来各算一份。"""
    assert srv.alert_sources._skew_of == srv.dispatcher.clock_skew_s


def test_布防模式接到事件派遣和接口上_访客到点退回记审计(srv):
    """W20:事件派遣、接口用同一份模式台;访客到点退回时记一笔审计(谁都没点,是站点自己退的)。"""
    assert srv.incidents.arming is srv.arming and srv.api.arming is srv.arming
    t = [srv.arming._now()]
    srv.arming._now = lambda: t[0]
    srv.arming.set_mode("visitor", by="olga", zones=["drive"], minutes=1)
    t[0] += 60_000
    assert srv.arming.tick() is True
    rows = srv.api.audit.list()
    assert rows[0]["actor"] == "site" and rows[0]["action"] == "mode visitor_expired"
    assert rows[0]["target"] == "armed"


def test_驱离接到待命点_事件派遣_接口上(srv):
    """W22:到了拦截点由驱离接管回程,驱离中的狗不派去别的拦截点,接口看得到。"""
    # W13:待命点的「别派回程」= 驱离接管的 或 回充那几趟
    srv.deterrence.holds = lambda rid, tid: tid == "incident-x"
    assert srv.standby.hold("A", "incident-x") and srv.standby.hold("A", "charge-dock-1")
    assert not srv.standby.hold("A", "sched-1")
    assert srv.incidents.busy == srv.deterrence.busy
    assert srv.incidents.charging == srv.charge.refuse and srv.api.charge is srv.charge
    assert srv.charge.busy == srv.deterrence.busy and srv.charge.alerts is srv.alerts
    assert srv.api.deterrence is srv.deterrence


def test_驱离接到告警台上(srv):
    """W24:驱离中看到人要报告警。"""
    assert srv.deterrence.alerts is srv.alerts
