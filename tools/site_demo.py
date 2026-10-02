#!/usr/bin/env python3
"""开发机上的本机演示站点(W15):没有狗的时候,拿它试手机 app、桌面版。

起一整套真东西(跟站点验收测试 ``test_site_acceptance.py`` 同一条路):``d1max-site`` 建站点、
签一台仿真狗
``A`` 的证书、加一个管理员 → Mosquitto(mTLS)→ ``d1max-site serve`` → ``d1max-agent --hal sim``。
站点 API
绑 ``127.0.0.2``(不是 127.0.0.1:站点只在非回环地址上开 HTTPS,而手机、桌面版只认 HTTPS + 证书指纹)。

    .venv/bin/python tools/site_demo.py            # 起好了打印地址、指纹、账号;Ctrl+C 全部收掉
    .venv/bin/python tools/site_demo.py --map-dir runs/w09i-sim/map_front
    # ↑ 用一张真的图(有预览)

桌面版:「站点」页右下角 +,填打印出来的地址和指纹,账号 ``demo``。每次起都是新的站点目录(``--dir``,
默认 ``~/.cache/d1max-site-demo``,起之前清掉),证书指纹每次都不一样。
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {k: v for k, v in os.environ.items()
       if k not in ("PYTHONPATH", "AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH")}
ENV["PYTHONUNBUFFERED"] = "1"
HOST = "127.0.0.2"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _site(home: Path, *args: str, env: dict | None = None) -> str:
    r = subprocess.run([sys.executable, "-m", "d1max_site.main", "--home", str(home), *args],
                       capture_output=True, text=True, timeout=120, env=ENV | (env or {}))
    if r.returncode:
        raise SystemExit(f"d1max-site {' '.join(args)} 没成:\n{r.stderr}")
    return r.stdout


def _wait(log: Path, text: str, p: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists() and text in log.read_text(errors="replace"):
            return
        if p.poll() is not None:
            raise SystemExit(f"{log.name} 退了:\n{log.read_text(errors='replace')[-2000:]}")
        time.sleep(0.2)
    raise SystemExit(f"{timeout:.0f} s 内没等到 {text!r}(看 {log})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # 默认不放仓库里:仓库路径带空格(「D1 Max」),Mosquitto 的配置按空白切词,路径里不许有空格
    ap.add_argument("--dir", type=Path, default=Path.home() / ".cache" / "d1max-site-demo")
    ap.add_argument("--api-port", type=int, default=8443)
    ap.add_argument("--password", default="demo-password-123")
    ap.add_argument("--map-dir", type=Path, default=None, help="建好的图(版本目录);不给用一张假的")
    a = ap.parse_args(argv)
    mosq = shutil.which("mosquitto") or "/usr/sbin/mosquitto"
    if not Path(mosq).exists():
        raise SystemExit("没有 mosquitto:sudo apt-get install mosquitto")
    if a.dir.exists():
        shutil.rmtree(a.dir)
    home, logs = a.dir / "site", a.dir / "logs"
    logs.mkdir(parents=True)
    bport = _free_port()
    _site(home, "init", "--site-id", "demo", "--hostname", HOST, "--broker-port", str(bport))
    _site(home, "enroll", "A", "--days", "30")
    _site(home, "add-admin", "demo", env={"D1MAX_SITE_PASSWORD": a.password})
    src = a.map_dir
    if src is None:
        src = a.dir / "map-src"
        src.mkdir()
        (src / "m.pgm").write_bytes(b"x")
    _site(home, "map-import", str(src), "--map-id", "estate-1", "--version", "7")
    fp = _site(home, "fingerprint").strip()

    procs: list[subprocess.Popen] = []

    def start(name: str, argv: list[str], ready: str) -> None:
        log = logs / f"{name}.log"
        p = subprocess.Popen(argv, stdout=open(log, "wb"), stderr=subprocess.STDOUT, env=ENV)
        procs.append(p)
        _wait(log, ready, p)

    try:
        start("mosquitto", [mosq, "-c", str(home / "broker" / "mosquitto.conf")],
              "mosquitto version")
        start("site", [sys.executable, "-m", "d1max_site.main", "--home", str(home), "serve",
                       "--api-host", HOST, "--api-port", str(a.api_port)], "d1max-site 起来了")
        crt = home / "ca" / "issued" / "A"
        start("agent", [sys.executable, "-m", "d1max_agent.main",
                        "--transport", f"mqtts://127.0.0.1:{bport}",
                        "--tls-ca", str(crt / "ca.crt"), "--tls-cert", str(crt / "robot.crt"),
                        "--tls-key", str(crt / "robot.key"), "--hal", "sim",
                        "--registration", str(crt / "registration.json"),
                        "--store-dir", str(a.dir / "agent"), "--runs-root", str(a.dir / "runs"),
                        "--map", "estate-1:7", "--home", "0,0,0"], "d1max-agent 起来了")
        print(f"演示站点起来了(日志在 {logs}):\n"
              f"  地址  https://{HOST}:{a.api_port}\n"
              f"  指纹  {fp}\n"
              f"  账号  demo / {a.password}\n"
              f"  狗    A(仿真)\n"
              f"Ctrl+C 全部收掉。", flush=True)
        # 等 Ctrl+C:用处理函数立个旗,不用屏蔽信号(屏蔽会被子进程继承,收掉时它们就不理 SIGTERM)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        while not stop.wait(1.0):
            dead = [p for p in procs if p.poll() is not None]
            if dead:
                print(f"有进程退了(看 {logs}),全部收掉", flush=True)
                break
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
