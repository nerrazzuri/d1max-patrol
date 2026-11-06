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
    # 11-05 现场:主控连不上时开警灯当场拒(原来收下、事后才报故障,保安以为开了)
    with pytest.raises(PayloadError, match="竞品主控连不上"):
        await p.set("strobe", True, 60)
    assert p.faults() == {}
    await p.set("strobe", False, 0)                     # 关照发,不拦
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
        await p.set("strobe", True, 60)
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


def test_现场1105_代理和HAL用到的上装属性_竞品转接都有():
    """11-05 现场:代理入口读 ``payload.cfg.inputs_reg``(继电器板输入,W56 风扇报警),竞品转接没有,
    代理起不来。把代理入口、HAL 里所有 ``payload.X`` / ``payload.cfg.Y`` 拿出来逐个对。"""
    import re
    from pathlib import Path

    import nexwar_adapter_d1max.hal as hal_mod
    root = Path(hal_mod.__file__).resolve().parents[4]
    main = root / "packages/robot-agent/src/nexwar_agent/main.py"
    used: set[tuple[str, ...]] = set()
    for f, pat in ((main, r"(?<![\w./])payload\.([a-z_]+)(?:\.([a-z_]+))?"),
                   (Path(hal_mod.__file__), r"\b_payload\.([a-z_]+)(?:\.([a-z_]+))?")):
        for m in re.finditer(pat, f.read_text(encoding="utf-8")):
            used.add(tuple(x for x in m.groups() if x))
    assert ("cfg", "inputs_reg") in used, "找属性的正则还管用"
    p = AnbotPayload(AnbotConfig(master="http://127.0.0.1:1/", host="127.0.0.1"))
    for path in sorted(used):
        obj: object = p
        for name in path:
            assert hasattr(obj, name), f"竞品转接缺 payload.{'.'.join(path)}"
            obj = getattr(obj, name)
    assert p.cfg.inputs_reg < 0 and p.input_bits() is None, "没有继电器板输入:不接风扇报警"


# ---------------------------------------------------------------- 11-05 下午:英文话术走 SSH + aplay


def _假ssh(tmp_path, *, rc: int = 0, sleep: float = 0.0):
    """假 ssh:把参数、收到的 wav 字节记下来;``sleep`` 模拟播放时长。"""
    log = tmp_path / "ssh.log"
    got = tmp_path / "ssh.wav"
    s = tmp_path / "ssh"
    s.write_text("#!/bin/sh\n"
                 f'echo "$@" >> "{log}"\n'
                 f'cat > "{got}"\n'
                 f"sleep {sleep}\n"
                 f"exit {rc}\n")
    s.chmod(0o755)
    return s, log, got


def _clips(tmp_path):
    d = tmp_path / "clips"
    d.mkdir(exist_ok=True)
    for n in ("blocked", "passing", "distance", "restricted"):
        (d / f"{n}.wav").write_bytes(b"RIFF" + n.encode())
    return d


async def test_录好的话术_ssh到竞品主控用aplay放_wav从管道送过去(master, tmp_path):
    ssh, log, got = _假ssh(tmp_path, sleep=0.1)
    ends: list = []
    p = AnbotPayload(_cfg(master.url, clips_dir=str(_clips(tmp_path)), ssh=str(ssh),
                          audio_host="firefly@192.168.113.101",
                          ssh_key="/home/robot/.nexwar/anbot_ssh"))
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    await p.sound("blocked", 30)
    await _等(lambda: ends)
    assert ends == [(True, "放完了")]
    assert got.read_bytes() == b"RIFFblocked"
    args = log.read_text()
    assert "-i /home/robot/.nexwar/anbot_ssh" in args and "BatchMode=yes" in args
    assert "firefly@192.168.113.101 aplay -q -D dmix_oneway_play -" in args
    with pytest.raises(PayloadError, match="没有这段话术"):
        await p.sound("closing", 30)
    with pytest.raises(PayloadError):
        await p.sound("../etc/passwd", 30)
    await p.close()


async def test_录好的话术_关喇叭真掐断_不回调_到点也掐(master, tmp_path):
    ssh, _, _ = _假ssh(tmp_path, sleep=5)
    ends: list = []
    p = AnbotPayload(_cfg(master.url, clips_dir=str(_clips(tmp_path)), ssh=str(ssh),
                          audio_host="firefly@h"))
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    await p.sound("passing", 30)
    assert p.playing()
    await p.stop_sound()
    assert not p.playing() and ends == []
    t0 = time.monotonic()
    await p.sound("passing", 0.3)
    await _等(lambda: ends)
    assert ends == [(True, "到点了")] and time.monotonic() - t0 < 2
    await p.close()


