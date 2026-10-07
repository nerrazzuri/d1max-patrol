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
