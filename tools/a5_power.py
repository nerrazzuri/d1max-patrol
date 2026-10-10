"""板子的功耗、算力占用(商业化 A5,决策 50):读 ``tegrastats`` 的日志,出平均、峰值。

在 Jetson 上(厂商 Orin、以后我们的 Orin NX 都是):

    sudo tegrastats --interval 1000 --logfile runs/a5/orin-idle.log      # 空闲量 5 分钟
    sudo tegrastats --interval 1000 --logfile runs/a5/orin-patrol.log    # 服务全起着巡逻 30 分钟
    python tools/a5_power.py runs/a5/orin-idle.log runs/a5/orin-patrol.log --budget-w 25

每份日志打:采样数、整板功耗(``VDD_IN``,老板子是 ``POM_5V_IN``)平均/p95/峰值、CPU 平均占用、
最忙那个核的平均占用、内存和交换区峰值、GPU 平均占用。给了 ``--budget-w``,功耗 p95 超了退出码 1。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

_RAM = re.compile(r"\bRAM (\d+)/(\d+)MB")
_SWAP = re.compile(r"\bSWAP (\d+)/(\d+)MB")
_CPU = re.compile(r"\bCPU \[([^\]]*)\]")
_GPU = re.compile(r"\bGR3D_FREQ (\d+)%")
#: 整板输入功耗:Orin NX / Nano 叫 VDD_IN(``7615mW/7615mW``),Xavier 一代叫 POM_5V_IN(``4000/4000``)。
_POWER = re.compile(r"\b(?:VDD_IN|POM_5V_IN) (\d+)(?:mW)?/(\d+)")
#: 分路功耗(``VDD_GPU_SOC 2389mW/2389mW``)。AGX Orin 没有整板那一路,只好把分路加起来(偏小:不含
#: 载板上别的耗电)。
_RAIL = re.compile(r"\b([A-Z][A-Z0-9_]*) (\d+)mW/\d+mW")


def parse_line(line: str) -> dict[str, Any] | None:
    """一行 tegrastats:回 ``{ram_mb, swap_mb, cpu: [每核 %], gpu, power_mw, power_src}``;
    不是 tegrastats 的行回 None。"""
    ram = _RAM.search(line)
    if ram is None:
        return None
    out: dict[str, Any] = {"ram_mb": int(ram.group(1)), "ram_total_mb": int(ram.group(2))}
    sw = _SWAP.search(line)
    out["swap_mb"] = int(sw.group(1)) if sw else 0
    cpu = _CPU.search(line)
    cores = []
    if cpu:
        for c in cpu.group(1).split(","):
            m = re.match(r"\s*(\d+)%", c)
            if m:                                           # "off" 的核不算
                cores.append(int(m.group(1)))
    out["cpu"] = cores
    gpu = _GPU.search(line)
    out["gpu"] = int(gpu.group(1)) if gpu else None
    pw = _POWER.search(line)
    if pw:
        out["power_mw"], out["power_src"] = int(pw.group(1)), "board"
    else:
        rails = [int(m.group(2)) for m in _RAIL.finditer(line)]
        out["power_mw"] = sum(rails) if rails else None
        out["power_src"] = "rails" if rails else None
    return out


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))] if s else 0.0


def summarize(lines: list[str]) -> dict[str, Any]:
    rows = [r for r in (parse_line(x) for x in lines) if r is not None]
    if not rows:
        return {"samples": 0}
    pw = [r["power_mw"] / 1000.0 for r in rows if r["power_mw"] is not None]
    avg_core = [statistics.fmean(r["cpu"]) for r in rows if r["cpu"]]
    busiest = [max(r["cpu"]) for r in rows if r["cpu"]]
    gpu = [r["gpu"] for r in rows if r["gpu"] is not None]
    return {"samples": len(rows),
            "power_avg_w": round(statistics.fmean(pw), 2) if pw else None,
            "power_p95_w": round(_pct(pw, 0.95), 2) if pw else None,
            "power_max_w": round(max(pw), 2) if pw else None,
            # board = 整板输入;rails = 分路加起来(AGX Orin,偏小)
            "power_src": rows[-1].get("power_src"),
            "cpu_avg_pct": round(statistics.fmean(avg_core), 1) if avg_core else None,
            "cpu_busiest_core_avg_pct": round(statistics.fmean(busiest), 1) if busiest else None,
            "ram_max_mb": max(r["ram_mb"] for r in rows),
            "ram_total_mb": rows[0]["ram_total_mb"],
            "swap_max_mb": max(r["swap_mb"] for r in rows),
            "gpu_avg_pct": round(statistics.fmean(gpu), 1) if gpu else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--budget-w", type=float, help="功耗预算(瓦):p95 超了退出码 1")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    out, bad = {}, False
    for p in a.logs:
        s = summarize(p.read_text(encoding="utf-8", errors="replace").splitlines())
        out[str(p)] = s
        if not s["samples"]:
            print(f"{p}:没有 tegrastats 的行")
            bad = True
            continue
        over = (a.budget_w is not None and s["power_p95_w"] is not None
                and s["power_p95_w"] > a.budget_w)
        bad = bad or over
        print(f"{p}:{s['samples']} 个采样;功耗 平均 {s['power_avg_w']} W / "
              f"p95 {s['power_p95_w']} W / 峰值 {s['power_max_w']} W;CPU 平均 "
              f"{s['cpu_avg_pct']}%(最忙的核 {s['cpu_busiest_core_avg_pct']}%);"
              f"内存峰值 {s['ram_max_mb']}/{s['ram_total_mb']} MB;"
              f"交换区峰值 {s['swap_max_mb']} MB;GPU 平均 {s['gpu_avg_pct']}%"
              + (f";**超预算 {a.budget_w} W**" if over else ""))
        if s.get("power_src") == "rails":
            print("  这块板子没报整板功耗,上面是分路加起来的,偏小;要准的接功率计量 DC 输入")
        if s["swap_max_mb"]:
            print("  用到交换区了:内存不够,板子要选大内存的")
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
