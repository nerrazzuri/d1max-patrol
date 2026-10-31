"""W58:长跑脚本(tools/soak.py)的纯函数部分。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("soak", ROOT / "tools" / "soak.py")
soak = importlib.util.module_from_spec(spec)
sys.modules["soak"] = soak
spec.loader.exec_module(soak)


def test_进程名_认得出站点代理和消息服务():
    assert soak.label([b"/x/.venv/bin/python", b"/x/.venv/bin/d1max-site", b"serve"]) == "site"
    assert soak.label([b"python", b"d1max-agent", b"--store-dir", b"/d/agent"]) == "agent-A"
    assert soak.label([b"python", b"d1max-agent", b"--store-dir", b"/d/agent-B"]) == "agent-B"
    assert soak.label([b"/usr/sbin/mosquitto", b"-c", b"m.conf"]) == "mosquitto"
    assert soak.label([b"python", b"tools/site_demo.py"]) == "demo"


def test_自己的进程树里找得到子进程():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert p.pid in soak.children(os.getpid())
        assert soak.proc_stat(p.pid)["rss_kb"] > 0
    finally:
        p.kill()
        p.wait()


def test_汇总_第一小时和最后一小时(tmp_path):
    out = tmp_path / "r"
    out.mkdir()
    rows = ["time,hours,proc,pid,rss_kb,fds,threads,dir_bytes,db_bytes,api_ms,new_tracebacks"]
    for h, rss in ((0.5, 100), (1.0, 110), (5.0, 300), (6.0, 310)):
        rows.append(f"t,{h},site,1,{rss},10,3,1000,500,20,0")
        rows.append(f"t,{h},demo,2,50,3,1,1000,500,20,{1 if h == 6.0 else 0}")
    (out / "samples.csv").write_text("\n".join(rows) + "\n")
    a = argparse.Namespace(out=out, dir=tmp_path / "d", host="127.0.0.2", api_port=1,
                           password="x", robots=2)
    s = soak.Soak(a)
    s.lat = [0.01] * 19 + [1.0]
    s.summary()
    got = json.loads((out / "summary.json").read_text())
    assert got["processes"]["site"]["rss_kb_first_hour"] == 105.0
    assert got["processes"]["site"]["rss_kb_last_hour"] == 305.0
    assert got["new_tracebacks"] == 1 and got["hours"] == 6.0
