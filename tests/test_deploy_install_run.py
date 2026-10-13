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
            "/etc/chrony": root / "etc/chrony",
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
  is-enabled) for u in "$@"; do case "$u" in *.service)
      [ -e "{root}/etc/systemd/system/multi-user.target.wants/$u" ] || exit 1;; esac; done ;;
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


def _跑(root: Path, script: Path, **extra: str) -> subprocess.CompletedProcess:
    env = {"PATH": f"{root / 'stub'}:{os.environ['PATH']}", "HOME": str(root)} | extra
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


# ---------------------------------------------------------------- W35(2026-10-13 C40221 第二轮)


def test_W35_包里带了离线轮子_没给pip参数就只从它装_给了就听给的(tmp_path):
    """现场:清单写的 ``/home/robot/wheels`` 在这只狗上不存在,包里其实带着 ``wheels/``(站点下发的
    版本就是从它装的)。没给 ``D1MAX_PIP_ARGS`` 就用包里的;给了(哪怕是空的)听给的。"""
    script = _准备(tmp_path)
    wheels = tmp_path / "pkg" / PKG_NAME / "wheels"
    wheels.mkdir()
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    pips = [c for c in _调用(tmp_path) if "-m pip install" in c]
    assert pips and all(f"--no-index --find-links={wheels}" in c for c in pips), pips
    assert "离线轮子" in got.stdout
    for given in ("--no-index --find-links=/media/usb/wheels", ""):
        root = tmp_path / f"given{len(given)}"
        root.mkdir()
        script = _准备(root)
        (root / "pkg" / PKG_NAME / "wheels").mkdir()
        got = _跑(root, script, D1MAX_PIP_ARGS=given)
        assert got.returncode == 0, got.stdout + got.stderr
        pips = [c for c in _调用(root) if "-m pip install" in c]
        assert pips and not any("find-links=" + str(root / "pkg") in c for c in pips), pips
        if given:
            assert all(given in c for c in pips)


def test_W35_包里没带轮子_不加参数(tmp_path):
    script = _准备(tmp_path)
    got = _跑(tmp_path, script)
    assert got.returncode == 0
    assert not any("--no-index" in c for c in _调用(tmp_path))


def test_W35_开关写了bridge但服务没设自启_提示一句_设了就不提(tmp_path):
    """现场:定位器、感知、人员检测三个服务是手工 ``start`` 的,整机断电再开都不起,代理一直报没连上。
    装机脚本照旧不替人 enable(真机验过才起用),但 env 里已经写了要用、服务却没设自启的,说出来。"""
    script = _准备(tmp_path)
    etc = tmp_path / "etc/d1max"
    etc.mkdir(parents=True)
    (etc / "env").write_text("D1MAX_SITE_MQTT=\nD1MAX_MAP=\nD1MAX_HOME=\nD1MAX_HAL=sim\n"
                             "D1MAX_LOCALIZER=bridge\n  D1MAX_OBSTACLES=bridge\n"
                             "#   D1MAX_PERSONS=bridge\n", encoding="utf-8")
    wants = tmp_path / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "d1max-obstacles.service").write_text("", encoding="utf-8")
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "sudo systemctl enable --now d1max-localizer" in got.stdout
    assert "enable --now d1max-obstacles" not in got.stdout, "已经设了自启"
    assert "enable --now d1max-persons" not in got.stdout, "注释掉的不算"
    assert not any(c.startswith("systemctl enable") and "localizer" in c
                   for c in _调用(tmp_path)), "只提示,不替人 enable"


def _chrony狗(root: Path, *, 主配置: str | None, env: str) -> Path:
    script = _准备(root)
    if 主配置 is not None:
        (root / "etc/chrony").mkdir(parents=True)
        (root / "etc/chrony/chrony.conf").write_text(主配置, encoding="utf-8")
    (root / "etc/d1max").mkdir(parents=True)
    (root / "etc/d1max/env").write_text("D1MAX_MAP=\nD1MAX_HOME=\nD1MAX_HAL=sim\n" + env,
                                        encoding="utf-8")
    shutil.copy2(DEPLOY / "d1max-timesync-conf", root / "pkg" / PKG_NAME / "deploy")
    return script


def test_决策61_对时归chrony管的狗_写一个单独的片段向站点对时_厂家的配置不动(tmp_path):
    """2026-10-13 C40221:厂家的 chrony 跟上装电脑(慢 15 s)和没信号的秒脉冲,狗的钟一直不准,站点按
    钟差拒发命令;整机断电再开是 1970 年。假 systemctl 不认 ``show``,等于 timesyncd 不在(masked)。"""
    vendor = "refclock PPS /dev/pps0\nserver 192.168.168.19 iburst\nconfdir /etc/chrony/conf.d\n"
    vendor = vendor.replace("/etc/chrony", str(tmp_path / "etc/chrony"))
    script = _chrony狗(tmp_path, 主配置=vendor, env="D1MAX_SITE_MQTT=mqtts://10.0.0.5:8883\n")
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    frag = (tmp_path / "etc/chrony/conf.d/d1max.conf").read_text(encoding="utf-8")
    assert "\nserver 10.0.0.5 iburst prefer trust\n" in frag and "\nmakestep 1.0 3\n" in frag
    assert (tmp_path / "etc/chrony/chrony.conf").read_text(encoding="utf-8") == vendor, "厂家的不动"
    assert "systemctl restart chrony" in _调用(tmp_path)
    assert "chrony 向 10.0.0.5 对时" in got.stdout
    assert not (tmp_path / "etc/systemd/timesyncd.conf.d/d1max.conf").exists()


def test_决策61_厂家的主配置不读片段目录_不改它_说清楚要加哪一行(tmp_path):
    vendor = "server 192.168.168.19 iburst\n# confdir /etc/chrony/conf.d\n"
    script = _chrony狗(tmp_path, 主配置=vendor, env="D1MAX_SITE_NTP=site.lan\n")
    got = _跑(tmp_path, script)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "server site.lan iburst prefer trust" in (
        tmp_path / "etc/chrony/conf.d/d1max.conf").read_text(encoding="utf-8")
    assert "systemctl restart chrony" not in _调用(tmp_path), "没生效就不说对上了"
    assert "confdir" in got.stderr and "不改厂家的文件" in got.stderr
    assert "对时:chrony 向" not in got.stdout


def test_决策61_站点地址没填_不留片段_没有chrony的机器照旧只提示(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    script = _chrony狗(a, 主配置="confdir /etc/chrony/conf.d\n", env="D1MAX_SITE_MQTT=\n")
    (a / "etc/chrony/conf.d").mkdir()
    (a / "etc/chrony/conf.d/d1max.conf").write_text("server old.lan iburst\n", encoding="utf-8")
    got = _跑(a, script)
    assert got.returncode == 0, got.stdout + got.stderr
    assert not (a / "etc/chrony/conf.d/d1max.conf").exists(), "指着旧主机的片段不留"
    b = tmp_path / "b"
    b.mkdir()
    script = _chrony狗(b, 主配置=None, env="D1MAX_SITE_MQTT=mqtts://10.0.0.5:8883\n")
    got = _跑(b, script)
    assert got.returncode == 0, got.stdout + got.stderr
    assert not (b / "etc/chrony").exists() and "我们不配" in got.stderr
