"""外审 W55-1(`reviews/W55-外审-1.md`)对特权执行器的几条:每条一个复现,修好以后它们过。

1. VPN:站点给不了命令钩子 —— 执行器只收结构化数据,自己生成配置和防火墙规则;
   站点生成器跟执行器连得上(第 9 条)。
2. root 跑、代码在机器人可写目录里的 ``d1max-bind`` 不在重启白名单里。
3. 站点升级:签名范围以外的(``build/``)装之前删掉。
4. 站点升级回退:库被换成符号链接也不沿着它写。
5. 服务单元:装的就是算指纹的那一份;多出来的没签名的单元拒。
11. 站点升级回退:系统文件(单元、执行器)一起换回去;换回去也起不来就留着材料、说清楚。
12. 恢复备份:同一台主机上把原来的 VPN 状态带过来。
"""

from __future__ import annotations

import io
import json
import os
import pwd
import tarfile
from pathlib import Path

import pytest

from d1max_contract import relsign
from d1max_contract.digest import tree_sha256
from tests.test_ops_runner import R, Rec, _cfg, _release

ME = pwd.getpwuid(os.getuid())
KEY = "A" * 43 + "="


# ------------------------------------------------------------------ 1、9 VPN

def _doc(**改):
    d = {"address": "172.29.70.1/24", "port": 51820, "private": KEY,
         "peers": [{"name": "eng-x", "kind": "engineer", "public": "B" * 43 + "=",
                    "psk": "C" * 43 + "=", "address": "172.29.70.200/32"},
                   {"name": "A", "kind": "robot", "public": "D" * 43 + "=",
                    "psk": "E" * 43 + "=", "address": "172.29.70.10/32"}]}
    d.update(改)
    return d


def _site_vpn(tmp_path, doc) -> tuple[dict, Path]:
    cfg = _cfg(tmp_path, "site")
    home = Path(cfg["site_home"])
    (home / "vpn").mkdir(parents=True)
    (home / "vpn" / "apply.json").write_text(json.dumps(doc))
    return cfg, tmp_path / "etc"


@pytest.mark.parametrize("改, 说", [
    ({"post_up": "touch /tmp/pwn"}, "只认"),
    ({"private": "x\nPostUp = touch /tmp/pwn"}, "私钥"),
    ({"address": "10.0.0.1/24"}, "地址"),
    ({"peers": [{"name": "a\nPostUp = id", "kind": "robot", "public": "D" * 43 + "=",
                 "psk": "E" * 43 + "=", "address": "172.29.70.10/32"}]}, "名字"),
    ({"peers": [{"name": "a", "kind": "robot", "public": "D" * 43 + "=", "psk": "E" * 43 + "=",
                 "address": "172.29.70.10/32", "PostUp": "id"}]}, "只认"),
    ({"peers": [{"name": "a", "kind": "robot", "public": "D" * 43 + "=", "psk": "E" * 43 + "=",
                 "address": "0.0.0.0/0"}]}, "地址"),
])
def test_VPN_站点给不了命令钩子_格式不对一律拒(tmp_path, 改, 说):
    cfg, etc = _site_vpn(tmp_path, _doc(**改))
    rec = Rec()
    with pytest.raises(R.OpError, match=说):
        R.Ops(cfg, run=rec, etc=etc).do("vpn_apply", {})
    assert not (etc / "wireguard" / "d1max.conf").exists() and rec.calls == []


def test_VPN_执行器自己生成配置_规则固定_只放行工程师到22(tmp_path):
    cfg, etc = _site_vpn(tmp_path, _doc())
    rec = Rec()
    R.Ops(cfg, run=rec, etc=etc).do("vpn_apply", {})
    conf = (etc / "wireguard" / "d1max.conf").read_text()
    hooks = [line for line in conf.splitlines() if line.startswith(("PostUp", "PostDown", "PreUp",
                                                                     "PreDown"))]
    assert all(h.split(" = ", 1)[1].startswith(("sysctl -q", "iptables ")) for h in hooks)
    assert "-s 172.29.70.200/32 -p tcp --dport 22 -j ACCEPT" in conf
    assert "172.29.70.10/32 -p tcp" not in conf, "机器人、手机不放行转发"
    assert ["systemctl", "restart", "wg-quick@d1max.service"] in rec.calls


