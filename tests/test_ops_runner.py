"""W55 特权执行器(deploy/d1max-ops-runner):白名单、请求文件的属主、证书换装、验签后才装服务单元。

执行器以 root 跑;测试里不碰系统,命令全换成记录器,系统目录换成临时目录。"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pwd
from pathlib import Path

import pytest

from d1max_contract import relsign
from d1max_contract.digest import tree_sha256
from d1max_contract.privops import SimOps, SpoolOps

ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    loader = importlib.machinery.SourceFileLoader("d1max_ops_runner",
                                                  str(ROOT / "deploy" / "d1max-ops-runner"))
    spec = importlib.util.spec_from_loader("d1max_ops_runner", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


R = _load_runner()
ME = pwd.getpwuid(os.getuid())


class Rec:
    def __init__(self, rc: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc or {}

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        return self.rc.get(" ".join(argv[:3]), 0), ""


def _cfg(tmp: Path, profile: str = "robot") -> dict:
    spool = tmp / "spool"
    (spool / "req").mkdir(parents=True)
    (spool / "done").mkdir()
    return {"profile": profile, "requester": ME.pw_name, "spool": str(spool),
            "data_root": str(tmp / "data"), "release_root": str(tmp / "opt"),
            "pubkey": str(tmp / "keys" / "pub.pem"), "site_home": str(tmp / "site")}


def _req(cfg: dict, rid: str, action: str, args: dict | None = None) -> None:
    (Path(cfg["spool"]) / "req" / f"{rid}.json").write_text(
        json.dumps({"id": rid, "action": action, "args": args or {}}))


def _done(cfg: dict, rid: str) -> dict:
    return json.loads((Path(cfg["spool"]) / "done" / f"{rid}.json").read_text())


def test_白名单_服务名不在单子上拒_动作不认识拒(tmp_path):
    cfg = _cfg(tmp_path)
    rec = Rec()
    ops = R.Ops(cfg, run=rec)
    _req(cfg, "a1b2c3d4", "restart", {"unit": "ssh"})
    _req(cfg, "a1b2c3d5", "rm_rf", {})
    _req(cfg, "a1b2c3d6", "restart", {"unit": "d1max-localizer"})
    assert R.process(cfg, ops, uid=ME.pw_uid, gid=ME.pw_gid) == 3
    assert not _done(cfg, "a1b2c3d4")["ok"] and "白名单" in _done(cfg, "a1b2c3d4")["error"]
    assert not _done(cfg, "a1b2c3d5")["ok"] and "不认识" in _done(cfg, "a1b2c3d5")["error"]
    assert _done(cfg, "a1b2c3d6")["ok"]
    assert rec.calls == [["systemctl", "restart", "d1max-localizer.service"]]
    assert not list((Path(cfg["spool"]) / "req").glob("*.json")), "处理过的请求都删掉"


def test_请求属主不是请求方_不理(tmp_path):
    cfg = _cfg(tmp_path)
    rec = Rec()
    _req(cfg, "a1b2c3d4", "reboot", {})
    assert R.process(cfg, R.Ops(cfg, run=rec), uid=ME.pw_uid + 12345, gid=ME.pw_gid) == 0
    assert rec.calls == [] and not (Path(cfg["spool"]) / "done" / "a1b2c3d4.json").exists()


def test_请求是符号链接_不跟(tmp_path):
    cfg = _cfg(tmp_path)
    target = tmp_path / "evil.json"
    target.write_text(json.dumps({"id": "a1b2c3d4", "action": "reboot", "args": {}}))
    (Path(cfg["spool"]) / "req" / "a1b2c3d4.json").symlink_to(target)
    rec = Rec()
    assert R.process(cfg, R.Ops(cfg, run=rec), uid=ME.pw_uid, gid=ME.pw_gid) == 0
    assert rec.calls == []


def test_重启代理自己不等_重启全部只动启用的_代理最后(tmp_path):
    cfg = _cfg(tmp_path)
    rec = Rec({"systemctl is-active --quiet": 3, "systemctl is-enabled --quiet": 1})
    ops = R.Ops(cfg, run=rec)
    assert ops.do("restart", {"unit": "d1max-agent"}) == {"unit": "d1max-agent"}
    assert rec.calls[-1] == ["systemctl", "--no-block", "restart", "d1max-agent.service"]
    rec2 = Rec()
    out = R.Ops(cfg, run=rec2).do("restart_all", {})
    assert out["restarted"][-1] == "d1max-agent" and len(out["restarted"]) == len(R.ROBOT_UNITS)
    restarts = [c for c in rec2.calls if "restart" in c]
    assert restarts[-1] == ["systemctl", "--no-block", "restart", "d1max-agent.service"]


def test_重启整机_延时要在范围里(tmp_path):
    ops = R.Ops(_cfg(tmp_path), run=Rec())
    with pytest.raises(R.OpError):
        ops.do("reboot", {"delay_s": 600})
    assert ops.do("reboot", {"delay_s": 3}) == {"delay_s": 3}


def test_换证书_核格式_原来的留prev_重启代理(tmp_path):
    cfg = _cfg(tmp_path)
    new = tmp_path / "data" / "cert-new"
    new.mkdir(parents=True)
    (new / "robot.crt").write_text("-----BEGIN CERTIFICATE-----\nNEW\n-----END CERTIFICATE-----\n")
    (new / "robot.key").write_text("-----BEGIN PRIVATE KEY-----\nNEW\n-----END PRIVATE KEY-----\n")
    (new / "registration.json").write_text('{"robot_id": "A"}')
    etc = tmp_path / "etc"
    (etc / "d1max" / "tls").mkdir(parents=True)
    (etc / "d1max" / "tls" / "robot.crt").write_text("OLD")
    rec = Rec()
    out = R.Ops(cfg, run=rec, etc=etc).do("cert_install", {})
    assert out["installed"] == ["robot.crt", "robot.key", "registration.json"]
    assert "NEW" in (etc / "d1max" / "tls" / "robot.crt").read_text()
    assert (etc / "d1max" / "tls" / "robot.crt.prev").read_text() == "OLD"
    assert oct((etc / "d1max" / "tls" / "robot.key").stat().st_mode & 0o777) == "0o600"
    assert rec.calls[-1] == ["systemctl", "--no-block", "restart", "d1max-agent.service"]


def test_换证书_格式不对拒_什么都不动(tmp_path):
    cfg = _cfg(tmp_path)
    new = tmp_path / "data" / "cert-new"
    new.mkdir(parents=True)
    for n in ("robot.crt", "robot.key", "registration.json"):
        (new / n).write_text("junk")
    etc = tmp_path / "etc"
    (etc / "d1max" / "tls").mkdir(parents=True)
    rec = Rec()
    with pytest.raises(R.OpError):
        R.Ops(cfg, run=rec, etc=etc).do("cert_install", {})
    assert rec.calls == [] and not (etc / "d1max" / "tls" / "robot.crt").exists()


def _release(tmp: Path, unit_text: str) -> tuple[Path, Path]:
    keys = tmp / "keys"
    relsign.keygen(keys / "priv.pem", keys / "pub.pem")
    name = "2026-10-31-abcdef12"
    root = tmp / "opt" / "releases" / name
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "d1max-localizer.service").write_text(unit_text)
    (root / "src").mkdir()
    (root / "src" / "x.py").write_text("print(1)\n")
    (root / "venv").mkdir()                       # 装好之后才长出来的,不进指纹
    (root / "venv" / "junk").write_text("x")
    from d1max_agent.engine.release import _packed
    man = {"name": name, "version": "1.0", "requires_mission_schema": 1,
           "content_sha256": tree_sha256(root, skip="release.json", keep=_packed)}
    man["signature"] = relsign.sign(man, keys / "priv.pem")
    (root / "release.json").write_text(json.dumps(man))
    return root, keys


def test_装服务单元_验签过了才装_有变化才daemon_reload(tmp_path):
    cfg = _cfg(tmp_path)
    unit = "[Unit]\nDescription=new\n[Service]\nUser=robot\n"
    root, _ = _release(tmp_path, unit)
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    rec = Rec()
    ops = R.Ops(cfg, run=rec, systemd_dir=sysd)
    got = ops.do("units_apply", {"release": root.name})
    assert got == {"changed": ["d1max-localizer.service"], "bind": False}
    assert (sysd / "d1max-localizer.service").read_text() == unit
    assert rec.calls == [["systemctl", "daemon-reload"]]
    rec.calls.clear()
    assert ops.do("units_apply", {"release": root.name}) == {"changed": [], "bind": False}
    assert rec.calls == []


def test_装服务单元_包被改过拒(tmp_path):
    cfg = _cfg(tmp_path)
    root, _ = _release(tmp_path, "[Unit]\nDescription=new\n")
    evil = "[Service]\nExecStart=/bin/sh -c evil\n"
    (root / "deploy" / "d1max-localizer.service").write_text(evil)
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    with pytest.raises(R.OpError, match="不匹配"):
        R.Ops(cfg, run=Rec(), systemd_dir=sysd).do("units_apply", {"release": root.name})
    assert not (sysd / "d1max-localizer.service").exists()


def test_装服务单元_没有公钥_版本名不对_都拒(tmp_path):
    cfg = _cfg(tmp_path)
    ops = R.Ops(cfg, run=Rec())
    with pytest.raises(R.OpError, match="版本名"):
        ops.do("units_apply", {"release": "../../etc"})
    with pytest.raises(R.OpError, match="公钥"):
        ops.do("units_apply", {"release": "2026-10-31-abcdef12"})


def test_打包规则两边一样():
    from d1max_agent.engine.release import _packed
    for rel in ("src/a.py", "venv/bin/python", "packages/x/build/lib/a.py", "build/a",
                "packages/x/src/a.py", "motion/build/patrol_agent", "motion/patrol_agent.cpp",
                "a/__pycache__/b.pyc", "x.egg-info/PKG-INFO", "packages/x/tests/t.py",
                "deploy/d1max-agent.service", "dist/x.whl"):
        assert R.packed(rel) == _packed(rel), rel


def test_站点动作_机器人上不认(tmp_path):
    ops = R.Ops(_cfg(tmp_path), run=Rec())
    with pytest.raises(R.OpError, match="不认识"):
        ops.do("update", {"package": "x.tar.gz"})
    site = R.Ops(_cfg(tmp_path / "s", "site"), run=Rec())
    with pytest.raises(R.OpError, match="白名单"):
        site.do("restart", {"unit": "d1max-agent"})


def test_VPN状态_解析wg_dump(tmp_path):
    def run(argv, timeout):
        return 0, ("priv\tpub\t51820\toff\n"
                   "PEER1\tpsk\t1.2.3.4:5\t172.29.70.10/32\t1793428000\t100\t200\t25\n")
    out = R.Ops(_cfg(tmp_path, "site"), run=run).do("wg_status", {})
    assert out == {"up": True, "peers": [{"public_key": "PEER1", "allowed_ips": "172.29.70.10/32",
                                          "latest_handshake": 1793428000, "rx": 100, "tx": 200}]}


def test_调用端_写请求_读结果_超时说清楚(tmp_path):
    cfg = _cfg(tmp_path)
    t = [0.0]

    def sleep(s):
        t[0] += s
        R.process(cfg, R.Ops(cfg, run=Rec()), uid=ME.pw_uid, gid=ME.pw_gid)

    c = SpoolOps(cfg["spool"], sleep=sleep, monotonic=lambda: t[0])
    got = c.call("restart", {"unit": "d1max-persons"})
    assert got["ok"] and got["unit"] == "d1max-persons"
    lazy = SpoolOps(cfg["spool"], sleep=lambda s: t.__setitem__(0, t[0] + s),
                    monotonic=lambda: t[0])
    from d1max_contract.privops import PrivOpsError
    with pytest.raises(PrivOpsError, match="未处理,请求已撤回"):
        lazy.call("timesync", timeout_s=1)
    assert not list((Path(cfg["spool"]) / "req").glob("*.json")), "撤回了:执行器再也拿不到"


def test_外审W55_2_执行器拿走了请求但没回结果_算结果未知(tmp_path):
    """C3:超时以后先撤回;撤不回(执行器已经改名认领)就是结果未知,不许当没执行。"""
    from d1max_contract.privops import PrivTimeout
    cfg = _cfg(tmp_path)
    t = [0.0]
    req = Path(cfg["spool"]) / "req"

    def sleep(s):
        t[0] += s
        for f in req.glob("*.json"):                   # 执行器认领了,但一直没做完
            f.rename(f.with_name(f.name + ".claim"))
    c = SpoolOps(cfg["spool"], sleep=sleep, monotonic=lambda: t[0])
    with pytest.raises(PrivTimeout) as e:
        c.call("reboot", {"delay_s": 5}, timeout_s=1)
    assert e.value.rid and not c.cancel(e.value.rid)


def test_外审W55_2_请求被撤回了_执行器不执行(tmp_path):
    cfg = _cfg(tmp_path)
    c = SpoolOps(cfg["spool"])
    rid = c.submit("restart", {"unit": "d1max-persons"})
    assert c.cancel(rid)
    rec = Rec()
    assert R.process(cfg, R.Ops(cfg, run=rec), uid=ME.pw_uid, gid=ME.pw_gid) == 0
    assert rec.calls == []


def test_模拟版_记下调用():
    s = SimOps(fail={"reboot": "忙"})
    assert s.call("restart", {"unit": "d1max-agent"})["ok"]
    assert s.call("reboot")["ok"] is False
    assert s.calls == [("restart", {"unit": "d1max-agent"}), ("reboot", {})]
