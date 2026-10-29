"""W46(决策 74):上装主控板的温度、风扇。读数换成假的;分级、回差、风扇停转、日志、设备健康、事件。"""

from __future__ import annotations

import csv
import json

import pytest

from d1max_agent.boardhealth import (
    BoardConfig,
    BoardMonitor,
    Limits,
    classify,
    read_nvme,
    read_soc,
)

L = Limits()


@pytest.mark.parametrize("soc, nvme, stopped, want", [
    (60, 50, 0, "ok"),
    (80, 50, 0, "warn"), (60, 65, 0, "warn"), (60, 50, 1, "warn"),
    (85, 50, 0, "shed"), (60, 70, 0, "shed"), (60, 50, 2, "shed"),
    (95, 50, 0, "critical"), (60, 75, 0, "critical"),
    (None, None, 0, "ok"),
])
def test_分三级_决策74的门槛(soc, nvme, stopped, want):
    assert classify(soc, nvme, stopped, "ok", L)[0] == want


def test_回差_降级要比门槛低3度():
    assert classify(83, 50, 0, "shed", L)[0] == "shed", "刚过 85 又掉到 83:还算降负载"
    assert classify(81.9, 50, 0, "shed", L)[0] == "warn"
    assert classify(78, 50, 0, "warn", L)[0] == "warn"
    assert classify(76.9, 50, 0, "warn", L)[0] == "ok"
    assert classify(77.5, 50, 0, "critical", L)[0] == "warn", "从危急一下掉很多:按回差后的算"
    assert classify(96, 50, 0, "warn", L)[0] == "critical", "升级不等回差"


def test_原因说人话():
    lvl, why = classify(86, 71, 2, "ok", L)
    assert lvl == "shed" and "SoC 86" in why and "NVMe 71" in why and "2 只风扇停了" in why


def test_读温度_各温区取摄氏度_NVMe从hwmon读(tmp_path):
    th = tmp_path / "thermal"
    for i, (t, v) in enumerate([("soc-thermal", 55000), ("bigcore0-thermal", 61200),
                                ("gpu-thermal", "坏")]):
        z = th / f"thermal_zone{i}"
        z.mkdir(parents=True)
        (z / "type").write_text(t + "\n")
        (z / "temp").write_text(f"{v}\n")
    assert read_soc(th) == {"soc": 55.0, "bigcore0": 61.2}
    hw = tmp_path / "hwmon"
    for i, (name, temp) in enumerate([("cpu", 40000), ("nvme", 47850)]):
        d = hw / f"hwmon{i}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "temp1_input").write_text(f"{temp}\n")
    assert read_nvme(hw) == pytest.approx(47.85)
    assert read_nvme(tmp_path / "nope") is None


def test_配置_缺省门槛_写错就拒(tmp_path):
    p = tmp_path / "board.json"
    p.write_text(json.dumps({"fans": [{"name": "exhaust_l", "chip": "gpiochip3", "line": 5}],
                             "limits": {"shed_soc": 84}}))
    c = BoardConfig.load(p)
    assert c.fans[0].pulses == 2 and c.fans[0].min_rpm == 300
    assert c.limits.shed_soc == 84 and c.limits.warn_soc == 80
    for bad in ({"fans": [{"name": "Bad", "chip": "gpiochip3", "line": 5}]},
                {"fans": [{"name": "a", "chip": "sda", "line": 5}]},
                {"fans": [{"name": "a", "chip": "gpiochip3", "line": 5},
                          {"name": "a", "chip": "gpiochip3", "line": 6}]},
                {"limits": {"warn_soc": 90, "shed_soc": 85}},
                {"limits": {"nope": 1}}):
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            BoardConfig.load(p)


class _假测速:
    def __init__(self, lines):
        self.lines = list(lines)
        self.n = dict.fromkeys(self.lines, 0)
        self.error = ""
        self.closed = False

    def take(self):
        out, self.n = self.n, dict.fromkeys(self.lines, 0)
        return out

    def close(self):
        self.closed = True


