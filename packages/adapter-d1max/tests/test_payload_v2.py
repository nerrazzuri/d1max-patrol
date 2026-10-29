"""W46 上装 V2(硬件版本 D,`docs/上装V2-硬件对接说明.md`):
- 4 路继电器:左灯、右灯、压电警笛、单色警灯;放警报时喇叭跟警笛一起响;
- 功放常电,GPIO 控 MUTE:先放开 MUTE、等一下再出声,闲了再静音;
- 继电器板自己掉电重启以后输出全关:代理要发现,重新下发。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_payload import _假跑, _等到, 假板子, 假进程

from d1max_adapter_d1max.payload import ModbusRelay, Payload, PayloadConfig, PayloadError

ROOT = Path(__file__).resolve().parents[3]

V2 = {"relay": {"coils": {"spot_l": 0, "spot_r": 1, "siren": 2, "beacon": 3}},
      "spotlight": {"left": "spot_l", "right": "spot_r"},
      "strobe": {"colors": {"red": "beacon"}, "default": "red"},
      "siren": {"coil": "siren", "clip": "siren"},
      "audio": {"device": "hw:1", "mute": {"chip": "gpiochip1", "line": 13, "mute_level": "high"},
                "amp_idle_s": 0.2, "amp_lead_s": 0.15}}


@pytest.fixture
def 板子():
    b = 假板子()
    yield b
    b.close()


class 假静音脚:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.history: list[tuple[float, bool]] = []
        self.fail = False
        self.closed = False

    def set(self, value: bool) -> None:
        if self.fail:
            raise OSError("GPIO 写不进去")
        self.value = value
        self.history.append((asyncio.get_running_loop().time(), value))

    def close(self) -> None:
        self.closed = True


def _v2(tmp_path, 板子=None, *, run=None, mute=None, **改) -> Payload:
    d = json.loads(json.dumps(V2))
    for k, v in 改.items():
        d[k] = v
    d["audio"]["clips_dir"] = str(tmp_path)
    p = tmp_path / "payload.json"
    p.write_text(json.dumps(d))
    cfg = PayloadConfig.load(p)
    relay = ModbusRelay(板子.open, timeout_s=0.1, retries=0) if 板子 is not None else None
    pl = Payload(cfg, relay=relay, run=run,
                 mute=mute if mute is not None else 假静音脚(cfg.mute_level == "high"))
    pl.RETRY_S = 0.1
    return pl


def test_V2配置_警笛继电器加喇叭_功放走GPIO(tmp_path):
    c = _v2(tmp_path).cfg
    assert c.siren_coil == "siren" and c.siren_clip == "siren"
    assert (c.mute_chip, c.mute_line, c.mute_level) == ("gpiochip1", 13, "high")
    assert c.amp_lead_s == 0.15 and not c.amp_coil
    assert c.strobe_colors == {"red": ("beacon",)}


@pytest.mark.parametrize("audio, 说", [
    ({"device": "hw:1", "amp": "x", "mute": {"chip": "gpiochip1", "line": 1}}, "只能二选一"),
    ({"device": "hw:1", "mute": {"chip": "gpiochip1", "line": -1}}, "还没填"),
    ({"device": "hw:1", "mute": {"chip": "sda", "line": 1}}, "gpiochip"),
    ({"device": "hw:1", "mute": {"chip": "gpiochip1", "line": 1, "mute_level": "x"}}, "high"),
    ({"device": "hw:1", "mute": {"chip": "gpiochip1", "line": 1}, "amp_lead_s": 5}, "amp_lead_s"),
    ({"mute": {"chip": "gpiochip1", "line": 1}}, "audio.device"),
])
def test_V2配置_功放GPIO写错就拒(tmp_path, audio, 说):
    with pytest.raises(PayloadError, match=说):
        _v2(tmp_path, audio=audio)


def test_V2缺省提前量_GPIO是0点2秒_继电器功放不等(tmp_path):
    c = _v2(tmp_path, audio={"device": "hw:1", "mute": {"chip": "gpiochip1", "line": 1}}).cfg
    assert c.amp_lead_s == 0.2 and c.mute_level == "high"
    assert PayloadConfig(audio_device="hw:1").amp_lead_s == 0.0


async def test_V2功放_先放开MUTE_等够提前量再出声_闲了再静音(tmp_path):
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    mute = 假静音脚(True)
    started: list[float] = []
    log: list = []

    async def run(*args, **kw):
        started.append(asyncio.get_running_loop().time())
        return 假进程(log, args, 0.1)
    p = _v2(tmp_path, run=run, mute=mute)
    await p.start()
    await p.sound("warn-zh", 5)
    assert mute.history and mute.history[0][1] is False, "MUTE 高有效:放开 = 拉低"
    assert started[0] - mute.history[0][0] >= 0.15 - 0.01, "放开 MUTE 以后等够提前量才出声"
    assert await _等到(lambda: not p.playing())
    assert mute.value is False, "刚放完先不静音(连着放不来回切)"
    assert await _等到(lambda: mute.value is True, 2.0), "闲够了静音"
    await p.sound("warn-zh", 5)
    n = len(mute.history)
    await p.sound("warn-zh", 5)
    assert len(mute.history) == n, "已经放开着:不再等提前量、不再写"
    await p.close()
    assert mute.value is True and mute.closed, "收尾:静音、放掉这根线"


async def test_V2功放_MUTE低有效_电平反过来(tmp_path):
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    mute = 假静音脚(False)
    p = _v2(tmp_path, run=_假跑([]), mute=mute,
            audio={"device": "hw:1", "amp_idle_s": 0.1, "amp_lead_s": 0,
                   "mute": {"chip": "gpiochip1", "line": 2, "mute_level": "low"}})
    await p.sound("warn-zh", 5)
    assert mute.value is True, "低有效:放开 = 拉高"
    assert await _等到(lambda: mute.value is False, 2.0)
    await p.close()


async def test_V2功放GPIO写不进去_喇叭当没放_报功放故障(tmp_path):
    (tmp_path / "warn-zh.wav").write_bytes(b"RIFF")
    mute = 假静音脚(True)
    mute.fail = True
    log: list = []
    p = _v2(tmp_path, run=_假跑(log), mute=mute)
    with pytest.raises(PayloadError, match="功放开不了"):
        await p.sound("warn-zh", 5)
    assert log == [] and not p.playing()
    assert "amp" in p.faults() and "MUTE" in p.faults()["amp"]
    assert any(d["id"] == "amp" and d["state"] == "error" for d in p.health())
    mute.fail = False
    await p.sound("warn-zh", 5)
    assert "amp" not in p.faults(), "写进去了就清掉"
    await p.close()


async def test_V2警笛_继电器和喇叭一起响_关就都停(板子, tmp_path):
    (tmp_path / "siren.wav").write_bytes(b"RIFF")
    log: list = []
    p = _v2(tmp_path, 板子, run=_假跑(log, 0.1))
    await p.start()
    assert p.has("siren") and p.options()["siren_via"] == "relay+speaker"
    await p.set("siren", True, 30)
    assert 板子.coils[2], "压电警笛合上"
    assert await _等到(lambda: len(log) >= 2), "喇叭也循环放警笛声"
    assert p.state()["siren"] is True
    await p.set("siren", False, 0)
    assert not 板子.coils[2] and not p.siren_on()
    await p.close()


async def test_V2警笛_没有警笛声文件_继电器照响(板子, tmp_path):
    p = _v2(tmp_path, 板子, run=_假跑([]))
    await p.start()
    assert p.has("siren") and p.options()["siren_via"] == "relay"
    await p.set("siren", True, 30)
    assert 板子.coils[2] and not p.siren_on()
    await p.close()


async def test_V2警笛_继电器板不回_报错_喇叭不单独响(板子, tmp_path):
    (tmp_path / "siren.wav").write_bytes(b"RIFF")
    p = _v2(tmp_path, 板子, run=_假跑([], 0.1))
    板子.mute = True
    with pytest.raises(PayloadError):
        await p.set("siren", True, 30)
    assert not p.siren_on(), "警笛主体没响成:这一下当没开,喇叭也别响"
    板子.mute = False
    await p.close()


async def test_V2继电器板自己掉电重启_代理发现了重新下发(板子, tmp_path):
    p = _v2(tmp_path, 板子)
    p.READBACK_S = 0.2
    await p.start()
    await p.set("spotlight", True, 60, side="left")
    await p.set("strobe", True, 60)
    assert 板子.coils[0] and 板子.coils[3]
    板子.coils = [False] * 8                               # 板子掉电重启:输出全关
    assert await _等到(lambda: 板子.coils[0] and 板子.coils[3], 2.0), "回读发现不对,重新合上"
    assert not 板子.coils[1] and not 板子.coils[2]
    板子.coils[1] = True                                    # 板子上多出一路开着的:关回去
    assert await _等到(lambda: not 板子.coils[1], 2.0)
    await p.close()


def test_V2配置样例_引脚没填就起不来_填了能读(tmp_path):
    raw = json.loads((ROOT / "deploy" / "payload.v2.example.json").read_text())
    tmp = tmp_path / "payload.json"
    tmp.write_text(json.dumps(raw))
    with pytest.raises(PayloadError, match="还没填"):
        PayloadConfig.load(tmp)
    raw["audio"]["mute"]["line"] = 5
    tmp.write_text(json.dumps(raw))
    c = PayloadConfig.load(tmp)
    assert c.coils == {"spot_l": 0, "spot_r": 1, "siren": 2, "beacon": 3}
    assert c.siren_coil and c.siren_clip and c.mute_line == 5
