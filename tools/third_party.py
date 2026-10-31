#!/usr/bin/env python3
"""第三方软件声明(W58):随站点包、机器人包发出去的第三方代码,各自的许可证全文。

    .venv/bin/python tools/third_party.py          # 重写 web/console/public/assets/ 下的声明
    .venv/bin/python tools/third_party.py --check  # 只查:有没有 GPL/AGPL 运行时依赖、全文全不全

覆盖:
- 站点、机器人运行时的 Python 库(我们的包声明的依赖,连同 ``mqtt``、``planning`` 两个附加项,
  逐层展开);
- 网页值班台打进构建产物的 npm 包(``package-lock.json`` 里不是 dev 的);
- 网页带的字体(``web/console/public/assets/OFL-*.txt``)。

手机 App 的第三方声明由 Flutter 自动收集,在 App「我的 → 设置 → 开源许可」里看。
装机时由系统装的程序(mosquitto、ffmpeg、ROS 2、MOLA 等)不是我们分发的,在文末单独列出。

构建网页(``npm run build``)时这份文件跟着进 ``d1max_site/web/assets/``,站点在
``/assets/third-party-notices.txt`` 提供。改了依赖以后重跑本脚本,再构建网页。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata as md
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "console" / "public" / "assets" / "third-party-notices.txt"
OURS = ("d1max-contract", "d1max-patrol", "d1max-robot-agent", "d1max-site-node",
        "d1max-localizer", "d1max-adapter-d1max", "d1max-adapter-sim")
#: 运行时要的附加项(``dev``、``build`` 不算)
EXTRAS = {"mqtt", "planning"}
COPYLEFT = re.compile(r"\b(A?GPL|SSPL)\b", re.I)

#: 装机时由系统或工程师另装、不随我们的包分发的程序(写清楚许可证,方便客户自查)
EXTERNAL = (
    ("Eclipse Mosquitto", "消息服务(站点主机)", "EPL-2.0 OR EDL-1.0", "系统包管理器安装"),
    ("FFmpeg", "视频转码(站点主机、机器人)", "LGPL-2.1+ / GPL-2.0+(视发行版编译选项)",
     "系统包管理器安装"),
    ("OpenSSL", "证书工具", "Apache-2.0", "系统自带"),
    ("chrony / systemd-timesyncd", "校时", "GPL-2.0", "系统自带"),
    ("WireGuard", "站点 VPN", "GPL-2.0(内核模块)/ 工具 GPL-2.0", "系统包管理器安装"),
    ("ROS 2 Humble", "机器人传感器通信", "Apache-2.0", "厂商出厂或系统包管理器安装"),
    ("MOLA", "建图、定位(独立进程,经 ROS 2 话题通信)", "GPL-3.0", "工程师用系统包管理器安装"),
    ("Navigation2", "可选导航后端(独立进程)", "Apache-2.0 / BSD", "工程师用系统包管理器安装"),
    ("ONNX Runtime", "人员检测推理", "MIT", "工程师安装"),
    ("OpenCV", "图像处理", "Apache-2.0", "系统包管理器安装"),
)

#: 人员检测模型:不随包分发,由工程师放到 /etc/d1max/person.onnx
MODEL_NOTE = ("人员检测模型(/etc/d1max/person.onnx)不随软件包分发,由工程师按现场放置。"
              "样机阶段用的是 YOLOv8n,其许可证为 AGPL-3.0;商用前须换用许可证允许商用的模型,"
              "或取得模型权利人的商业授权。")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_closure() -> list[md.Distribution]:
    """我们的包运行时要的第三方库,逐层展开(只看装在当前环境里的)。"""
    seen: dict[str, md.Distribution] = {}
    todo: list[tuple[str, set[str]]] = [(n, EXTRAS) for n in OURS]
    while todo:
        name, extras = todo.pop()
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        key = _norm(dist.metadata["Name"])
        for req in dist.requires or []:
            m = re.match(r"\s*([A-Za-z0-9._-]+)(?:\[([^\]]*)\])?", req)
            if not m:
                continue
            marker = req.split(";", 1)[1] if ";" in req else ""
            em = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
            if em and em.group(1) not in extras:
                continue
            sub = {e.strip() for e in (m.group(2) or "").split(",") if e.strip()}
            dep = _norm(m.group(1))
            if dep not in seen and not dep.startswith("d1max-"):
                todo.append((dep, sub))
            elif dep.startswith("d1max-") and sub:
                todo.append((dep, sub))
        if not key.startswith("d1max-"):
            seen[key] = dist
    return [seen[k] for k in sorted(seen)]


def python_license(dist: md.Distribution) -> str:
    m = dist.metadata
    lic = m.get("License-Expression") or ""
    if not lic:
        raw = (m.get("License") or "").strip()
        if raw and "\n" not in raw and len(raw) < 80:
            lic = raw
    if not lic:
        cls = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or []
               if c.startswith("License ::") and c.count("::") >= 2]
        lic = " / ".join(cls)
    return lic or "见许可证全文"


def python_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    out = []
    for f in dist.files or []:
        s = str(f)
        base = Path(s).name.upper()
        if ".dist-info/" in s and base.startswith(("LICEN", "COPYING", "NOTICE", "AUTHORS")):
            try:
                out.append((s, Path(dist.locate_file(f)).read_text(encoding="utf-8",
                                                                    errors="replace")))
            except OSError:
                pass
    return out


MIT_TEXT = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


def _header_copyright(d: Path) -> list[str]:
    """没有许可证文件的包:从源码开头找版权行。"""
    out: list[str] = []
    for f in sorted(d.glob("*.js")):
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
            t = ln.strip("/*! \t")
            if t.lower().startswith("copyright") and t not in out:
                out.append(t)
    return out


def npm_packages() -> list[tuple[str, str, str, str]]:
    """网页打进构建产物的 npm 包:(名字, 版本, 许可证, 许可证全文)。"""
    lock = json.loads((ROOT / "web/console/package-lock.json").read_text(encoding="utf-8"))
    out = []
    for path, v in sorted(lock.get("packages", {}).items()):
        if not path or v.get("dev") or v.get("devOptional"):
            continue
        name = path.split("node_modules/")[-1]
        text = ""
        d = ROOT / "web/console" / path
        for f in sorted(d.glob("*")) if d.is_dir() else []:
            if f.name.upper().startswith(("LICEN", "COPYING")):
                text = f.read_text(encoding="utf-8", errors="replace")
                break
        if not text and v.get("license") == "MIT" and d.is_dir():
            cr = _header_copyright(d)
            if cr:
                text = "\n".join(cr) + "\n\n" + MIT_TEXT
        out.append((name, v.get("version", ""), v.get("license", ""), text))
    return out


def fonts() -> list[tuple[str, str]]:
    d = ROOT / "web/console/public/assets"
    return [(f.name, f.read_text(encoding="utf-8", errors="replace"))
            for f in sorted(d.glob("OFL-*.txt"))]


def render() -> tuple[str, list[str]]:
    """生成声明全文;回 (全文, 问题清单)。"""
    problems: list[str] = []
    sec: list[str] = []
    bar = "=" * 78
    head = ["Nexus Warden 第三方软件声明 / Third-party software notices",
            "Copyright (c) 2026 Nexus Dynamic. Nexus Warden 本身保留全部权利(见 LICENSE)。",
            "下面列出随 Nexus Warden 站点包、机器人包一起分发的第三方软件及其许可证全文。",
            "手机 App 的第三方声明在 App「我的 → 设置 → 开源许可」里。", ""]
    py = python_closure()
    npm = npm_packages()
    ft = fonts()
    index = ["一览:", *[f"  - {d.metadata['Name']} {d.version}  ({python_license(d)})" for d in py],
             *[f"  - {n} {v}  ({lic}, 网页)" for n, v, lic, _ in npm],
             *[f"  - {f[:-4]}  (SIL OFL-1.1, 网页字体)" for f, _ in ft], ""]
    for d in py:
        lic = python_license(d)
        if COPYLEFT.search(lic):
            problems.append(f"Python 库 {d.metadata['Name']} 许可证是 {lic}")
        texts = python_texts(d)
        if not texts:
            problems.append(f"Python 库 {d.metadata['Name']} 没找到许可证全文")
        sec += [bar, f"{d.metadata['Name']} {d.version} —— {lic}", bar]
        for name, text in texts:
            sec += [f"--- {name}", text.rstrip(), ""]
    for name, ver, lic, text in npm:
        if COPYLEFT.search(lic):
            problems.append(f"npm 包 {name} 许可证是 {lic}")
        if not text:
            problems.append(f"npm 包 {name} 没找到许可证全文(先 npm ci)")
        sec += [bar, f"{name} {ver} —— {lic}(网页值班台)", bar, text.rstrip(), ""]
    for name, text in ft:
        sec += [bar, f"{name[:-4]} —— SIL Open Font License 1.1(网页字体)", bar, text.rstrip(), ""]
    ext = [bar, "不随我们的包分发、装机时另装的程序(供客户自查)", bar,
           *[f"  - {n}:{use};{lic};{how}" for n, use, lic, how in EXTERNAL], "",
           "人员检测模型:", "  " + MODEL_NOTE, ""]
    return "\n".join(head + index + sec + ext) + "\n", problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成第三方软件声明")
    ap.add_argument("--check", action="store_true", help="只检查,不写文件")
    a = ap.parse_args(argv)
    text, problems = render()
    for p in problems:
        print("问题:", p, file=sys.stderr)
    if not a.check:
        OUT.write_text(text, encoding="utf-8")
        print(f"写好了 {OUT.relative_to(ROOT)}({len(text.encode()) // 1024} KB)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
