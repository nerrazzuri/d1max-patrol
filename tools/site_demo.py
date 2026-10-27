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
    .venv/bin/python tools/site_demo.py --host 192.168.5.12 --recordings 30
    # ↑ 绑局域网地址:同一个网里的手机装上 App 就能连(配对码:d1max-site --home <目录>/site pair-code)

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


def _seed_recordings(home: Path, work: Path, minutes: int) -> None:
    """放 ``minutes`` 分钟的假录像(狗 A,前后两路,到现在为止;中间空 5 分钟,看得出空档)。站点还没
    起来时直接往它的库里登记。画面是 ffmpeg 的测试图,带一个走动的秒表。"""
    import time

    from d1max_site.db import SiteDB
    from d1max_site.recordings import SEGMENT_S, RecordingStore
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("没有 ffmpeg:sudo apt-get install ffmpeg")
    clip = work / "demo-segment.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=10:duration={SEGMENT_S}", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "20",
                    "-movflags", "+faststart", str(clip)], check=True)
    data = clip.read_bytes()
    now = int(time.time())
    last = now - now % SEGMENT_S - SEGMENT_S
    db = SiteDB(home / "site.db")
    try:
        store = RecordingStore(db, home / "recordings", now_ms=lambda: now * 1000)
        for i in range(minutes):
            if minutes > 20 and 10 <= i < 15:
                continue                                # 空档
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(last - i * SEGMENT_S))
            for cam in ("front", "back"):
                store.put("A", f"{cam}/{stamp}", "video.mp4", offset=0, data=data,
                          total=len(data))
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # 默认不放仓库里:仓库路径带空格(「D1 Max」),Mosquitto 的配置按空白切词,路径里不许有空格
    ap.add_argument("--dir", type=Path, default=Path.home() / ".cache" / "d1max-site-demo")
    ap.add_argument("--api-port", type=int, default=8443)
    ap.add_argument("--host", default=HOST,
                    help="站点 API 绑哪个地址。给这台机器的局域网地址(比如 192.168.5.12),"
                         "同一个网里的手机就连得上;默认只本机")
    ap.add_argument("--password", default="demo-password-123")
    ap.add_argument("--map-dir", type=Path, default=None, help="建好的图(版本目录);不给用一张假的")
    ap.add_argument("--robots", type=int, default=1, choices=range(1, 9), metavar="N",
                    help="起几台仿真狗(A、B、C……;每台推测试图画面),试多台同时看画面的负载用")
    ap.add_argument("--recordings", type=int, default=0, metavar="N",
                    help="往站点里放 N 分钟的假录像(要 ffmpeg;看值班台的录像页用)")
    a = ap.parse_args(argv)
    mosq = shutil.which("mosquitto") or "/usr/sbin/mosquitto"
    if not Path(mosq).exists():
        raise SystemExit("没有 mosquitto:sudo apt-get install mosquitto")
    if a.dir.exists():
        shutil.rmtree(a.dir)
    home, logs = a.dir / "site", a.dir / "logs"
    logs.mkdir(parents=True)
    bport = _free_port()
    _site(home, "init", "--site-id", "demo", "--hostname", a.host, "--broker-port", str(bport))
    names = [chr(ord("A") + i) for i in range(a.robots)]
    for n in names:
        _site(home, "enroll", n, "--days", "30")
    if a.host.startswith("127.") and a.host != "127.0.0.1":
        # 站点绑在回环别名(127.0.0.2)上时,SRT 收流回包的源地址是 127.0.0.1,狗推到 127.0.0.2 连不上
        # (「Input/output error」):画面让狗推到 127.0.0.1。真站点用局域网地址,没这个事。
        import json
        cfg_path = home / "site.json"
        cfg = json.loads(cfg_path.read_text())
        cfg.setdefault("video", {})["srt_host"] = "127.0.0.1"
        cfg_path.write_text(json.dumps(cfg, indent=1))
    _site(home, "add-admin", "demo", env={"D1MAX_SITE_PASSWORD": a.password})
    src = a.map_dir
    if src is None:
        src = a.dir / "map-src"
        src.mkdir()
        (src / "m.pgm").write_bytes(b"x")
    _site(home, "map-import", str(src), "--map-id", "estate-1", "--version", "7")
    fp = _site(home, "fingerprint").strip()
    if a.recordings > 0:
        _seed_recordings(home, a.dir, a.recordings)

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
                       "--api-host", a.host, "--api-port", str(a.api_port)], "d1max-site 起来了")
        for i, n in enumerate(names):
            crt = home / "ca" / "issued" / n
            sub = "" if n == "A" else f"-{n}"          # A 跟以前一样,别的各用各的目录
            start("agent" + sub, [sys.executable, "-m", "d1max_agent.main",
                                  "--transport", f"mqtts://127.0.0.1:{bport}",
                                  "--tls-ca", str(crt / "ca.crt"),
                                  "--tls-cert", str(crt / "robot.crt"),
                                  "--tls-key", str(crt / "robot.key"), "--hal", "sim",
                                  "--registration", str(crt / "registration.json"),
                                  "--store-dir", str(a.dir / f"agent{sub}"),
                                  "--runs-root", str(a.dir / f"runs{sub}"),
                                  "--map", "estate-1:7", "--home", f"{i * 1.0},0,0"],
                  "d1max-agent 起来了")
        print(f"演示站点起来了(日志在 {logs}):\n"
              f"  地址  https://{a.host}:{a.api_port}\n"
              f"  指纹  {fp}\n"
              f"  账号  demo / {a.password}\n"
              f"  狗    {'、'.join(names)}(仿真)\n"
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
