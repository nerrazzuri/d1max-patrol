"""上装(W21):Modbus RTU 继电器板(假板子挂在 pty 上)、到点自己关、起来与收尾全关、
喇叭放话术与 TTS。"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import threading
import tty

import pytest

from d1max_adapter_d1max.payload import (
    ModbusRelay,
    Payload,
    PayloadConfig,
    PayloadError,
    crc16,
    frame,
)


class 假板子:
    """pty 另一头的 Modbus RTU 从站:8 个线圈,认 01 / 05。``mute`` 时不回,``garble`` 时 CRC 错。"""

    def __init__(self, slave: int = 1) -> None:
        self.master, self.slave_fd = os.openpty()
        tty.setraw(self.slave_fd)
        tty.setraw(self.master)
        self.slave = slave
        self.coils = [False] * 8
        self.mute = False
        self.garble = False
        self.exception = False
        #: 执行了、应答丢了(W21 外审):线圈照改,不回。
        self.drop_reply = False
        self.writes: list[tuple[int, bool]] = []
        self._stop = False
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def open(self) -> int:
        return os.dup(self.slave_fd)

    def _serve(self) -> None:
        import select
        buf = b""
        while not self._stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                buf += os.read(self.master, 64)
            except OSError:
                return
            while len(buf) >= 8:
                req, buf = buf[:8], buf[8:]
                if struct.unpack("<H", req[-2:])[0] != crc16(req[:-2]) or req[0] != self.slave:
                    continue
                if self.mute:
                    continue
                fn = req[1]
                if self.exception:
                    os.write(self.master, frame(bytes([self.slave, fn | 0x80, 0x02])))
                    continue
                if fn == 0x05:
                    _, _, coil, val = struct.unpack(">BBHH", req[:6])
                    self.coils[coil] = val == 0xFF00
                    self.writes.append((coil, val == 0xFF00))
                    if self.drop_reply:
                        continue
                    resp = frame(req[:6])
                elif fn == 0x01:
                    _, _, start, n = struct.unpack(">BBHH", req[:6])
                    bits = sum(1 << i for i in range(n) if self.coils[start + i])
                    nb = (n + 7) // 8
                    resp = frame(bytes([self.slave, 1, nb]) + bits.to_bytes(nb, "little"))
                else:
                    continue
                if self.garble:
                    resp = resp[:-1] + bytes([resp[-1] ^ 0xFF])
                os.write(self.master, resp)

    def close(self) -> None:
        self._stop = True
        self.t.join(1)
        os.close(self.master)
        os.close(self.slave_fd)


@pytest.fixture
def 板子():
    b = 假板子()
    yield b
    b.close()


def test_CRC按Modbus算():
    # 标准例子:01 03 00 00 00 0A → CRC C5CD(报文里低字节在前:CD C5)
    assert frame(bytes.fromhex("01030000000A")).hex() == "01030000000ac5cd"


def test_写线圈_读线圈(板子):
    r = ModbusRelay(板子.open, timeout_s=0.3)
    r.write_coil(2, True)
    r.write_coil(5, True)
    assert 板子.coils[2] and 板子.coils[5]
    assert r.read_coils(0, 8) == [False, False, True, False, False, True, False, False]
    r.write_coil(2, False)
    assert not 板子.coils[2]
    r.close()


def test_板子不回_CRC错_回异常码_都报错不当成成了(板子):
    r = ModbusRelay(板子.open, timeout_s=0.1, retries=1)
    板子.mute = True
    with pytest.raises(PayloadError, match="没回|不全"):
        r.write_coil(0, True)
    板子.mute, 板子.garble = False, True
    with pytest.raises(PayloadError, match="CRC"):
        r.write_coil(0, True)
    板子.garble, 板子.exception = False, True
    with pytest.raises(PayloadError, match="异常码"):
        r.write_coil(0, True)
    r.close()


def test_站号不对的应答不认(板子):
    r = ModbusRelay(板子.open, slave=2, timeout_s=0.1, retries=0)
    with pytest.raises(PayloadError):
        r.write_coil(0, True)                        # 板子是 1 号,不理 2 号的请求
    r.close()


def test_配置_只认三路_线圈不许重复(tmp_path):
    p = tmp_path / "payload.json"
    p.write_text(json.dumps({"relay": {"coils": {"strobe": 0, "siren": 1, "spotlight": 2},
                                       "baud": 9600, "slave": 3},
                             "audio": {"device": "plughw:1,0"}}))
    c = PayloadConfig.load(p)
    assert c.coils == {"strobe": 0, "siren": 1, "spotlight": 2} and c.slave == 3
    assert c.port == "/dev/ttyCH9344USB5" and c.audio_device == "plughw:1,0"
    for bad in ({"relay": {"coils": {"horn": 0}}}, {"relay": {"coils": {"siren": 0, "strobe": 0}}},
                {"relay": {"coils": {"siren": -1}}}, [1]):
        p.write_text(json.dumps(bad))
        with pytest.raises(PayloadError):
            PayloadConfig.load(p)


def _上装(板子, **audio) -> Payload:
    cfg = PayloadConfig(coils={"strobe": 0, "siren": 1, "spotlight": 2}, **audio)
    return Payload(cfg, relay=ModbusRelay(板子.open, timeout_s=0.3))


async def test_开一路带最长时间_到点自己关_再开从现在重算(板子):
    p = _上装(板子)
    await p.set("siren", True, 0.3)
    assert 板子.coils[1] and p.state()["siren"] is True
    await asyncio.sleep(0.2)
    await p.set("siren", True, 0.3)                  # 再开一次:从现在重算
    await asyncio.sleep(0.2)
    assert 板子.coils[1], "重开之后不该按第一次的时间关"
    # 到点之后由看护循环关(每 RETRY_S 一拍):等它,不赌固定的睡眠时长(CI 慢机上挂过)
    assert await _等到(lambda: not 板子.coils[1] and p.state()["siren"] is False)
    await p.close()


async def test_没接的一路_时长不对_都拒(板子):
    p = Payload(PayloadConfig(coils={"siren": 1}), relay=ModbusRelay(板子.open))
    with pytest.raises(PayloadError, match="没接"):
        await p.set("spotlight", True, 5)
    for bad in (0, -1, 601):
        with pytest.raises(PayloadError, match="max_s"):
            await p.set("siren", True, bad)
    assert not p.has("speaker") and p.has("siren") and not p.has("strobe")
    await p.close()


async def test_起来先全关_收尾也全关(板子):
    板子.coils[:3] = [True, True, True]              # 上次进程死掉时开着的
    p = _上装(板子)
    await p.start()
    assert 板子.coils[:3] == [False, False, False]
    await p.set("strobe", True, 60)
    await p.set("spotlight", True, 60)
    await p.close()
    assert 板子.coils[:3] == [False, False, False]


async def test_到点关不掉_过一秒再关(板子):
    p = _上装(板子)
    await p.set("spotlight", True, 0.1)
    板子.mute = True
    await asyncio.sleep(0.6)                          # 到点那一下关不掉(板子不回)
    assert 板子.coils[2]
    板子.mute = False
    assert await _等到(lambda: not 板子.coils[2]), "关不掉要接着关"
    await p.close()


class 假进程:
    def __init__(self, log, args, secs):
        self.args, self.returncode = args, None
        self._done = asyncio.Event()
        log.append(args)
        asyncio.get_running_loop().call_later(secs, self._finish)

    def _finish(self):
        if self.returncode is None:
            self.returncode = 0
            self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._done.set()


def _假跑(log, secs=0.05):
    async def run(*args, **kw):
        return 假进程(log, args, secs)
    return run


async def test_喇叭放录好的话术_话术名不许带路径(tmp_path):
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    log: list = []
    p = Payload(PayloadConfig(audio_device="plughw:1,0", clips_dir=str(tmp_path)), run=_假跑(log))
    await p.sound("warn-zh", 5)
    await asyncio.sleep(0.1)
    assert log == [("aplay", "-q", "-D", "plughw:1,0", str(tmp_path / "warn-zh.wav"))]
    for bad in ("../etc/passwd", "a/b", ".hidden", "nope"):
        with pytest.raises(PayloadError):
            await p.sound(bad, 5)
    await p.close()


async def test_喇叭到点掐掉_新的一段打断旧的(tmp_path):
    (tmp_path / "long.wav").write_bytes(b"RIFF")
    log: list = []
    p = Payload(PayloadConfig(audio_device="hw:1", clips_dir=str(tmp_path)), run=_假跑(log, 10))
    await p.sound("long", 0.2)
    await asyncio.sleep(0.05)
    assert p.playing()
    proc = p._proc
    assert await _等到(lambda: not p.playing()), "到点要掐"
    assert proc.returncode == -9, "到点要掐掉播放进程"
    await p.sound("long", 5)
    await asyncio.sleep(0.05)
    first = p._proc
    await p.sound("long", 5)
    assert first.returncode == -9, "新的一段打断旧的"
    await p.close()
    assert not p.playing()


async def test_TTS_没配就拒_配了先合成再放(tmp_path):
    log: list = []
    p = Payload(PayloadConfig(audio_device="hw:1"), run=_假跑(log))
    with pytest.raises(PayloadError, match="没配 TTS"):
        await p.sound("tts:zh:请离开", 5)
    p = Payload(PayloadConfig(audio_device="hw:1",
                              tts_command="espeak-ng -v {lang} -w {wav} {text}"), run=_假跑(log))
    await p.sound("tts:en:Please leave now", 5)
    await asyncio.sleep(0.2)
    assert log[0][:4] == ("espeak-ng", "-v", "en", "-w") and log[0][5] == "Please leave now"
    assert log[1][0] == "aplay" and log[1][-1] == log[0][4]
    for bad in ("tts:zh:", "tts:中文:hi", "tts:en:" + "x" * 301):
        with pytest.raises(PayloadError):
            await p.sound(bad, 5)
    await p.close()


async def test_没接喇叭_放就拒(tmp_path):
    p = Payload(PayloadConfig())
    with pytest.raises(PayloadError, match="没接喇叭"):
        await p.sound("x", 5)


async def test_真狗HAL装上上装_能力照实报_入口走继电器板和喇叭(板子, tmp_path):
    from d1max_adapter_d1max.hal import D1MaxHal
    from d1max_contract.hal import HalUnsupported
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    log: list = []
    p = Payload(PayloadConfig(coils={"strobe": 0, "siren": 1}, audio_device="hw:1",
                              clips_dir=str(tmp_path)),
                relay=ModbusRelay(板子.open, timeout_s=0.3), run=_假跑(log))
    hal = D1MaxHal(backend=object(), payload=p)               # type: ignore[arg-type]
    acts = hal.hal_capabilities().actuators
    assert acts["strobe"] and acts["siren"] and acts["speaker"] and not acts["spotlight"]
    await hal.siren(True, 5)
    await hal.strobe("warn", "flash", 5)
    assert 板子.coils[0] and 板子.coils[1]
    await hal.siren(False, 0)
    await hal.strobe("warn", "off", 0)
    assert not 板子.coils[0] and not 板子.coils[1]
    with pytest.raises(HalUnsupported):
        await hal.spotlight(True, 5)
    await hal.sound("warn-zh", 5)
    await asyncio.sleep(0.1)
    assert log and log[-1][-1].endswith("warn-zh.wav")
    await hal.sound("", 0)
    assert not p.playing()
    assert hal.sound_clips() == ("warn-zh",) and hal.sound_tts() is False
    await p.close()


def test_真狗HAL没装上装_四路都报没有():
    from d1max_adapter_d1max.hal import D1MaxHal
    hal = D1MaxHal(backend=object())                          # type: ignore[arg-type]
    acts = hal.hal_capabilities().actuators
    assert not any(acts[k] for k in ("strobe", "siren", "speaker", "spotlight"))
    assert hal.sound_clips() == ()


async def test_话术名不许越出话术目录(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    (tmp_path / "secret.wav").write_bytes(b"RIFF")             # 话术目录外面真有这个文件
    log: list = []
    p = Payload(PayloadConfig(audio_device="hw:1", clips_dir=str(clips)), run=_假跑(log))
    for bad in ("../secret", "..", "/tmp/x"):
        with pytest.raises(PayloadError, match="话术名"):
            await p.sound(bad, 5)
    assert log == []


async def test_真狗HAL断开_上装全关(板子):
    from d1max_adapter_d1max.hal import D1MaxHal

    class 假旁路:
        async def close(self):
            pass
    p = _上装(板子)
    hal = D1MaxHal(backend=假旁路(), payload=p)                 # type: ignore[arg-type]
    hal._payload_up = True                                     # 当它连上过(连接要真旁路进程)
    await hal.siren(True, 60)
    await hal.spotlight(True, 60)
    assert 板子.coils[1] and 板子.coils[2]
    await hal.close()
    assert not any(板子.coils[:3]), "断开:声光全关"


# -------------------------------------------- W21 外审:关闭保障、起来全关、喇叭的真结果


async def _等到(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def _快(p: Payload) -> Payload:
    p.RETRY_S = 0.1                                       # 看护循环快一点,测试别等太久
    p.relay.timeout_s, p.relay.retries = 0.1, 0
    return p


async def test_外审1_开着时手动关失败_原截止时间过了照样关(板子):
    p = _快(_上装(板子))
    await p.set("siren", True, 0.5)
    板子.mute = True
    with pytest.raises(PayloadError):
        await p.set("siren", False, 0)                  # 关的那一下板子没回
    assert 板子.coils[1] and "siren" in p.problems()
    await asyncio.sleep(0.6)                              # 原截止时间也过了
    板子.mute = False
    assert await _等到(lambda: not 板子.coils[1]), "关没成要一直接着关"
    assert await _等到(lambda: p.problems() == [])
    await p.close()


async def test_外审1_再开一次失败_不丢关闭保障(板子):
    p = _快(_上装(板子))
    await p.set("spotlight", True, 60)
    板子.mute = True
    with pytest.raises(PayloadError):
        await p.set("spotlight", True, 60)               # 再开(续时间)没成
    板子.mute = False
    assert await _等到(lambda: not 板子.coils[2]), "开没成就当「要关」接着关"
    await p.close()


async def test_外审1_板子执行了开_应答丢了_照样关上(板子):
    p = _快(_上装(板子))
    await p.start()                                       # 先确认过「关」:之后才真靠「不明」来补关
    assert p.state()["strobe"] is False
    板子.drop_reply = True
    with pytest.raises(PayloadError):
        await p.set("strobe", True, 60)
    assert p.state()["strobe"] is None, "应答丢了:状态不明"
    assert 板子.coils[0], "板子其实开了"
    板子.drop_reply = False
    assert await _等到(lambda: not 板子.coils[0])
    await p.close()


async def test_外审1_到点关与新命令不交叉_新开的按新截止时间(板子):
    p = _快(_上装(板子))
    await p.set("siren", True, 0.3)
    await asyncio.sleep(0.25)
    await p.set("siren", True, 1.0)                       # 快到点时又开:按新的算
    await asyncio.sleep(0.3)
    assert 板子.coils[1], "旧的到点不许把新开的关掉"
    assert await _等到(lambda: not 板子.coils[1], 2.0)
    await p.close()


async def test_外审2_起来全关失败_接着关_期间报故障(板子):
    板子.coils[:3] = [True, True, True]                   # 上次进程死掉时开着的
    板子.mute = True
    p = _快(_上装(板子))
    await p.start()                                       # 不挡代理起来
    assert sorted(p.problems()) == ["siren", "spotlight", "strobe"]
    assert 板子.coils[:3] == [True, True, True]
    板子.mute = False
    assert await _等到(lambda: 板子.coils[:3] == [False, False, False]), "通了之后接着关"
    assert await _等到(lambda: p.problems() == [])
    await p.close()


async def test_外审2_真狗HAL把关不上的几路当故障报(板子):
    from d1max_adapter_d1max.hal import D1MaxHal

    class 假旁路:
        def current_faults(self, fresh_s):
            return []
    板子.mute = True
    p = _快(_上装(板子))
    await p.start()
    hal = D1MaxHal(backend=假旁路(), payload=p)            # type: ignore[arg-type]
    codes = sorted(f.code for f in await hal.faults())
    assert codes == ["payload_siren", "payload_spotlight", "payload_strobe"]
    assert all(not f.fatal and "接着关" in f.text for f in await hal.faults())
    板子.mute = False
    assert await _等到(lambda: p.problems() == [])
    assert await hal.faults() == ()
    await p.close()


class 坏进程(假进程):
    def __init__(self, log, args, secs, rc):
        super().__init__(log, args, secs)
        self._rc = rc

    def _finish(self):
        if self.returncode is None:
            self.returncode = self._rc
            self._done.set()


async def test_外审3_播放器不存在_当场报错_不回调(tmp_path):
    (tmp_path / "w.wav").write_bytes(b"RIFF")

    async def 没有播放器(*args, **kw):
        raise FileNotFoundError("aplay")
    ends: list = []
    p = Payload(PayloadConfig(audio_device="hw:1", clips_dir=str(tmp_path)), run=没有播放器)
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    with pytest.raises(PayloadError, match="放不出来"):
        await p.sound("w", 5)
    assert not p.playing() and ends == []


async def test_外审3_TTS退出码不对_回调失败_不放(tmp_path):
    log: list = []
    ends: list = []

    async def run(*args, **kw):
        return 坏进程(log, args, 0.01, 1)
    p = Payload(PayloadConfig(audio_device="hw:1", tts_command="espeak-ng -w {wav} {text}"),
                run=run)
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    await p.sound("tts:en:hello", 5)
    assert await _等到(lambda: ends)
    assert ends[0][0] is False and "TTS 合成失败" in ends[0][1]
    assert len(log) == 1, "合成失败就不放"


async def test_复查2_TTS合成不占着调用方_合成中关掉_掐掉合成_也不再放(tmp_path):
    log: list = []
    ends: list = []
    p = Payload(PayloadConfig(audio_device="hw:1", tts_command="espeak-ng -w {wav} {text}"),
                run=_假跑(log, 30))                       # 合成要 30 秒
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await p.sound("tts:zh:请离开", 60)
    assert loop.time() - t0 < 0.5, "不在调用方里等合成"
    tts_proc = p._proc
    assert tts_proc.args[0] == "espeak-ng" and tts_proc.returncode is None
    await p.stop_sound()                                  # 合成中关喇叭
    assert tts_proc.returncode == -9, "合成进程当场掐掉"
    await asyncio.sleep(0.2)
    assert [a[0] for a in log] == ["espeak-ng"], "关掉之后不许再起播放器"
    assert ends == [] and not p.playing()


async def test_复查2_合成完了正要起播放器时被关_起出来的也掐掉(tmp_path):
    log: list = []
    spawned: list = []
    gate = asyncio.Event()

    async def run(*args, **kw):
        if args[0] == "aplay":
            await gate.wait()                             # 起播放器这一下卡住
        pr = 假进程(log, args, 0.01 if args[0] != "aplay" else 30)
        spawned.append(pr)
        return pr
    p = Payload(PayloadConfig(audio_device="hw:1", tts_command="espeak-ng -w {wav} {text}"),
                run=run)
    await p.sound("tts:zh:请离开", 60)
    await asyncio.sleep(0.1)                              # 合成完了,卡在起播放器
    stop = asyncio.ensure_future(p.stop_sound())
    await asyncio.sleep(0.05)
    gate.set()
    await stop
    await asyncio.sleep(0.05)
    assert spawned[-1].args[0] == "aplay" and spawned[-1].returncode == -9


async def test_外审3_放完_放坏了_到点_都回调_被打断的不回调(tmp_path):
    (tmp_path / "w.wav").write_bytes(b"RIFF")
    ends: list = []
    log: list = []
    rcs = iter([0, 1])

    async def run(*args, **kw):
        return 坏进程(log, args, 0.05, next(rcs, 0))
    p = Payload(PayloadConfig(audio_device="hw:1", clips_dir=str(tmp_path)), run=run)
    p.on_sound_end = lambda ok, why: ends.append((ok, why))
    await p.sound("w", 5)                                 # 短话术:0.05 s 放完
    assert await _等到(lambda: len(ends) == 1)
    assert ends[0] == (True, "放完了")
    await p.sound("w", 5)                                 # 退出码 1
    assert await _等到(lambda: len(ends) == 2)
    assert ends[1][0] is False and "退出码 1" in ends[1][1]
    p._run = _假跑(log, 10)
    await p.sound("w", 0.1)                               # 到点
    assert await _等到(lambda: len(ends) == 3) and ends[2] == (True, "到点了")
    await p.sound("w", 5)
    await asyncio.sleep(0.05)
    await p.sound("w", 5)                                 # 打断上一段:上一段不回调
    await p.stop_sound()                                  # 关:也不回调
    await asyncio.sleep(0.1)
    assert len(ends) == 3


# -------------------------------------------- W40:上装 V1.2(决策 70)


V12 = {"relay": {"coils": {"spot_l": 0, "spot_r": 1, "bcn_r": 2, "bcn_g": 3, "bcn_b": 4,
                           "amp": 5}},
       "spotlight": {"left": "spot_l", "right": "spot_r"},
       "strobe": {"colors": {"red": "bcn_r", "yellow": ["bcn_r", "bcn_g"], "green": "bcn_g",
                             "blue": "bcn_b"}, "default": "red"},
       "siren": {"clip": "siren"},
       "audio": {"device": "hw:1", "amp": "amp", "amp_idle_s": 0.2}}


def _v12(tmp_path, 板子=None, *, run=None, **改) -> Payload:
    d = json.loads(json.dumps(V12))
    for k, v in 改.items():
        d[k] = v
    d["audio"]["clips_dir"] = str(tmp_path)
    p = tmp_path / "payload.json"
    p.write_text(json.dumps(d))
    cfg = PayloadConfig.load(p)
    relay = ModbusRelay(板子.open, timeout_s=0.1, retries=0) if 板子 is not None else None
    pl = Payload(cfg, relay=relay, run=run)
    pl.RETRY_S = 0.1
    return pl


def test_V12配置_线圈起名_各路引用(tmp_path):
    p = _v12(tmp_path)
    c = p.cfg
    assert c.spotlight == {"left": ("spot_l",), "right": ("spot_r",)}
    assert c.strobe_colors["yellow"] == ("bcn_r", "bcn_g") and c.strobe_default == "red"
    assert c.siren_clip == "siren" and not c.siren_coil and c.amp_coil == "amp"
    assert c.role_of("spot_r") == "右聚光灯" and c.role_of("bcn_g") == "警灯"
    assert c.role_of("amp") == "功放"


@pytest.mark.parametrize("改, 说", [
    ({"spotlight": {"left": "spot_l"}}, "left、right 都写"),
    ({"spotlight": {"left": "spot_l", "right": "nope"}}, "不在 relay.coils"),
    ({"spotlight": {"left": "spot_l", "right": "bcn_r"}}, "同时归"),
    ({"strobe": {"colors": {"red": "bcn_r"}, "default": "red"}}, "没有哪一路用它"),
    ({"strobe": {"colors": {"Red": ["bcn_r", "bcn_g", "bcn_b"]}}}, "颜色名"),
    ({"strobe": {"colors": {"red": ["bcn_r", "bcn_g", "bcn_b"]}, "default": "pink"}}, "default"),
    ({"strobe": {"colors": {"red": ["bcn_r", "bcn_g", "bcn_b"]}, "blink_s": 0.1}}, "blink_s"),
    ({"siren": {"clip": "siren", "coil": "amp"}}, "同时归"),     # W46 起两样能一起配
    ({"siren": {"clip": "../x"}}, "siren.clip"),
])
def test_V12配置_写错就拒(tmp_path, 改, 说):
    with pytest.raises(PayloadError, match=说):
        _v12(tmp_path, **改)


def test_V12配置_喇叭警笛和功放要先接喇叭(tmp_path):
    with pytest.raises(PayloadError, match="audio.device"):
        _v12(tmp_path, audio={"amp": "amp"})


async def test_V12左右灯_能只开一边_只关一边(板子, tmp_path):
    p = _v12(tmp_path, 板子)
    await p.set("spotlight", True, 60, side="left")
    assert 板子.coils[0] and not 板子.coils[1]
    await p.set("spotlight", True, 60, side="right")
    assert 板子.coils[0] and 板子.coils[1]
    await p.set("spotlight", False, 0, side="left")
    assert not 板子.coils[0] and 板子.coils[1]
    await p.set("spotlight", True, 60)                     # 两边
    await p.set("spotlight", False, 0)
    assert not 板子.coils[0] and not 板子.coils[1]
    assert p.options()["spotlight_sides"] == ["left", "right"]
    await p.close()


async def test_老配置的聚光灯不分左右_只开一边就拒(板子):
    p = _上装(板子)
    with pytest.raises(PayloadError, match="不分左右"):
        await p.set("spotlight", True, 5, side="left")
    assert "spotlight_sides" not in p.options() and "strobe_colors" not in p.options()
    await p.close()


async def test_V12警灯_按颜色合线圈_换色先关后开(板子, tmp_path):
    p = _v12(tmp_path, 板子)
    await p.start()
    await p.set("strobe", True, 60)                         # 缺省红
    assert 板子.coils[2:5] == [True, False, False]
    await p.set("strobe", True, 60, color="yellow")
    assert 板子.coils[2:5] == [True, True, False]
    n = len(板子.writes)
    await p.set("strobe", True, 60, color="blue")
    assert 板子.coils[2:5] == [False, False, True]
    assert [w for w in 板子.writes[n:] if 2 <= w[0] <= 4] == [(2, False), (3, False), (4, True)], \
        "先关旧颜色再开新颜色"
    with pytest.raises(PayloadError, match="没有 pink 色"):
        await p.set("strobe", True, 60, color="pink")
    await p.set("strobe", False, 0)
    assert 板子.coils[2:5] == [False, False, False]
    o = p.options()
    assert o["strobe_colors"] == ["red", "yellow", "green", "blue"] and o["strobe_default"] == "red"
    await p.close()


async def test_V12警灯换色开到一半没成_开了的那个也关回去(板子, tmp_path):
    p = _v12(tmp_path, 板子)
    await p.start()

    orig = p.relay.write_coil

    def 绿的写不进去(coil, on):
        if coil == 3 and on:
            raise PayloadError("没回")
        return orig(coil, on)
    p.relay.write_coil = 绿的写不进去
    with pytest.raises(PayloadError):
        await p.set("strobe", True, 60, color="yellow")
    assert 板子.coils[2], "红的那个其实开了"
    p.relay.write_coil = orig
    assert await _等到(lambda: not 板子.coils[2]), "黄只开了一半是错的颜色:都关回去"
    await p.close()


async def test_V12警灯软件打闪_到点停_灭的半拍不算故障(板子, tmp_path):
    d = dict(V12["strobe"], blink_s=0.5)
    p = _v12(tmp_path, 板子, strobe=d)
    await p.start()
    await p.set("strobe", True, 1.6)
    seen: list[bool] = []
    probs: list[list[str]] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + 1.4
    while loop.time() < end:
        seen.append(板子.coils[2])
        probs.append(p.problems())
        await asyncio.sleep(0.05)
    assert True in seen and False in seen, "要一亮一灭"
    assert all(x == [] for x in probs), "打闪灭的那半拍不是故障"
    assert await _等到(lambda: not 板子.coils[2] and not p._blinking(), 2.0), "到点停在灭"
    await p.close()


async def test_V12功放_说话前合上_不出声了过一会断开(板子, tmp_path):
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    log: list = []
    order: list = []

    async def run(*args, **kw):
        order.append(("play", 板子.coils[5]))
        return 假进程(log, args, 0.1)
    p = _v12(tmp_path, 板子, run=run)
    await p.start()
    await p.sound("warn-zh", 5)
    assert order == [("play", True)], "功放先合上再放"
    assert await _等到(lambda: not p.playing())
    assert 板子.coils[5], "刚放完:功放还留一会(连着放话术不来回开关)"
    assert await _等到(lambda: not 板子.coils[5], 2.0), "闲置够了断开"
    await p.close()


async def test_V12功放合不上_喇叭当没放(板子, tmp_path):
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    log: list = []
    p = _v12(tmp_path, 板子, run=_假跑(log))
    板子.mute = True
    with pytest.raises(PayloadError, match="功放开不了"):
        await p.sound("warn-zh", 5)
    assert log == [] and not p.playing()
    板子.mute = False
    await p.close()


async def test_V12喇叭警笛_循环放_到点停_关就停(tmp_path):
    (tmp_path / "siren.wav").write_bytes(b"RIFF")
    log: list = []
    p = _v12(tmp_path, None, run=_假跑(log, 0.1), relay={"coils": {}},
             spotlight={}, strobe={}, audio={"device": "hw:1"})
    assert p.has("siren") and p.options()["siren_via"] == "speaker"
    await p.set("siren", True, 0.45)
    assert await _等到(lambda: len(log) >= 3), "一遍放完接着放"
    assert all(a[-1].endswith("siren.wav") for a in log)
    assert await _等到(lambda: not p.siren_on(), 2.0), "到点停"
    n = len(log)
    await asyncio.sleep(0.3)
    assert len(log) == n
    await p.set("siren", True, 30)
    await asyncio.sleep(0.05)
    proc = p._siren_proc
    await p.set("siren", False, 0)
    assert not p.siren_on() and proc.returncode == -9, "关:当场掐掉"
    await p.close()


async def test_V12喇叭警笛_说话时让一让_说完接着响(tmp_path):
    (tmp_path / "siren.wav").write_bytes(b"RIFF")
    (tmp_path / "notified-zh.wav").write_bytes(b"RIFF")
    log: list = []
    p = _v12(tmp_path, None, run=_假跑(log, 0.3), relay={"coils": {}},
             spotlight={}, strobe={}, audio={"device": "hw:1"})
    await p.set("siren", True, 30)
    await asyncio.sleep(0.05)
    siren1 = p._siren_proc
    await p.sound("notified-zh", 5)
    assert siren1.returncode == -9, "说话:先掐掉这一遍警笛"
    assert p._siren_proc is None
    await asyncio.sleep(0.1)
    assert log[-1][-1].endswith("notified-zh.wav"), "说话时警笛不抢喇叭"
    assert await _等到(lambda: not p.playing())
    assert await _等到(lambda: p._siren_proc is not None), "说完接着响"
    assert p.siren_error == "", "被说话掐掉的那一遍不算放坏"
    await p.close()


async def test_V12喇叭警笛_没有警笛声文件就不报这一路(tmp_path):
    p = _v12(tmp_path, None, relay={"coils": {}}, spotlight={}, strobe={},
             audio={"device": "hw:1"})
    assert not p.has("siren") and "siren_via" not in p.options()
    with pytest.raises(PayloadError, match="警笛声"):
        await p.set("siren", True, 5)
    await p.set("siren", False, 0)                          # 关永远可以


async def test_V12喇叭警笛放坏了_报警笛故障_好了就没(tmp_path):
    from d1max_adapter_d1max.hal import D1MaxHal
    (tmp_path / "siren.wav").write_bytes(b"RIFF")
    log: list = []
    rc = {"v": 1}

    async def run(*args, **kw):
        return 坏进程(log, args, 0.05, rc["v"])

    class 假旁路:
        def current_faults(self, fresh_s):
            return []
    p = _v12(tmp_path, None, run=run, relay={"coils": {}}, spotlight={}, strobe={},
             audio={"device": "hw:1"})
    p.SIREN_RETRY_S = 0.1
    hal = D1MaxHal(backend=假旁路(), payload=p)              # type: ignore[arg-type]
    await p.set("siren", True, 30)
    assert await _等到(lambda: p.problems() == ["siren"])
    f = await hal.faults()
    assert [x.code for x in f] == ["payload_siren"] and "退出码 1" in f[0].text
    rc["v"] = 0
    assert await _等到(lambda: p.problems() == []), "放成了就清掉"
    await p.close()


async def test_V12故障按路合起来报_线圈名站点不认识(板子, tmp_path):
    from d1max_adapter_d1max.hal import D1MaxHal

    class 假旁路:
        def current_faults(self, fresh_s):
            return []
    板子.mute = True
    p = _v12(tmp_path, 板子)
    await p.start()
    hal = D1MaxHal(backend=假旁路(), payload=p)              # type: ignore[arg-type]
    f = {x.code: x.text for x in await hal.faults()}
    assert sorted(f) == ["payload_amp", "payload_spotlight", "payload_strobe"]
    assert "左聚光灯、右聚光灯" in f["payload_spotlight"] and "接着关" in f["payload_strobe"]
    assert hal.deter_options()["spotlight_sides"] == ["left", "right"]
    板子.mute = False
    assert await _等到(lambda: p.problems() == [])
    await p.close()


async def test_V12真狗HAL_颜色和左右灯走到上装(板子, tmp_path):
    from d1max_adapter_d1max.hal import D1MaxHal
    p = _v12(tmp_path, 板子)
    hal = D1MaxHal(backend=object(), payload=p)             # type: ignore[arg-type]
    await hal.strobe("green", "flash", 5)
    assert 板子.coils[2:5] == [False, True, False]
    await hal.strobe("warn", "flash", 5)                    # 缺省颜色
    assert 板子.coils[2:5] == [True, False, False]
    await hal.strobe("warn", "off", 0)
    assert 板子.coils[2:5] == [False, False, False]
    await hal.spotlight_side("right", True, 5)
    assert 板子.coils[:2] == [False, True]
    await hal.spotlight(False, 0)
    assert 板子.coils[:2] == [False, False]
    await p.close()


async def test_V12设备健康_继电器板_喇叭_喇叭警笛(板子, tmp_path):
    from d1max_adapter_d1max.payload import alsa_card_present
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    p = _v12(tmp_path, 板子, run=_假跑([]))
    p.card_present = lambda dev: True
    h = {d["id"]: d for d in p.health()}
    assert h["relay"]["state"] == "offline", "还一个都没确认过"
    assert h["siren"]["state"] == "error" and "警笛声" in h["siren"]["detail"]
    await p.start()
    h = {d["id"]: d for d in p.health()}
    assert h["relay"]["state"] == "online" and h["speaker"]["state"] == "online"
    板子.mute = True
    with pytest.raises(PayloadError):
        await p.set("spotlight", True, 5, side="left")
    h = {d["id"]: d for d in p.health()}
    assert h["relay"]["state"] == "error" and "左聚光灯" in h["relay"]["detail"]
    板子.mute = False
    assert await _等到(lambda: {d["id"]: d for d in p.health()}["relay"]["state"] == "online")
    p.card_present = lambda dev: False
    assert {d["id"]: d for d in p.health()}["speaker"]["state"] == "offline"
    await p.close()
    asound = tmp_path / "asound"
    (asound / "card1").mkdir(parents=True)
    (asound / "Device").mkdir()
    assert alsa_card_present("plughw:1,0", asound) is True
    assert alsa_card_present("hw:2", asound) is False
    assert alsa_card_present("hw:CARD=Device,DEV=0", asound) is True
    assert alsa_card_present("default", asound) is None


async def test_V12设备健康_喇叭放坏了算故障_放成了就好(tmp_path):
    (tmp_path / "w.wav").write_bytes(b"RIFF")
    rc = {"v": 1}

    async def run(*args, **kw):
        return 坏进程([], args, 0.02, rc["v"])
    p = Payload(PayloadConfig(audio_device="hw:1", clips_dir=str(tmp_path)), run=run)
    p.card_present = lambda dev: None
    await p.sound("w", 5)
    assert await _等到(lambda: not p.playing())
    assert p.health()[0]["state"] == "error" and "退出码 1" in p.health()[0]["detail"]
    rc["v"] = 0
    await p.sound("w", 5)
    assert await _等到(lambda: not p.playing())
    assert p.health()[0]["state"] == "online"


def test_V12配置样例读得进来_警笛声生成得出来(tmp_path):
    import subprocess
    import sys
    import wave
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    c = PayloadConfig.load(root / "deploy" / "payload.v12.example.json")
    assert set(c.spotlight) == {"left", "right"} and c.siren_clip == "siren"
    out = tmp_path / "siren.wav"
    subprocess.run([sys.executable, "-m", "d1max_adapter_d1max.siren", str(out)], check=True,
                   capture_output=True)
    with wave.open(str(out)) as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000 and w.getnframes() == 32000
