"""broker 侧的设备认证与 ACL(总设计 §3.6)。前两条是纯渲染测试;其余要真 Mosquitto。"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from d1max_contract.paho_transport import PahoTransport
from d1max_contract.topics import PUBLISH_KINDS, TopicAcl, Topics
from d1max_site.broker_conf import BrokerPaths, render_acl, render_conf

SITE = "estate-1"


def test_ACL里狗侧条目与契约TopicAcl出自同一张表():
    acl = render_acl(SITE)
    writes = {ln.split()[2] for ln in acl.splitlines() if ln.startswith("pattern write")}
    reads = {ln.split()[2] for ln in acl.splitlines() if ln.startswith("pattern read")}
    t = Topics(site_id=SITE, robot_id="R")
    g = TopicAcl(t)
    assert {w.replace("%u", "R") for w in writes} == {t.of(k) for k in PUBLISH_KINDS}
    assert all(g.may_publish(w.replace("%u", "R")) for w in writes)
    assert {r.replace("%u", "R") for r in reads} == {t.cmd}
    assert "user site:estate-1" in acl and f"topic write site/{SITE}/robot/+/cmd" in acl
    assert f"topic read site/{SITE}/robot/+/cmd" not in acl.splitlines(), "站点不读 cmd"


def test_配置要求客户端证书_禁匿名_路径有空白就拒(tmp_path):
    p = BrokerPaths(cafile=tmp_path / "ca", certfile=tmp_path / "c", keyfile=tmp_path / "k",
                    crlfile=tmp_path / "crl", aclfile=tmp_path / "acl")
    conf = render_conf(port=8883, paths=p)
    for line in ("allow_anonymous false", "require_certificate true",
                 "use_identity_as_username true", "use_username_as_clientid true",
                 "max_packet_size 262144", "listener 8883 0.0.0.0",
                 f"crlfile {tmp_path / 'crl'}"):
        assert line in conf.splitlines(), line
    with pytest.raises(ValueError):
        render_conf(port=1, paths=BrokerPaths(cafile=tmp_path / "a b", certfile=p.certfile,
                                              keyfile=p.keyfile, crlfile=p.crlfile,
                                              aclfile=p.aclfile))


# ------------------------------------------------------------ 真 broker


async def _连(broker, who: str, **tls) -> PahoTransport:
    t = PahoTransport(broker.url, client_id=f"{who}-{uuid.uuid4().hex[:6]}", **tls)
    await t.connect()
    return t


class 收件:
    def __init__(self) -> None:
        self.got: list[tuple[str, bytes]] = []

    async def __call__(self, m) -> None:
        self.got.append((m.topic, m.payload))


async def test_狗发自己的主题站点收得到(broker):
    site = await _连(broker, "site", **broker.site_tls())
    ears = 收件()
    await site.subscribe(f"site/{SITE}/robot/+/status", ears)
    await asyncio.sleep(0.3)
    a = await _连(broker, "A", **broker.robot_tls("A"))
    await a.publish(Topics(site_id=SITE, robot_id="A").status, b"hi", qos=1)
    await asyncio.sleep(0.5)
    assert (f"site/{SITE}/robot/A/status", b"hi") in ears.got
    await a.close()
    await site.close()


async def test_A冒充B发主题_站点收不到(broker):
    site = await _连(broker, "site", **broker.site_tls())
    ears = 收件()
    await site.subscribe(f"site/{SITE}/robot/+/#", ears)
    await asyncio.sleep(0.3)
    a = await _连(broker, "A", **broker.robot_tls("A"))
    await a.publish(Topics(site_id=SITE, robot_id="B").status, b"fake", qos=1)
    await a.publish(Topics(site_id=SITE, robot_id="A").cmd, b"self-cmd", qos=1)
    await a.publish(Topics(site_id=SITE, robot_id="A").status, b"real", qos=1)   # 对照
    await asyncio.sleep(1.0)
    assert ears.got == [(f"site/{SITE}/robot/A/status", b"real")], \
        "A 只许发自己的上行主题;cmd 只有站点能发(对照那条收到了,说明站点订阅是活的)"
    await a.close()
    await site.close()


async def test_A订B的cmd收不到_B自己收得到(broker):
    a = await _连(broker, "A", **broker.robot_tls("A"))
    b = await _连(broker, "B", **broker.robot_tls("B"))
    a_ears, b_ears = 收件(), 收件()
    tb = Topics(site_id=SITE, robot_id="B")
    await a.subscribe(tb.cmd, a_ears)
    await b.subscribe(tb.cmd, b_ears)
    await asyncio.sleep(0.3)
    site = await _连(broker, "site", **broker.site_tls())
    await site.publish(tb.cmd, json.dumps({"x": 1}).encode(), qos=1)
    await asyncio.sleep(1.0)
    assert b_ears.got and not a_ears.got
    for t in (a, b, site):
        await t.close()


async def test_没有客户端证书连不上(broker):
    t = PahoTransport(broker.url, client_id="anon", tls_ca=str(broker.ca.ca_cert))
    with pytest.raises((ConnectionError, TimeoutError)):
        await t.connect()


async def test_别的CA签的证书连不上(broker, tmp_path):
    from d1max_site.ca import SiteCA
    other = SiteCA(tmp_path / "other")
    other.init(SITE)
    bundle = other.issue_robot("A", days=30, now_ms=1_800_000_000_000)
    t = PahoTransport(broker.url, client_id="evil", tls_ca=str(broker.ca.ca_cert),
                      tls_cert=str(bundle.dir / "robot.crt"),
                      tls_key=str(bundle.dir / "robot.key"))
    with pytest.raises((ConnectionError, TimeoutError)):
        await t.connect()


async def test_吊销后重启broker_A连不上_B照常(broker):
    broker.ca.revoke("A")
    broker.restart()
    with pytest.raises((ConnectionError, TimeoutError)):
        await _连(broker, "A", **broker.robot_tls("A"))
    b = await _连(broker, "B", **broker.robot_tls("B"))
    await b.close()


async def test_拿别人的client_id连_踢不掉站点也接管不了B的会话(broker):
    """client_id 不跟证书绑的话:A 用 ``site:estate-1`` 连,会把站点踢下线;用 ``B`` 连,会接管 B 的
    持久会话,B 离线期间攒着的 cmd(包括 abort)就丢了。broker 要用证书名当 client_id。"""
    ups: list[bool] = []
    site = PahoTransport(broker.url, client_id="site:estate-1", **broker.site_tls())  # 同生产
    site.on_connection(ups.append)
    await site.connect()
    ups.clear()
    b = PahoTransport(broker.url, client_id="B", **broker.robot_tls("B"))
    b_ears = 收件()
    tb = Topics(site_id=SITE, robot_id="B")
    await b.subscribe(tb.cmd, b_ears)
    await b.connect()
    for fake in ("site:estate-1", "B"):
        a = PahoTransport(broker.url, client_id=fake, **broker.robot_tls("A"))
        await a.connect()
        await asyncio.sleep(0.5)
        await a.close()
    await asyncio.sleep(0.5)
    assert site.connected and False not in ups, "站点被踢过"
    await site.publish(tb.cmd, b"to-B", qos=1)
    await asyncio.sleep(0.8)
    assert (tb.cmd, b"to-B") in b_ears.got, "B 的会话被接管了"
    for t in (b, site):
        await t.close()


async def test_A订通配的cmd也收不到别人的(broker):
    a = await _连(broker, "A", **broker.robot_tls("A"))
    b = await _连(broker, "B", **broker.robot_tls("B"))
    a_ears, b_ears = 收件(), 收件()
    for f in (f"site/{SITE}/robot/+/cmd", "#", f"site/{SITE}/#"):
        await a.subscribe(f, a_ears)
    tb = Topics(site_id=SITE, robot_id="B")
    await b.subscribe(tb.cmd, b_ears)
    await asyncio.sleep(0.3)
    site = await _连(broker, "site", **broker.site_tls())
    await site.publish(tb.cmd, b"for-B", qos=1)
    await asyncio.sleep(1.0)
    assert b_ears.got and not [g for g in a_ears.got if g[1] == b"for-B"]
    for t in (a, b, site):
        await t.close()


async def test_吊销后broker重启之前_A的上行仍被接受_这是已知窗口(broker):
    """Mosquitto 只在启动时读 CRL。记下来:吊销到重启之间,已登记的证书还能连、还能发。
    派遣器那边吊销是立刻生效的(注册表),broker 这边要重启(报告取舍 3)。"""
    broker.ca.revoke("A")
    site = await _连(broker, "site", **broker.site_tls())
    ears = 收件()
    await site.subscribe(f"site/{SITE}/robot/+/status", ears)
    await asyncio.sleep(0.3)
    a = await _连(broker, "A", **broker.robot_tls("A"))
    await a.publish(Topics(site_id=SITE, robot_id="A").status, b"still", qos=1)
    await asyncio.sleep(0.5)
    assert (f"site/{SITE}/robot/A/status", b"still") in ears.got
    for t in (a, site):
        await t.close()
