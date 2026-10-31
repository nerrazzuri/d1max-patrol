#!/usr/bin/env python3
"""长跑测试(W58):在开发机上起一套演示站点(仿真机器人),连续跑几十个小时,看会不会慢慢坏掉。

    .venv/bin/python tools/soak.py --hours 72 --out runs/soak/<日期>

做的事:
- 起 ``tools/site_demo.py``(两台仿真机器人,单独的端口和目录,不碰别的演示站点);
- 布防;每 ``--patrol-min`` 分钟给每台派一趟临时巡逻;每 ``--incident-min`` 分钟用签名回调报一次入侵
  (站点派机器人去拦截点、出报警);每 ``--resolve-min`` 分钟把没关的报警确认并关闭(当保安);
- 每 ``--sample-min`` 分钟记一行:演示站点那一棵进程树里每个进程的内存(RSS)、文件句柄、线程数,
  目录占用、站点库大小、接口耗时、日志里新出现的 Traceback 数;写 ``samples.csv``;
- 结束(到时间或 Ctrl+C)写 ``summary.json``:每个进程第一个小时和最后一个小时的平均内存、句柄,
  磁盘涨了多少,操作失败多少次,接口耗时 p95。

只按记下的 PID 管进程(演示脚本的 PID 和它的子进程),收尾时只停演示脚本(它自己收掉子进程)。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAP_ID, MAP_VER = "estate-1", "7"
SOURCE, ZONE, INTERCEPT = "soak-cam", "soak-zone", "soak-gate"
LOOP = [(0.6, 0.0), (0.6, 0.6), (0.0, 0.6), (0.0, 0.0)]


# ------------------------------------------------------------------ 进程树(按 PID,不按名字)

def children(pid: int) -> list[int]:
    """``pid`` 的全部子孙(读 /proc/<pid>/task/*/children)。"""
    out: list[int] = []
    todo = [pid]
    while todo:
        p = todo.pop()
        try:
            tasks = os.listdir(f"/proc/{p}/task")
        except OSError:
            continue
        for t in tasks:
            try:
                kids = Path(f"/proc/{p}/task/{t}/children").read_text().split()
            except OSError:
                continue
            for k in kids:
                if int(k) not in out:
                    out.append(int(k))
                    todo.append(int(k))
    return out


def proc_stat(pid: int) -> dict[str, Any] | None:
    """一个进程的名字(命令行里认得出的那一段)、RSS(KB)、句柄数、线程数。进程没了回 None。"""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None
    f = dict(ln.split(":", 1) for ln in status.splitlines() if ":" in ln)
    return {"pid": pid, "name": label(cmd), "rss_kb": int(f.get("VmRSS", "0 kB").split()[0]),
            "fds": fds, "threads": int(f.get("Threads", "0").strip())}


def label(cmd: list[bytes]) -> str:
    """命令行 → 短名字:``site``、``agent-A``、``mosquitto``、``ffmpeg`` ……"""
    args = [a.decode("utf-8", "replace") for a in cmd if a]
    text = " ".join(args)
    if "mosquitto" in text:
        return "mosquitto"
    if "d1max-site" in text or "d1max_site" in text:
        return "site"
    if "d1max-agent" in text or "d1max_agent" in text:
        for i, a in enumerate(args):
            if a == "--store-dir" and i + 1 < len(args):
                tail = Path(args[i + 1]).name.removeprefix("agent").lstrip("-_")
                return f"agent-{tail or 'A'}"
        return "agent"
    if "site_demo.py" in text:
        return "demo"
    return Path(args[0]).name if args else "?"


def dir_bytes(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


# ------------------------------------------------------------------ 站点接口

class Api:
    def __init__(self, base: str, password: str) -> None:
        self.base, self.password, self.token = base, password, ""
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def raw(self, method: str, path: str, body: Any = None, head: dict[str, str] | None = None,
            data: bytes | None = None) -> tuple[int, Any, float]:
        h = {"Content-Type": "application/json", **(head or {})}
        if self.token:
            h.setdefault("Authorization", f"Bearer {self.token}")
        if data is None and body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
                code, text = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": str(e)}, time.monotonic() - t0
        try:
            got = json.loads(text or b"{}")
        except ValueError:
            got = {}
        return code, got, time.monotonic() - t0

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any, float]:
        code, got, dt = self.raw(method, path, body)
        if code == 401:                                   # 会话到期:重新登录再来一次
            self.login()
            code, got, dt = self.raw(method, path, body)
        return code, got, dt

    def login(self) -> None:
        self.token = ""
        code, got, _ = self.raw("POST", "/api/login", {"name": "demo", "password": self.password})
        if code != 200:
            raise RuntimeError(f"登录失败 {code} {got}")
        self.token = got["token"]


# ------------------------------------------------------------------ 主流程

def site_cli(home: Path, *argv: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    r = subprocess.run([str(ROOT / ".venv/bin/d1max-site"), "--home", str(home), *argv],
                       capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"d1max-site {' '.join(argv)}: {r.stderr.strip()}")
    return r.stdout.strip()


def wait_ready(log: Path, demo: subprocess.Popen, timeout: float = 180) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if demo.poll() is not None:
            raise RuntimeError(f"演示站点没起来,看 {log}")
        if "演示站点起来了" in log.read_text(errors="replace"):
            return
        time.sleep(1)
    raise RuntimeError(f"演示站点 {timeout:.0f} 秒没起来,看 {log}")


class Soak:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.out: Path = a.out
        self.demo_dir: Path = a.dir
        self.api = Api(f"https://{a.host}:{a.api_port}", a.password)
        self.robots = [chr(ord("A") + i) for i in range(a.robots)]
        self.secret = ""
        self.counts: dict[str, int] = {}
        self.lat: list[float] = []
        self.tb_seen: dict[str, int] = {}
        self.out.mkdir(parents=True, exist_ok=True)
        self.events = (self.out / "events.log").open("a", encoding="utf-8")

    def note(self, what: str, ok: bool, detail: str = "") -> None:
        key = f"{what}_{'ok' if ok else 'fail'}"
        self.counts[key] = self.counts.get(key, 0) + 1
        if not ok or what in ("setup", "restart"):
            self.events.write(f"{time.strftime('%F %T')} {key} {detail}\n")
            self.events.flush()

    def setup(self) -> None:
        self.api.login()
        home = self.demo_dir / "site"
        self.secret = site_cli(home, "source-add", SOURCE).split()[-1]
        site_cli(home, "intercept", INTERCEPT, "--map", f"{MAP_ID}:{MAP_VER}",
                 "--pose", "0.6,0.6,0")
        site_cli(home, "zone", ZONE, INTERCEPT)
        code, got, _ = self.api.call("POST", f"/api/maps/{MAP_ID}/{MAP_VER}/zones/confirm",
                                     {"revision": 0, "confirm": True, "empty_ok": True})
        self.note("setup", code == 200, f"zones/confirm {code} {got.get('error', '')}")
        code, got, _ = self.api.call("POST", "/api/mode", {"mode": "armed"})
        self.note("setup", code == 200, f"arm {code} {got.get('error', '')}")

    def patrol(self) -> None:
        pts = [{"x": x, "y": y} for x, y in LOOP]
        for r in self.robots:
            code, got, _ = self.api.call("POST", f"/api/robots/{r}/patrol",
                                         {"points": pts, "map_id": MAP_ID, "map_version": MAP_VER})
            self.note("patrol", code == 200, f"{r} {code} {got.get('error', '')}")

    def incident(self) -> None:
        body = json.dumps({"event_id": f"soak-{time.time_ns()}", "type": "intrusion",
                           "zone": ZONE, "occurred_at": int(time.time() * 1000),
                           "detail": {"by": "soak"}}).encode()
        stamp = str(int(time.time() * 1000))
        sig = hmac.new(bytes.fromhex(self.secret), stamp.encode() + b"." + body,
                       hashlib.sha256).hexdigest()
        code, got, _ = self.api.raw("POST", "/api/incidents", data=body, head={
            "X-D1MAX-Source": SOURCE, "X-D1MAX-Timestamp": stamp, "X-D1MAX-Signature": sig,
            "Authorization": ""})
        self.note("incident", code == 200, f"{code} {got.get('error', '')}")

    def resolve(self) -> None:
        code, got, _ = self.api.call("GET", "/api/alerts")
        if code != 200:
            self.note("alerts", False, f"{code}")
            return
        rows = got.get("alerts") if isinstance(got, dict) else None
        for al in rows if isinstance(rows, list) else []:
            if not isinstance(al, dict) or al.get("resolved_ms"):
                continue
            key = al.get("key") or al.get("id")
            if not key:
                continue
            q = urllib.parse.quote(str(key), safe="")
            c1, _, _ = self.api.call("POST", f"/api/alerts/{q}/ack", {})
            c2, g2, _ = self.api.call("POST", f"/api/alerts/{q}/resolve", {"verdict": "handled"})
            self.note("resolve", c2 == 200, f"{key} ack {c1} resolve {c2} {g2.get('error', '')}")

    def tracebacks(self) -> int:
        new = 0
        for log in (self.demo_dir / "logs").glob("*.log") if (self.demo_dir / "logs").is_dir() \
                else []:
            try:
                n = log.read_text(errors="replace").count("Traceback")
            except OSError:
                continue
            new += max(0, n - self.tb_seen.get(log.name, 0))
            self.tb_seen[log.name] = n
        return new

    def sample(self, w: csv.writer, demo_pid: int, t0: float) -> None:
        code, _, dt = self.api.call("GET", "/api/robots/overview")
        self.note("overview", code == 200, str(code))
        self.lat.append(dt)
        db = self.demo_dir / "site" / "site.db"
        now = round((time.monotonic() - t0) / 3600, 3)
        disk = dir_bytes(self.demo_dir)
        dbs = db.stat().st_size if db.exists() else 0
        tb = self.tracebacks()
        for pid in [demo_pid, *children(demo_pid)]:
            s = proc_stat(pid)
            if s:
                w.writerow([time.strftime("%F %T"), now, s["name"], s["pid"], s["rss_kb"], s["fds"],
                            s["threads"], disk, dbs, round(dt * 1000), tb])

    def run(self) -> int:
        a = self.a
        self.out.mkdir(parents=True, exist_ok=True)
        log = self.out / "demo.log"
        argv = [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/site_demo.py"),
                "--dir", str(self.demo_dir), "--host", a.host, "--api-port", str(a.api_port),
                "--intake-port", str(a.intake_port), "--robots", str(a.robots),
                "--password", a.password]
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        demo = subprocess.Popen(argv, stdout=log.open("wb"), stderr=subprocess.STDOUT, env=env,
                                cwd=ROOT, start_new_session=True)
        (self.out / "demo.pid").write_text(str(demo.pid))
        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
        try:
            wait_ready(log, demo)
            self.setup()
            t0 = time.monotonic()
            end = t0 + a.hours * 3600
            # 刚起来时站点还不知道机器人的钟差,会拒派会动的命令(正常保护):第一趟晚 90 秒
            due = {"patrol": t0 + 90, "incident": t0 + 120, "resolve": t0 + 300, "sample": t0}
            every = {"patrol": a.patrol_min * 60, "incident": a.incident_min * 60,
                     "resolve": a.resolve_min * 60, "sample": a.sample_min * 60}
            with (self.out / "samples.csv").open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time", "hours", "proc", "pid", "rss_kb", "fds", "threads",
                            "dir_bytes", "db_bytes", "api_ms", "new_tracebacks"])
                while not stop["now"] and time.monotonic() < end:
                    if demo.poll() is not None:
                        self.note("restart", False, f"演示站点退出了,码 {demo.returncode}")
                        break
                    now = time.monotonic()
                    for job in ("sample", "patrol", "incident", "resolve"):
                        if now >= due[job]:
                            due[job] = now + every[job]
                            try:
                                (self.sample(w, demo.pid, t0) if job == "sample"
                                 else getattr(self, job)())
                            except Exception as exc:  # noqa: BLE001 —— 长跑不因为一次失败停
                                self.note(job, False, repr(exc))
                            f.flush()
                    time.sleep(2)
        finally:
            if demo.poll() is None:
                demo.send_signal(signal.SIGINT)
                try:
                    demo.wait(60)
                except subprocess.TimeoutExpired:
                    demo.kill()
            self.summary()
        return 0

    def summary(self) -> None:
        rows = []
        p = self.out / "samples.csv"
        if p.exists():
            with p.open(encoding="utf-8") as f:
                rows = [r for r in csv.DictReader(f) if r.get("hours") not in (None, "hours")]
        out: dict[str, Any] = {"counts": self.counts, "processes": {}}
        if rows:
            last_h = max(float(r["hours"]) for r in rows)
            for name in sorted({r["proc"] for r in rows}):
                mine = [r for r in rows if r["proc"] == name]
                first = [r for r in mine if float(r["hours"]) <= 1.0]
                tail = [r for r in mine if float(r["hours"]) >= last_h - 1.0]

                def avg(rs: list[dict[str, str]], k: str) -> float:
                    return round(sum(float(r[k]) for r in rs) / len(rs), 1) if rs else 0.0
                out["processes"][name] = {
                    "rss_kb_first_hour": avg(first, "rss_kb"),
                    "rss_kb_last_hour": avg(tail, "rss_kb"),
                    "fds_first_hour": avg(first, "fds"), "fds_last_hour": avg(tail, "fds"),
                    "pids": sorted({r["pid"] for r in mine})}
            out["hours"] = last_h
            out["dir_bytes"] = [int(rows[0]["dir_bytes"]), int(rows[-1]["dir_bytes"])]
            out["db_bytes"] = [int(rows[0]["db_bytes"]), int(rows[-1]["db_bytes"])]
            out["new_tracebacks"] = sum(int(r["new_tracebacks"]) for r in rows
                                        if r["proc"] == "demo")
        lat = sorted(self.lat)
        if lat:
            out["api_ms_p95"] = round(lat[int(0.95 * (len(lat) - 1))] * 1000)
        (self.out / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="演示站点长跑测试")
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--out", type=Path, required=True, help="结果目录(samples.csv、summary.json)")
    ap.add_argument("--dir", type=Path, default=Path.home() / ".cache" / "d1max-soak",
                    help="演示站点目录(起之前清掉)")
    ap.add_argument("--host", default="127.0.0.2")
    ap.add_argument("--api-port", type=int, default=8473)
    ap.add_argument("--intake-port", type=int, default=8474)
    ap.add_argument("--robots", type=int, default=2)
    ap.add_argument("--password", default="soak-password-123")
    ap.add_argument("--patrol-min", type=float, default=10)
    ap.add_argument("--incident-min", type=float, default=30)
    ap.add_argument("--resolve-min", type=float, default=5)
    ap.add_argument("--sample-min", type=float, default=5)
    return Soak(ap.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
