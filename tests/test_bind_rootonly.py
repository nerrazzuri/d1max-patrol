"""配网服务(W42)以 root 跑:它的代码不许放在机器人账号能改的地方(外审 W55-1 顺带发现的老问题)。

以前 ``d1max-bind.service`` 以 root 执行 ``/opt/d1max/current/deploy/d1max-bind-start``,用的是
``/opt/d1max/bin/python`` 和版本目录里的代码 —— 那些都归机器人账号(robot)。robot 改掉其中任何一个,
下次配网服务起来就是 root。现在:

- 要的模块从**签过名的版本包**里拷进 root 独占的 ``/usr/local/lib/d1max/bind/``;
- 用系统的 ``/usr/bin/python3 -I``(不读环境变量、不读用户目录、不把脚本目录放进搜索路径)跑;
- 装机脚本装一份;远程换版本时特权执行器验签以后换上(跟服务单元同一遍读)、重启配网服务。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from d1max_contract import relsign
from d1max_contract.digest import tree_sha256
from tests.test_ops_runner import R, Rec, _cfg

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "d1max-bind.service"


def test_服务单元_用系统python跑root目录里的代码_不碰版本目录():
    text = UNIT.read_text(encoding="utf-8")
    svc = text.split("[Service]", 1)[1]
    assert "User=root" in svc
    execs = [ln for ln in svc.splitlines() if ln.startswith("Exec")]
    assert execs == ["ExecStart=/usr/bin/python3 -I /usr/local/lib/d1max/bind/run.py"]
    assert "/opt/d1max" not in svc


def _install(tmp: Path) -> Path:
    files = {rel: (ROOT / rel).read_bytes() for rel in R.BIND_FILES}
    assert R.install_bind(files, tmp) is True
    return tmp / "bind"


def test_root目录里那份代码自己就能起来_不从别处导入(tmp_path):
    bind = _install(tmp_path / "lib")
    probe = (
        "import sys, runpy\n"
        f"sys.path.insert(0, {str(bind / 'lib')!r})\n"
        "import d1max_patrol.bind.cli, d1max_patrol.bind.server, d1max_patrol.bind.core\n"
        "import d1max_patrol.bind.wipe, d1max_patrol.bind.factory, d1max_patrol.bind.sitelink\n"
        "mine = [m for m, x in sys.modules.items() if m.startswith(('d1max', 'paho', 'numpy'))]\n"
        "bad = [m for m in mine\n"
        f"       if not (sys.modules[m].__file__ or '').startswith({str(bind)!r})]\n"
        "print(len(mine), bad)\n")
    out = subprocess.run(["/usr/bin/python3", "-I", "-c", probe], capture_output=True, text=True,
                         check=True).stdout.split()
    assert int(out[0]) >= 10 and out[1] == "[]", out
    # 入口:没给 D1MAX_BIND_HOST 就说清楚、退出(不是 ImportError)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("D1MAX_", "PYTHON"))}
    got = subprocess.run(["/usr/bin/python3", "-I", str(bind / "run.py")], capture_output=True,
                         text=True, env=env)
    assert got.returncode != 0 and "D1MAX_BIND_HOST" in got.stderr
    assert "Traceback" not in got.stderr


def test_装一份_再装一样的不动_文件全是只读(tmp_path):
    bind = _install(tmp_path / "lib")
    files = {rel: (ROOT / rel).read_bytes() for rel in R.BIND_FILES}
    assert R.install_bind(files, tmp_path / "lib") is False, "一样的不重装"
    for p in bind.rglob("*"):
        if p.is_file():
            assert p.stat().st_mode & 0o022 == 0, p
    assert (bind / "run.py").stat().st_mode & 0o111


def _release(tmp: Path) -> Path:
    keys = tmp / "keys"
    relsign.keygen(keys / "priv.pem", keys / "pub.pem")
    name = "2026-10-31-abcdef12"
    root = tmp / "opt" / "releases" / name
    for rel in R.BIND_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes((ROOT / rel).read_bytes())
    (root / "deploy" / "d1max-bind.service").write_bytes(UNIT.read_bytes())
    from d1max_agent.engine.release import _packed
    man = {"name": name, "version": "1.0", "requires_mission_schema": 1,
           "content_sha256": tree_sha256(root, skip="release.json", keep=_packed)}
    man["signature"] = relsign.sign(man, keys / "priv.pem")
    (root / "release.json").write_text(json.dumps(man))
    return root


def test_换版本_验签以后换上配网代码和单元_重启配网服务(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["lib_dir"] = str(tmp_path / "lib")
    root = _release(tmp_path)
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    rec = Rec()
    got = R.Ops(cfg, run=rec, systemd_dir=sysd).do("units_apply", {"release": root.name})
    assert got["bind"] is True and "d1max-bind.service" in got["changed"]
    assert (tmp_path / "lib" / "bind" / "run.py").is_file()
    assert ["systemctl", "--no-block", "try-restart", "d1max-bind.service"] in rec.calls


def test_换版本_配网代码被改过_拒_什么都不装(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["lib_dir"] = str(tmp_path / "lib")
    root = _release(tmp_path)
    (root / "src" / "d1max_patrol" / "bind" / "wipe.py").write_text("import os; os.system('id')\n")
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    with pytest.raises(R.OpError, match="不匹配"):
        R.Ops(cfg, run=Rec(), systemd_dir=sysd).do("units_apply", {"release": root.name})
    assert not (tmp_path / "lib" / "bind").exists()


def test_装机脚本装配网代码():
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert '/usr/bin/python3 /usr/local/lib/d1max/ops-runner --install-bind "$PKG"' in text


def test_能远程重启配网服务了():
    assert "d1max-bind" in R.ROBOT_UNITS, "代码在 root 目录里了,重启它不再是提权"


def test_执行器的配网文件单子_都在仓库里():
    missing = [rel for rel in R.BIND_FILES if not (ROOT / rel).is_file()]
    assert not missing
    assert sys.version_info >= (3, 10)


def test_W57_换版本_验签以后装上新的解绑公钥和作废名单(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["lib_dir"] = str(tmp_path / "lib")
    keys = tmp_path / "keys"
    relsign.keygen(keys / "priv.pem", keys / "pub.pem")
    name = "2026-10-31-abcdef12"
    root = tmp_path / "opt" / "releases" / name
    (root / "deploy" / "unbind-pub.d").mkdir(parents=True)
    (root / "deploy" / "unbind-pub.d" / "2026-11.pem").write_text("PUB-NEW")
    (root / "deploy" / "unbind-revoked.txt").write_text("abcdef0123456789\n")
    from d1max_agent.engine.release import _packed
    man = {"name": name, "version": "1.0", "requires_mission_schema": 1,
           "content_sha256": tree_sha256(root, skip="release.json", keep=_packed)}
    man["signature"] = relsign.sign(man, keys / "priv.pem")
    (root / "release.json").write_text(json.dumps(man))
    etc = tmp_path / "etc"
    (etc / "d1max" / "unbind-pub.d").mkdir(parents=True)
    (etc / "d1max" / "unbind-pub.d" / "stale.pem").write_text("OLD")
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    got = R.Ops(cfg, run=Rec(), systemd_dir=sysd, etc=etc).do("units_apply", {"release": name})
    assert got["unbind_keys"] == ["2026-11.pem"]
    assert sorted(p.name for p in (etc / "d1max" / "unbind-pub.d").iterdir()) == ["2026-11.pem"]
    assert (etc / "d1max" / "unbind-revoked.txt").read_text() == "abcdef0123456789\n"


def test_W57_装机脚本装解绑公钥和作废名单():
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "/etc/d1max/unbind-pub.d" in text and "/etc/d1max/unbind-revoked.txt" in text
