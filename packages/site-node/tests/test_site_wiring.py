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
    h = tmp_path / "site"
    assert site_main.main(["--home", str(h), "init", "--site-id", "estate-1",
                           "--hostname", "localhost", "--broker-port", "18883"]) == 0
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