def _mon(tmp_path, *, soc=60.0, nvme=50.0):
    cfg = BoardConfig(fans=BoardConfig.parse_fans([
        {"name": "exhaust_l", "chip": "gpiochip3", "line": 5},
        {"name": "exhaust_r", "chip": "gpiochip3", "line": 6},
        {"name": "stir", "chip": "gpiochip4", "line": 1}]),
        log_dir=str(tmp_path / "log"))
    t = [1000.0]
    temps = {"soc": soc, "nvme": nvme}
    events: list = []
    counters: dict = {}

    def counter(chip, lines):
        counters[chip] = _假测速(lines)
        return counters[chip]
    m = BoardMonitor(cfg, emit=lambda k, d: events.append((k, d)), mono=lambda: t[0],
                     wall=lambda: 1_793_000_000 + t[0],
                     read_soc=lambda: {"soc": temps["soc"]} if temps["soc"] is not None else {},
                     read_nvme=lambda: temps["nvme"], counter=counter)
    m.start()
    return m, t, temps, events, counters


def _转(counters, rpm, dt, names=("gpiochip3:5", "gpiochip3:6", "gpiochip4:1")):
    for n in names:
        chip, line = n.split(":")
        counters[chip].n[int(line)] = int(rpm / 60 * 2 * dt)


def test_风扇转速_连着两拍太慢才算停转(tmp_path):
    m, t, temps, events, counters = _mon(tmp_path)
    t[0] += 10
    _转(counters, 3000, 10)
    m.sample()
    assert m.rpm == {"exhaust_l": 3000, "exhaust_r": 3000, "stir": 3000}
    assert m.level == "ok"
    t[0] += 10
    _转(counters, 3000, 10, names=("gpiochip3:5", "gpiochip3:6"))
    m.sample()
    assert m.stopped() == [] and m.level == "ok", "慢一拍不算(刚上电、读数抖)"
    t[0] += 10
    _转(counters, 3000, 10, names=("gpiochip3:5", "gpiochip3:6"))
    m.sample()
    assert m.stopped() == ["stir"] and m.level == "warn"
    assert events[-1][0] == "board_thermal" and events[-1][1]["level"] == "warn"
    assert events[-1][1]["fans_stopped"] == ["stir"]
    fans = {d["id"]: d for d in m.health()}
    assert fans["fan:stir"]["state"] == "error" and fans["fan:exhaust_l"]["state"] == "online"


def test_级别变了才报事件_日志每拍一行(tmp_path):
    m, t, temps, events, counters = _mon(tmp_path)
    for soc in (60, 81, 82, 86, 86, 96, 70):
        t[0] += 10
        _转(counters, 3000, 10)
        temps["soc"] = soc
        m.sample()
    assert [d["level"] for k, d in events] == ["warn", "shed", "critical", "ok"]
    assert events[2][1]["soc_c"] == 96
    files = list((tmp_path / "log").glob("board-*.csv"))
    assert len(files) == 1
    rows = list(csv.DictReader(files[0].open()))
    assert len(rows) == 7 and rows[0]["soc_c"] == "60.0" and rows[-1]["level"] == "ok"
    assert rows[0]["fan_stir_rpm"] == "3000"


def test_设备健康_按级别_读不到温度算离线(tmp_path):
    m, t, temps, events, counters = _mon(tmp_path, soc=86)
    t[0] += 10
    _转(counters, 3000, 10)
    m.sample()
    b = {d["id"]: d for d in m.health()}["board"]
    assert b["state"] == "error" and "SoC 86" in b["detail"]
    temps["soc"], temps["nvme"] = None, None
    t[0] += 10
    _转(counters, 3000, 10)
    m.sample()
    b = {d["id"]: d for d in m.health()}["board"]
    assert b["state"] == "offline"


def test_测速线拿不到_那几只风扇算故障_温度照记(tmp_path):
    cfg = BoardConfig(fans=BoardConfig.parse_fans([
        {"name": "stir", "chip": "gpiochip4", "line": 1}]), log_dir=str(tmp_path))

    def counter(chip, lines):
        raise OSError("busy")
    m = BoardMonitor(cfg, emit=lambda k, d: None, mono=lambda: 0.0, wall=lambda: 0.0,
                     read_soc=lambda: {"soc": 50.0}, read_nvme=lambda: None, counter=counter)
    m.start()
    m.sample()
    fan = {d["id"]: d for d in m.health()}["fan:stir"]
    assert fan["state"] == "error" and "busy" in fan["detail"]
    assert m.level == "ok", "测速线拿不到不等于风扇停了,别降负载"
    m.close()


def test_配置样例读得进来():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    c = BoardConfig.load(root / "deploy" / "board.example.json")
    assert [f.name for f in c.fans] == ["exhaust_l", "exhaust_r", "stir"]
    assert c.limits == Limits()
