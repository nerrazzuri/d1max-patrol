"""鉴权:PIN 换 token,token 走请求头。

**这一组测试防的是"看起来加了锁,其实锁没扣上"。** 鉴权跟别的功能不一样:
它坏掉的时候页面照常能用、报告照常出得来,没有任何一处会红 —— 直到有人在
客户现场把狗开走。所以这里的断言全是"某某确实进不来",而不是"某某能进来"。

三条最容易漏、漏了就等于没做的:

* **限速。** 六位 PIN 一百万种,不限速几分钟就爆破完了。
* **锁定期间连对的 PIN 也不放。** 不然爆破的人猜中的那一下照样能进,
  限速就只是给他多花了几秒。
* **``?token=`` 的口子必须是窄的。** 它只是给 ``<img>`` / ``EventSource`` /
  ``<a href>`` 这几个设不了请求头的浏览器 API 留的;一旦它对所有接口都认,
  前面那套"请求头天然免疫 CSRF"就白做了。
"""

from __future__ import annotations

import json

import pytest

from d1max_patrol.app.auth import (
    MAX_THROTTLE_CLIENTS,
    Denied,
    Guard,
    Throttle,
    TokenStore,
    bearer,
    host_is_literal,
    query_token_ok,
)
from d1max_patrol.app.server import PIN_ENV, AppServer, _build_parser, check_exposure
from tests.app.conftest import request, sse, status

PIN = "428913"


@pytest.fixture
def server_pin(ctx):
    s = AppServer(ctx, port=0, pin=PIN)
    s.start()
    yield s
    s.stop()


def unlock(server, pin: str = PIN):
    """打 ``/api/auth``,返回 (状态码, 响应体 dict)。"""
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload={"pin": pin})
    return code, json.loads(body)


