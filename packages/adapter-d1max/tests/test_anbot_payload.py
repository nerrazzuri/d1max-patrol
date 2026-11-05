"""POC 上装转接(11-05,用户批准):借竞品上装的警灯、喇叭。竞品主控上跑 ROS1,我们在 Orin 上用纯 Python
注册成发布端:警灯 ``/sensor/strobeLightSwitch``(std_msgs/Bool)、喊话 ``/anbot/voice/play``
(std_msgs/String,竞品自己念文字)。这里用假的 ROS1 master + 假的订阅端(照 TCPROS 握手)。"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import threading
import time
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer

import pytest

from nexwar_adapter_d1max.anbot import (
    BOOL_MD5,
    STRING_MD5,
    AnbotConfig,
    AnbotPayload,
    load_payload,
)
from nexwar_adapter_d1max.payload import Payload, PayloadError

STROBE = "/sensor/strobeLightSwitch"
VOICE = "/anbot/voice/play"


class 假Master:
    """ROS1 master:记发布端注册了谁;``down`` 时不开端口(模拟竞品主控没开)。"""

    def __init__(self) -> None:
        self.srv = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)
        self.pubs: dict[str, str] = {}
        self.registers = 0
        self.unregisters: list[str] = []
        self.srv.register_function(self._reg, "registerPublisher")
        self.srv.register_function(self._unreg, "unregisterPublisher")
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _reg(self, caller, topic, mtype, api):
        self.registers += 1
        self.pubs[topic] = api
        return [1, "ok", []]

    def _unreg(self, caller, topic, api):
        self.unregisters.append(topic)
        self.pubs.pop(topic, None)
        return [1, "ok", 1]

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class 假订阅端:
    """竞品那头的订阅端(led_control / anbotControl):问发布端要 TCPROS 地址、握手、收消息。"""

    def __init__(self, api: str, topic: str, mtype: str, md5: str) -> None:
        code, _, proto = xmlrpc.client.ServerProxy(api).requestTopic(
            "/led_control", topic, [["TCPROS"]])
        assert code == 1 and proto[0] == "TCPROS"
        self.s = socket.create_connection((proto[1], proto[2]), timeout=2)
        fields = {"callerid": "/led_control", "topic": topic, "type": mtype, "md5sum": md5,
                  "tcp_nodelay": "1"}
        body = b"".join(struct.pack("<I", len(f)) + f
                        for f in (f"{k}={v}".encode() for k, v in fields.items()))
        self.s.sendall(struct.pack("<I", len(body)) + body)
        self.header = self._fields(self._frame())
        self.msgs: list[bytes] = []
        self.buf = b""
        threading.Thread(target=self._read, daemon=True).start()

    def _exact(self, n: int) -> bytes:
        b = b""
        while len(b) < n:
            d = self.s.recv(n - len(b))
            if not d:
                raise EOFError
            b += d
        return b

    def _frame(self) -> bytes:
        return self._exact(struct.unpack("<I", self._exact(4))[0])

    @staticmethod
    def _fields(raw: bytes) -> dict[str, str]:
        out, i = {}, 0
        while i < len(raw):
            n = struct.unpack("<I", raw[i:i + 4])[0]
            k, _, v = raw[i + 4:i + 4 + n].decode().partition("=")
            out[k] = v
            i += 4 + n
        return out

    def _read(self) -> None:
        self.s.settimeout(None)
        try:
            while True:
                self.msgs.append(self._frame())
        except (EOFError, OSError):
            pass

    def bools(self) -> list[bool]:
        return [m == b"\x01" for m in self.msgs]

    def texts(self) -> list[str]:
        return [m[4:4 + struct.unpack("<I", m[:4])[0]].decode() for m in self.msgs]

    def close(self) -> None:
        self.s.close()


def _cfg(master: str, **kw) -> AnbotConfig:
    return AnbotConfig(master=master, host="127.0.0.1", **({"republish_s": 0.05,
                                                            "secs_per_char": 0.01} | kw))


async def _等(cond, s: float = 3.0) -> None:
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > s:
            raise AssertionError("等不到")
        await asyncio.sleep(0.02)


@pytest.fixture
def master():
    m = 假Master()
    yield m
    m.close()


async def _接上(m: 假Master, p: AnbotPayload, topic: str) -> 假订阅端:
    await _等(lambda: topic in m.pubs)
    if topic == STROBE:
        sub = 假订阅端(m.pubs[topic], topic, "std_msgs/Bool", BOOL_MD5)
    else:
        sub = 假订阅端(m.pubs[topic], topic, "std_msgs/String", STRING_MD5)
    await _等(lambda: p.connections(topic) >= 1)
    return sub


async def test_能力只报警灯和喇叭_喇叭只认文字(master):
    p = AnbotPayload(_cfg(master.url))
    assert p.has("strobe") and p.has("speaker")
    assert not p.has("siren") and not p.has("spotlight")
    assert p.options() == {}
    assert p.cfg.tts_command and not p.cfg.clips_dir, "HAL 据此报能 TTS、没有录好的话术"


async def test_开警灯_握手按标准Bool_开着一直重发_到点自己关(master):
    p = AnbotPayload(_cfg(master.url))
    await p.start()
    sub = await _接上(master, p, STROBE)
    assert sub.header["type"] == "std_msgs/Bool" and sub.header["md5sum"] == BOOL_MD5
    assert sub.header["latching"] == "1", "竞品的灯控程序重启了也拿得到现在该亮还是灭"
    await _等(lambda: sub.bools() and sub.bools()[-1] is False)        # 起来先关一遍
    await p.set("strobe", True, 0.4)
    await _等(lambda: sub.bools().count(True) >= 3)                     # 竞品自己会发,我们定时重发
    await _等(lambda: sub.bools()[-1] is False, 2.0)                    # 到点自己关
    n = len(sub.msgs)
    await asyncio.sleep(0.3)
    assert len(sub.msgs) == n, "关了就不再发:不跟竞品自己的用法抢"
    await p.close()
    sub.close()


async def test_关警灯_代理收尾_都发关_注销(master):
    p = AnbotPayload(_cfg(master.url))
    await p.start()
    sub = await _接上(master, p, STROBE)
    await p.set("strobe", True, 60)
    await _等(lambda: True in sub.bools())
    await p.set("strobe", False, 0)
    await _等(lambda: sub.bools()[-1] is False)
    await p.set("strobe", True, 60)
    await _等(lambda: sub.bools()[-1] is True)
    await p.close()
    await _等(lambda: sub.bools()[-1] is False)
    assert STROBE in master.unregisters and VOICE in master.unregisters
    sub.close()


async def test_喊话_发文字_按字数估念完回调_录好的话术不认(master):
    ends: list[tuple[bool, str]] = []
    p = AnbotPayload(_cfg(master.url, secs_per_char=0.02))
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    await p.start()
    sub = await _接上(master, p, VOICE)
    assert sub.header["type"] == "std_msgs/String" and sub.header["md5sum"] == STRING_MD5
    assert sub.header.get("latching", "0") == "0", "喊话不能 latching:竞品重连会把上一句再念一遍"
    t0 = time.monotonic()
    await p.sound("tts:zh:请注意，这里是巡逻机器人", 30)
    await _等(lambda: sub.texts() == ["请注意，这里是巡逻机器人"])
    await _等(lambda: ends)
    assert ends == [(True, "放完了(按字数估)")]
    assert time.monotonic() - t0 >= 12 * 0.02 - 0.02
    with pytest.raises(PayloadError, match="只能念文字"):
        await p.sound("warn_1", 30)
    with pytest.raises(PayloadError):
        await p.sound("tts:zh:", 30)
    await p.close()
    sub.close()


async def test_喊话被关_不回调(master):
    ends: list = []
    p = AnbotPayload(_cfg(master.url, secs_per_char=0.5))
    p.on_sound_end = lambda ok, why: ends.append(why)
    await p.start()
    sub = await _接上(master, p, VOICE)
    await p.sound("tts:zh:请让一让", 30)
    await p.stop_sound()
    await asyncio.sleep(0.2)
    assert ends == [], "被关的不回调(同继电器上装)"
    await p.close()
    sub.close()


async def test_竞品主控没开_起得来不卡_喊话当场报错_警灯报故障_开了自己注册上():
    # 先占一个端口再放掉:那里没人听
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = AnbotPayload(_cfg(f"http://127.0.0.1:{port}/", reregister_s=0.1))
    t0 = time.monotonic()
    await p.start()
    assert time.monotonic() - t0 < 1.0, "主控没开不能把代理卡住"
    with pytest.raises(PayloadError, match="竞品主控"):
        await p.sound("tts:zh:请注意", 30)
    await p.set("strobe", True, 60)
    assert "strobe" in p.faults() and "竞品主控" in p.faults()["strobe"]
    h = {d["id"]: d for d in p.health()}
    assert h["link:anbot"]["state"] == "offline"
    # 主控起来了(同一个端口)
    m = 假Master.__new__(假Master)
    m.srv = SimpleXMLRPCServer(("127.0.0.1", port), logRequests=False, allow_none=True)
    m.pubs, m.registers, m.unregisters = {}, 0, []
    m.srv.register_function(m._reg, "registerPublisher")
    m.srv.register_function(m._unreg, "unregisterPublisher")
    threading.Thread(target=m.srv.serve_forever, daemon=True).start()
    try:
        sub = await _接上(m, p, STROBE)
        await _等(lambda: True in sub.bools())
        await _等(lambda: p.faults() == {})
        assert {d["id"]: d for d in p.health()}["link:anbot"]["state"] == "degraded", "播音没连上"
        voice = await _接上(m, p, VOICE)
        assert {d["id"]: d for d in p.health()}["link:anbot"]["state"] == "online"
        await p.close()
        sub.close()
        voice.close()
    finally:
        m.close()


async def test_竞品主控重启_注册丢了_定时重新注册(master):
    p = AnbotPayload(_cfg(master.url, reregister_s=0.1))
    await p.start()
    await _等(lambda: STROBE in master.pubs)
    n = master.registers
    master.pubs.clear()
    await _等(lambda: STROBE in master.pubs and master.registers > n)
    await p.close()


async def test_参数不对当场报错(master):
    p = AnbotPayload(_cfg(master.url))
    with pytest.raises(PayloadError):
        await p.set("strobe", True, 0)
    with pytest.raises(PayloadError):
        await p.set("strobe", True, 601)
    with pytest.raises(PayloadError):
        await p.set("spotlight", True, 10)
    with pytest.raises(PayloadError):
        await p.sound("tts:zh:" + "啊" * 301, 30)


def test_按kind选上装_竞品的读得出_老的照旧(tmp_path):
    a = tmp_path / "anbot.json"
    a.write_text(json.dumps({"kind": "anbot_ros1", "master": "http://192.168.113.203:11311",
                             "host": "192.168.113.159"}))
    p = load_payload(a)
    assert isinstance(p, AnbotPayload)
    assert p.cfg.strobe_topic == STROBE and p.cfg.voice_topic == VOICE
    assert p.cfg.secs_per_char == 0.25
    old = tmp_path / "relay.json"
    old.write_text(json.dumps({"audio": {"device": "plughw:1,0"}}))
    assert isinstance(load_payload(old), Payload)
    for bad in ({"kind": "anbot_ros1"}, {"kind": "anbot_ros1", "master": "x", "host": "h"},
                {"kind": "anbot_ros1", "master": "http://a:1", "host": "h", "secs_per_char": 0},
                {"kind": "什么"}):
        (tmp_path / "bad.json").write_text(json.dumps(bad))
        with pytest.raises(PayloadError):
            load_payload(tmp_path / "bad.json")


async def test_接到HAL上_能力里有警灯喇叭能TTS_没有录好的话术(master, tmp_path, monkeypatch):
    from nexwar_adapter_d1max.hal import D1MaxHal
    monkeypatch.chdir(tmp_path)
    (tmp_path / "stray.wav").write_bytes(b"x")       # 当前目录里有 wav 也不算话术
    p = AnbotPayload(_cfg(master.url))
    hal = D1MaxHal(mps_per_unit=0.4, radps_per_unit=1.0, deadband_mps=0.05, max_fraction=0.5,
                   payload=p)
    acts = hal.hal_capabilities().actuators
    assert acts["strobe"] and acts["speaker"] and not acts["siren"] and not acts["spotlight"]
    assert hal.sound_tts() and hal.sound_clips() == ()
