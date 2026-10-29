"""上装(W21,决策 36):警灯、警笛、聚光灯走 **RS485 Modbus RTU 继电器板**,喇叭走 **USB 声卡 + 功放**。

W40(上装 V1.2,决策 70):左右两只聚光灯能分开开关;多色警灯按颜色合不同的线圈(RGB 硬接线),
可以软件打闪;没有独立警笛时用喇叭循环放警笛声;功放的电源可以接在继电器上,出声才合上。
线圈在配置里起名字,各路引用名字(见 :class:`PayloadConfig`)。

W46(上装 V2,硬件版本 D):压电警笛接继电器,放警报时喇叭跟着一起放警笛声(两样都配);功放常电,
用 GPIO 控 MUTE 脚(先放开、等 ``amp_lead_s`` 再出声,防爆音);看护循环每 ``READBACK_S`` 秒回读一次
线圈,继电器板自己掉电重启(输出全关)以后重新下发。

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
#: GPIO 控 MUTE 的功放:放开以后等这么久再出声(硬件组建议约 200 ms,防爆音)。
AMP_LEAD_S = 0.2
AMP_LEAD_MAX_S = 2.0
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


def alsa_card_present(dev: str, asound: Path = Path("/proc/asound")) -> bool | None:
    """ALSA 设备名对应的声卡插着没有:``hw:1``、``plughw:1,0``、``hw:CARD=Device,DEV=0``
    → 看 ``/proc/asound/card1`` 或 ``/proc/asound/Device``。认不出的写法(``default``)回 None
    (不知道)。"""
    m = re.fullmatch(r"(?:plug)?hw:(?:CARD=)?([A-Za-z0-9_]+)(?:,.*)?", dev)
    if m is None:
        return None
    card = m.group(1)
    return (asound / (f"card{card}" if card.isdigit() else card)).exists()


# ------------------------------------------------------------ 配置

#: 线圈名(配置里自己起):小写字母开头,字母、数字、下划线。
_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
#: 颜色名,跟契约 ``deter.COLOR`` 一致。
_COLOR = re.compile(r"[a-z][a-z0-9_]{0,15}")
#: 软件打闪的半周期范围(秒)。机械继电器寿命有限:要闪得快就用固态继电器或灯自带的闪烁。
BLINK_MIN_S, BLINK_MAX_S = 0.5, 5.0


def _names(v: Any, what: str) -> tuple[str, ...]:
    """一个线圈名或一串线圈名 → 元组。"""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
        raise PayloadError(f"{what} 要是线圈名或线圈名列表")
    if len(set(v)) != len(v):
        raise PayloadError(f"{what} 里线圈名重复")
    return tuple(v)


@dataclass(frozen=True)
class PayloadConfig:
    """``/etc/d1max/payload.json``(代理 ``--payload``)。没有这个文件就是没装上装,能力全报 false。

    W40(上装 V1.2,决策 70)起线圈有自己的名字,各路声光引用它们::

        {"relay": {"port": "/dev/ttyCH9344USB5", "baud": 9600, "slave": 1,
                   "coils": {"spot_l": 0, "spot_r": 1, "bcn_r": 2, "bcn_g": 3, "bcn_b": 4,
                             "amp": 5}},
         "spotlight": {"left": "spot_l", "right": "spot_r"},
         "strobe": {"colors": {"red": "bcn_r", "yellow": ["bcn_r", "bcn_g"], "green": "bcn_g"},
                    "default": "red", "blink_s": 0},
         "siren": {"clip": "siren"},
         "audio": {"device": "plughw:1,0", "amp": "amp", "amp_idle_s": 15}}

    老配置(W21:``coils`` 的键就是 ``strobe``、``siren``、``spotlight``,不写各路)照旧认。
    每个线圈都得有一路引用它(不认识的线圈不许开关),一个线圈只归一路。
    """

    port: str = "/dev/ttyCH9344USB5"
    baud: int = 9600
    slave: int = 1
    #: 线圈名 → 第几个线圈(从 0 数)。
    coils: dict[str, int] = field(default_factory=dict)
    #: ALSA 设备(``aplay -l`` 看;USB 声卡多半是 ``plughw:1,0``)。空 = 没接喇叭。
    audio_device: str = ""
    clips_dir: str = "/var/lib/d1max/clips"
    #: TTS 命令模板,``{lang}`` ``{wav}`` ``{text}`` 会替换。空 = 不支持 TTS(只放录好的话术)。
    tts_command: str = ""
    player: str = "aplay"
    #: 聚光灯:``{"both": 线圈们}`` = 只有一路;``{"left": …, "right": …}`` = 左右能分开开关。
    spotlight: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 警灯:颜色 → 要合上的线圈(RGB 硬接线的灯,黄 = 红 + 绿)。老配置只有一个 ``default``。
    strobe_colors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    strobe_default: str = ""
    #: 警灯软件打闪的半周期(秒);0 = 常亮(灯自己闪,或者就不闪)。
    blink_s: float = 0.0
    #: 警笛:接在继电器上(``siren_coil``),或者用喇叭放一段警笛声(``siren_clip``,话术目录里)。
    siren_coil: str = ""
    siren_clip: str = ""
    #: 功放的电源接在哪个线圈上(空 = 功放常电)。喇叭要出声前合上,不出声 ``amp_idle_s`` 秒后断开。
    amp_coil: str = ""
    amp_idle_s: float = 15.0
    #: 功放常电、MUTE 脚接 GPIO(W46):哪个芯片、第几根线、什么电平是静音。跟 ``amp_coil`` 二选一。
    mute_chip: str = ""
    mute_line: int = -1
    mute_level: str = "high"
    #: 放开功放以后等多久再出声(防爆音)。负数 = 缺省:GPIO 0.2 秒,继电器 0。
    amp_lead_s: float = -1.0

    def __post_init__(self) -> None:
        if self.amp_lead_s < 0:
            object.__setattr__(self, "amp_lead_s", AMP_LEAD_S if self.mute_chip else 0.0)
        # 老配置(W21):coils 的键就是那几路
        if not self.spotlight and "spotlight" in self.coils:
            object.__setattr__(self, "spotlight", {"both": ("spotlight",)})
        if not self.strobe_colors and "strobe" in self.coils:
            object.__setattr__(self, "strobe_colors", {"default": ("strobe",)})
        if not self.strobe_default and self.strobe_colors:
            object.__setattr__(self, "strobe_default", next(iter(self.strobe_colors)))
        if not self.siren_coil and not self.siren_clip and "siren" in self.coils:
            object.__setattr__(self, "siren_coil", "siren")
        self._check()

    def _check(self) -> None:
        for k, v in self.coils.items():
            if not isinstance(k, str) or not _NAME.fullmatch(k) or isinstance(v, bool) \
                    or not isinstance(v, int) or not 0 <= v < 256:
                raise PayloadError("relay.coils 是「线圈名 → 0–255 的线圈号」,"
                                   f"名字小写字母开头:{k!r}")
        if len(set(self.coils.values())) != len(self.coils):
            raise PayloadError("relay.coils 两路接在了同一个线圈上")
        if self.spotlight and set(self.spotlight) not in ({"both"}, {"left", "right"}):
            raise PayloadError("spotlight 要么只写 both(一路),要么 left、right 都写")
        for c in self.strobe_colors:
            if not _COLOR.fullmatch(c):
                raise PayloadError(f"strobe.colors 的颜色名不对:{c!r}")
        if self.strobe_colors and self.strobe_default not in self.strobe_colors:
            raise PayloadError(f"strobe.default {self.strobe_default!r} 不在 colors 里")
        if self.blink_s and not BLINK_MIN_S <= self.blink_s <= BLINK_MAX_S:
            raise PayloadError(f"strobe.blink_s 要是 0 或 {BLINK_MIN_S}–{BLINK_MAX_S} 秒")
        if self.siren_clip and (not _CLIP.fullmatch(self.siren_clip)
                                or self.siren_clip.startswith(".")):
            raise PayloadError(f"siren.clip 只许字母、数字、. _ -:{self.siren_clip!r}")
        if self.mute_chip:
            from d1max_contract.gpio import GpioError, chip_path
            try:
                chip_path(self.mute_chip)
            except GpioError as exc:
                raise PayloadError(f"audio.mute.chip:{exc}") from exc
            if isinstance(self.mute_line, bool) or not isinstance(self.mute_line, int) \
                    or self.mute_line < 0:
                raise PayloadError("audio.mute.line 还没填(按 GPIO 引脚方案写;装机时 gpioinfo 查)")
            if self.mute_level not in ("high", "low"):
                raise PayloadError("audio.mute.mute_level 只认 high 或 low(哪个电平是静音)")
            if self.amp_coil:
                raise PayloadError("功放只能二选一:audio.amp(电源接继电器)"
                                   "或 audio.mute(GPIO 控 MUTE)")
        if not 0 <= self.amp_lead_s <= AMP_LEAD_MAX_S:
            raise PayloadError(f"audio.amp_lead_s 要在 0–{AMP_LEAD_MAX_S:.0f} 秒")
        if (self.siren_clip or self.amp_coil or self.mute_chip) and not self.audio_device:
            raise PayloadError("喇叭警笛、功放都要先接喇叭(audio.device)")
        if not 0 <= self.amp_idle_s <= MAX_ON_S:
            raise PayloadError(f"audio.amp_idle_s 要在 0–{MAX_ON_S:.0f} 秒")
        roles: dict[str, set[str]] = {
            "spotlight": {c for cs in self.spotlight.values() for c in cs},
            "strobe": {c for cs in self.strobe_colors.values() for c in cs},
            "siren": {self.siren_coil} if self.siren_coil else set(),
            "audio.amp": {self.amp_coil} if self.amp_coil else set()}
        seen: dict[str, str] = {}
        for role, cs in roles.items():
            for c in cs:
                if c not in self.coils:
                    raise PayloadError(f"{role} 引用的线圈 {c!r} 不在 relay.coils 里")
                if c in seen:
                    raise PayloadError(f"线圈 {c!r} 同时归 {seen[c]} 和 {role}")
                seen[c] = role
        spare = sorted(set(self.coils) - set(seen))
        if spare:
            raise PayloadError(f"线圈 {spare} 没有哪一路用它(不认识的线圈不开关;没接就删掉)")

    @property
    def strobe_coils(self) -> frozenset[str]:
        return frozenset(c for cs in self.strobe_colors.values() for c in cs)

    def role_of(self, coil: str) -> str:
        """这个线圈归哪一路(故障说明用)。"""
        for side, cs in self.spotlight.items():
            if coil in cs:
                return {"both": "聚光灯", "left": "左聚光灯", "right": "右聚光灯"}[side]
        if coil in self.strobe_coils:
            return "警灯"
        if coil == self.siren_coil:
            return "警笛"
        if coil == self.amp_coil:
            return "功放"
        return coil

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
        spot = d.get("spotlight") or {}
        strobe = d.get("strobe") or {}
        siren = d.get("siren") or {}
        if not all(isinstance(x, dict) for x in (relay, audio, spot, strobe, siren)):
            raise PayloadError("relay、audio、spotlight、strobe、siren 都要是对象")
        coils = relay.get("coils") or {}
        if not isinstance(coils, dict):
            raise PayloadError("relay.coils 要是对象")
        colors = strobe.get("colors") or {}
        if not isinstance(colors, dict):
            raise PayloadError("strobe.colors 要是对象")
        mute = audio.get("mute") or {}
        if not isinstance(mute, dict):
            raise PayloadError("audio.mute 要是对象:{\"chip\": \"gpiochipN\", \"line\": N, "
                               "\"mute_level\": \"high\"}")
        line = mute.get("line", -1)
        if not isinstance(mute.get("chip", ""), str) or not isinstance(line, int) \
                or isinstance(line, bool) or not isinstance(mute.get("mute_level", "high"), str):
            raise PayloadError("audio.mute 的 chip、mute_level 要是字符串,line 要是整数")
        try:
            blink = float(strobe.get("blink_s", 0))
            idle = float(audio.get("amp_idle_s", cls.amp_idle_s))
            lead = float(audio.get("amp_lead_s", -1.0))
            baud, slave = int(relay.get("baud", cls.baud)), int(relay.get("slave", cls.slave))
        except (TypeError, ValueError) as exc:
            raise PayloadError(f"上装配置的数字不对: {exc}") from exc
        if not all(isinstance(siren.get(k, ""), str) for k in ("coil", "clip")) \
                or not isinstance(audio.get("amp", ""), str) \
                or not isinstance(strobe.get("default", ""), str):
            raise PayloadError("siren.coil、siren.clip、audio.amp、strobe.default 要是字符串")
        return cls(port=str(relay.get("port", cls.port)), baud=baud, slave=slave,
                   coils=dict(coils),
                   audio_device=str(audio.get("device", "")),
                   clips_dir=str(audio.get("clips_dir", cls.clips_dir)),
                   tts_command=str(audio.get("tts_command", "")),
                   player=str(audio.get("player", "aplay")),
                   spotlight={k: _names(v, f"spotlight.{k}") for k, v in spot.items()},
                   strobe_colors={k: _names(v, f"strobe.colors.{k}") for k, v in colors.items()},
                   strobe_default=strobe.get("default", ""), blink_s=blink,
                   siren_coil=siren.get("coil", ""), siren_clip=siren.get("clip", ""),
                   amp_coil=audio.get("amp", ""), amp_idle_s=idle,
                   mute_chip=mute.get("chip", ""), mute_line=line,
                   mute_level=mute.get("mute_level", "high"), amp_lead_s=lead)


# ------------------------------------------------------------ 上装


class _Ended(Exception):
    def __init__(self, ok: bool, why: str) -> None:
        super().__init__(why)
        self.ok, self.why = ok, why


class Payload:
    """上装的异步入口(HAL 调它)。

    **继电器:每个线圈一个期望状态 + 一个一直在跑的看护循环**(W21 外审;W40 起按线圈,不按路):
    - 开:这一路要合上的线圈期望「开」、截止时刻 = 现在 + ``max_s``;截止一到期望变「关」。
      警灯换颜色:新颜色的线圈开,别的颜色的线圈关(先关后开)。
    - 关:这一路(或这一边)的线圈期望「关」。
    - 期望跟继电器板**确认过的**状态对不上(或者状态不明),看护循环每 ``RETRY_S`` 秒重试,
      直到板子确认。所以关不掉会一直关、起来时上次留着的会一直关到关上为止。
    - **开没成**(板子没回、应答丢了)一律把这一下要开的线圈都改成「关」:板子可能其实开了,宁可接着关
      (黄灯开了一半就是错的颜色)。
    - 继电器一把锁:到点关、打闪与新来的命令不交叉。
    - 状态不明(关不上、没确认)的线圈由 :meth:`problems` 报出来,HAL 当故障报给站点。
    - 警灯软件打闪(``blink_s``):期望按开始的时刻算相位,看护循环那时每 ``BLINK_TICK_S`` 秒一拍。

    **喇叭**:当场起进程(TTS 是合成进程,不等它合成完),起不来就报错;合成失败、放坏了、放完了、
    到点了都回调 ``on_sound_end(ok, 原因)``,代理据此当场放开优先级、记事件。被新的一段打断的不回调。

    **喇叭警笛**(``siren_clip``,决策 70:V1.2 没有独立警笛):警笛开着就循环放警笛声;要说话时警笛先让
    (掐掉警笛这一遍),说完接着响,到点停。**功放**(``amp_coil``):出声前合上,都不出声 ``amp_idle_s``
    秒后断开(看护循环关)。
    """

    RETRY_S = 0.5
    BLINK_TICK_S = 0.1
    #: 多久回读一次线圈(W46:继电器板自己掉电重启以后输出全关,要发现、重新下发)。
    READBACK_S = 5.0
    #: 警笛等说话说完时多久看一次。
    SIREN_POLL_S = 0.1
    #: 警笛放不出来时隔多久再试。
    SIREN_RETRY_S = 1.0

    def __init__(self, cfg: PayloadConfig, *, relay: ModbusRelay | None = None,
                 run: Callable[..., Any] | None = None, mute: Any = None) -> None:
        self.cfg = cfg
        self.relay = relay
        if relay is None and cfg.coils:
            from d1max_contract.serialport import open_serial
            self.relay = ModbusRelay(lambda: open_serial(cfg.port, cfg.baud), slave=cfg.slave)
        #: 功放 MUTE 脚(W46;``set(电平)``、``close()``,测试换掉)。拿不到这根线不挡代理起来:
        #: 灯、警笛照样能用,喇叭出声时报「功放开不了」。
        self.amp_error = ""
        self.mute = mute
        if mute is None and cfg.mute_chip:
            from d1max_contract.gpio import OutputLine
            try:
                self.mute = OutputLine(cfg.mute_chip, cfg.mute_line, value=self._muted_level,
                                       consumer="d1max-amp")
            except OSError as exc:
                self.amp_error = str(exc)
                log.error("功放 MUTE 脚拿不到,喇叭出不了声: %s", exc)
        #: GPIO 功放:放开着到单调钟几点(None = 要静音);现在是不是放开着。
        self._amp_until: float | None = None
        self._amp_open = False
        #: 起子进程(测试换掉)。
        self._run = run or asyncio.create_subprocess_exec
        #: 期望:开着到单调钟几点(None = 要关)。按线圈。
        self._until: dict[str, float | None] = {c: None for c in cfg.coils}
        #: 板子确认过的状态;None = 不明(起来时、写失败之后)。
        self._known: dict[str, bool | None] = {c: None for c in cfg.coils}
        self._relay_lock: asyncio.Lock | None = None
        #: 警灯从几点开始闪(None = 不闪)。
        self._blink_from: float | None = None
        self._guard: asyncio.Task | None = None
        self._closed = False
        self._sound: asyncio.Task | None = None
        self._proc: Any = None
        #: 说话那一段正在起(还没登记成 ``_sound``):警笛这时别抢着放下一遍。
        self._speech_pending = False
        self._siren: asyncio.Task | None = None
        self._siren_proc: Any = None
        #: 警笛这一遍是被说话掐掉的(不算放坏了)。
        self._siren_cut = False
        #: 喇叭警笛最近一次放不出来的原因(放成了就清掉)。
        self.siren_error = ""
        #: 说话那一段最近一次没放成的原因(放成了就清掉;设备健康用,W40)。
        self.sound_error = ""
        #: 声卡在不在(测试换掉):ALSA 设备名 → 在不在。
        self.card_present: Callable[[str], bool | None] = alsa_card_present
        self.on_sound_end: Callable[[bool, str], Any] | None = None

    # ---- 有什么

    def _siren_wav(self) -> Path:
        return Path(self.cfg.clips_dir) / f"{self.cfg.siren_clip}.wav"

    def _speaker_siren(self) -> bool:
        """喇叭放得了警笛声(配了、接了喇叭、声音文件在)。"""
        return bool(self.cfg.siren_clip and self.cfg.audio_device) and self._siren_wav().is_file()

    def has(self, output: str) -> bool:
        if output == "speaker":
            return bool(self.cfg.audio_device)
        if output == "siren" and self.cfg.siren_clip and not self.cfg.siren_coil:
            return self._speaker_siren()
        if self.relay is None:
            return False
        if output == "spotlight":
            return bool(self.cfg.spotlight)
        if output == "strobe":
            return bool(self.cfg.strobe_colors)
        if output == "siren":
            return bool(self.cfg.siren_coil)
        return False

    def options(self) -> dict[str, Any]:
        """能力里多报的(W40):聚光灯分不分左右、警灯有哪些颜色、警笛是喇叭放的还是继电器上的。"""
        d: dict[str, Any] = {}
        if self.has("spotlight") and "left" in self.cfg.spotlight:
            d["spotlight_sides"] = ["left", "right"]
        if self.has("strobe") and list(self.cfg.strobe_colors) != ["default"]:
            d["strobe_colors"] = list(self.cfg.strobe_colors)
            d["strobe_default"] = self.cfg.strobe_default
        if self.has("siren"):
            if not self.cfg.siren_coil:
                d["siren_via"] = "speaker"
            else:
                d["siren_via"] = "relay+speaker" if self._speaker_siren() else "relay"
        return d

    # ---- 继电器

    def _lock(self) -> asyncio.Lock:
        if self._relay_lock is None:
            self._relay_lock = asyncio.Lock()
        return self._relay_lock

    def _blinking(self) -> bool:
        return self._blink_from is not None and any(
            self._until.get(c) is not None for c in self.cfg.strobe_coils)

    def _ensure_guard(self) -> None:
        if not self._closed and (self.cfg.coils or self.cfg.mute_chip) \
                and (self._guard is None or self._guard.done()):
            self._guard = asyncio.get_running_loop().create_task(self._watch())

    async def start(self) -> None:
        """起来:每个线圈当「状态不明、要关」(上次进程死掉时开着的,继电器板自己不会关),先试一遍,
        关不掉看护循环接着关。关不掉不挡代理起来,由 :meth:`problems` 报故障。"""
        self._ensure_guard()
        for c in self.cfg.coils:
            await self._reconcile(c)

    def _want(self, coil: str) -> bool:
        loop = asyncio.get_running_loop()
        until = self._until.get(coil)
        if until is not None and loop.time() >= until:
            self._until[coil] = None                   # 到点:期望变「关」
            until = None
        if until is None:
            return False
        if self.cfg.blink_s and self._blink_from is not None and coil in self.cfg.strobe_coils:
            return int((loop.time() - self._blink_from) / self.cfg.blink_s) % 2 == 0
        return True

    async def _write(self, coil: str, on: bool) -> None:
        assert self.relay is not None
        try:
            await asyncio.to_thread(self.relay.write_coil, self.cfg.coils[coil], on)
        except Exception:
            self._known[coil] = None                   # 板子到底在什么状态,不知道了
            raise
        self._known[coil] = on

    async def _reconcile(self, coil: str) -> None:
        """期望跟确认过的不一样(或不明)就写一次。失败只记日志,下一拍再来。
        外审 R1:警灯的几路是一组(一个颜色可能要两路一起亮)—— 有一路**开**不成,整组撤掉「要开」、
        接着关,不留半个颜色;旧颜色还有一路没关掉,新颜色那几路先不开。"""
        async with self._lock():
            want = self._want(coil)
            if self._known.get(coil) is want:
                return
            group = coil in self.cfg.strobe_coils
            if want and group and any(
                    self._known.get(c) is not False and not self._want(c)
                    for c in self.cfg.strobe_coils if c != coil):
                return                                 # 组里还有该关的没关掉:先不开这一路
            try:
                await self._write(coil, want)
            except Exception as exc:                   # noqa: BLE001 - 下一拍重试
                if not want:
                    log.warning("上装线圈 %s(%s)关不掉(接着关): %s", coil,
                                self.cfg.role_of(coil), exc)
                elif group:
                    log.warning("警灯线圈 %s 开不了:整组撤掉、接着关: %s", coil, exc)
                    for c in self.cfg.strobe_coils:
                        self._until[c] = None

    async def _readback(self) -> None:
        """读一遍线圈,跟确认过的对不上的改成「不明」(下一步 :meth:`_reconcile` 重新下发)。
        读不出来不管:写的时候自然会报。"""
        if self.relay is None or not self.cfg.coils:
            return
        nums = self.cfg.coils
        lo = min(nums.values())
        async with self._lock():
            try:
                bits = await asyncio.to_thread(self.relay.read_coils, lo,
                                               max(nums.values()) - lo + 1)
            except Exception:                          # noqa: BLE001 - 写的时候会报
                return
            for name, n in nums.items():
                k = self._known.get(name)
                if k is not None and n - lo < len(bits) and bits[n - lo] != k:
                    log.warning("继电器板上 %s(%s)跟记的不一样(板子掉过电?),重新下发",
                                name, self.cfg.role_of(name))
                    self._known[name] = None

    async def _watch(self) -> None:
        loop = asyncio.get_running_loop()
        next_read = loop.time() + self.READBACK_S
        while not self._closed:
            if loop.time() >= next_read:
                await self._readback()
                next_read = loop.time() + self.READBACK_S
            for c in self.cfg.coils:
                await self._reconcile(c)
            self._amp_idle_check()
            await asyncio.sleep(self.BLINK_TICK_S if self._blinking() else self.RETRY_S)

    def _pick(self, output: str, on: bool, side: str, color: str
              ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """→ (要开的线圈, 要关的线圈)。"""
        cfg = self.cfg
        if output == "spotlight":
            if side == "both":
                cs = tuple(c for v in cfg.spotlight.values() for c in v)
            elif side in cfg.spotlight:
                cs = cfg.spotlight[side]
            else:
                raise PayloadError("这台狗的聚光灯不分左右")
            return (cs, ()) if on else ((), cs)
        if output == "strobe":
            every = tuple(c for c in cfg.coils if c in cfg.strobe_coils)
            if not on:
                return (), every
            color = color or cfg.strobe_default
            if color not in cfg.strobe_colors:
                raise PayloadError(f"警灯没有 {color} 色(有 {'/'.join(cfg.strobe_colors)})")
            want = cfg.strobe_colors[color]
            return want, tuple(c for c in every if c not in want)
        return ((cfg.siren_coil,), ()) if on else ((), (cfg.siren_coil,))

    async def set(self, output: str, on: bool, max_s: float, *, side: str = "both",
                  color: str = "") -> None:
        """开一路(带最长时间)或关一路(聚光灯可以只开、关一边;警灯可以挑颜色)。
        板子没确认就抛 :class:`PayloadError`;**关没成会一直接着关,开没成也当「要关」接着关**
        (板子可能其实开了)。"""
        if output not in RELAY_OUTPUTS:
            raise PayloadError(f"继电器上只有 {RELAY_OUTPUTS}")
        if on and not 0 < max_s <= MAX_ON_S:
            raise PayloadError(f"max_s 要在 0–{MAX_ON_S:.0f} 秒之间")
        if output == "siren" and self.cfg.siren_clip and not self.cfg.siren_coil:
            await self._siren_sound(on, max_s)
            return
        if not self.has(output):
            raise PayloadError(f"上装没接 {output}")
        both = output == "siren" and bool(self.cfg.siren_clip)
        if both and not on:
            await self._siren_sound(False, 0)          # 关:喇叭先停,继电器关不上也不许接着放
        await self._set_coils(output, on, max_s, side, color)
        if both and on and self._speaker_siren():
            try:                                       # 喇叭是补音量的:放不出来不算警笛没开
                await self._siren_sound(True, max_s)
            except PayloadError as exc:
                self.siren_error = str(exc)
                log.warning("警笛的喇叭那一半放不出来: %s", exc)

    async def _set_coils(self, output: str, on: bool, max_s: float, side: str,
                         color: str) -> None:
        turn_on, turn_off = self._pick(output, on, side, color)
        self._ensure_guard()
        async with self._lock():
            now = asyncio.get_running_loop().time()
            for c in turn_off:
                self._until[c] = None
            for c in turn_on:
                self._until[c] = now + max_s
            if output == "strobe":
                self._blink_from = now if on and self.cfg.blink_s else None
            failed: Exception | None = None
            for c in turn_off:                         # 先关后开:换颜色时不混出第三种色
                try:
                    await self._write(c, False)
                except Exception as exc:               # noqa: BLE001 - 看护循环接着关
                    failed = failed or exc
            # 外审 R1:旧颜色有一路没关掉就不开新的(不然红关不掉再开绿 = 混成黄)
            for c in (() if failed is not None else turn_on):
                try:
                    await self._write(c, self._want(c))
                except Exception as exc:               # noqa: BLE001 - 下面统一当「要关」
                    failed = failed or exc
                    break
            if failed is not None:
                if on:
                    for c in turn_on:                  # 开没成:这一下要开的都当「要关」
                        self._until[c] = None
                if isinstance(failed, PayloadError):
                    raise failed
                raise PayloadError(f"继电器板写不进去: {failed}") from failed

    def state(self) -> dict[str, bool | None]:
        """每个线圈板子确认过的状态(None = 不明),加上喇叭在不在出声。"""
        d: dict[str, bool | None] = dict(self._known)
        if self.cfg.audio_device:
            d["speaker"] = self.playing()
            if self.cfg.siren_clip:
                d["siren"] = self.siren_on()
        return d

    def problems(self) -> list[str]:
        """状态不明或关不上的线圈,加上放不出来的喇叭警笛(HAL 当故障报)。
        期望开、还没确认开的不算(那是正在开);打闪时灭的那半拍也不算。"""
        out = self._coil_problems()
        if self.siren_error and self.siren_on():
            out.append("siren")
        if self.amp_error:
            out.append("amp")
        return out

    def _coil_problems(self) -> list[str]:
        return [c for c in self.cfg.coils if self._known.get(c) is None
                or (self._known.get(c) and self._until.get(c) is None)]

    def _output_of(self, coil: str) -> str:
        if coil in self.cfg.strobe_coils:
            return "strobe"
        if coil == self.cfg.amp_coil:
            return "amp"
        if coil == self.cfg.siren_coil:
            return "siren"
        return "spotlight"

    def faults(self) -> dict[str, str]:
        """:meth:`problems` 按路合起来:``strobe``/``siren``/``spotlight``/``amp`` → 说明(给人看)。
        站点按路出告警(``payload_<路>``),线圈名是现场配置起的,站点不认识。"""
        roles: dict[str, list[str]] = {}
        for c in self._coil_problems():
            roles.setdefault(self._output_of(c), []).append(self.cfg.role_of(c))
        extra: dict[str, list[str]] = {}
        if self.siren_error and self.siren_on():
            extra.setdefault("siren", []).append(f"喇叭警笛放不出来:{self.siren_error}")
        if self.amp_error:
            extra.setdefault("amp", []).append(f"功放 MUTE 脚写不进去:{self.amp_error}")
        text = {}
        for o in dict.fromkeys([*roles, *extra]):
            parts = ([f"上装{'、'.join(dict.fromkeys(roles[o]))}状态不明或关不上"
                      "(继电器板没确认,正在接着关)"] if o in roles else []) + extra.get(o, [])
            text[o] = ";".join(parts)
        return text

    def health(self) -> list[dict[str, Any]]:
        """设备健康(W40,``d1max_contract.devices``):继电器板、喇叭、喇叭警笛。**灯亮没亮不知道**
        (没有灯具传感器):继电器板「在线」只是说命令写进去了、板子确认了。"""
        from d1max_contract.devices import device
        out = []
        if self.cfg.coils:
            unknown = [c for c in self.cfg.coils if self._known.get(c) is None]
            stuck = [c for c in self.cfg.coils
                     if self._known.get(c) is True and self._until.get(c) is None]
            if len(unknown) == len(self.cfg.coils):
                out.append(device("relay", "offline",
                                  "继电器板一直没确认(串口、站号、板子供电?)"))
            elif unknown or stuck:
                names = "、".join(dict.fromkeys(self.cfg.role_of(c) for c in unknown + stuck))
                out.append(device("relay", "error", f"{names}没确认或关不上,正在接着关"))
            else:
                out.append(device("relay", "online", f"{len(self.cfg.coils)} 个线圈都确认过"))
        if self.cfg.audio_device:
            here = self.card_present(self.cfg.audio_device)
            if here is False:
                out.append(device("speaker", "offline",
                                  f"声卡 {self.cfg.audio_device} 不在(USB 掉了?)"))
            elif self.sound_error:
                out.append(device("speaker", "error", self.sound_error))
            else:
                out.append(device("speaker", "online", self.cfg.audio_device))
        if self.cfg.mute_chip:
            where = f"{self.cfg.mute_chip}:{self.cfg.mute_line}"
            out.append(device("amp", "error", f"功放 MUTE 脚 {where} 写不进去:{self.amp_error}")
                       if self.amp_error else device("amp", "online", f"MUTE 脚 {where}"))
        if self.cfg.siren_clip and not self.cfg.siren_coil:
            if not self._siren_wav().is_file():
                out.append(device("siren", "error", f"没有警笛声文件 {self._siren_wav()}"))
            elif self.siren_error:
                out.append(device("siren", "error", f"喇叭警笛放不出来:{self.siren_error}"))
            else:
                out.append(device("siren", "online", "喇叭放警笛声"))
        return out

    # ---- 功放

    @property
    def _muted_level(self) -> bool:
        return self.cfg.mute_level == "high"

    async def _amp_on(self, until: float) -> None:
        """出声前放开功放,至少放开到 ``until`` + 闲置时间;刚放开的等 ``amp_lead_s`` 再出声。
        放不开就报错(不出声的喇叭当没放)。"""
        if self.cfg.mute_chip:
            await self._amp_unmute(until)
            return
        a = self.cfg.amp_coil
        if not a or self.relay is None:
            return
        self._ensure_guard()
        async with self._lock():
            cur = self._until.get(a)
            self._until[a] = max(cur or 0.0, until + self.cfg.amp_idle_s)
            if self._known.get(a) is True:
                return
            try:
                await self._write(a, True)
            except Exception as exc:
                self._until[a] = None
                raise PayloadError(f"功放开不了(继电器板): {exc}") from exc
        if self.cfg.amp_lead_s:
            await asyncio.sleep(self.cfg.amp_lead_s)

    async def _amp_unmute(self, until: float) -> None:
        self._ensure_guard()
        self._amp_until = max(self._amp_until or 0.0, until + self.cfg.amp_idle_s)
        if self._amp_open:
            return
        try:
            if self.mute is None:
                raise OSError(self.amp_error or "没拿到这根线")
            self.mute.set(not self._muted_level)
        except Exception as exc:
            self._amp_until = None
            self.amp_error = str(exc)
            raise PayloadError(f"功放开不了(MUTE 脚): {exc}") from exc
        self.amp_error = ""
        self._amp_open = True
        if self.cfg.amp_lead_s:
            await asyncio.sleep(self.cfg.amp_lead_s)

    def _amp_idle_check(self) -> None:
        """看护循环每拍:GPIO 功放闲够了就静音(静音写不进去下一拍再来)。"""
        if not self._amp_open or self.mute is None:
            return
        until = self._amp_until
        if until is not None and asyncio.get_running_loop().time() < until:
            return
        try:
            self.mute.set(self._muted_level)
        except Exception as exc:                       # noqa: BLE001 - 下一拍再静音
            self.amp_error = str(exc)
            log.warning("功放静音不了(接着试): %s", exc)
            return
        self._amp_open = False
        self._amp_until = None

    def _amp_release(self) -> None:
        """一段声音停了:另一样(说话 / 警笛)也不响的话,功放闲置 ``amp_idle_s`` 秒后
        由看护循环断开(或静音)。"""
        me = asyncio.current_task()
        busy = any(t is not None and not t.done() and t is not me
                   for t in (self._sound, self._siren))
        if self.cfg.mute_chip:
            if not busy and self._amp_until is not None:
                self._amp_until = min(self._amp_until,
                                      asyncio.get_running_loop().time() + self.cfg.amp_idle_s)
            return
        a = self.cfg.amp_coil
        if not a or self.relay is None:
            return
        cur = self._until.get(a)
        if not busy and cur is not None:
            self._until[a] = min(cur, asyncio.get_running_loop().time() + self.cfg.amp_idle_s)

    # ---- 喇叭警笛

    def siren_on(self) -> bool:
        return self._siren is not None and not self._siren.done()

    async def _siren_sound(self, on: bool, max_s: float) -> None:
        await self._siren_stop()
        if not on:
            return
        if not self.has("siren"):
            raise PayloadError(f"喇叭警笛没有警笛声:{self._siren_wav()}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_s
        await self._amp_on(deadline)
        self.siren_error = ""
        self._siren = loop.create_task(self._siren_loop(deadline))

    async def _siren_loop(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        wav = str(self._siren_wav())
        try:
            while loop.time() < deadline:
                if self.playing() or self._speech_pending:   # 在说话:警笛让一让
                    await asyncio.sleep(self.SIREN_POLL_S)
                    continue
                try:
                    proc = await self._run(self.cfg.player, "-q", "-D", self.cfg.audio_device, wav,
                                           stdout=asyncio.subprocess.DEVNULL,
                                           stderr=asyncio.subprocess.DEVNULL)
                except OSError as exc:
                    self.siren_error = f"播放器起不来: {exc}"
                    log.warning("喇叭警笛放不出来: %s", exc)
                    await asyncio.sleep(self.SIREN_RETRY_S)
                    continue
                self._siren_proc = proc
                if self.playing() or self._speech_pending:  # 起的那一下有人开始说话了:让
                    self._siren_kill()
                    continue
                try:
                    rc = await asyncio.wait_for(proc.wait(), max(0.1, deadline - loop.time()))
                except asyncio.TimeoutError:
                    break
                finally:
                    self._siren_kill()
                if self._siren_cut:
                    self._siren_cut = False                   # 被说话掐掉的,不算坏
                elif rc != 0:
                    self.siren_error = f"播放器退出码 {rc}"
                    log.warning("喇叭警笛放坏了: 退出码 %s", rc)
                    await asyncio.sleep(self.SIREN_RETRY_S)
                else:
                    self.siren_error = ""
        finally:
            self._siren_kill()
            self._amp_release()

    def _siren_kill(self) -> None:
        p, self._siren_proc = self._siren_proc, None
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass

    async def _siren_stop(self) -> None:
        t, self._siren = self._siren, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._siren_kill()
        self._siren_cut = False

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
        """放一段(打断正在放的)。参数不对、没有这段、进程起不来当场报错;**不在这里等 TTS 合成**
        (W21 复查:合成最长要几十秒,这期间代理的命令锁被占着,「关喇叭」得排队)。合成、播放都在后台,
        合成失败、播放器退出码不对、放完、到点都回调 ``on_sound_end``;被新的一段打断、被关的不回调,
        正在合成的被关掉就不再起播放。喇叭警笛正响着:掐掉这一遍,说完接着响。"""
        if not self.cfg.audio_device:
            raise PayloadError("上装没接喇叭")
        if not 0 < max_s <= MAX_ON_S:
            raise PayloadError(f"max_s 要在 0–{MAX_ON_S:.0f} 秒之间")
        tts, wav = self._resolve(clip_or_tts)
        await self.stop_sound()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_s
        self._speech_pending = True
        try:
            if self._siren_proc is not None:
                self._siren_cut = True
                self._siren_kill()
            await self._amp_on(deadline)
            try:
                first = await self._run(*(tts or [self.cfg.player, "-q", "-D",
                                                   self.cfg.audio_device, wav]),
                                        stdout=asyncio.subprocess.DEVNULL,
                                        stderr=asyncio.subprocess.DEVNULL)
            except OSError as exc:
                self._amp_release()
                raise PayloadError(f"喇叭放不出来: {exc}") from exc
            self._proc = first
            self._sound = loop.create_task(self._play(first, wav if tts else None, deadline))
        finally:
            self._speech_pending = False

    async def _play(self, first: Any, then_wav: str | None, deadline: float) -> None:
        """``first`` 是合成进程(``then_wav`` 给了)或播放进程。"""
        loop = asyncio.get_running_loop()
        ok, why = True, "放完了"
        proc = first
        try:
            if then_wav is not None:
                rc = await asyncio.wait_for(first.wait(), max(0.1, deadline - loop.time()))
                if rc != 0:
                    raise _Ended(False, f"TTS 合成失败(退出码 {rc})")
                spawn = asyncio.ensure_future(self._run(
                    self.cfg.player, "-q", "-D", self.cfg.audio_device, then_wav,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
                try:
                    proc = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # 起播放器的那一下被关掉了:起出来的进程也要掐,不许留着放
                    try:
                        (await spawn).kill()
                    except Exception:            # noqa: BLE001 - 起不来就没什么可掐的
                        pass
                    raise
                except OSError as exc:
                    raise _Ended(False, f"喇叭放不出来: {exc}") from exc
                self._proc = proc
            rc = await asyncio.wait_for(proc.wait(), max(0.1, deadline - loop.time()))
            if rc != 0:
                raise _Ended(False, f"播放器退出码 {rc}(设备不对、文件坏了?)")
        except _Ended as e:
            ok, why = e.ok, e.why
            self.sound_error = why
            log.warning("喇叭没放成: %s", why)
        except asyncio.TimeoutError:
            why = "到点了"
        except asyncio.CancelledError:
            self._kill()
            self._amp_release()
            raise                                         # 被新的一段打断、被关:不回调
        finally:
            if self._proc is proc or self._proc is first:
                self._kill()
        self._amp_release()
        if ok:
            self.sound_error = ""
        if self.on_sound_end is not None:
            try:
                self.on_sound_end(ok, why)
            except Exception:
                log.exception("喇叭放完的回调炸了")

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass

    async def stop_sound(self) -> None:
        """掐掉说话那一段(喇叭警笛不管,它有自己的开关)。"""
        t, self._sound = self._sound, None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._kill()

    async def close(self) -> None:
        """全关:每个线圈都关、喇叭和喇叭警笛掐掉、看护循环停。断开、收尾都走这里(关不掉记日志)。"""
        self._closed = True
        if self._guard is not None:
            self._guard.cancel()
            try:
                await self._guard
            except asyncio.CancelledError:
                pass
            self._guard = None
        await self.stop_sound()
        await self._siren_stop()
        self._blink_from = None
        for c in self.cfg.coils:
            self._until[c] = None
            try:
                async with self._lock():
                    await self._write(c, False)
            except Exception:
                log.exception("上装线圈 %s 收尾时关不掉", c)
        if self.mute is not None:
            try:
                self.mute.set(self._muted_level)
            except Exception:
                log.exception("功放收尾时静音不了")
            self.mute.close()
            self._amp_open = False
        if self.relay is not None:
            self.relay.close()
