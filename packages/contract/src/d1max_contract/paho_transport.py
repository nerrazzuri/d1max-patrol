"""真 broker 的 Transport 实现(paho-mqtt 2.x)。只在真机与冒烟测试里用;
全部业务代码只认 :class:`d1max_contract.transport.Transport`。

paho 在自己的线程里跑网络循环;这里把回调用 ``call_soon_threadsafe`` 投回 asyncio
循环,``publish`` 在线程池里等 ``wait_for_publish``。``clean_session=False`` +
固定 ``client_id``:断线期间发给我们的 QoS 1 报文由 broker 保留,重连后补投。
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from d1max_contract.transport import ConnectionCallback, Handler, Message, topic_matches

try:  # pragma: no cover - 只在装了 paho 的环境里覆盖
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise ImportError("PahoTransport 需要 paho-mqtt:pip install 'd1max-contract[mqtt]'") from exc


class PahoTransport:
    def __init__(self, url: str, client_id: str, *, keepalive_s: int = 30) -> None:
        u = urlparse(url)
        if u.scheme not in ("mqtt", "mqtts"):
            raise ValueError(f"url 要是 mqtt:// 或 mqtts://,给的是 {url!r}")
        self._host = u.hostname or "127.0.0.1"
        self._port = u.port or (8883 if u.scheme == "mqtts" else 1883)
        self._tls = u.scheme == "mqtts"
        self._keepalive = keepalive_s
        self.client_id = client_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[tuple[str, int, Handler]] = []
        self._cbs: list[ConnectionCallback] = []
        self._connected = False
        self._connected_evt: asyncio.Event | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                                   clean_session=False, protocol=mqtt.MQTTv311)
        if self._tls:
            self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def connected(self) -> bool:
        return self._connected

    def set_will(self, topic: str, payload: bytes, qos: int = 1, retain: bool = True) -> None:
        self._client.will_set(topic, payload, qos=qos, retain=retain)

    def on_connection(self, cb: ConnectionCallback) -> None:
        self._cbs.append(cb)

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._connected_evt = asyncio.Event()
        self._client.connect_async(self._host, self._port, keepalive=self._keepalive)
        self._client.loop_start()
        await asyncio.wait_for(self._connected_evt.wait(), timeout=10)

    async def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
        self._set_connected(False)

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1,
                      retain: bool = False) -> None:
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if qos >= 1 and self._connected:
            await asyncio.get_running_loop().run_in_executor(None, info.wait_for_publish, 10)

    async def subscribe(self, topic_filter: str, handler: Handler, *, qos: int = 1) -> None:
        """可以在连接前登记:handler 先在名单里,CONNACK 后 broker 补投的离线命令才有人接。
        真正的 SUBSCRIBE 在 ``_on_connect`` 里统一发(重连也重发)。"""
        self._subs.append((topic_filter, qos, handler))
        if self._connected:
            self._client.subscribe(topic_filter, qos=qos)

    # ------------------------------------------------------------ paho 线程里的回调

    def _set_connected(self, up: bool) -> None:
        if up == self._connected:
            return
        self._connected = up
        for cb in list(self._cbs):
            cb(up)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        def go() -> None:
            self._set_connected(True)
            if self._connected_evt is not None:
                self._connected_evt.set()
            # 重连后重订(clean_session=False 下 broker 记得,再订一次不碍事)。
            for topic_filter, qos, _ in self._subs:
                client.subscribe(topic_filter, qos=qos)
        self._post(go)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._post(lambda: self._set_connected(False))

    def _on_message(self, client, userdata, message) -> None:
        msg = Message(message.topic, bytes(message.payload), message.qos, bool(message.retain))

        def go() -> None:
            for topic_filter, _, handler in self._subs:
                if topic_matches(topic_filter, msg.topic):
                    asyncio.ensure_future(handler(msg))
        self._post(go)

    def _post(self, fn) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn)