def test_VPN_站点的生成器跟执行器连得上_规则两边一样(tmp_path):
    from d1max_site.vpn import VpnState
    v = VpnState(tmp_path / "site" / "vpn")
    v.init(endpoint="vpn.example.com")
    v.add("robot", "A")
    v.add("engineer", "eng-bob", expires_ms=10)
    v.write_apply()
    cfg = _cfg(tmp_path, "site")
    R.Ops(cfg, run=Rec(), etc=tmp_path / "etc").do("vpn_apply", {})
    ours = (tmp_path / "etc" / "wireguard" / "d1max.conf").read_text()
    theirs = v.server_conf()
    rules = [x for x in theirs.splitlines() if x.startswith(("PostUp", "PostDown"))]
    assert rules and rules == [x for x in ours.splitlines() if x.startswith(("PostUp", "PostDown"))]
    assert R.VPN_NET == str(__import__("d1max_site.vpn", fromlist=["SUBNET"]).SUBNET
                            .network_address).rsplit(".", 1)[0]


def test_VPN_数据被换成链接_不跟(tmp_path):
    cfg, etc = _site_vpn(tmp_path, _doc())
    vpn = Path(cfg["site_home"]) / "vpn"
    (vpn / "apply.json").unlink()
    (tmp_path / "other.json").write_text(json.dumps(_doc()))
    (vpn / "apply.json").symlink_to(tmp_path / "other.json")
    with pytest.raises(R.OpError):
        R.Ops(cfg, run=Rec(), etc=etc).do("vpn_apply", {})


# ------------------------------------------------------------------ 2 d1max-bind

def test_配网服务的代码在root目录里_重启它不是提权(tmp_path):
    """第 2 条先是把它移出白名单;后来把它的代码挪进 root 独占目录(test_bind_rootonly.py),
    又放回来。"""
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "d1max-bind.service").read_text()
    assert "/opt/d1max" not in unit.split("[Service]", 1)[1]
    rec = Rec()
    R.Ops(_cfg(tmp_path), run=rec).do("restart", {"unit": "d1max-bind"})
    assert rec.calls == [["systemctl", "restart", "d1max-bind.service"]]


# ------------------------------------------------------------------ 5 服务单元

def test_服务单元_多出来没签名的单元_拒(tmp_path):
    cfg = _cfg(tmp_path)
    root, _ = _release(tmp_path, "[Unit]\nDescription=new\n")
    (root / "deploy" / "d1max-nav2.service").write_text("[Service]\nUser=root\nExecStart=/x\n")
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    with pytest.raises(R.OpError, match="不匹配"):
        R.Ops(cfg, run=Rec(), systemd_dir=sysd).do("units_apply", {"release": root.name})
    assert not list(sysd.iterdir())


def test_服务单元_装的就是算指纹的那一份(tmp_path, monkeypatch):
    """算完指纹再去读一遍的话,中间可以换内容;现在只读一遍:读到什么就算什么、装什么。"""
    cfg = _cfg(tmp_path)
    root, _ = _release(tmp_path, "[Unit]\nDescription=new\n")
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    reads = []
    real = R.read_nofollow

    def spy(path, **kw):
        reads.append(Path(path).name)
        return real(path, **kw)
    monkeypatch.setattr(R, "read_nofollow", spy)
    R.Ops(cfg, run=Rec(), systemd_dir=sysd).do("units_apply", {"release": root.name})
    assert reads.count("d1max-localizer.service") == 1


# ------------------------------------------------------------------ 3、4、11 站点升级

