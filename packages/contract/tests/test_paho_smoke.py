"""对真 broker 的冒烟(决定 2):``D1MAX_MQTT_TEST_URL=mqtt://host:1883`` 设了才跑。
本机没装 mosquitto 就跳过 —— 全量测试不依赖外部进程。"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

URL = os.environ.get("D1MAX_MQTT_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="没设 D1MAX_MQTT_TEST_URL,跳过真 broker 冒烟")


async def test_pub_sub_retained_lwt_qos1():
    from d1max_contract.paho_transport import PahoTransport

    tag = uuid.uuid4().hex[:8]
    topic = f"d1max-test/{tag}/status"
    got: list[bytes] = []

    async def 收(m):
        got.append(m.payload)

    dog = PahoTransport(URL, client_id=f"dog-{tag}")
    dog.set_will(topic, b"offline", qos=1, retain=True)
    await dog.connect()
    await dog.publish(topic, b"online", qos=1, retain=True)
    site = PahoTransport(URL, client_id=f"site-{tag}")
    await site.connect()
    await site.subscribe(topic, 收, qos=1)
    await asyncio.sleep(0.5)
    assert got == [b"online"], "retained 要投给后订阅者"
    dog._client.socket().close()          # 非正常断开 → LWT
    for _ in range(40):
        await asyncio.sleep(0.25)
        if b"offline" in got:
            break
    assert got[-1] == b"offline"
    await site.publish(topic, b"", qos=1, retain=True)   # 清掉 retained
    await site.close()