async def test_录好的话术_ssh失败_回调失败_说清楚(master, tmp_path):
    ssh, _, _ = _假ssh(tmp_path, rc=255)
    ends: list = []
    p = AnbotPayload(_cfg(master.url, clips_dir=str(_clips(tmp_path)), ssh=str(ssh),
                          audio_host="firefly@h"))
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    await p.sound("distance", 30)
    await _等(lambda: ends)
    assert ends[0][0] is False and "255" in ends[0][1]


async def test_没配audio_host_录好的话术不报_只能念文字(master, tmp_path):
    from nexwar_adapter_d1max.hal import D1MaxHal
    p = AnbotPayload(_cfg(master.url, clips_dir=str(_clips(tmp_path))))
    hal = D1MaxHal(mps_per_unit=0.4, radps_per_unit=1.0, deadband_mps=0.05, max_fraction=0.5,
                   payload=p)
    assert hal.sound_clips() == ()
    with pytest.raises(PayloadError, match="只能念文字"):
        await p.sound("blocked", 30)
    p2 = AnbotPayload(_cfg(master.url, clips_dir=str(_clips(tmp_path)), audio_host="firefly@h"))
    hal2 = D1MaxHal(mps_per_unit=0.4, radps_per_unit=1.0, deadband_mps=0.05, max_fraction=0.5,
                    payload=p2)
    assert hal2.sound_clips() == ("blocked", "distance", "passing", "restricted")


def test_配置_audio_host_ssh_key(tmp_path):
    a = tmp_path / "anbot.json"
    a.write_text(json.dumps({"kind": "anbot_ros1", "master": "http://192.168.113.203:11311",
                             "host": "192.168.113.159", "audio_host": "firefly@192.168.113.101",
                             "clips_dir": "/var/lib/nexwar/clips"}))
    p = load_payload(a)
    assert p.cfg.audio_host == "firefly@192.168.113.101"
    assert p.cfg.ssh_key == "/home/robot/.nexwar/anbot_ssh"
    assert p.cfg.aplay_device == "dmix_oneway_play"
    for bad in ({"audio_host": "-oProxyCommand=x"}, {"audio_host": "a b"},
                {"aplay_device": "x;rm"}):
        (tmp_path / "b.json").write_text(json.dumps(
            {"kind": "anbot_ros1", "master": "http://a:1", "host": "h"} | bad))
        with pytest.raises(PayloadError):
            load_payload(tmp_path / "b.json")


# ---------------------------------------------------------------- 11-05:用户定不改竞品,密码登录


async def test_密码登录_走SSH_ASKPASS_密码只在环境变量里_不进命令行和日志(master, tmp_path, caplog):
    """竞品 /home/firefly 是 777,sshd 不认钥匙;改它就是改竞品,用户不同意。密码在 /etc/nexwar/env
    (systemd 交给代理),ssh 从 SSH_ASKPASS 要;Orin 上没有 sshpass,不用装。"""
    log = tmp_path / "ssh.log"
    pw_seen = tmp_path / "pw"
    s = tmp_path / "ssh"
    s.write_text("#!/bin/sh\n"
                 f'echo "$@" >> "{log}"\n'
                 f'echo "REQ=$SSH_ASKPASS_REQUIRE" >> "{log}"\n'
                 f'"$SSH_ASKPASS" "firefly@h\'s password: " > "{pw_seen}"\n'
                 "cat > /dev/null\n")
    s.chmod(0o755)
    env = {"NEXWAR_ANBOT_SSH_HOST": "192.168.113.101", "NEXWAR_ANBOT_SSH_USER": "firefly",
           "NEXWAR_ANBOT_SSH_PASSWORD": "pa ss'w$rd"}
    a = tmp_path / "anbot.json"
    a.write_text(json.dumps({"kind": "anbot_ros1", "master": master.url, "host": "127.0.0.1",
                             "clips_dir": str(_clips(tmp_path)), "ssh": str(s)}))
    caplog.set_level("DEBUG")
    p = load_payload(a, env=env)
    assert p.cfg.audio_host == "firefly@192.168.113.101"
    ends: list = []
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    await p.sound("restricted", 30)
    await _等(lambda: ends)
    assert ends == [(True, "放完了")]
    assert pw_seen.read_text() == "pa ss'w$rd\n"
    args = log.read_text()
    assert "pa ss" not in args and "BatchMode=yes" not in args
    assert "REQ=force" in args and "PubkeyAuthentication=no" in args
    assert "pa ss" not in caplog.text and "pa ss" not in repr(p.cfg)
    await p.close()


