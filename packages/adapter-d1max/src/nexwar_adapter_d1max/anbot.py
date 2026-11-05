"""POC 上装转接(11-05,用户批准):V2 上装到之前,借竞品上装的警灯和喇叭。

竞品主控上跑 ROS1(master 一般在 ``http://192.168.113.203:11311``),Orin 跟它同一个网段、网线直连。
Orin 上不装 ROS1:这里用标准库实现最小的 ROS1 发布端(XML-RPC 注册 + TCPROS 握手),现场 11-05 验过:

- 警灯 ``/sensor/strobeLightSwitch``(std_msgs/Bool,订阅端 led_control):两边爆闪一起亮灭。
- 喊话 ``/anbot/voice/play``(std_msgs/String,订阅端 anbotControl):竞品自己把文字念出来。

跟 W21 继电器上装(``payload.py``)一样的入口,HAL、代理、站点、网页、手机都不用改。区别:

- **警灯**:竞品自己的程序也发这个话题,可能把我们的盖掉 —— 开着的时候每 ``republish_s`` 秒重发一次;
  到点、关、代理收尾都发一次「关」,关着就不再发(不跟竞品自己的用法抢)。latching:竞品灯控程序重启了
  也拿得到现在该亮还是灭。
- **喊话**:只认 ``tts:<语言>:<文字>``(竞品只收文字,不放录好的话术;语言不看,竞品念中文)。
  **不 latching**:不然竞品重连会把上一句再念一遍。**念到一半停不下来**(没找到停播的话题):
  「关喇叭」只是不回调、不再发新的。念完没有回执,按字数估(``secs_per_char``,现场 12 字约 3 秒)。
  没连上竞品(主控没开、订阅端没连上)当场报错,不让站点以为已经喊了。
- 没有警笛、聚光灯;云台、``/anbot/light/control`` 没接(取值没验过)。
- **风险**(写进 POC 说明):竞品主控得一直开着,它的 dog_control 能抢运动控制(10-19 撞玻璃门)。

配置(``/etc/nexwar/payload.json``)::

    {"kind": "anbot_ros1", "master": "http://192.168.113.203:11311", "host": "192.168.113.159"}

``host`` 是 Orin 在那个网段上的地址:竞品要按它连回来(XML-RPC 与 TCPROS 都只开在这个地址上)。
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import logging
import os
import re
import signal
import socket
import struct
import tempfile
import threading
import time
import xmlrpc.client
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

from nexwar_adapter_d1max.payload import (
    MAX_ON_S,
    MAX_TTS_CHARS,
    Payload,
    PayloadConfig,
    PayloadError,
)

log = logging.getLogger(__name__)

BOOL_MD5 = "8b94c1b53db61fb6aed406028ad6332a"
STRING_MD5 = "992ce8a1687cec8c8bd883ec73ca41d1"
KIND = "anbot_ros1"
HOST_ENV = "NEXWAR_ANBOT_SSH_HOST"
USER_ENV = "NEXWAR_ANBOT_SSH_USER"
PASSWORD_ENV = "NEXWAR_ANBOT_SSH_PASSWORD"
_HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}@[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DEV = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:,.-]{0,63}")
_CLIP = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}")


# ---------------------------------------------------------------- 最小 ROS1 发布端


class _Transport(xmlrpc.client.Transport):
    """带超时的 XML-RPC:竞品主控没开、网线拔了,不能把调用的线程卡死。"""

    def __init__(self, timeout_s: float) -> None:
        super().__init__()
        self._timeout = timeout_s

    def make_connection(self, host: Any) -> http.client.HTTPConnection:
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _header(fields: dict[str, str]) -> bytes:
    body = b"".join(struct.pack("<I", len(f)) + f
                    for f in (f"{k}={v}".encode() for k, v in fields.items()))
    return struct.pack("<I", len(body)) + body


def _recv_exact(s: socket.socket, n: int) -> bytes:
    b = b""
    while len(b) < n:
        d = s.recv(n - len(b))
        if not d:
            raise ConnectionError("对方关了连接")
        b += d
    return b


class _QuietHandler(SimpleXMLRPCRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class Ros1Publisher:
    """一个话题的发布端:XML-RPC 节点接口(竞品的订阅端来问 TCPROS 地址)+ TCPROS 监听。
    方法都是阻塞的(套接字带超时),异步那头用 ``asyncio.to_thread`` 调。"""

    def __init__(self, *, master: str, host: str, caller: str, topic: str, mtype: str,
                 md5: str, definition: str, latching: bool, timeout_s: float = 1.0) -> None:
        self.master, self.host, self.caller, self.topic = master, host, caller, topic
        self.mtype, self.md5, self.definition = mtype, md5, definition
        self.latching, self.timeout_s = latching, timeout_s
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._last: bytes | None = None
        self._tcp: socket.socket | None = None
        self._rpc: SimpleXMLRPCServer | None = None
        self.api = ""
        self.registered = False
        self.error = "还没注册"

    # 起、停

    def open(self) -> None:
        """开 TCPROS 监听和 XML-RPC 节点接口(只开在 ``host`` 上)。
        开不了抛 OSError(网口还没起来)。"""
        if self._rpc is not None:
            return
        tcp = socket.socket()
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            tcp.bind((self.host, 0))
            tcp.listen(8)
            rpc = SimpleXMLRPCServer((self.host, 0), requestHandler=_QuietHandler,
                                     logRequests=False, allow_none=True)
        except OSError:
            tcp.close()
            raise
        port = tcp.getsockname()[1]
        rpc.register_function(
            lambda c, t, protos: [1, "", ["TCPROS", self.host, port]]
            if t == self.topic and any(p and p[0] == "TCPROS" for p in protos)
            else [0, "只有 TCPROS", []], "requestTopic")
        rpc.register_function(lambda c: [1, "", 0], "getPid")
        rpc.register_function(lambda c: [1, "", []], "getBusInfo")
        rpc.register_function(lambda c: [1, "", self.master], "getMasterUri")
        rpc.register_function(lambda c, t, p: [1, "", 0], "publisherUpdate")
        rpc.register_function(lambda c, k, v: [1, "", 0], "paramUpdate")
        rpc.register_function(lambda c, m="": [1, "", 0], "shutdown")
        self._tcp, self._rpc = tcp, rpc
        self.api = f"http://{self.host}:{rpc.server_address[1]}/"
        threading.Thread(target=self._accept, daemon=True, name=f"ros1-{self.topic}").start()
        threading.Thread(target=rpc.serve_forever, daemon=True).start()

    def _proxy(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(self.master, transport=_Transport(self.timeout_s))

    def register(self) -> bool:
        """向 master 注册(可以重复:竞品主控重启以后注册就丢了,定时再来一次)。"""
        try:
            code, msg, _ = self._proxy().registerPublisher(self.caller, self.topic, self.mtype,
                                                           self.api)
        except (OSError, xmlrpc.client.Error, ValueError) as exc:
            self.registered, self.error = False, f"连不上竞品主控 {self.master}: {exc}"
            return False
        self.registered = code == 1
        self.error = "" if self.registered else f"竞品主控拒绝注册: {msg}"
        return self.registered

    def close(self) -> None:
        if self.registered:
            with contextlib.suppress(OSError, xmlrpc.client.Error, ValueError):
                self._proxy().unregisterPublisher(self.caller, self.topic, self.api)
        self.registered = False
        rpc, tcp, self._rpc, self._tcp = self._rpc, self._tcp, None, None
        if rpc is not None:
            rpc.shutdown()
            rpc.server_close()
        if tcp is not None:
            with contextlib.suppress(OSError):
                tcp.shutdown(socket.SHUT_RDWR)
            tcp.close()
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            c.close()

    # 订阅端

    def _accept(self) -> None:
        tcp = self._tcp
        while tcp is not None and self._tcp is tcp:
            try:
                c, _ = tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._handshake, args=(c,), daemon=True).start()

    def _handshake(self, c: socket.socket) -> None:
        try:
            c.settimeout(self.timeout_s * 2)
            _recv_exact(c, struct.unpack("<I", _recv_exact(c, 4))[0])     # 订阅端的头不看
            c.sendall(_header({"callerid": self.caller, "topic": self.topic, "type": self.mtype,
                               "md5sum": self.md5, "message_definition": self.definition,
                               "latching": "1" if self.latching else "0"}))
            c.settimeout(self.timeout_s)
            with self._lock:
                if self.latching and self._last is not None:
                    c.sendall(self._last)
                self._conns.append(c)
        except OSError:
            c.close()

    def connections(self) -> int:
        with self._lock:
            return len(self._conns)

    def publish(self, msg: bytes) -> int:
        """发给所有连着的订阅端;发不出去的连接扔掉。→ 发到了几个。"""
        frame = struct.pack("<I", len(msg)) + msg
        with self._lock:
            if self.latching:
                self._last = frame
            ok = []
            for c in self._conns:
                try:
                    c.sendall(frame)
                    ok.append(c)
                except OSError:
                    c.close()
            self._conns = ok
            return len(ok)


def bool_msg(v: bool) -> bytes:
    return b"\x01" if v else b"\x00"


def string_msg(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


# ---------------------------------------------------------------- 上装


@dataclass(frozen=True)
class AnbotConfig:
    master: str
    host: str
    strobe_topic: str = "/sensor/strobeLightSwitch"
    voice_topic: str = "/anbot/voice/play"
    caller: str = "/nexwar_agent"
    #: 念一个字大约多久(现场 11-05:12 个字约 3 秒)。
    secs_per_char: float = 0.25
    #: 警灯开着多久重发一次(竞品自己的程序可能发「关」把我们盖掉)。
    republish_s: float = 1.0
    #: 多久重新注册一次(竞品主控重启了注册就丢了)。
    reregister_s: float = 10.0
    #: 录好的话术(英文)放在哪台竞品主控上:``账号@地址``(11-05 用户批准 POC 阶段登录竞品主控放音频)。
    #: 空 = 不放录好的话术,只念文字。登录用 SSH 密钥(``ssh_key``),密码不存在我们这边任何地方。
    audio_host: str = ""
    ssh_key: str = "/home/robot/.nexwar/anbot_ssh"
    #: 竞品 /etc/asound.conf 里跟它自己的讯飞 TTS 同一条的 dmix。
    aplay_device: str = "dmix_oneway_play"
    ssh: str = "ssh"
    # HAL 看这两个:能 TTS;录好的话术在哪(没配 audio_host 就当没有)
    tts_command: str = "anbot"
    clips_dir: str = "/var/lib/nexwar/clips"
    #: 代理入口看这个:没有继电器板输入(W56 风扇报警不接)
    inputs_reg: int = -1

    def __post_init__(self) -> None:
        u = urlsplit(self.master)
        if u.scheme != "http" or not u.hostname:
            raise PayloadError(f"master 要写成 http://<地址>:<端口>: {self.master!r}")
        if not self.host or not isinstance(self.host, str):
            raise PayloadError("host 要写 Orin 在竞品网段上的地址")
        for t in (self.strobe_topic, self.voice_topic, self.caller):
            if not isinstance(t, str) or not t.startswith("/"):
                raise PayloadError(f"话题、节点名要以 / 开头: {t!r}")
        for k in ("secs_per_char", "republish_s", "reregister_s"):
            v = getattr(self, k)
            if not isinstance(v, (int, float)) or not 0 < v <= 60:
                raise PayloadError(f"{k} 要在 0–60 之间: {v!r}")
        if self.audio_host and not _HOST.fullmatch(str(self.audio_host)):
            raise PayloadError(f"audio_host 要写成 账号@地址: {self.audio_host!r}")
        if not _DEV.fullmatch(str(self.aplay_device)):
            raise PayloadError(f"aplay_device 只许字母、数字、_ : , . -: {self.aplay_device!r}")
        for k in ("ssh_key", "ssh", "clips_dir"):
            v = getattr(self, k)
            if not isinstance(v, str) or any(ch.isspace() for ch in v) or v.startswith("-"):
                raise PayloadError(f"{k} 写错了: {v!r}")
        if not self.audio_host:
            object.__setattr__(self, "clips_dir", "")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnbotConfig:
        known = {"master", "host", "strobe_topic", "voice_topic", "caller", "secs_per_char",
                 "republish_s", "reregister_s", "audio_host", "ssh_key", "aplay_device",
                 "clips_dir", "ssh"}
        extra = set(d) - known - {"kind"}
        if extra:
            raise PayloadError(f"竞品上装配置里有不认识的项: {', '.join(sorted(extra))}")
        if "master" not in d or "host" not in d:
            raise PayloadError("竞品上装配置要有 master 和 host")
        try:
            return cls(**{k: d[k] for k in known if k in d})
        except TypeError as exc:
            raise PayloadError(f"竞品上装配置写错了: {exc}") from exc


class AnbotPayload:
    """竞品上装的异步入口(HAL 调它,跟 :class:`Payload` 同一套方法)。"""

    def __init__(self, cfg: AnbotConfig, *, password: str = "") -> None:
        self.cfg = cfg
        #: 登录竞品主控的密码(11-05 用户定:不改竞品,POC 存密码)。只放内存、只经环境变量交给
        #: SSH_ASKPASS;不进命令行、不进日志、不进 ``cfg``(repr 不带)。空 = 用 ``ssh_key``。
        self._password = password
        self._askpass: Path | None = None
        self.on_sound_end: Callable[[bool, str], Any] | None = None
        self._strobe = Ros1Publisher(master=cfg.master, host=cfg.host, caller=cfg.caller,
                                     topic=cfg.strobe_topic, mtype="std_msgs/Bool",
                                     md5=BOOL_MD5, definition="bool data\n", latching=True)
        self._voice = Ros1Publisher(master=cfg.master, host=cfg.host, caller=cfg.caller,
                                    topic=cfg.voice_topic, mtype="std_msgs/String",
                                    md5=STRING_MD5, definition="string data\n", latching=False)
        self._pubs = (self._strobe, self._voice)
        self._until: float | None = None              # 警灯开着:到这个时刻(monotonic)
        self._guard: asyncio.Task[None] | None = None
        self._speech: asyncio.Task[None] | None = None
        self._open_error = ""
        self._next_register = 0.0

    # 能力

    def has(self, output: str) -> bool:
        return output in ("strobe", "speaker")

    def options(self) -> dict[str, Any]:
        return {}

    def input_bits(self) -> int | None:
        return None

    def connections(self, topic: str) -> int:
        return sum(p.connections() for p in self._pubs if p.topic == topic)

    # 起、停

    async def start(self) -> None:
        """开监听、注册、先发一次「关」;看护循环接着重发、到点关、定时重新注册。不等竞品主控。"""
        self._until = None
        await self._publish_strobe(False)
        if self._guard is None or self._guard.done():
            self._guard = asyncio.get_running_loop().create_task(self._watch())

    async def _ensure(self) -> None:
        now = time.monotonic()
        if now < self._next_register:
            return
        self._next_register = now + self.cfg.reregister_s
        try:
            for p in self._pubs:
                await asyncio.to_thread(p.open)
            self._open_error = ""
        except OSError as exc:
            self._open_error = f"Orin 上 {self.cfg.host} 开不了端口(网口没起来?): {exc}"
            return
        for p in self._pubs:
            await asyncio.to_thread(p.register)

    async def _watch(self) -> None:
        while True:
            try:
                await self._ensure()
                if self._until is not None:
                    if time.monotonic() >= self._until:
                        self._until = None
                        await self._publish_strobe(False)
                    else:
                        await self._publish_strobe(True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("竞品上装看护循环出错(接着跑)")
            await asyncio.sleep(self.cfg.republish_s)

    async def close(self) -> None:
        """全关:警灯发「关」,不再估喊话,注销,关端口。"""
        t, self._guard = self._guard, None
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self.stop_sound()
        self._until = None
        try:
            await self._publish_strobe(False)
        except Exception:
            log.exception("竞品上装收尾时发不出「关警灯」")
        for p in self._pubs:
            await asyncio.to_thread(p.close)
        self._next_register = 0.0

    # 警灯

    async def _publish_strobe(self, on: bool) -> int:
        await self._ensure()
        return await asyncio.to_thread(self._strobe.publish, bool_msg(on))

    async def set(self, output: str, on: bool, max_s: float, *, side: str = "both",
                  color: str = "") -> None:
        if output != "strobe":
            raise PayloadError(f"竞品上装只有警灯和喇叭,没有 {output}")
        if not on:
            self._until = None
            await self._publish_strobe(False)
            return
        if not 0 < max_s <= MAX_ON_S:
            raise PayloadError(f"max_s 要在 0–{MAX_ON_S:.0f} 秒之间")
        self._until = time.monotonic() + max_s
        await self._publish_strobe(True)

    # 喊话

    async def sound(self, clip_or_tts: str, max_s: float) -> None:
        if not clip_or_tts.startswith("tts:"):
            await self._clip(clip_or_tts, max_s)
            return
        _, _, rest = clip_or_tts.partition(":")
        _, _, text = rest.partition(":")
        if not text.strip() or len(text) > MAX_TTS_CHARS:
            raise PayloadError(f"TTS 要写成 tts:<语言>:<文字>(文字 1–{MAX_TTS_CHARS} 字)")
        if not 0 < max_s <= MAX_ON_S:
            raise PayloadError(f"max_s 要在 0–{MAX_ON_S:.0f} 秒之间")
        await self._ensure()
        await self.stop_sound()
        if self._voice.connections() == 0:
            raise PayloadError(self._why_down(self._voice) or "竞品主控的播音程序没连上")
        if await asyncio.to_thread(self._voice.publish, string_msg(text)) == 0:
            raise PayloadError("竞品主控的播音程序断开了,没念出来")
        est = min(max_s, len(text) * self.cfg.secs_per_char)
        self._speech = asyncio.get_running_loop().create_task(self._speak_end(est))

    async def _clip(self, name: str, max_s: float) -> None:
        """录好的话术:SSH 到竞品主控跑 ``aplay``,wav 从管道送过去(文件不用拷到竞品上;关喇叭掐 ssh,
        那头 aplay 读到头就停)。放完、放坏了、到点都回调;被关的不回调。"""
        if not self.cfg.audio_host:
            raise PayloadError("竞品上装只能念文字(tts:<语言>:<文字>):"
                               "没配 audio_host,不放录好的话术")
        if not _CLIP.fullmatch(name) or name.startswith("."):
            raise PayloadError(f"话术名只许字母、数字、. _ -: {name!r}")
        path = Path(self.cfg.clips_dir) / f"{name}.wav"
        if not path.is_file():
            raise PayloadError(f"没有这段话术: {name}")
        if not 0 < max_s <= MAX_ON_S:
            raise PayloadError(f"max_s 要在 0–{MAX_ON_S:.0f} 秒之间")
        await self.stop_sound()
        common = ["-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=accept-new"]
        env = None
        if self._password:
            # 密码:ssh 没有终端(新会话)时找 SSH_ASKPASS 要;那个小脚本从环境变量里念出来
            auth = ["-o", "PubkeyAuthentication=no",
                    "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "NumberOfPasswordPrompts=1"]
            env = dict(os.environ) | {"SSH_ASKPASS": str(self._askpass_script()),
                                      "SSH_ASKPASS_REQUIRE": "force",
                                      "DISPLAY": os.environ.get("DISPLAY", ":0"),
                                      PASSWORD_ENV: self._password}
        else:
            auth = ["-i", self.cfg.ssh_key, "-o", "BatchMode=yes"]
        cmd = [self.cfg.ssh, *auth, *common, self.cfg.audio_host,
               f"aplay -q -D {self.cfg.aplay_device} -"]
        try:
            with path.open("rb") as f:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=f, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True, env=env)
        except OSError as exc:
            raise PayloadError(f"放不了录好的话术(ssh 起不来): {exc}") from exc
        self._speech = asyncio.get_running_loop().create_task(self._clip_end(proc, max_s))

    def _askpass_script(self) -> Path:
        """只念环境变量的小脚本(私有临时目录,0700)。密码本身不落盘。"""
        if self._askpass is None or not self._askpass.exists():
            d = Path(tempfile.mkdtemp(prefix="nexwar_askpass-"))
            p = d / "askpass"
            p.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "${PASSWORD_ENV}"\n', encoding="utf-8")
            p.chmod(0o700)
            self._askpass = p
        return self._askpass

    async def _clip_end(self, proc: Any, max_s: float) -> None:
        try:
            try:
                _, err = await asyncio.wait_for(proc.communicate(), max_s)
            except asyncio.TimeoutError:                # 3.10 上它不是内建的 TimeoutError
                ok, why = True, "到点了"
            else:
                rc = proc.returncode
                ok = rc == 0
                why = "放完了" if ok else (
                    f"竞品主控放不出来(ssh/aplay 退出码 {rc}):"
                    + (err or b"").decode("utf-8", "replace").strip()[:150])
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)       # 连子进程一起(管道才会关)
                with contextlib.suppress(Exception):
                    await proc.wait()
        self._speech = None
        fn = self.on_sound_end
        if fn is not None:
            fn(ok, why)

    async def _speak_end(self, est: float) -> None:
        await asyncio.sleep(est)
        self._speech = None
        fn = self.on_sound_end
        if fn is not None:
            fn(True, "放完了(按字数估)")

    async def stop_sound(self) -> None:
        """不再估这一句(竞品那头念到一半停不下来:没找到停播的话题)。被关的不回调。"""
        t, self._speech = self._speech, None
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    def playing(self) -> bool:
        return self._speech is not None and not self._speech.done()

    # 健康

    def _why_down(self, p: Ros1Publisher) -> str:
        if self._open_error:
            return self._open_error
        if not p.registered:
            return f"竞品主控连不上:{p.error}"
        if p.connections() == 0:
            return "竞品主控上的订阅端还没连上(竞品程序没起来?)"
        return ""

    def faults(self) -> dict[str, str]:
        """警灯开着却发不到竞品那头:按路报(站点出 ``payload_strobe`` 告警)。"""
        if self._until is None:
            return {}
        why = self._why_down(self._strobe)
        return {"strobe": f"警灯命令发不到竞品主控:{why}"} if why else {}

    def health(self) -> list[dict[str, Any]]:
        from nexwar_contract.devices import device
        why = self._why_down(self._strobe) or self._why_down(self._voice)
        if self._open_error or not all(p.registered for p in self._pubs):
            return [device("link:anbot", "offline", why)]
        if why:
            return [device("link:anbot", "degraded", why)]
        return [device("link:anbot", "online", "竞品主控上的警灯、播音程序都连上了")]


def load_payload(path: str | Path,
                 env: Mapping[str, str] | None = None) -> Payload | AnbotPayload:
    """按 ``kind`` 选上装:``anbot_ros1`` 是竞品转接,没写就是 W21 继电器上装。

    竞品转接放录好的话术要登录竞品主控:``/etc/nexwar/env`` 里的 ``NEXWAR_ANBOT_SSH_HOST``、
    ``NEXWAR_ANBOT_SSH_USER``、``NEXWAR_ANBOT_SSH_PASSWORD``(systemd 交给代理;
    仓库里、日志里都没有)。
    三样要么都给,要么都不给(不给 = 只念文字,或者 payload.json 里写 ``audio_host`` 用钥匙)。"""
    env = os.environ if env is None else env
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PayloadError(f"上装配置无法读取: {exc}") from exc
    if not isinstance(d, dict):
        raise PayloadError("上装配置要是 JSON 对象")
    kind = d.get("kind", "relay")
    if kind == KIND:
        keys = (HOST_ENV, USER_ENV, PASSWORD_ENV)
        have = [k for k in keys if env.get(k)]
        if have and len(have) != len(keys):
            raise PayloadError("登录竞品主控要 " + "、".join(keys) + " 三样都给,缺 "
                               + "、".join(k for k in keys if k not in have))
        if have:
            d = d | {"audio_host": f"{env[USER_ENV]}@{env[HOST_ENV]}"}
        return AnbotPayload(AnbotConfig.from_dict(d), password=env.get(PASSWORD_ENV, ""))
    if kind != "relay":
        raise PayloadError(f"上装 kind 只认 relay、{KIND}: {kind!r}")
    return Payload(PayloadConfig.load(path))