def token_of(server) -> str:
    code, body = unlock(server)
    assert code == 200, body
    return body["token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ PIN 本身


def test_不给pin就是不鉴权():
    """本机开发用的默认路径。它要是也要 PIN,没人会好好用这套东西。"""
    assert not Guard().enabled


def test_给了pin就鉴权():
    assert Guard(PIN).enabled


@pytest.mark.parametrize("bad", ["", "1", "123", "   "])
def test_pin太短直接拒(bad):
    with pytest.raises(ValueError, match="至少"):
        Guard(bad)


@pytest.mark.parametrize("bad", ["12 34", "12\t34", "12\n34"])
def test_pin里有空白直接拒(bad):
    """空白进了 PIN,人自己都记不清到底输没输那一下。"""
    with pytest.raises(ValueError):
        Guard(bad)


def test_pin两头的空格会被吃掉():
    """从环境变量里读出来的值常常带个换行。"""
    g = Guard(f"  {PIN}\n")
    assert g.unlock(PIN, "1.2.3.4")


def test_对的pin换得到token():
    g = Guard(PIN)
    assert len(g.unlock(PIN, "1.2.3.4")) > 20


def test_每次换的token都不一样():
    g = Guard(PIN)
    assert g.unlock(PIN, "a") != g.unlock(PIN, "b")


def test_错的pin不给token():
    g = Guard(PIN)
    with pytest.raises(Denied) as e:
        g.unlock("000000", "1.2.3.4")
    assert e.value.status == 401


@pytest.mark.parametrize("junk", [None, 123, ["428913"], {"pin": PIN}])
def test_pin不是字符串也不能蒙混过去(junk):
    """``{"pin": null}`` 不能因为"两边都是假值"就放行。"""
    g = Guard(PIN)
    with pytest.raises(Denied):
        g.unlock(junk, "1.2.3.4")


def test_没设pin的时候解锁接口自己也不给token():
    with pytest.raises(Denied) as e:
        Guard().unlock(PIN, "1.2.3.4")
    assert e.value.status == 400


# -------------------------------------------------------------------- 限速


def test_连错到阈值就锁上():
    g = Guard(PIN)
    for _ in range(5):
        with pytest.raises(Denied):
            g.unlock("000000", "1.2.3.4")
    with pytest.raises(Denied) as e:
        g.unlock("000000", "1.2.3.4")
    assert e.value.status == 429


def test_锁着的时候连对的pin也不放():
    """这条才是限速真正的价值。

    要是锁定期间对的 PIN 照样能进,爆破的人猜中的那一下就照样能进 ——
    限速只让他多花了几秒钟,等于没有。
    """
    g = Guard(PIN)
    for _ in range(5):
        with pytest.raises(Denied):
            g.unlock("000000", "1.2.3.4")
    with pytest.raises(Denied) as e:
        g.unlock(PIN, "1.2.3.4")
    assert e.value.status == 429


def test_锁的是那一个来源不是所有人():
    """一个人手滑锁不住旁边那台手机。"""
    g = Guard(PIN)
    for _ in range(6):
        with pytest.raises(Denied):
            g.unlock("000000", "1.2.3.4")
    assert g.unlock(PIN, "5.6.7.8")


def test_输对一次计数就清零():
    g = Guard(PIN)
    for _ in range(4):
        with pytest.raises(Denied):
            g.unlock("000000", "1.2.3.4")
    g.unlock(PIN, "1.2.3.4")
    with pytest.raises(Denied) as e:      # 又是第一次错,不该直接锁
        g.unlock("000000", "1.2.3.4")
    assert e.value.status == 401


def test_没到阈值不锁():
    t = Throttle(max_fails=5)
    assert [t.fail("x", now=0.0) for _ in range(4)] == [0.0, 0.0, 0.0, 0.0]


def test_越错越久():
    t = Throttle(max_fails=1, lockout_s=10.0)
    assert t.fail("x", now=0.0) == 10.0
    assert t.fail("x", now=0.0) == 20.0
    assert t.fail("x", now=0.0) == 40.0


def test_退避有封顶():
    """指数涨下去几十次就是几十年,那等于把机器锁死了 —— 现场没法收场。"""
    t = Throttle(max_fails=1, lockout_s=10.0, max_lockout_s=100.0)
    for _ in range(20):
        wait = t.fail("x", now=0.0)
    assert wait == 100.0


def test_锁到点了就自动开():
    t = Throttle(max_fails=1, lockout_s=10.0)
    t.fail("x", now=0.0)
    assert t.locked_for("x", now=5.0) > 0
    assert t.locked_for("x", now=10.1) == 0.0


def test_限速表有内存兜底():
    """每来一个新来源就多一条记录,没兜底的话扫端口的人能让它长到网段那么大。

    这是这一卷里唯一一个会随外部输入无限长的结构(``TokenStore`` 有
    ``MAX_TOKENS``,``NonceStore`` 有 ``MAX_NONCES``,``LeaseBook`` 有
    ``AUDIT_MAX``)。
    """
    t = Throttle(max_fails=5, max_clients=8)
    for i in range(200):
        t.fail(f"10.0.0.{i}", now=0.0)
    assert t.tracked <= 8


def test_内存压力先淘汰没锁着的():
    """**这是这条兜底要紧的那一句。**

    一个正在被锁的来源,不许因为别人在灌"还没到阈值"的记录就被放行 —— 不然
    爆破的人只要同时从很多个地址发几下请求,就能把自己的锁擦掉,限速当场变成
    摆设。所以淘汰先挑 ``until <= now`` 的。
    """
    t = Throttle(max_fails=5, lockout_s=100.0, max_clients=4)
    for _ in range(5):
        t.fail("坏人", now=0.0)
    assert t.locked_for("坏人", now=1.0) > 0
    for i in range(200):                    # 拿一堆没锁着的条目去挤
        t.fail(f"10.0.0.{i}", now=1.0)
    assert t.tracked <= 4
    assert t.locked_for("坏人", now=1.0) > 0, "锁还没走完的人被内存压力放掉了"


def test_全都锁着的时候上限还是上限():
    """兜底那一档:第一档一条都扔不动的时候,内存**仍然**不许无限长。

    "内存有界"是这个类必须守住的承诺,而一个能换地址的人可以让每一条记录都
    处在锁定中。这时只好扔最快要解锁的那些 —— 让出的东西很少,因为他换一个
    新地址本来就直接得到一个干净的计数(计数是按 IP 记的)。
    """
    t = Throttle(max_fails=1, lockout_s=100.0, max_clients=4)
    for i in range(200):
        t.fail(f"10.0.0.{i}", now=1.0)      # 每一条都当场锁上
    assert t.tracked <= 4


def test_默认的限速表上限():
    assert MAX_THROTTLE_CLIENTS == 512


# ------------------------------------------------------------------ token 库


def test_签出来的token认得出():
    s = TokenStore()
    assert s.valid(s.issue())


def test_乱编的token认不出():
    s = TokenStore()
    s.issue()
    assert not s.valid("我猜一个")


def test_空token认不出():
    assert not TokenStore().valid("")


def test_闲太久就作废():
    s = TokenStore(idle_s=100.0)
    t = s.issue(now=0.0)
    assert not s.valid(t, now=101.0)


def test_一直在用就不会掉线():
    """算的是"闲置"不是"签发"。正在遥控的人被踢下线,狗就在那儿站着不动了。"""
    s = TokenStore(idle_s=100.0)
    t = s.issue(now=0.0)
    for now in (50.0, 140.0, 230.0, 320.0):
        assert s.valid(t, now=now), f"{now} 秒时不该掉"


def test_作废之后不占地方():
    s = TokenStore(idle_s=100.0)
    s.issue(now=0.0)
    s.valid("x", now=1000.0)
    assert s.count == 0


def test_token数量有上限():
    """防的是有人反复解锁把内存撑爆。"""
    s = TokenStore(cap=3)
    for _ in range(10):
        s.issue()
    assert s.count == 3


def test_满了淘汰最老的():
    s = TokenStore(cap=2)
    old, mid = s.issue(), s.issue()
    new = s.issue()
    assert not s.valid(old)
    assert s.valid(mid) and s.valid(new)


def test_能主动作废():
    s = TokenStore()
    t = s.issue()
    s.revoke(t)
    assert not s.valid(t)


# ---------------------------------------------------------------- 请求头解析


@pytest.mark.parametrize("raw,want", [
    ("Bearer abc", "abc"),
    ("bearer abc", "abc"),
    ("BEARER abc", "abc"),
    ("Bearer  abc ", "abc"),
    ("Basic abc", None),
    ("abc", None),
    ("Bearer", None),
    ("Bearer  ", None),
    ("", None),
])
def test_bearer解析(raw, want):
    assert bearer({"authorization": raw}) == want


def test_没有这个头就是没有():
    assert bearer({}) is None


# ------------------------------------------------------------------ Host 头


@pytest.mark.parametrize("host", [
    "192.168.168.100:8095", "192.168.168.100", "127.0.0.1:8095",
    "localhost", "localhost:8095", "[::1]:8095", "[::1]", "",
])
def test_ip字面量放行(host):
    assert host_is_literal(host)


@pytest.mark.parametrize("host", [
    "evil.example.com", "evil.example.com:8095", "d1max.local",
    "192.168.168.100.evil.com", "[::1",
])
def test_域名一律不收(host):
    """DNS rebinding:把域名解到狗的 IP 上,同源策略就把攻击者当自己人了。"""
    assert not host_is_literal(host)


# -------------------------------------------------- ?token= 的口子有多大


@pytest.mark.parametrize("path", [
    "/api/events",
    "/api/video/front",
    "/api/runs/20260905-120000/photos/p1.jpg",
    "/api/runs/20260905-120000/report.html",
])
def test_设不了请求头的那几条认查询串(path):
    assert query_token_ok("GET", path)


@pytest.mark.parametrize("path", [
    "/api/state", "/api/missions", "/api/maps", "/api/runs",
    "/api/procs/slam/log", "/api/auth", "/api/events/x",
])
def test_别的路径不认查询串(path):
    assert not query_token_ok("GET", path)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_会改东西的方法一律不认查询串(method):
    """查询串会进浏览器历史、进 Referer。能读到是一回事,能开狗是另一回事。"""
    assert not query_token_ok(method, "/api/events")


# ------------------------------------------------------------ 接上真服务


def test_没给pin的服务老行为一字不变(server):
    """1300 多个既有用例全靠这一条 —— 默认路径上不能多出任何一道门。"""
    assert status(server, "/api/state") == 200


def test_给了pin就进不去了(server_pin):
    assert status(server_pin, "/api/state") == 401


def test_拿着token就进得去(server_pin):
    assert status(server_pin, "/api/state",
                  headers=auth(token_of(server_pin))) == 200


def test_乱编的token进不去(server_pin):
    assert status(server_pin, "/api/state", headers=auth("guess-me")) == 401


def test_解锁接口自己不要token(server_pin):
    assert unlock(server_pin)[0] == 200


def test_解锁给错pin是401(server_pin):
    assert unlock(server_pin, "000000")[0] == 401


def test_解锁体不是对象就是400(server_pin):
    code, _, _ = request(server_pin, "/api/auth", method="POST",
                         raw=b'"428913"')
    assert code == 400


def test_页面和静态资源不拦(server_pin):
    """人得先能把页面打开,才有地方输 PIN。里面也确实没有秘密。"""
    assert status(server_pin, "/") == 200
    assert status(server_pin, "/static/app.js") == 200


@pytest.mark.parametrize("path", [
    "/api/state", "/api/missions", "/api/maps", "/api/runs", "/api/mapping",
    "/api/bundle", "/api/schedule",
])
def test_读的接口也得有token(server_pin, path):
    """现场照片是客户资产,比机器本身更敏感。读也要拦。"""
    assert status(server_pin, path) == 401


@pytest.mark.parametrize("path", [
    "/api/estop", "/api/teleop", "/api/mapping/record/start",
    "/api/run/abort", "/api/run/suspend", "/api/pose/reset",
    "/api/bundle/apply", "/api/bundle/rollback",
])
def test_能动机器的接口更得有token(server_pin, path):
    assert status(server_pin, path, method="POST", payload={}) == 401


def test_没挂路由的接口也是先401不是404(server_pin):
    """先 404 会把"这台有哪些接口"白送出去,而且那是没解锁的人问出来的。"""
    assert status(server_pin, "/api/no-such-thing") == 401


def test_401的体是json而且说得清(server_pin):
    code, body, _ = request(server_pin, "/api/state")
    assert code == 401
    assert "error" in json.loads(body)


def test_事件流认查询串(server_pin):
    """``EventSource`` 设不了请求头 —— 这条是它唯一的活路。"""
    with sse(server_pin, f"/api/events?token={token_of(server_pin)}") as frames:
        assert next(frames)["kind"] == "state"


def test_事件流不给token还是进不去(server_pin):
    assert status(server_pin, "/api/events") == 401


def test_查询串在别的接口上不认(server_pin):
    """口子必须是窄的。这条一破,请求头那套 CSRF 免疫就白做了。"""
    tok = token_of(server_pin)
    assert status(server_pin, f"/api/state?token={tok}") == 401


def test_查询串在post上不认(server_pin):
    tok = token_of(server_pin)
    assert status(server_pin, f"/api/estop?token={tok}",
                  method="POST", payload={}) == 401


def test_域名访问一律403(server_pin):
    assert status(server_pin, "/", headers={"Host": "evil.example.com"}) == 403


def test_没设pin也挡域名(server):
    """DNS rebinding 打的是浏览器,跟服务端有没有 PIN 没关系。"""
    assert status(server, "/api/state",
                  headers={"Host": "evil.example.com"}) == 403


def test_同一个token能反复用(server_pin):
    tok = token_of(server_pin)
    for _ in range(3):
        assert status(server_pin, "/api/state", headers=auth(tok)) == 200


def test_多台手机各拿各的token(server_pin):
    """一开始就要能管多只,反过来也得能容得下多个客户端。"""
    a, b = token_of(server_pin), token_of(server_pin)
    assert a != b
    assert status(server_pin, "/api/state", headers=auth(a)) == 200
    assert status(server_pin, "/api/state", headers=auth(b)) == 200


# ------------------------------------------------------- 命令行这一侧


def test_只听本机不需要pin():
    check_exposure("127.0.0.1", None)


def test_绑局域网没pin就退出():
    with pytest.raises(SystemExit, match="--pin"):
        check_exposure("0.0.0.0", None)


def test_绑局域网给了pin就放行():
    check_exposure("0.0.0.0", PIN)


def test_pin不合规也是退出不是traceback():
    """现场看到的应该是一句人话,不是一屏堆栈。"""
    with pytest.raises(SystemExit, match="不合规"):
        check_exposure("0.0.0.0", "12")


def test_本机模式下的烂pin一样拦():
    """先在自己电脑上试的时候就该报,别等到现场绑 0.0.0.0 那一刻才发现。"""
    with pytest.raises(SystemExit, match="不合规"):
        check_exposure("127.0.0.1", "12")


def test_pin可以从环境变量给(monkeypatch):
    """命令行上的 --pin 在 ``ps`` 里是明文的,同机器上别的账号看得见。"""
    monkeypatch.setenv(PIN_ENV, PIN)
    assert _build_parser().parse_args([]).pin == PIN


def test_没设环境变量就是不鉴权(monkeypatch):
    monkeypatch.delenv(PIN_ENV, raising=False)
    assert _build_parser().parse_args([]).pin is None
