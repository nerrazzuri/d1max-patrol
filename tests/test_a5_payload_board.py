"""商业化 A5 自带算力板:zenoh 路由起不起、配置对不对;出厂自检查网关;链路、功耗两个量的工具。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from d1max_patrol import provision as dog

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "deploy" / "d1max-zenohd-start"


def _tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _start(tmp_path, env_text, *args):
    (tmp_path / "env").write_text(env_text)
    return subprocess.run(["bash", str(START), *args], capture_output=True, text=True,
                          env={"D1MAX_ENV_FILE": str(tmp_path / "env"), "PATH": "/usr/bin:/bin"})


def test_路由_没填网关不起_填了连网关只听本机_值不干净拒(tmp_path):
    assert _start(tmp_path, "D1MAX_SITE_MQTT=x\n", "--check").returncode == 1
    assert _start(tmp_path, "D1MAX_GATEWAY=192.168.168.100\n", "--check").returncode == 0
    p = _start(tmp_path, "D1MAX_GATEWAY=192.168.168.100\nD1MAX_GATEWAY_ZENOH_PORT=7448\n",
               "--print")
    assert p.returncode == 0
    assert '"tcp/192.168.168.100:7448"' in p.stdout
    assert '"tcp/127.0.0.1:7447"' in p.stdout and "[::]" not in p.stdout, "只在本机听"
    assert _start(tmp_path, "D1MAX_GATEWAY=a;reboot\n", "--check").returncode == 4
    assert _start(tmp_path, "D1MAX_GATEWAY=h\nD1MAX_GATEWAY_ZENOH_PORT=x1\n",
                  "--check").returncode == 4


def test_路由单元_有条件才起_只装不启用():
    unit = (ROOT / "deploy" / "d1max-zenohd.service").read_text(encoding="utf-8")
    assert "ExecCondition=/opt/d1max/current/deploy/d1max-zenohd-start --check" in unit
    assert "User=robot" in unit
    装 = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert 'install -m 0644 "$PKG/deploy/d1max-zenohd.service" /etc/systemd/system/' in 装
    assert "enable d1max-zenohd" not in 装.replace("enable --now d1max-zenohd", "")
    for u in ("localizer", "obstacles", "persons", "lidar-merge"):
        assert "d1max-zenohd.service" in (ROOT / "deploy" / f"d1max-{u}.service").read_text(), u


def _齐(etc):
    (etc / "tls").mkdir(parents=True)
    for n in ("ca.crt", "robot.crt", "robot.key"):
        (etc / "tls" / n).write_text("x")
    (etc / "tls/robot.key").chmod(0o600)
    (etc / "registration.json").write_text('{"robot_id": "A"}')


def _查(etc, probe):
    return {c.name: c for c in dog.selfcheck(etc=etc, user=None, run=lambda c: (0, "active"),
                                              mqtt_probe=lambda u, e: "", tcp_probe=probe)}


def test_出厂自检_填了网关才查_连不上FAIL(tmp_path):
    etc = tmp_path / "etc"
    _齐(etc)
    (etc / "env").write_text("D1MAX_SITE_MQTT=mqtts://s:8883\nD1MAX_MAP=m:1\nD1MAX_HOME=0,0,0\n")
    hits = []
    assert "网关" not in _查(etc, lambda h, p: hits.append((h, p)) or "")
    assert hits == [], "装在厂商 Orin 上不查网关"
    (etc / "env").write_text((etc / "env").read_text() + "D1MAX_GATEWAY=192.168.168.100\n")
    rows = _查(etc, lambda h, p: hits.append((h, p)) or "")
    assert rows["网关"].level == "PASS" and hits == [("192.168.168.100", 7447)]
    rows = _查(etc, lambda h, p: "Connection refused")
    assert rows["网关"].level == "FAIL" and "Connection refused" in rows["网关"].detail


def test_链路_帧率带宽断档_钟对不上按抖动判():
    lb = _tool("a5_link_bench")
    s = lb.TopicStats("/front_lidar")
    t = 1000.0
    for i in range(100):
        t += 0.1 if i != 50 else 0.5                           # 一处断档
        s.add(t, t - 0.02, 1_000_000)
    r = s.summary(10.5)
    assert r["frames"] == 100 and r["gaps"] == 1 and r["mb_per_s"] == pytest.approx(9.524, 0.01)
    assert r["lat_p50_ms"] == pytest.approx(20, abs=0.5) and not r["clock_mismatch"]
    assert lb.verdict(r, want_hz=10, min_hz_ratio=0.9, max_p95_ms=50)[0] == "PASS"
    assert lb.verdict(r, want_hz=10, min_hz_ratio=0.96, max_p95_ms=50)[0] == "FAIL", "9.52<9.6"
    assert lb.verdict(r, want_hz=10, min_hz_ratio=0.9, max_p95_ms=10)[0] == "FAIL"
    far = lb.TopicStats("/x")                                   # 时间戳差两百天
    for i in range(50):
        t = 1.8e9 + i * 0.1
        far.add(t, t - 200 * 86400 - (0.2 if i % 10 == 0 else 0), 10)
    r = far.summary(5.0)
    assert r["clock_mismatch"] and r["jitter_p95_ms"] == pytest.approx(200, abs=1)
    v, why = lb.verdict(r, want_hz=10, min_hz_ratio=0.9, max_p95_ms=50)
    assert v == "FAIL" and "抖动" in why
    assert lb.verdict(r, want_hz=10, min_hz_ratio=0.9, max_p95_ms=300)[0] == "PASS"
    assert lb.verdict(lb.TopicStats("/y").summary(5), want_hz=10, min_hz_ratio=0.9,
                      max_p95_ms=50)[0] == "FAIL"
    assert "Mbit/s" in lb.render([dict(r, topic="/x", verdict="PASS", why="")])


NX = ("10-10-2026 10:00:00 RAM 6123/15389MB (lfb 2x4MB) SWAP 120/7694MB (cached 0MB) "
      "CPU [12%@1420,8%@1420,off,90%@1420] EMC_FREQ 0% GR3D_FREQ 30%@[612] cpu@45.5C "
      "VDD_IN 7615mW/7615mW VDD_CPU_GPU_CV 1200mW/1200mW VDD_SOC 2000mW/2000mW")
AGX = ("RAM 2448/30536MB (lfb 6729x4MB) SWAP 0/15268MB (cached 0MB) CPU [1%@729,0%@729] "
       "EMC_FREQ 0%@2133 GR3D_FREQ 0%@[305,305] VDD_GPU_SOC 2389mW/2389mW "
       "VDD_CPU_CV 397mW/397mW VIN_SYS_5V0 2015mW/2015mW")


def test_功耗_整板那一路优先_没有就加分路_关掉的核不算(tmp_path, capsys):
    pw = _tool("a5_power")
    r = pw.parse_line(NX)
    assert r["power_mw"] == 7615 and r["power_src"] == "board" and r["cpu"] == [12, 8, 90]
    assert r["gpu"] == 30 and r["swap_mb"] == 120
    r = pw.parse_line(AGX)
    assert r["power_mw"] == 2389 + 397 + 2015 and r["power_src"] == "rails"
    assert pw.parse_line("别的日志行") is None
    s = pw.summarize([NX, NX.replace("7615mW/7615mW", "9615mW/9615mW"), "x"])
    assert s["samples"] == 2 and s["power_avg_w"] == pytest.approx(8.62)
    assert s["power_max_w"] == pytest.approx(9.62) and s["swap_max_mb"] == 120
    log = tmp_path / "t.log"
    log.write_text(NX + "\n")
    assert pw.main([str(log), "--budget-w", "10"]) == 0
    assert pw.main([str(log), "--budget-w", "5"]) == 1
    assert "交换区" in capsys.readouterr().out