def _site_pkg(tmp_path, *, evil_build=True) -> tuple[dict, Path]:
    keys = tmp_path / "keys"
    relsign.keygen(keys / "priv.pem", keys / "pub.pem")
    src = tmp_path / "build-src" / "site-1.2.0"
    (src / "deploy" / "site").mkdir(parents=True)
    (src / "deploy" / "site" / "install-site.sh").write_text("echo install\n")
    (src / "packages" / "contract" / "src").mkdir(parents=True)
    (src / "packages" / "contract" / "src" / "a.py").write_text("x = 1\n")
    from d1max_agent.engine.release import _packed
    man = {"name": "site-1.2.0", "version": "1.2.0", "requires_mission_schema": 1,
           "content_sha256": tree_sha256(src, skip="release.json", keep=_packed)}
    man["signature"] = relsign.sign(man, keys / "priv.pem")
    (src / "release.json").write_text(json.dumps(man))
    if evil_build:                                   # 签名之后塞进去:不在指纹里
        (src / "packages" / "contract" / "build" / "lib").mkdir(parents=True)
        (src / "packages" / "contract" / "build" / "lib" / "evil.pth").write_text("import os\n")
    cfg = _cfg(tmp_path, "site")
    home = Path(cfg["site_home"])
    (home / "updates").mkdir(parents=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(src, arcname="site-1.2.0")
    (home / "updates" / "site-1.2.0.tar.gz").write_bytes(buf.getvalue())
    (home / "site.db").write_bytes(b"OLD-DB")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "py").write_text("old")
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    (sysd / "d1max-site.service").write_text("old unit")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "ops-runner").write_text("old runner")
    cfg.update({"site_venv": str(venv), "site_pubkey": str(keys / "pub.pem"),
                "lib_dir": str(lib), "spool": str(tmp_path / "spool")})
    return cfg, sysd


class SiteRun(Rec):
    """装机脚本:改 venv、单元、执行器;记下装的时候包里有没有 build/。"""

    def __init__(self, tmp_path):
        super().__init__()
        self.tmp = tmp_path
        self.saw_build = None

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[:1] == ["bash"]:
            pkg = Path(argv[1]).parents[2]
            self.saw_build = (pkg / "packages" / "contract" / "build").exists()
            (self.tmp / "venv" / "bin" / "py").write_text("new")
            (self.tmp / "systemd" / "d1max-site.service").write_text("new unit")
            (self.tmp / "lib" / "ops-runner").write_text("new runner")
        return 0, ""


def test_站点升级_签名以外的build目录装之前删掉(tmp_path, monkeypatch):
    cfg, sysd = _site_pkg(tmp_path)
    run = SiteRun(tmp_path)
    monkeypatch.setattr(R.Ops, "_healthy", lambda self, w: True)
    got = R.Ops(cfg, run=run, systemd_dir=sysd, etc=tmp_path / "etc").do(
        "update", {"package": "site-1.2.0.tar.gz"})
    assert got["rolled_back"] is False
    assert run.saw_build is False, "build/ 不在签名里:装之前删掉"


def test_站点升级前库就是链接_不升级(tmp_path, monkeypatch):
    cfg, sysd = _site_pkg(tmp_path, evil_build=False)
    home = Path(cfg["site_home"])
    (tmp_path / "victim").write_text("ROOT FILE")
    (home / "site.db").unlink()
    (home / "site.db").symlink_to(tmp_path / "victim")
    monkeypatch.setattr(R.Ops, "_healthy", lambda self, w: True)
    rec = SiteRun(tmp_path)
    with pytest.raises(R.OpError, match="未升级"):
        R.Ops(cfg, run=rec, systemd_dir=sysd, etc=tmp_path / "etc").do(
            "update", {"package": "site-1.2.0.tar.gz"})
    assert rec.saw_build is None, "没跑装机脚本"


def test_站点升级回退_系统文件一起换回去_库是链接也不沿着写(tmp_path, monkeypatch):
    cfg, sysd = _site_pkg(tmp_path, evil_build=False)
    home = Path(cfg["site_home"])
    victim = tmp_path / "victim"
    victim.write_text("ROOT FILE")
    healthy = iter([False, True])
    monkeypatch.setattr(R.Ops, "_healthy", lambda self, w: next(healthy))

    class Run(SiteRun):
        def __call__(self, argv, timeout):
            out = super().__call__(argv, timeout)
            if argv[:1] == ["bash"]:                    # 装的时候站点账号把库换成链接
                (home / "site.db").unlink()
                (home / "site.db").symlink_to(victim)
            return out
    with pytest.raises(R.OpError, match="已退回原版本"):
        R.Ops(cfg, run=Run(tmp_path), systemd_dir=sysd, etc=tmp_path / "etc").do(
            "update", {"package": "site-1.2.0.tar.gz"})
    assert (home / "site.db").read_bytes() == b"OLD-DB" and not (home / "site.db").is_symlink()
    assert victim.read_text() == "ROOT FILE", "不沿着链接读写"
    assert (sysd / "d1max-site.service").read_text() == "old unit"
    assert (tmp_path / "lib" / "ops-runner").read_text() == "old runner"
    assert (tmp_path / "venv" / "bin" / "py").read_text() == "old"


