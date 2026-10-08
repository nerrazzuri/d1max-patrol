"""``deploy/footprint.sh``:装机前后足迹对照(2026-10-08 装 C40221 时查出两处)。

1. ``install.sh`` 声明过的每一处写盘(``# @写盘``),footprint 都得看得见、判成「我们的」,它上面那几层
   目录判成「被勘察目录自己的 mtime」—— 当时数据根 ``/var/lib/d1max`` 早在勘察范围里,diff 却没跟上,
   现场报成「不是我们的」;对时配置 ``/etc/systemd/timesyncd.conf.d/d1max.conf`` 则根本看不见。
2. snapshot 不许落在发行包里:按手册在包目录里跑,快照落进包,包的哈希变了,``install.sh`` 拒收。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOOTPRINT = ROOT / "deploy" / "footprint.sh"
INSTALL = ROOT / "deploy" / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("awk") is None or shutil.which("bash") is None,
                                reason="要 bash 和 awk")


def _勘察范围() -> list[tuple[str, int]]:
    text = FOOTPRINT.read_text("utf-8")
    块 = text.split("SURVEY_DIRS=(", 1)[1].split("\n)", 1)[0]
    return [(d, int(n)) for d, n in re.findall(r'^\s*"(/[^":]*):(\d+)"', 块, re.M)]


def _写盘() -> list[str]:
    return re.findall(r"^# @写盘 (\S+)", INSTALL.read_text("utf-8"), re.M)


def _判(paths: list[str]) -> dict[str, str]:
    """拿 footprint.sh 里 diff 用的 ``is_ours`` / ``is_parent`` 原样判一遍:ours / parent / bad。"""
    text = FOOTPRINT.read_text("utf-8")
    函数 = text[text.index("function is_ours(p)"):text.index("function verdict(p)")]
    prog = 函数 + '{ print (is_ours($0) ? "ours" : is_parent($0) ? "parent" : "bad") "|" $0 }'
    out = subprocess.run(["awk", prog], input="\n".join(paths) + "\n", capture_output=True,
                         text=True, check=True).stdout
    return {p: v for v, p in (行.split("|", 1) for 行 in out.splitlines())}


def _看得见(p: str, 范围: list[tuple[str, int]]) -> bool:
    for d, n in 范围:
        if p == d:
            return True
        if p.startswith(d.rstrip("/") + "/"):
            if p[len(d):].strip("/").count("/") + 1 <= n:
                return True
    return False


def test_装机声明的每处写盘_footprint都看得见并且判成我们的():
    范围 = _勘察范围()
    assert ("/var/lib", 1) in 范围
    写盘 = _写盘()
    assert "/var/lib/d1max" in 写盘 and "/etc/systemd/timesyncd.conf.d/d1max.conf" in 写盘
    # 每一处:它自己或者离它最近、看得见的那层祖先,要判成我们的(否则这次写盘在 diff 里要么
    # 看不见,要么挂在别人名下)。
    露面 = {}
    for p in 写盘:
        q = p
        while q != "/" and not _看得见(q, 范围):
            q = str(Path(q).parent)
        露面[p] = q
    判 = _判(sorted(set(露面.values())))
    错 = {p: q for p, q in 露面.items() if 判[q] != "ours"}
    assert not 错, f"这几处写盘在 diff 里露面的那一条不算我们的:{错}"


def test_写盘上面看得见的各层目录_判成被勘察目录自己的mtime():
    范围 = _勘察范围()
    上层 = set()
    for p in _写盘():
        q = str(Path(p).parent)
        while q != "/":
            if _看得见(q, 范围):
                上层.add(q)
            q = str(Path(q).parent)
    判 = _判(sorted(上层))
    错 = {q: v for q, v in 判.items() if v == "bad"}
    assert not 错, f"装机必然改到 mtime 的上层目录被判成「不是我们的」:{错}"


def test_每个勘察根目录本身都判成被勘察目录():
    判 = _判([d for d, _ in _勘察范围()])
    assert {d: v for d, v in 判.items() if v != "parent"} == {}


def _跑快照(cwd: Path, *args: str, 勘察: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(FOOTPRINT), "snapshot", *args], cwd=cwd, text=True,
                          capture_output=True, env={"PATH": "/usr/bin:/bin",
                                                    "D1MAX_FOOTPRINT_DIRS": f"{勘察}:1"},
                          timeout=60)


def test_快照不许落进发行包(tmp_path: Path):
    包 = tmp_path / "2026-10-08-21720f09"
    (包 / "deploy").mkdir(parents=True)
    (包 / "release.json").write_text("{}", "utf-8")
    勘察 = tmp_path / "勘察"
    勘察.mkdir()
    # 在包目录里不给文件名(手册的老写法):拒绝,包里一个字节都不多
    r = _跑快照(包, 勘察=勘察)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "发行包" in r.stderr
    assert not (包 / "runs").exists()
    # 在包的子目录里跑、或者显式给一个包里的文件名,同样拒绝
    assert _跑快照(包 / "deploy", 勘察=勘察).returncode == 2
    assert _跑快照(tmp_path, str(包 / "x" / "f.txt"), 勘察=勘察).returncode == 2
    assert sorted(p.name for p in 包.rglob("*")) == ["deploy", "release.json"]
    # 包外:照常写
    r = _跑快照(tmp_path, 勘察=勘察)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(list((tmp_path / "runs" / "footprint").glob("*-footprint.txt"))) == 1