def test_密码登录_env里只配了一半_说清楚(tmp_path):
    a = tmp_path / "anbot.json"
    a.write_text(json.dumps({"kind": "anbot_ros1", "master": "http://a:1", "host": "h"}))
    with pytest.raises(PayloadError, match="NEXWAR_ANBOT_SSH_PASSWORD"):
        load_payload(a, env={"NEXWAR_ANBOT_SSH_HOST": "1.2.3.4", "NEXWAR_ANBOT_SSH_USER": "f"})
    p = load_payload(a, env={})
    assert p.cfg.audio_host == "" and p.cfg.clips_dir == ""


def test_POC1105_钟不对时代理没起来_单独放一段话术_放完退出(tmp_path):
    """开机就绪播报的 ``not_ready_time``:``nexwar_wait_clock`` 等钟时调它(代理还没起来)。"""
    import json as _json

    from nexwar_adapter_d1max.say import main
    ssh, log, got = _假ssh(tmp_path, sleep=0.05)
    clips = _clips(tmp_path)
    (clips / "not_ready_time.wav").write_bytes(b"RIFFtime")
    cfg = tmp_path / "payload.json"
    cfg.write_text(_json.dumps({"kind": "anbot_ros1", "master": "http://127.0.0.1:9/",
                                "host": "127.0.0.1", "clips_dir": str(clips), "ssh": str(ssh),
                                "audio_host": "firefly@192.168.113.101",
                                "ssh_key": "/home/robot/.nexwar/anbot_ssh"}))
    assert main(["not_ready_time"], env={"NEXWAR_PAYLOAD": str(cfg)}) == 0
    assert got.read_bytes() == b"RIFFtime"
    log.unlink()
    assert main(["no_such_clip"], env={"NEXWAR_PAYLOAD": str(cfg)}) == 0, "没有这段:不放、不算错"
    assert main(["ready"], env={"NEXWAR_PAYLOAD": str(tmp_path / "none.json")}) == 0, "没装上装"
    assert not log.exists()


# ------------------------------------------------ POC 11-06:云台、雨刷、求助和急停按钮

PTZ = "/anbot/ptz/pose/set"
WIPER = "/anbot/ptz/wiper/enable"


async def test_POC1106_云台_按Float32MultiArray发水平俯仰_雨刷开了到点关(master):
    from nexwar_adapter_d1max.anbot import F32MA_MD5
    p = AnbotPayload(_cfg(master.url))
    assert p.has("ptz") and p.has("wiper")
    await p.start()
    await _等(lambda: PTZ in master.pubs and WIPER in master.pubs)
    ptz = 假订阅端(master.pubs[PTZ], PTZ, "std_msgs/Float32MultiArray", F32MA_MD5)
    wip = 假订阅端(master.pubs[WIPER], WIPER, "std_msgs/Bool", BOOL_MD5)
    await _等(lambda: p.connections(PTZ) >= 1 and p.connections(WIPER) >= 1)
    assert ptz.header["type"] == "std_msgs/Float32MultiArray" and ptz.header["md5sum"] == F32MA_MD5
    await p.aim(30.0, -5.5)
    await _等(lambda: ptz.msgs)
    m = ptz.msgs[-1]
    assert struct.unpack("<III", m[:12]) == (0, 0, 2), "布局空、data_offset 0、两个数"
    assert struct.unpack("<2f", m[12:20]) == pytest.approx((30.0, -5.5))
    await p.set("wiper", True, 0.3)
    await _等(lambda: wip.bools() and wip.bools()[-1] is True)
    await _等(lambda: wip.bools()[-1] is False, 2.0)
    await p.close()
    ptz.close()
    wip.close()


async def test_POC1106_云台没连上_当场报错(master):
    p = AnbotPayload(_cfg(master.url))
    await p.start()
    await _等(lambda: PTZ in master.pubs)
    with pytest.raises(PayloadError, match="云台"):
        await p.aim(10.0, 0.0)
    await p.close()


def _假tail(tmp_path, lines: list[str]):
    """假 ssh:不管参数,把这些行吐出来(像 tail -F 看到的),再挂着不退。"""
    s = tmp_path / "ssh-tail"
    body = "".join(f"printf '%s\\n' '{ln}'\n" for ln in lines)
    s.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path / "tail.args"}"\n{body}sleep 30\n')
    s.chmod(0o755)
    return s


