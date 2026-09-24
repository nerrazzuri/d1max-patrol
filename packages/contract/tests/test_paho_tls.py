"""PahoTransport 的 mTLS 参数与「被拒就是没连上」(W00c1 Task 1)。不需要真 broker:
paho 的网络调用换成空操作,CONNACK 回调由测试手动触发。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from d1max_contract.paho_transport import PahoTransport


def test_mqtts给证书就要成对给(tmp_path):
    with pytest.raises(ValueError):
        PahoTransport("mqtts://h:8883", "r", tls_ca=str(tmp_path / "ca"),
                      tls_cert=str(tmp_path / "c"))
    with pytest.raises(ValueError):
        PahoTransport("mqtts://h:8883", "r", tls_key=str(tmp_path / "k"))


def test_mqtt明文不许带证书(tmp_path):
    with pytest.raises(ValueError):
        PahoTransport("mqtt://h:1883", "r", tls_ca=str(tmp_path / "ca"))


def test_mqtts把ca与客户端证书交给paho(monkeypatch):
    seen = {}
    import paho.mqtt.client as mqtt

    def 记(self, **kw):
        seen.update(kw)

    monkeypatch.setattr(mqtt.Client, "tls_set", 记)
    PahoTransport("mqtts://h:8883", "r", tls_ca="/x/ca.crt", tls_cert="/x/r.crt",
                  tls_key="/x/r.key")
    assert seen == {"ca_certs": "/x/ca.crt", "certfile": "/x/r.crt", "keyfile": "/x/r.key"}


def _假网络(t: PahoTransport, monkeypatch) -> None:
    monkeypatch.setattr(t._client, "connect_async", lambda *a, **k: None)
    monkeypatch.setattr(t._client, "loop_start", lambda: None)
    monkeypatch.setattr(t._client, "loop_stop", lambda: None)


async def test_CONNACK被拒_connect抛错而不是当成连上(monkeypatch):
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    t = PahoTransport("mqtt://127.0.0.1:1883", "r")
    _假网络(t, monkeypatch)
    ups: list[bool] = []
    t.on_connection(ups.append)
    task = asyncio.ensure_future(t.connect())
    await asyncio.sleep(0.05)
    threading.Thread(target=t._on_connect, args=(
        t._client, None, None, ReasonCode(PacketTypes.CONNACK, "Not authorized"))).start()
    with pytest.raises(ConnectionError, match="Not authorized"):
        await asyncio.wait_for(task, 5)
    assert t.connected is False and ups == []


async def test_连接失败回调_connect立刻抛错(monkeypatch):
    t = PahoTransport("mqtt://127.0.0.1:1883", "r")
    _假网络(t, monkeypatch)
    task = asyncio.ensure_future(t.connect())
    await asyncio.sleep(0.05)
    threading.Thread(target=t._on_connect_fail, args=(t._client, None)).start()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(task, 5)
    assert t.connected is False


async def test_CONNACK之前就断开_connect立刻抛错(monkeypatch):
    t = PahoTransport("mqtt://127.0.0.1:1883", "r")
    _假网络(t, monkeypatch)
    task = asyncio.ensure_future(t.connect())
    await asyncio.sleep(0.05)
    threading.Thread(target=t._on_disconnect, args=(t._client, None, None, "handshake")).start()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(task, 5)
