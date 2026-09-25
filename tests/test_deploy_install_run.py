"""真的把 ``deploy/install.sh`` 跑一遍(W00c5e 内部评审:部署测试以前只对字符串和顺序,设计稿
§9 答应过假根跑一遍)。

**假根**:脚本里的 ``/opt/d1max``、``/etc/d1max``、``/etc/systemd``、``/etc/sudoers.d``、
``/usr/local/sbin``、``/var/lib/d1max`` 全换成临时目录底下的同名路径;``systemctl``、``sudo``、
``chown``、``id``、``python3`` 换成只记账的假命令(``python3 -m venv`` 造一个只记账的解释器)。
所以这里验的是脚本自己的**盘上动作与先后**:装什么、清什么、什么时候起代理;验不了 pip、
``release install`` 这些真正干活的子进程(那些各有各的测试)。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
PKG_NAME = "2026-09-26-09a63614"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt",
                                reason="要 bash")


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _准备(root: Path) -> Path:
    """造假根、假命令、一个包目录;返回改过路径的 install.sh。"""
    subs = {"/opt/d1max": root / "opt/d1max", "/etc/d1max": root / "etc/d1max",
            "/etc/systemd": root / "etc/systemd", "/etc/sudoers.d": root / "etc/sudoers.d",
            "/usr/local/sbin": root / "usr/local/sbin", "/var/lib/d1max": root / "var/lib/d1max"}
    text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    for old, new in subs.items():
        text = text.replace(old, str(new))
    script = root / "install.sh"
    script.write_text(text, encoding="utf-8")
    shutil.copy2(DEPLOY / "d1max-agent.service", root / "d1max-agent.service")
    (root / "etc/systemd/system").mkdir(parents=True)
    log = root / "calls"
    stub = root / "stub"
    stub.mkdir()
    _stub(stub / "systemctl", f'''echo "systemctl $*" >> "{log}"
case "$1" in
  is-active) exit 3 ;;
  disable) for u in "$@"; do case "$u" in *.service)
      rm -f "{root}/etc/systemd/system/multi-user.target.wants/$u";; esac; done ;;
  enable) mkdir -p "{root}/etc/systemd/system/multi-user.target.wants"
      ln -sf "{root}/etc/systemd/system/$2" \\
        "{root}/etc/systemd/system/multi-user.target.wants/$2" ;;
esac
exit 0
''')
    _stub(stub / "sudo", f'''echo "sudo $*" >> "{log}"
[ "$1" = "-u" ] && shift 2
exec "$@"
''')
    _stub(stub / "chown", f'echo "chown $*" >> "{log}"\n')
    _stub(stub / "id", "exit 0\n")
    _stub(stub / "python3", f'''echo "python3 $*" >> "{log}"
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\necho "venvpy $*" >> "{log}"\\nexit 0\\n' > "$3/bin/python"
  chmod +x "$3/bin/python"
fi
exit 0
''')
    pkg = root / "pkg" / PKG_NAME
    (pkg / "deploy").mkdir(parents=True)
    shutil.copy2(DEPLOY / "d1max-agent.service", pkg / "deploy")
    shutil.copy2(DEPLOY / "d1max-agent-start", pkg / "deploy")
    (pkg / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return script


def _跑(root: Path, script: Path) -> subprocess.CompletedProcess:
    env = {"PATH": f"{root / 'stub'}:{os.environ['PATH']}", "HOME": str(root)}
    return subprocess.run(["bash", str(script), str(root / "pkg" / PKG_NAME)],
                          capture_output=True, text=True, timeout=120, env=env)


def _调用(root: Path) -> list[str]:
    return (root / "calls").read_text(encoding="utf-8").splitlines()


def test_新机器_装上代理并自启_env模板没有PIN(tmp_path):
    script = _准备(tmp_path)
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    unit = tmp_path / "etc/systemd/system/d1max-agent.service"
    assert unit.is_file()
    wants = tmp_path / "etc/systemd/system/multi-user.target.wants"
    assert (wants / "d1max-agent.service").is_symlink()
    env = (tmp_path / "etc/d1max/env").read_text(encoding="utf-8")
    assert "D1MAX_PIN" not in env and "\nD1MAX_HAL=sim\n" in env
    assert oct((tmp_path / "etc/d1max/env").stat().st_mode & 0o777) == "0o600"
    assert "还没有" in got.stdout, "6/7 说证书包还没有"
    calls = _调用(tmp_path)
    assert "systemctl start d1max-agent.service" in calls, "没有注册文件:交给单元的条件去跳过"


def test_老机器_清掉老服务助手白名单任务包_老env的PIN行不动只提示(tmp_path):
    script = _准备(tmp_path)
    sysd = tmp_path / "etc/systemd/system"
    (sysd / "d1max-patrol.service").write_text("[Unit]\n", encoding="utf-8")
    (sysd / "multi-user.target.wants").mkdir()
    (sysd / "multi-user.target.wants/d1max-patrol.service").symlink_to(
        sysd / "d1max-patrol.service")
    for p in ("usr/local/sbin/d1max-privileged", "usr/local/sbin/d1max-restart-now",
              "etc/sudoers.d/d1max", "opt/d1max/bundles/site-1/bundle.yaml"):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("x", encoding="utf-8")
    (tmp_path / "etc/d1max").mkdir(parents=True)
    (tmp_path / "etc/d1max/env").write_text(
        "D1MAX_SN=C40221\nD1MAX_PIN=<老PIN>\nD1MAX_CONSOLE_URL=\n", encoding="utf-8")
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    for p in ("etc/systemd/system/d1max-patrol.service",
              "etc/systemd/system/multi-user.target.wants/d1max-patrol.service",
              "usr/local/sbin/d1max-privileged", "usr/local/sbin/d1max-restart-now",
              "etc/sudoers.d/d1max", "opt/d1max/bundles"):
        assert not (tmp_path / p).exists(), f"{p} 应该被清掉"
    env = (tmp_path / "etc/d1max/env").read_text(encoding="utf-8")
    assert "D1MAX_PIN=<老PIN>" in env, "人手写的配置文件不许改"
    assert "已经没人读了" in got.stdout
    assert "缺这几行" in got.stdout and "D1MAX_SITE_MQTT" in got.stdout
    calls = _调用(tmp_path)
    停老 = calls.index("systemctl disable --now d1max-patrol.service")
    assert 停老 < calls.index("systemctl daemon-reload") < calls.index(
        "systemctl enable d1max-agent.service")


def test_登记了但站点值空着_代理先不起(tmp_path):
    script = _准备(tmp_path)
    (tmp_path / "etc/d1max/tls").mkdir(parents=True)
    for f in ("registration.json", "tls/ca.crt", "tls/robot.crt", "tls/robot.key"):
        (tmp_path / "etc/d1max" / f).write_text("x", encoding="utf-8")
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "代理先不起" in got.stderr and "D1MAX_SITE_MQTT" in got.stderr
    assert "systemctl start d1max-agent.service" not in _调用(tmp_path)


def test_登记了_站点值填了_证书读得到_起代理(tmp_path):
    script = _准备(tmp_path)
    (tmp_path / "etc/d1max/tls").mkdir(parents=True)
    for f in ("registration.json", "tls/ca.crt", "tls/robot.crt", "tls/robot.key"):
        (tmp_path / "etc/d1max" / f).write_text("x", encoding="utf-8")
    (tmp_path / "etc/d1max/env").write_text(
        "D1MAX_SITE_MQTT=mqtts://site:8883\nD1MAX_MAP=estate:1\nD1MAX_HOME=0,0,0\n"
        "D1MAX_HAL=sim\n", encoding="utf-8")
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "代理先不起" not in got.stderr
    assert "systemctl start d1max-agent.service" in _调用(tmp_path)
    assert "缺这几行" not in got.stdout


def test_证书读不到_代理先不起(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root 读得到 0000 的文件,这条测不出来")
    script = _准备(tmp_path)
    (tmp_path / "etc/d1max/tls").mkdir(parents=True)
    for f in ("registration.json", "tls/ca.crt", "tls/robot.crt", "tls/robot.key"):
        (tmp_path / "etc/d1max" / f).write_text("x", encoding="utf-8")
    (tmp_path / "etc/d1max/tls/robot.key").chmod(0)
    (tmp_path / "etc/d1max/env").write_text(
        "D1MAX_SITE_MQTT=mqtts://site:8883\nD1MAX_MAP=estate:1\nD1MAX_HOME=0,0,0\n",
        encoding="utf-8")
    try:
        got = _跑(tmp_path, script)
    finally:
        (tmp_path / "etc/d1max/tls/robot.key").chmod(0o600)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "代理先不起" in got.stderr and "robot.key" in got.stderr
    assert "systemctl start d1max-agent.service" not in _调用(tmp_path)


@pytest.mark.parametrize("坏法", ["没有", "链接"])
def test_包里没有代理启动脚本_盘上一个字节都不动就停(tmp_path, 坏法):
    """W00c5 外审阻断 1 的配套:老服务那一代的包 7/7 必被 ``release activate`` 拒,而那时候 5/7
    已经把老服务清掉了 —— 要在动盘之前就停。"""
    script = _准备(tmp_path)
    start = tmp_path / "pkg" / PKG_NAME / "deploy" / "d1max-agent-start"
    start.unlink()
    if 坏法 == "链接":
        start.symlink_to(DEPLOY / "d1max-agent-start")
    got = _跑(tmp_path, script)
    assert got.returncode == 2, got.stdout + got.stderr
    assert "启动脚本" in got.stderr
    assert not (tmp_path / "opt/d1max").exists(), "1/7 还没开始"
    assert not (tmp_path / "calls").exists(), "systemctl、chown、python 一个都没跑"
