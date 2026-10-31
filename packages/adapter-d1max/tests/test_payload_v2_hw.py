"""W56 上装 V2 硬件组 10-31 答复(`docs/上装V2-硬件对接说明.md` 第八节):
- 继电器板中盛 4 入 4 出:站号 1、38400 8N1;05 写线圈 0–3;04 读输入寄存器 0x0032(最低位 IN1);
  保持寄存器 0x0030 = 50 时断线 5 秒全关 —— 软件至少每秒跟板子说一次话;
- 不用主控 GPIO:风扇堵转报警线接继电器板 IN1–IN3,报警电平可配置(在 boardhealth 那边测);
- 功放没有 MUTE 线:不播报时用系统声音设置静音,播报时打开。"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest
from test_payload import _等到, 假板子

from d1max_adapter_d1max.payload import (
    AlsaMute,
    ModbusRelay,
    Payload,
    PayloadConfig,
    PayloadError,
    frame,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def 板子():
    b = 假板子()
    yield b
    b.close()


def _example(tmp_path, **改) -> PayloadConfig:
    raw = json.loads((ROOT / "deploy" / "payload.v2.example.json").read_text())
    for path, v in 改.items():
        a, b = path.split(".")
        raw[a][b] = v
    raw["audio"]["clips_dir"] = str(tmp_path)
    p = tmp_path / "payload.json"
    p.write_text(json.dumps(raw))
    return PayloadConfig.load(p)


def _pl(tmp_path, 板子, *, mute=None, **改) -> Payload:
    cfg = _example(tmp_path, **改)
    pl = Payload(cfg, relay=ModbusRelay(板子.open, timeout_s=0.1, retries=0), mute=mute)
    pl.RETRY_S = 0.05
    return pl


class 假混音:
    def __init__(self) -> None:
        self.muted = True
        self.history: list[bool] = []

    def set(self, value: bool) -> None:
        self.muted = value
        self.history.append(value)

    def close(self) -> None:
        pass


def test_硬件组的报文例子_打开CH1():
    body = struct.pack(">BBHH", 1, 0x05, 0, 0xFF00)
    assert frame(body) == bytes.fromhex("01050000FF008C3A")


def test_V2配置样例直接能用_38400_心跳_断线全关_系统静音(tmp_path):
    c = _example(tmp_path)
    assert (c.baud, c.slave) == (38400, 1)
    assert c.coils == {"spot_l": 0, "spot_r": 1, "siren": 2, "beacon": 3}
    assert c.inputs_reg == 0x32 and (c.watchdog_reg, c.watchdog_ds) == (0x30, 50)
    assert not c.mute_chip, "V2 不用主控 GPIO"
    assert c.mixer_card and c.mixer_control
    assert c.amp_lead_s >= 0


@pytest.mark.parametrize("改, 说", [
    ({"relay.inputs_reg": -2}, "inputs_reg"),
    ({"relay.watchdog_ds": 0}, "watchdog_ds"),
    ({"audio.mute": {"mixer": {"card": "", "control": "x"}}}, "mixer"),
    ({"audio.mute": {"mixer": {"card": "a b", "control": "x"}}}, "mixer"),
    ({"audio.mute": {"chip": "gpiochip1", "line": 3, "mixer": {"card": "c", "control": "x"}}},
     "二选一"),
])
def test_V2配置写错就拒(tmp_path, 改, 说):
    with pytest.raises(PayloadError, match=说):
        _example(tmp_path, **改)


async def test_每秒至少问一次板子_灯都关着也问_顺带读到输入(板子, tmp_path):
    p = _pl(tmp_path, 板子, mute=假混音())
    板子.inputs = 0b0101
    await p.start()
    await asyncio.sleep(2.6)
    asks = [t for t, fn in 板子.reqs if fn == 0x04]
    assert len(asks) >= 3, "5 秒不通信板子就全关:至少每秒一次"
    assert max(b - a for a, b in zip(asks, asks[1:], strict=False)) <= 1.1
    assert p.input_bits() == 0b0101
    await p.close()


async def test_读不到输入_过一会儿就当不知道_继电器报故障(板子, tmp_path):
    p = _pl(tmp_path, 板子, mute=假混音())
    p.INPUT_STALE_S = 0.5
    await p.start()
    assert await _等到(lambda: p.input_bits() == 0, 2.0)
    板子.mute = True                                         # 线断了
    assert await _等到(lambda: p.input_bits() is None, 3.0)
    assert await _等到(lambda: next(d for d in p.health() if d["id"] == "relay")["state"]
                       == "error", 3.0)
    板子.mute = False
    assert await _等到(lambda: p.input_bits() == 0, 3.0)
    await p.close()


async def test_断线自动全关没设_设备健康说出来(板子, tmp_path):
    板子.holding[0x30] = 0
    p = _pl(tmp_path, 板子, mute=假混音())
    await p.start()
    assert await _等到(lambda: p.watchdog_seen is not None, 2.0)
    relay = next(d for d in p.health() if d["id"] == "relay")
    assert relay["state"] == "degraded" and "0x0030" in relay["detail"] and "50" in relay["detail"]
    await p.close()
    板子.holding[0x30] = 50
    p = _pl(tmp_path, 板子, mute=假混音())
    await p.start()
    assert await _等到(lambda: p.watchdog_seen == 50, 2.0)
    assert await _等到(lambda: next(d for d in p.health() if d["id"] == "relay")["state"]
                       == "online", 2.0)
    await p.close()


async def test_功放_系统静音_说话前打开_闲了再静音(板子, tmp_path):
    (tmp_path / "hello.wav").write_bytes(b"RIFF")
    mix = 假混音()

    async def 跑(*argv, **kw):
        class P:
            returncode = 0

            async def wait(self):
                await asyncio.sleep(0.05)
                return 0

            def kill(self):
                pass
        assert mix.muted is False, "出声前要先打开"
        return P()
    p = _pl(tmp_path, 板子, mute=mix)
    p._run = 跑
    p.cfg = p.cfg.__class__(**{**p.cfg.__dict__, "amp_idle_s": 0.2})
    await p.start()
    assert mix.muted is True, "起来先静音"
    await p.sound("hello", 5)
    assert await _等到(lambda: not p.playing(), 2.0)
    assert await _等到(lambda: mix.muted is True, 2.0), "闲了再静音"
    assert False in mix.history
    await p.close()


def test_系统静音_调amixer_开关对应mute和unmute():
    calls = []

    def run(argv, **kw):
        calls.append(argv)

        class R:
            returncode = 0
            stderr = ""
        return R()
    m = AlsaMute("rockchipes8388", "Headphone", run=run)
    m.set(True)
    m.set(False)
    assert calls == [["amixer", "-q", "-c", "rockchipes8388", "sset", "Headphone", "mute"],
                     ["amixer", "-q", "-c", "rockchipes8388", "sset", "Headphone", "unmute"]]

    def bad(argv, **kw):
        class R:
            returncode = 1
            stderr = "Unable to find simple control"
        return R()
    with pytest.raises(OSError, match="Unable to find"):
        AlsaMute("c", "x", run=bad).set(True)


def test_生产设置断线全关_写0x0030_读回来(板子):
    from d1max_adapter_d1max.payload import relay_setup
    板子.holding[0x30] = 0
    got = relay_setup(ModbusRelay(板子.open, timeout_s=0.2, retries=0), watchdog_ds=50)
    assert 板子.holding[0x30] == 50 and got["watchdog_ds"] == 50
    assert "重新上电" in got["note"]
