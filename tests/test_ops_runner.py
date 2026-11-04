"""W55 特权执行器(deploy/nexwar_ops_runner):白名单、请求文件的属主、证书换装、验签后才装服务单元。

执行器以 root 跑;测试里不碰系统,命令全换成记录器,系统目录换成临时目录。"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pwd
from pathlib import Path

import pytest

from nexwar_contract import relsign
from nexwar_contract.digest import tree_sha256
from nexwar_contract.privops import SimOps, SpoolOps

ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    loader = importlib.machinery.SourceFileLoader("nexwar_ops_runner",
                                                  str(ROOT / "deploy" / "nexwar_ops_runner"))
    spec = importlib.util.spec_from_loader("nexwar_ops_runner", loader)
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
    _req(cfg, "a1b2c3d6", "restart", {"unit": "nexwar_localizer"})
    assert R.process(cfg, ops, uid=ME.pw_uid, gid=ME.pw_gid) == 3
    assert not _done(cfg, "a1b2c3d4")["ok"] and "白名单" in _done(cfg, "a1b2c3d4")["error"]
    assert not _done(cfg, "a1b2c3d5")["ok"] and "不认识" in _done(cfg, "a1b2c3d5")["error"]
    assert _done(cfg, "a1b2c3d6")["ok"]
    assert rec.calls == [["systemctl", "restart", "nexwar_localizer.service"]]
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
    assert ops.do("restart", {"unit": "nexwar_agent"}) == {"unit": "nexwar_agent"}
    assert rec.calls[-1] == ["systemctl", "--no-block", "restart", "nexwar_agent.service"]
    rec2 = Rec()
    out = R.Ops(cfg, run=rec2).do("restart_all", {})
    assert out["restarted"][-1] == "nexwar_agent" and len(out["restarted"]) == len(R.ROBOT_UNITS)
    restarts = [c for c in rec2.calls if "restart" in c]
    assert restarts[-1] == ["systemctl", "--no-block", "restart", "nexwar_agent.service"]


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
    (etc / "nexwar" / "tls").mkdir(parents=True)
    (etc / "nexwar" / "tls" / "robot.crt").write_text("OLD")
    rec = Rec()
    out = R.Ops(cfg, run=rec, etc=etc).do("cert_install", {})
    assert out["installed"] == ["robot.crt", "robot.key", "registration.json"]
    assert "NEW" in (etc / "nexwar" / "tls" / "robot.crt").read_text()
    assert (etc / "nexwar" / "tls" / "robot.crt.prev").read_text() == "OLD"
    assert oct((etc / "nexwar" / "tls" / "robot.key").stat().st_mode & 0o777) == "0o600"
    assert rec.calls[-1] == ["systemctl", "--no-block", "restart", "nexwar_agent.service"]


def test_换证书_格式不对拒_什么都不动(tmp_path):
    cfg = _cfg(tmp_path)
    new = tmp_path / "data" / "cert-new"
    new.mkdir(parents=True)
    for n in ("robot.crt", "robot.key", "registration.json"):
        (new / n).write_text("junk")
    etc = tmp_path / "etc"
    (etc / "nexwar" / "tls").mkdir(parents=True)
    rec = Rec()
    with pytest.raises(R.OpError):
        R.Ops(cfg, run=rec, etc=etc).do("cert_install", {})
    assert rec.calls == [] and not (etc / "nexwar" / "tls" / "robot.crt").exists()


def _release(tmp: Path, unit_text: str) -> tuple[Path, Path]:
    keys = tmp / "keys"
    relsign.keygen(keys / "priv.pem", keys / "pub.pem")
    name = "2026-10-31-abcdef12"
    root = tmp / "opt" / "releases" / name
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "nexwar_localizer.service").write_text(unit_text)
    (root / "src").mkdir()
    (root / "src" / "x.py").write_text("print(1)\n")
    (root / "venv").mkdir()                       # 装好之后才长出来的,不进指纹
    (root / "venv" / "junk").write_text("x")
    from nexwar_agent.engine.release import _packed
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
    assert got == {"changed": ["nexwar_localizer.service"], "bind": False}
    assert (sysd / "nexwar_localizer.service").read_text() == unit
    assert rec.calls == [["systemctl", "daemon-reload"]]
    rec.calls.clear()
    assert ops.do("units_apply", {"release": root.name}) == {"changed": [], "bind": False}
    assert rec.calls == []


def test_装服务单元_包被改过拒(tmp_path):
    cfg = _cfg(tmp_path)
    root, _ = _release(tmp_path, "[Unit]\nDescription=new\n")
    evil = "[Service]\nExecStart=/bin/sh -c evil\n"
    (root / "deploy" / "nexwar_localizer.service").write_text(evil)
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    with pytest.raises(R.OpError, match="不匹配"):
        R.Ops(cfg, run=Rec(), systemd_dir=sysd).do("units_apply", {"release": root.name})
    assert not (sysd / "nexwar_localizer.service").exists()


def test_装服务单元_没有公钥_版本名不对_都拒(tmp_path):
    cfg = _cfg(tmp_path)
    ops = R.Ops(cfg, run=Rec())
    with pytest.raises(R.OpError, match="版本名"):
        ops.do("units_apply", {"release": "../../etc"})
    with pytest.raises(R.OpError, match="公钥"):
        ops.do("units_apply", {"release": "2026-10-31-abcdef12"})


def test_打包规则两边一样():
    from nexwar_agent.engine.release import _packed
    for rel in ("src/a.py", "venv/bin/python", "packages/x/build/lib/a.py", "build/a",
                "packages/x/src/a.py", "motion/build/patrol_agent", "motion/patrol_agent.cpp",
                "a/__pycache__/b.pyc", "x.egg-info/PKG-INFO", "packages/x/tests/t.py",
                "deploy/nexwar_agent.service", "dist/x.whl"):
        assert R.packed(rel) == _packed(rel), rel


def test_站点动作_机器人上不认(tmp_path):
    ops = R.Ops(_cfg(tmp_path), run=Rec())
    with pytest.raises(R.OpError, match="不认识"):
        ops.do("update", {"package": "x.tar.gz"})
    site = R.Ops(_cfg(tmp_path / "s", "site"), run=Rec())
    with pytest.raises(R.OpError, match="白名单"):
        site.do("restart", {"unit": "nexwar_agent"})


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
    got = c.call("restart", {"unit": "nexwar_persons"})
    assert got["ok"] and got["unit"] == "nexwar_persons"
    lazy = SpoolOps(cfg["spool"], sleep=lambda s: t.__setitem__(0, t[0] + s),
                    monotonic=lambda: t[0])
    from nexwar_contract.privops import PrivOpsError
    with pytest.raises(PrivOpsError, match="未处理,请求已撤回"):
        lazy.call("timesync", timeout_s=1)
    assert not list((Path(cfg["spool"]) / "req").glob("*.json")), "撤回了:执行器再也拿不到"


def test_外审W55_2_执行器拿走了请求但没回结果_算结果未知(tmp_path):
    """C3:超时以后先撤回;撤不回(执行器已经改名认领)就是结果未知,不许当没执行。"""
    from nexwar_contract.privops import PrivTimeout
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
    rid = c.submit("restart", {"unit": "nexwar_persons"})
    assert c.cancel(rid)
    rec = Rec()
    assert R.process(cfg, R.Ops(cfg, run=rec), uid=ME.pw_uid, gid=ME.pw_gid) == 0
    assert rec.calls == []


def test_模拟版_记下调用():
    s = SimOps(fail={"reboot": "忙"})
    assert s.call("restart", {"unit": "nexwar_agent"})["ok"]
    assert s.call("reboot")["ok"] is False
    assert s.calls == [("restart", {"unit": "nexwar_agent"}), ("reboot", {})]


def test_建图时暂停_只许停合并雷达和定位器_恢复是启动_工单21(tmp_path):
    cfg = _cfg(tmp_path)
    rec = Rec()
    ops = R.Ops(cfg, run=rec)
    assert ops.do("pause", {"unit": "nexwar_lidar_merge"}) == {"unit": "nexwar_lidar_merge",
                                                              "stopped": True}
    assert ops.do("resume", {"unit": "nexwar_localizer"}) == {"unit": "nexwar_localizer"}
    assert rec.calls == [["systemctl", "is-active", "--quiet", "nexwar_lidar_merge.service"],
                         ["systemctl", "stop", "nexwar_lidar_merge.service"],
                         ["systemctl", "start", "nexwar_localizer.service"]]
    for u in ("nexwar_agent", "nexwar_obstacles", "ssh", "../x"):
        with pytest.raises(R.OpError):
            ops.do("pause", {"unit": u})
    assert len(rec.calls) == 3


def test_外审_建图前没在跑的不停_回告诉代理没停(tmp_path):
    rec = Rec({"systemctl is-active --quiet": 3})
    out = R.Ops(_cfg(tmp_path), run=rec).do("pause", {"unit": "nexwar_localizer"})
    assert out == {"unit": "nexwar_localizer", "stopped": False}
    assert rec.calls == [["systemctl", "is-active", "--quiet", "nexwar_localizer.service"]]


class _钟Rec(Rec):
    """``chronyc tracking`` 按 ``leap`` 回;别的照 Rec。"""

    def __init__(self, leap: str) -> None:
        super().__init__()
        self.leap = leap

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[:3] == ["chronyc", "-n", "tracking"]:
            return 0, f"Reference ID    : 00000000 ()\nLeap status     : {self.leap}\n"
        return 0, ""


def test_W70_对时带站点钟差_NTP没对上_按站点的钟拨(tmp_path, monkeypatch):
    """2026-11-03 C40221:站点在笔记本上、经隧道接入,机器人上不了外网,chrony 没源,钟慢 28 s。"""
    import time as _t
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/" + n if n == "chronyc" else None)
    rec = _钟Rec("Not synchronised")
    got = R.Ops(_cfg(tmp_path), run=rec).timesync({"offset_ms": 28_000})
    assert got["stepped"] is True and got["offset_ms"] == 28_000
    date = [c for c in rec.calls if c[0] == "date"]
    assert len(date) == 1 and date[0][:3] == ["date", "-u", "-s"]
    assert abs(float(date[0][3].lstrip("@")) - (_t.time() + 28.0)) < 5


def test_W70_NTP对上了_不按站点的钟拨(tmp_path, monkeypatch):
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/" + n if n == "chronyc" else None)
    rec = _钟Rec("Normal")
    got = R.Ops(_cfg(tmp_path), run=rec).timesync({"offset_ms": 28_000})
    assert got["stepped"] is False and not [c for c in rec.calls if c[0] == "date"]


def test_W70_差得少不拨_参数不对拒(tmp_path, monkeypatch):
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/" + n if n == "chronyc" else None)
    rec = _钟Rec("Not synchronised")
    ops = R.Ops(_cfg(tmp_path), run=rec)
    assert ops.timesync({"offset_ms": 300})["stepped"] is False
    for bad in ("28000", True, 10**13, 1.5):
        with pytest.raises(R.OpError):
            ops.timesync({"offset_ms": bad})
    assert not [c for c in rec.calls if c[0] == "date"]


def test_W70_断电再开是1970年_往前拨几十年照拨(tmp_path, monkeypatch):
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/" + n if n == "chronyc" else None)
    monkeypatch.setattr(R.time, "time", lambda: 86_400.0)          # 1970-01-02
    rec = _钟Rec("Not synchronised")
    off = (1_793_000_000 - 86_400) * 1000
    ops = R.Ops(_cfg(tmp_path), run=rec)
    assert ops.timesync({"offset_ms": off})["stepped"] is True
    with pytest.raises(R.OpError):
        ops.timesync({"offset_ms": -off})


def test_W70_chrony停着_makestep失败_照样按站点的钟拨(tmp_path, monkeypatch):
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/" + n if n == "chronyc" else None)

    class 停着(_钟Rec):
        def __call__(self, argv, timeout):
            self.calls.append(argv)
            return (1, "506 Cannot talk to daemon") if argv[0] == "chronyc" else (0, "")
    rec = 停着("")
    got = R.Ops(_cfg(tmp_path), run=rec).timesync({"offset_ms": -28_000})
    assert got["stepped"] is True and [c for c in rec.calls if c[0] == "date"]


def test_W70_站点来对时_不先makestep把钟拽回厂家板(tmp_path, monkeypatch):
    """11-03 C40221:chrony 跟厂家板(慢 28 s)对时,每次站点对时先 makestep 拽回去、再拨回来,
    一分钟拉锯一次。"""
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/" + n if n == "chronyc" else None)
    rec = _钟Rec("Not synchronised")
    got = R.Ops(_cfg(tmp_path), run=rec).timesync({"offset_ms": 28_000})
    assert got["stepped"] is True
    assert not [c for c in rec.calls if c[:3] == ["chronyc", "-a", "makestep"]]


def test_W70_NTP说对上了可跟站点差很多_不拨_回报打架(tmp_path, monkeypatch):
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/" + n if n == "chronyc" else None)
    rec = _钟Rec("Normal")
    ops = R.Ops(_cfg(tmp_path), run=rec)
    got = ops.timesync({"offset_ms": 28_000})
    assert got["stepped"] is False and got.get("conflict") is True and "厂家板" in got["why"]
    got = ops.timesync({"offset_ms": 3_000})
    assert got["stepped"] is False and "conflict" not in got
