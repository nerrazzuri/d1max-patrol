"""上装(W21,决策 36):警灯、警笛、聚光灯走 **RS485 Modbus RTU 继电器板**,喇叭走 **USB 声卡 + 功放**。

硬件还没选型,这一层只认通用的东西:
- 继电器板:Modbus RTU,功能码 05 写单个线圈(``FF00`` 开、``0000`` 关)、01 读线圈。
  哪一路接什么、站号、波特率都在配置里(``PayloadConfig``)。
  狗的 RS485 口是 ``/dev/ttyCH9344USB5``(SDK 开发指南 §1.5)。
- 喇叭:ALSA 播放(``aplay -D <设备>``)。话术是录好的 wav(``clips_dir/<名字>.wav``);
  ``tts:<语言>:<文字>`` 走可配置的外部命令(比如 ``espeak-ng``),狗上装没装是真机项。
- 供电不管(决策 36):上装常电(扩展舱按钮开着),开关都靠继电器板。``SetPeriphPower`` 要 SDK 0.2.0,狗上
  是 0.1.1。

**声光必须自己会停**(解耦总设计:带最大持续时间、断线不许一直响):每一路开的时候带 ``max_s``,到点这里
自己关;``close()`` 全关;起来先全关(上次进程死掉时开着的那几路)。进程被杀到 systemd 拉起来之间那几秒
继电器保持原状 —— 这是继电器板的物理性质,真机项里写明。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import shlex
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 能接到继电器上的几路(HAL 的执行器名)。
RELAY_OUTPUTS = ("strobe", "siren", "spotlight")
MAX_ON_S = 600.0                                     # 一次最长开 10 分钟,再长要再下命令
_CLIP = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_LANG = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?")
MAX_TTS_CHARS = 300


class PayloadError(RuntimeError):
    """上装没配、配错、设备没回。"""


# ------------------------------------------------------------ Modbus RTU


def crc16(data: bytes) -> int:
    """Modbus RTU 的 CRC16(多项式 0xA001,初值 0xFFFF);报文里低字节在前。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(body: bytes) -> bytes:
    return body + struct.pack("<H", crc16(body))