def test_站点升级回退_库放回去是换目录项(tmp_path, monkeypatch):
    cfg, sysd = _site_pkg(tmp_path, evil_build=False)
    home = Path(cfg["site_home"])
    healthy = iter([False, True])
    monkeypatch.setattr(R.Ops, "_healthy", lambda self, w: next(healthy))

    class Run(SiteRun):
        def __call__(self, argv, timeout):
            out = super().__call__(argv, timeout)
            if argv[:1] == ["bash"]:
                (home / "site.db").write_bytes(b"NEW-DB")
            return out
    with pytest.raises(R.OpError, match="已退回原版本"):
        R.Ops(cfg, run=Run(tmp_path), systemd_dir=sysd, etc=tmp_path / "etc").do(
            "update", {"package": "site-1.2.0.tar.gz"})
    assert (home / "site.db").read_bytes() == b"OLD-DB"


def test_站点升级回退后还起不来_材料留着_说清楚(tmp_path, monkeypatch):
    cfg, sysd = _site_pkg(tmp_path, evil_build=False)
    monkeypatch.setattr(R.Ops, "_healthy", lambda self, w: False)
    with pytest.raises(R.OpError, match="恢复材料留在") as e:
        R.Ops(cfg, run=SiteRun(tmp_path), systemd_dir=sysd, etc=tmp_path / "etc").do(
            "update", {"package": "site-1.2.0.tar.gz"})
    where = str(e.value).split("恢复材料留在 ", 1)[1].split("(", 1)[0]
    assert Path(where, "venv").is_dir()


# ------------------------------------------------------------------ 12 恢复备份带回 VPN

def test_恢复备份_原来的VPN状态带过来_不跟链接(tmp_path):
    cfg = _cfg(tmp_path, "site")
    home = Path(cfg["site_home"])
    (home / "vpn" / "peers").mkdir(parents=True)
    (home / "vpn" / "state.json").write_text('{"server": {}}')
    (home / "vpn" / "peers" / "A.conf").write_text("[Interface]\n")
    (tmp_path / "secret").write_text("ROOT ONLY")
    (home / "vpn" / "evil").symlink_to(tmp_path / "secret")
    backup = tmp_path / "backup"
    backup.mkdir()
    (home / "site.json").write_text(json.dumps({"backup_dir": str(backup)}))
    venv = tmp_path / "venv"
    cfg["site_venv"] = str(venv)

    class Run(Rec):
        def __call__(self, argv, timeout):
            self.calls.append(argv)
            if argv[:1] == ["runuser"]:
                (Path(argv[argv.index("--home") + 1]) / "site.db").write_text("restored")
            return 0, ""
    got = R.Ops(cfg, run=Run()).do("restore", {})
    assert got["vpn_files"] == 2
    assert (home / "vpn" / "state.json").read_text() == '{"server": {}}'
    assert (home / "vpn" / "peers" / "A.conf").is_file()
    assert not (home / "vpn" / "evil").exists()


# ================================================================== 外审 W55-2

def test_外审W55_2_C1_解包不用包里的权限_根目录条目跳过(tmp_path):
    src = tmp_path / "src" / "pkg"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.sh").write_text("echo hi\n")
    os.chmod(src / "sub" / "a.sh", 0o777)
    os.chmod(src / "sub", 0o777)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        root = tarfile.TarInfo(".")
        root.type, root.mode = tarfile.DIRTYPE, 0o777
        tar.addfile(root)
        tar.add(src, arcname="pkg")
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue()), mode="r:gz") as tar:
        R.safe_extract(tar, work)
    assert oct(work.stat().st_mode & 0o777) == "0o700", "解压根目录的权限不许被包改掉"
    assert oct((work / "pkg" / "sub").stat().st_mode & 0o777) == "0o755"
    assert oct((work / "pkg" / "sub" / "a.sh").stat().st_mode & 0o777) == "0o755"


