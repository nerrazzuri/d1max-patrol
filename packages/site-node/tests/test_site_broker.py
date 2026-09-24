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
                 "use_identity_as_username true", "listener 8883 0.0.0.0",
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
