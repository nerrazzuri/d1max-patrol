"""改名迁移 ``deploy/nexwar_migrate``(d1max → nexwar):假根目录里摆一台改名前装好的机器人 + 站点主机,
迁过去看目录、配置、单元、记录;再跑一遍什么都不动;新旧都有内容时停下。"""
from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATE = ROOT / "deploy" / "nexwar_migrate"


def _stub(tmp: Path, enabled: set[str]) -> Path:
    """systemctl 桩:is-enabled 按给的集合答;别的命令记到 calls 里。"""
    log = tmp / "calls"
    s = tmp / "systemctl"
    s.write_text("#!/bin/sh\n"
                 f'echo "$*" >> "{log}"\n'
                 'if [ "$1" = is-enabled ]; then\n'
                 f'  for u in {" ".join(sorted(enabled))}; do [ "$3" = "$u" ] && exit 0; done; exit 1\n'
                 "fi\nexit 0\n")
    s.chmod(0o755)
    return s


def _run(root: Path, stub: Path, what: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NEXWAR_MIGRATE_ROOT": str(root), "NEXWAR_MIGRATE_SYSTEMCTL": str(stub)}
    return subprocess.run(["sh", str(MIGRATE), what], env=env, capture_output=True, text=True)


def _老机器(root: Path) -> None:
    etc = root / "etc"
    (etc / "d1max" / "tls").mkdir(parents=True)
    (etc / "d1max" / "env").write_text(
        "D1MAX_HAL=d1max\nD1MAX_DEVICE_PIN=246810\nD1MAX_PAYLOAD=/etc/d1max/payload.json\n")
    (etc / "d1max" / "env").chmod(0o600)
    (etc / "d1max" / "tls" / "robot.key").write_text("KEY /etc/d1max")
    (etc / "d1max-site").mkdir()
    (etc / "d1max-site" / "env").write_text("D1MAX_SITE_LAT=1.5\n")
    (etc / "d1max-ops.json").write_text('{"root": "/var/lib/d1max-ops"}')
    (etc / "wireguard").mkdir()
    (etc / "wireguard" / "d1max.conf").write_text("[Interface]\n")
    (etc / "sudoers.d").mkdir()
    (etc / "sudoers.d" / "d1max").write_text("robot ALL=...")
    (root / "var/lib/d1max/outbox").mkdir(parents=True)
    (root / "var/lib/d1max/outbox/run1.json").write_text("{}")
    (root / "var/lib/d1max-site").mkdir(parents=True)
    (root / "var/lib/d1max-site/site.db").write_text("db")
    (root / "opt/d1max/current").mkdir(parents=True)
    unit = etc / "systemd" / "system"
    (unit / "multi-user.target.wants").mkdir(parents=True)
    for u in ("d1max-agent.service", "d1max-sidecar.service", "d1max-lidar-merge.service",
              "d1max-ops.path"):
        (unit / u).write_text("[Unit]\n")
    (unit / "multi-user.target.wants" / "d1max-agent.service").symlink_to(unit / "d1max-agent.service")


def test_改名迁移_老机器迁成新名字_证书原样_键改名_开着的单元按新名字再开(tmp_path):
    root = tmp_path / "root"
    _老机器(root)
    enabled = {"d1max-agent.service", "d1max-sidecar.service", "wg-quick@d1max.service"}
    stub = _stub(tmp_path, enabled)
    r = _run(root, stub, "prepare")
    assert r.returncode == 0, r.stderr
    assert "246810" not in r.stdout + r.stderr, "PIN 不打印"
    etc = root / "etc"
    assert not (etc / "d1max").exists() and not (etc / "d1max-site").exists()
    assert (etc / "nexwar" / "tls" / "robot.key").read_text() == "KEY /etc/d1max", "私钥原样(不改内容)"
    env = (etc / "nexwar" / "env").read_text()
    assert env == ("NEXWAR_HAL=d1max\nNEXWAR_DEVICE_PIN=246810\n"
                   "NEXWAR_PAYLOAD=/etc/nexwar/payload.json\n"), "键改名、路径改新、机型名 d1max 不动"
    assert (etc / "nexwar" / "env").stat().st_mode & 0o777 == 0o600
    assert (etc / "nexwar_site" / "env").read_text() == "NEXWAR_SITE_LAT=1.5\n"
    assert (etc / "nexwar_ops.json").read_text() == '{"root": "/var/lib/nexwar_ops"}'
    assert (etc / "wireguard" / "nexwar.conf").exists()
    assert not (etc / "sudoers.d" / "d1max").exists()
    assert (root / "var/lib/nexwar/outbox/run1.json").exists()
    assert (root / "var/lib/nexwar_site/site.db").read_text() == "db"
    assert (root / "opt/nexwar/current").is_dir()
    assert not list((etc / "systemd" / "system").rglob("d1max-*")), "老单元文件、自启链都删了"
    backups = list((root / "var/backups").glob("nexwar-migrate-*.tar.gz"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o077 == 0, "备份只有 root 能读"
    with tarfile.open(backups[0]) as t:
        names = t.getnames()
    assert "etc/d1max/env" in names and "etc/d1max/tls/robot.key" in names
    calls = (tmp_path / "calls").read_text()
    assert "disable --now d1max-agent.service" in calls and "disable --now d1max-ops.path" in calls

    # 新版装机脚本装上了新单元;finish 按新名字开 prepare 时开着的
    unit = etc / "systemd" / "system"
    for u in ("nexwar_agent.service", "nexwar_sidecar.service", "nexwar_lidar_merge.service"):
        (unit / u).write_text("[Unit]\n")
    r = _run(root, stub, "finish")
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls").read_text()
    assert "enable --now nexwar_agent.service" in calls
    assert "enable --now nexwar_sidecar.service" in calls, "现场手工开的旁路不能丢"
    assert "enable --now wg-quick@nexwar.service" in calls
    assert "enable --now nexwar_lidar_merge.service" not in calls, "原来没开的不开"

    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    r = _run(root, stub, "prepare")
    assert r.returncode == 0 and r.stdout == "", "迁过的再跑:什么都不做"
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before


def test_改名迁移_新旧都有内容_不猜_停下(tmp_path):
    root = tmp_path / "root"
    _老机器(root)
    (root / "etc" / "nexwar").mkdir()
    (root / "etc" / "nexwar" / "env").write_text("NEXWAR_HAL=sim\n")
    r = _run(root, _stub(tmp_path, set()), "prepare")
    assert r.returncode == 2 and "都有内容" in r.stderr
    assert (root / "etc" / "d1max" / "env").exists(), "一个字节都没动"


def test_改名迁移_新机器_什么都不做(tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    r = _run(root, _stub(tmp_path, set()), "prepare")
    assert r.returncode == 0 and r.stdout == ""
    assert _run(root, _stub(tmp_path, set()), "finish").returncode == 0


def test_两个装机脚本都先迁再装_装完再开():
    for f in ("deploy/install.sh", "deploy/site/install-site.sh"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert "nexwar_migrate\" prepare" in text and "nexwar_migrate\" finish" in text, f
        assert text.index("nexwar_migrate\" prepare") < text.index("nexwar_migrate\" finish"), f