async def test_POC1106_求助和急停按钮_登录竞品主控盯日志_按下各报一次_松开不报(master, tmp_path):
    ssh = _假tail(tmp_path, [
        "[2026-11-05 16:54:01] [gpio] [button] [sos] receive a trigger event with 59",
        "[2026-11-05 16:54:01] [gpio] [button] [sos] receive a trigger event with 59",
        "led_control: Emergency btn status change: 0",
        "led_control: Emergency btn status change: 1",
        "led_control: Pub emergency cmd: 0",
    ])
    got: list[str] = []
    p = AnbotPayload(_cfg(master.url, audio_host="anbot@192.168.113.101", ssh=str(ssh)))
    p.on_button = got.append
    await p.start()
    await _等(lambda: len(got) >= 2)
    await asyncio.sleep(0.2)
    assert got == ["sos", "estop"], "同一秒里重复的那一行不再报;松开、Pub 那几行不报"
    args = (tmp_path / "tail.args").read_text()
    assert "tail" in args and "/logs/current/can.log" in args and "anbot@192.168.113.101" in args
    await p.close()


async def test_POC1106_没配登录竞品主控_不盯按钮(master, tmp_path):
    p = AnbotPayload(_cfg(master.url))
    p.on_button = lambda b: None
    await p.start()
    assert p._buttons_task is None
    await p.close()


async def test_POC1106_接到HAL上_能力里有云台雨刷_按钮回调_急停先当场停(master, tmp_path):
    from nexwar_adapter_d1max.hal import D1MaxHal
    p = AnbotPayload(_cfg(master.url))
    hal = D1MaxHal(mps_per_unit=0.4, radps_per_unit=1.0, deadband_mps=0.05, max_fraction=0.5,
                   payload=p)
    acts = hal.hal_capabilities().actuators
    assert acts["wiper"] and acts["ptz"]
    calls: list = []

    async def aim(pan, tilt):
        calls.append(("aim", pan, tilt))

    async def setp(out, on, max_s, **kw):
        calls.append(("set", out, on, max_s))

    async def stop():
        calls.append(("stop",))
    p.aim, p.set, hal.stop = aim, setp, stop
    await hal.ptz(30.0, -5.0)
    await hal.wiper(True, 10.0)
    await hal.wiper(False, 0.0)
    assert calls == [("aim", 30.0, -5.0), ("set", "wiper", True, 10.0),
                     ("set", "wiper", False, 0.0)]
    got: list = []
    hal.set_button_listener(got.append)
    p.on_button("sos")
    p.on_button("estop")
    await asyncio.sleep(0.05)
    assert got == ["sos", "estop"]
    assert ("stop",) in calls, "急停按钮:HAL 先当场停,站点叫停随后到"


async def test_POC1106_继电器上装没有云台雨刷():
    from nexwar_adapter_d1max.hal import D1MaxHal
    from nexwar_contract.hal import HalUnsupported
    hal = D1MaxHal(mps_per_unit=0.4, radps_per_unit=1.0, deadband_mps=0.05, max_fraction=0.5)
    acts = hal.hal_capabilities().actuators
    assert not acts["wiper"] and not acts["ptz"]
    with pytest.raises(HalUnsupported):
        await hal.ptz(0.0, 0.0)
    hal.set_button_listener(lambda b: None)            # 没上装:不报错


async def test_POC1106_开机调竞品音量_配了才调_登录竞品主控跑amixer(master, tmp_path):
    log = tmp_path / "ssh.log"
    s = tmp_path / "ssh-vol"
    s.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    s.chmod(0o755)
    p = AnbotPayload(_cfg(master.url, audio_host="anbot@192.168.113.101", ssh=str(s),
                          volume_control="DACL", volume_percent=70, sos_log="", estop_log=""))
    await p.start()
    await _等(lambda: log.exists() and "amixer" in log.read_text())
    assert "amixer -q sset 'DACL' 70%" in log.read_text()
    await p.close()


def test_POC1106_音量配置_控件名只认字母数字_百分比在范围里():
    for bad in ({"volume_control": "DACL; rm -rf /"},
                {"volume_control": "DACL", "volume_percent": 150},
                {"volume_control": "DACL", "volume_card": "0 && x"}):
        with pytest.raises(PayloadError):
            AnbotConfig.from_dict({"master": "http://127.0.0.1:1/", "host": "127.0.0.1"} | bad)
    c = AnbotConfig.from_dict({"master": "http://127.0.0.1:1/", "host": "127.0.0.1",
                               "volume_control": "Speaker Playback", "volume_card": "1"})
    assert c.volume_percent == 70