def test_外审W55_2_C1_同一个文件出现两次_拒(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for body in (b"good", b"evil"):
            ti = tarfile.TarInfo("pkg/a.sh")
            ti.size = len(body)
            tar.addfile(ti, io.BytesIO(body))
    work = tmp_path / "work"
    work.mkdir()
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue()), mode="r:gz") as tar, \
            pytest.raises(R.OpError, match="两次"):
        R.safe_extract(tar, work)


@pytest.mark.parametrize("name, unit, ok", [
    ("d1max-localizer.service", "[Service]\nUser=robot\nExecStart=/x\n", True),
    ("d1max-localizer.service", "[Service]\nExecStart=/x\n", False),
    ("d1max-localizer.service", "[Service]\nUser=root\nExecStart=/x\n", False),
    ("d1max-bind.service", "[Service]\nUser=root\n" + R.BIND_EXEC + "\n", True),
    ("d1max-bind.service",
     "[Service]\nUser=root\nExecStart=/opt/d1max/current/deploy/d1max-bind-start\n", False),
])
def test_外审W55_2_C2_服务单元_只装非root的和从root目录跑的配网(name, unit, ok):
    assert (R.unit_problem(name, unit.encode(), bind_ok=True) == "") is ok


def test_外审W55_2_C2_老签名包装不回老的配网单元(tmp_path):
    cfg = _cfg(tmp_path)
    old = "[Service]\nUser=root\nExecStart=/opt/d1max/current/deploy/d1max-bind-start\n"
    root, _ = _release(tmp_path, "[Service]\nUser=robot\n")
    (root / "deploy" / "d1max-bind.service").write_text(old)
    from d1max_agent.engine.release import _packed
    man = json.loads((root / "release.json").read_text())
    man.pop("signature")
    man["content_sha256"] = tree_sha256(root, skip="release.json", keep=_packed)
    man["signature"] = relsign.sign(man, tmp_path / "keys" / "priv.pem")
    (root / "release.json").write_text(json.dumps(man))
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    (sysd / "d1max-bind.service").write_text("SAFE")
    got = R.Ops(cfg, run=Rec(), systemd_dir=sysd).do("units_apply", {"release": root.name})
    assert (sysd / "d1max-bind.service").read_text() == "SAFE"
    assert any("d1max-bind.service" in x for x in got["skipped"])


def test_外审W55_2_C5_换证书_旧证书留底不跟链接(tmp_path):
    cfg = _cfg(tmp_path)
    new = Path(cfg["data_root"]) / "cert-new"
    new.mkdir(parents=True)
    (new / "robot.crt").write_text("-----BEGIN CERTIFICATE-----\nNEW\n")
    (new / "robot.key").write_text("-----BEGIN PRIVATE KEY-----\nNEW\n")
    (new / "registration.json").write_text("{}")
    etc = tmp_path / "etc"
    tls = etc / "d1max" / "tls"
    tls.mkdir(parents=True)
    (tls / "robot.crt").write_text("OLD")
    victim = tmp_path / "victim"
    victim.write_text("ROOT FILE")
    (tls / "robot.crt.prev").symlink_to(victim)            # 布下的链接
    R.Ops(cfg, run=Rec(), etc=etc).do("cert_install", {})
    assert victim.read_text() == "ROOT FILE"
    prev = tls / "robot.crt.prev"
    assert prev.read_text() == "OLD" and not prev.is_symlink()
    assert (tls / "robot.crt").read_text().endswith("NEW\n")


def test_外审W55_2_C4_恢复VPN_目标目录被换成链接_不写过去(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, "site")
    home = Path(cfg["site_home"])
    aside = tmp_path / "aside"
    (aside / "vpn").mkdir(parents=True)
    (aside / "vpn" / "state.json").write_text("{}")
    home.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "vpn").symlink_to(elsewhere)                     # 站点账号先布好链接
    with pytest.raises(R.OpError):
        R.Ops(cfg, run=Rec())._keep_vpn(aside, home, ME.pw_uid, ME.pw_gid)
    assert not list(elsewhere.iterdir())


def test_外审W55_2_I4_全部重启_有服务起不来就说_不重启代理(tmp_path):
    rec = Rec({"systemctl restart d1max-localizer.service": 1})
    with pytest.raises(R.OpError, match="d1max-localizer"):
        R.Ops(_cfg(tmp_path), run=rec).do("restart_all", {})
    assert ["systemctl", "--no-block", "restart", "d1max-agent.service"] not in rec.calls
