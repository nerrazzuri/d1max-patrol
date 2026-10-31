"""W56(硬件组 10-31):主控板不接 GPIO,三只风扇用带堵转报警线的型号,报警线接继电器板 IN1–IN3
(IN1、IN2 排风扇,IN3 搅风扇);报警电平高低要等风扇到货才知道,做成可配置。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_agent.boardhealth import BoardConfig, BoardConfigError, BoardMonitor

ROOT = Path(__file__).resolve().parents[3]
FANS = [{"name": "exhaust_l", "relay_input": 0, "alarm_active": "high"},
        {"name": "exhaust_r", "relay_input": 1, "alarm_active": "high"},
        {"name": "stir", "relay_input": 2, "alarm_active": "low"}]


def _mon(tmp_path, bits: list, fans=FANS) -> BoardMonitor:
    cfg = BoardConfig(fans=BoardConfig.parse_fans(fans), log_dir=str(tmp_path / "log"))
    t = [0.0]

    def mono():
        t[0] += 10.0
        return t[0]
    m = BoardMonitor(cfg, emit=lambda k, d: None, mono=mono, wall=lambda: 1_800_000_000.0,
                     read_soc=lambda: {"soc": 50.0}, read_nvme=lambda: 40.0,
                     relay_inputs=lambda: bits[0], read_freqs=dict, read_cooling=dict)
    m.start()
    return m


def _state(m, name):
    return next(d for d in m.health() if d["id"] == f"fan:{name}")


def test_配置_报警线接继电器输入_电平可配():
    got = BoardConfig.parse_fans(FANS)
    assert [(f.name, f.relay_input, f.alarm_active) for f in got] == [
        ("exhaust_l", 0, "high"), ("exhaust_r", 1, "high"), ("stir", 2, "low")]


@pytest.mark.parametrize("fan, 说", [
    ({"name": "a", "relay_input": 16}, "relay_input"),
    ({"name": "a", "relay_input": 0, "alarm_active": "x"}, "alarm_active"),
    ({"name": "a", "relay_input": 0, "chip": "gpiochip1", "line": 1}, "二选一"),
])
def test_配置写错就拒(fan, 说):
    with pytest.raises(BoardConfigError, match=说):
        BoardConfig.parse_fans([fan])


def test_板子配置样例_V2直接能用():
    cfg = BoardConfig.load(ROOT / "deploy" / "board.example.json")
    assert [f.relay_input for f in cfg.fans] == [0, 1, 2]


def test_高电平报警_连着两拍才算停_一只停是警戒(tmp_path):
    bits = [0b100]                                    # stir 低有效:IN3 高 = 正常
    m = _mon(tmp_path, bits)
    m.sample()
    assert m.stopped() == [] and _state(m, "exhaust_l")["state"] == "online"
    bits[0] = 0b101                                   # IN1 报警
    m.sample()
    assert m.stopped() == [], "一拍不算"
    m.sample()
    assert m.stopped() == ["exhaust_l"] and m.level == "warn"
    assert "堵转" in _state(m, "exhaust_l")["detail"]
    bits[0] = 0b100
    m.sample()
    assert m.stopped() == []


def test_低电平报警_反过来(tmp_path):
    bits = [0b000]                                    # IN3 低 = 搅风扇报警
    m = _mon(tmp_path, bits)
    m.sample()
    m.sample()
    assert m.stopped() == ["stir"]


def test_两只停了_降负载(tmp_path):
    bits = [0b011 | 0b100]
    m = _mon(tmp_path, bits)
    m.sample()
    m.sample()
    assert sorted(m.stopped()) == ["exhaust_l", "exhaust_r"] and m.level == "shed"


def test_继电器板读不到_风扇算故障不算停转_通了恢复(tmp_path):
    bits = [None]
    m = _mon(tmp_path, bits)
    m.sample()
    m.sample()
    assert m.stopped() == [] and m.level == "ok"
    assert _state(m, "exhaust_l")["state"] == "error"
    assert "继电器板" in _state(m, "exhaust_l")["detail"]
    bits[0] = 0b100
    m.sample()
    assert _state(m, "exhaust_l")["state"] == "online"


def test_没接上装继电器板_风扇说清楚(tmp_path):
    cfg = BoardConfig(fans=BoardConfig.parse_fans(FANS[:1]), log_dir=str(tmp_path / "log"))
    m = BoardMonitor(cfg, emit=lambda k, d: None, read_soc=lambda: {"soc": 50.0},
                     read_nvme=lambda: None, read_freqs=dict, read_cooling=dict)
    m.start()
    m.sample()
    assert "未配置" in _state(m, "exhaust_l")["detail"]


def test_样例里没有GPIO了():
    raw = json.loads((ROOT / "deploy" / "board.example.json").read_text())
    assert not any("chip" in f for f in raw["fans"])