class ModbusRelay:
    """一块继电器板。阻塞式、线程安全(一把锁一问一答)。``fd`` 由 ``open_fd`` 打开(测试换成 pty)。"""

    def __init__(self, open_fd: Callable[[], int], *, slave: int = 1, timeout_s: float = 0.3,
                 retries: int = 1) -> None:
        if not 1 <= slave <= 247:
            raise PayloadError(f"Modbus 站号是 1–247,给的是 {slave}")
        self._open = open_fd
        self.slave = slave
        self.timeout_s = timeout_s
        self.retries = retries
        self._fd: int | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None

    def _read(self, fd: int, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            r, _, _ = select.select([fd], [], [], self.timeout_s)
            if not r:
                break
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _ask(self, body: bytes, want: int) -> bytes:
        """发一帧、收 ``want`` 字节的应答(含 CRC),核站号、功能码、CRC。
        异常应答(功能码 | 0x80)报错。"""
        last = "没回"
        for _ in range(1 + self.retries):
            with self._lock:
                if self._fd is None:
                    self._fd = self._open()
                fd = self._fd
                try:
                    termios_flush(fd)
                    os.write(fd, frame(body))
                    head = self._read(fd, 2)
                    if len(head) == 2 and head[1] == body[1] | 0x80:
                        rest = self._read(fd, 3)
                        raise PayloadError(f"继电器板回了异常码 {rest[:1].hex() or '?'}")
                    rest = self._read(fd, want - 2) if len(head) == 2 else b""
                except OSError as exc:
                    self._fd = None
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    last = f"串口出错: {exc}"
                    continue
            got = head + rest
            if len(got) < want:
                last = f"应答不全({len(got)}/{want} 字节)"
                continue
            if got[0] != self.slave or got[1] != body[1]:
                last = f"应答对不上(站号 {got[0]}、功能码 {got[1]})"
                continue
            if struct.unpack("<H", got[-2:])[0] != crc16(got[:-2]):
                last = "应答 CRC 不对"
                continue
            return got
        raise PayloadError(f"继电器板(站号 {self.slave}):{last}")

    def write_coil(self, coil: int, on: bool) -> None:
        body = struct.pack(">BBHH", self.slave, 0x05, coil, 0xFF00 if on else 0x0000)
        got = self._ask(body, 8)
        if got[:6] != body:                           # 05 的应答原样回显请求
            raise PayloadError("继电器板的回显跟请求对不上")

    def read_coils(self, start: int, count: int) -> list[bool]:
        body = struct.pack(">BBHH", self.slave, 0x01, start, count)
        nbytes = (count + 7) // 8
        got = self._ask(body, 5 + nbytes)
        if got[2] != nbytes:
            raise PayloadError("继电器板读线圈的字节数不对")
        bits = int.from_bytes(got[3:3 + nbytes], "little")
        return [bool(bits >> i & 1) for i in range(count)]


def termios_flush(fd: int) -> None:
    """发之前清掉上一次没读完的字节(不然下一问读到的是上一问的尾巴)。pty 也认。"""
    import termios
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except termios.error:
        pass


# ------------------------------------------------------------ 配置


@dataclass(frozen=True)
class PayloadConfig:
    """``/etc/d1max/payload.json``(代理 ``--payload``)。没有这个文件就是没装上装,能力全报 false。"""

    port: str = "/dev/ttyCH9344USB5"
    baud: int = 9600
    slave: int = 1
    #: 哪一路接在第几个线圈(从 0 数)。没写的就是没接。
    coils: dict[str, int] = field(default_factory=dict)
    #: ALSA 设备(``aplay -l`` 看;USB 声卡多半是 ``plughw:1,0``)。空 = 没接喇叭。
    audio_device: str = ""
    clips_dir: str = "/var/lib/d1max/clips"
    #: TTS 命令模板,``{lang}`` ``{wav}`` ``{text}`` 会替换。空 = 不支持 TTS(只放录好的话术)。
    tts_command: str = ""
    player: str = "aplay"

    @classmethod
    def load(cls, path: str | Path) -> PayloadConfig:
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PayloadError(f"上装配置读不了: {exc}") from exc
        if not isinstance(d, dict):
            raise PayloadError("上装配置要是 JSON 对象")
        relay = d.get("relay") or {}
        audio = d.get("audio") or {}
        coils = relay.get("coils") or {}
        if not isinstance(coils, dict) or any(
                k not in RELAY_OUTPUTS or isinstance(v, bool) or not isinstance(v, int)
                or not 0 <= v < 256 for k, v in coils.items()):
            raise PayloadError(f"relay.coils 只认 {RELAY_OUTPUTS},值是 0–255 的线圈号")
        if len(set(coils.values())) != len(coils):
            raise PayloadError("relay.coils 两路接在了同一个线圈上")
        return cls(port=str(relay.get("port", cls.port)), baud=int(relay.get("baud", cls.baud)),
                   slave=int(relay.get("slave", cls.slave)), coils=dict(coils),
                   audio_device=str(audio.get("device", "")),
                   clips_dir=str(audio.get("clips_dir", cls.clips_dir)),
                   tts_command=str(audio.get("tts_command", "")),
                   player=str(audio.get("player", "aplay")))


# ------------------------------------------------------------ 上装


class Payload:
    """上装的异步入口(HAL 调它)。每一路开的时候带最长时间,到点自己关;
    同一路再开一次就从现在重新算。"""

    def __init__(self, cfg: PayloadConfig, *, relay: ModbusRelay | None = None,
                 run: Callable[..., Any] | None = None) -> None:
        self.cfg = cfg
        self.relay = relay
        if relay is None and cfg.coils:
            from d1max_contract.serialport import open_serial
            self.relay = ModbusRelay(lambda: open_serial(cfg.port, cfg.baud), slave=cfg.slave)
        #: 起子进程(测试换掉)。
        self._run = run or asyncio.create_subprocess_exec
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._on: dict[str, bool] = {}
        self._sound: asyncio.Task | None = None
        self._proc: Any = None

    def has(self, output: str) -> bool:
        if output == "speaker":
            return bool(self.cfg.audio_device)
        return output in self.cfg.coils and self.relay is not None

    async def start(self) -> None:
        """起来先全关:上次进程死掉时开着的那几路(继电器板自己不会关)。关不掉只记日志,不挡代理起来。"""
        for out in self.cfg.coils:
            try:
                await self._coil(out, False)
            except PayloadError:
                log.exception("上装 %s 起来时关不掉", out)

    async def _coil(self, output: str, on: bool) -> None:
        if not self.has(output):
            raise PayloadError(f"上装没接 {output}")
        assert self.relay is not None
        await asyncio.to_thread(self.relay.write_coil, self.cfg.coils[output], on)
        self._on[output] = on

    async def set(self, output: str, on: bool, max_s: float) -> None:
        """开一路(带最长时间)或关一路。"""
        if output not in RELAY_OUTPUTS:
            raise PayloadError(f"继电器上只有 {RELAY_OUTPUTS}")
        t = self._timers.pop(output, None)
        if t is not None:
            t.cancel()
        if not on:
            await self._coil(output, False)
            return
        if not 0 < max_s <= MAX_ON_S:
            raise PayloadError(f"max_s 要在 0–{MAX_ON_S:.0f} 秒之间")
        await self._coil(output, True)
        loop = asyncio.get_running_loop()
        self._timers[output] = loop.call_later(
            max_s, lambda: loop.create_task(self._expire(output)))

    async def _expire(self, output: str) -> None:
        self._timers.pop(output, None)
        try:
            await self._coil(output, False)
        except PayloadError:
            log.exception("上装 %s 到点关不掉:下一次再关", output)
            loop = asyncio.get_running_loop()
            self._timers[output] = loop.call_later(1.0, lambda: loop.create_task(
                self._expire(output)))

    def state(self) -> dict[str, bool]:
        return {o: self._on.get(o, False) for o in self.cfg.coils} | (
            {"speaker": self.playing()} if self.cfg.audio_device else {})

    # ---- 喇叭

    def playing(self) -> bool:
        return self._sound is not None and not self._sound.done()

    def _resolve(self, clip_or_tts: str) -> tuple[list[str] | None, str]:
        """→ (先跑的 TTS 命令或 None, 要放的 wav 路径)。"""
        if clip_or_tts.startswith("tts:"):
            _, _, rest = clip_or_tts.partition(":")
            lang, _, text = rest.partition(":")
            if not self.cfg.tts_command:
                raise PayloadError("这台狗没配 TTS:只能放录好的话术")
            if not _LANG.fullmatch(lang) or not text.strip() or len(text) > MAX_TTS_CHARS:
                raise PayloadError(f"TTS 要写成 tts:<语言>:<文字>(文字 1–{MAX_TTS_CHARS} 字)")
            wav = f"/tmp/d1max-tts-{os.getpid()}.wav"
            cmd = [a.format(lang=lang, wav=wav, text=text)
                   for a in shlex.split(self.cfg.tts_command)]
            return cmd, wav
        if not _CLIP.fullmatch(clip_or_tts) or clip_or_tts.startswith("."):
            raise PayloadError(f"话术名只许字母、数字、. _ -: {clip_or_tts!r}")
        p = Path(self.cfg.clips_dir) / f"{clip_or_tts}.wav"
        if not p.is_file():
            raise PayloadError(f"没有这段话术: {clip_or_tts}")
        return None, str(p)

    async def sound(self, clip_or_tts: str, max_s: float) -> None:
        """放一段(打断正在放的)。``max_s`` 到了就掐掉。参数不对、没有这段当场报错;放本身在后台。"""
        if not self.cfg.audio_device:
            raise PayloadError("上装没接喇叭")
        if not 0 < max_s <= MAX_ON_S:
            raise PayloadError(f"max_s 要在 0–{MAX_ON_S:.0f} 秒之间")
        tts, wav = self._resolve(clip_or_tts)
        await self.stop_sound()
        self._sound = asyncio.get_running_loop().create_task(self._play(tts, wav, max_s))

    async def _play(self, tts: list[str] | None, wav: str, max_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_s
        try:
            if tts is not None:
                self._proc = await self._run(*tts, stdout=asyncio.subprocess.DEVNULL,
                                             stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(self._proc.wait(), max(0.1, deadline - loop.time()))
            self._proc = await self._run(self.cfg.player, "-q", "-D", self.cfg.audio_device, wav,
                                         stdout=asyncio.subprocess.DEVNULL,
                                         stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(self._proc.wait(), max(0.1, deadline - loop.time()))
        except asyncio.TimeoutError:
            pass                                          # 到点:finally 里掐
        except OSError:
            log.exception("喇叭放不出来")
        finally:
            self._kill()

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass

    async def stop_sound(self) -> None:
        t, self._sound = self._sound, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._kill()

    async def close(self) -> None:
        """全关:每一路都关、喇叭掐掉。断开、收尾都走这里。"""
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        await self.stop_sound()
        for out in self.cfg.coils:
            try:
                await self._coil(out, False)
            except PayloadError:
                log.exception("上装 %s 收尾时关不掉", out)
        if self.relay is not None:
            self.relay.close()
