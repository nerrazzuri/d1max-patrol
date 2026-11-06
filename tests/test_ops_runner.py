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


# ---------------------------------------------------------------- 不靠 SSH(11-06,用户定)


def _env(tmp: Path, text: str) -> Path:
    p = tmp / "etc" / "nexwar" / "env"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    p.chmod(0o640)
    return p


def test_不靠SSH_改设置_只认白名单的键和值_别的行原样_PIN不出现在回执里(tmp_path):
    cfg = _cfg(tmp_path)
    env = _env(tmp_path, "NEXWAR_PIN=123456\n# 注释\nNEXWAR_PERSONS=none\nNEXWAR_PERSONS=none\n"
                         "NEXWAR_SIDECAR_ARGS=--x 1\n")
    rec = Rec()
    ops = R.Ops(cfg, run=rec, etc=tmp_path / "etc")
    out = ops.do("env_set", {"changes": {"NEXWAR_PERSONS": "bridge", "NEXWAR_MAX_FRACTION": "0.7"}})
    text = env.read_text()
    assert text == ("NEXWAR_PIN=123456\n# 注释\nNEXWAR_PERSONS=bridge\n"
                    "NEXWAR_SIDECAR_ARGS=--x 1\nNEXWAR_MAX_FRACTION=0.7\n")
    assert (env.stat().st_mode & 0o777) == 0o640
    assert "123456" not in json.dumps(out)
    assert ["systemctl", "enable", "--now", "nexwar_persons.service"] in rec.calls
    assert ["systemctl", "try-restart", "nexwar_sidecar.service"] in rec.calls
    assert rec.calls[-1] == ["systemctl", "--no-block", "restart", "nexwar_agent.service"], \
        "代理最后、不等(它是请求方)"
    for bad in ({"NEXWAR_PIN": "1"}, {"NEXWAR_PERSONS": "bridge\nNEXWAR_PIN=0"},
                {"NEXWAR_MAX_FRACTION": "2.0"}, {}, {"NEXWAR_OBS_PERMIT": 1}):
        with pytest.raises(R.OpError):
            ops.do("env_set", {"changes": bad})
    assert env.read_text() == text, "被拒的一个字都不改"


def test_不靠SSH_关人员检测_停它的服务_开障碍许可重启感知和旁路(tmp_path):
    cfg = _cfg(tmp_path)
    env = _env(tmp_path, "NEXWAR_PERSONS=bridge\n")
    rec = Rec()
    ops = R.Ops(cfg, run=rec, etc=tmp_path / "etc")
    ops.do("env_set", {"changes": {"NEXWAR_PERSONS": "none", "NEXWAR_OBS_PERMIT": "1"}})
    assert env.read_text() == "NEXWAR_PERSONS=none\nNEXWAR_OBS_PERMIT=1\n"
    assert ["systemctl", "disable", "--now", "nexwar_persons.service"] in rec.calls
    assert ["systemctl", "try-restart", "nexwar_obstacles.service"] in rec.calls
    assert ["systemctl", "try-restart", "nexwar_sidecar.service"] in rec.calls


def test_不靠SSH_env是符号链接_不改(tmp_path):
    cfg = _cfg(tmp_path)
    real = tmp_path / "real-env"
    real.write_text("NEXWAR_PERSONS=none\n")
    (tmp_path / "etc" / "nexwar").mkdir(parents=True)
    (tmp_path / "etc" / "nexwar" / "env").symlink_to(real)
    with pytest.raises(R.OpError):
        R.Ops(cfg, run=Rec(), etc=tmp_path / "etc").do(
            "env_set", {"changes": {"NEXWAR_PERSONS": "bridge"}})
    assert real.read_text() == "NEXWAR_PERSONS=none\n"


def _proc(tmp: Path, procs: dict[int, tuple[int, str]]) -> Path:
    root = tmp / "proc"
    for pid, (ppid, cmd) in procs.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "stat").write_text(f"{pid} (x y) S {ppid} 1 1 0")
    (root / "self").mkdir()
    return root


def test_不靠SSH_暂停厂家进程_只按代码里的单子_连子进程_我们的不碰_恢复发CONT(tmp_path):
    import signal
    procs = {
        100: (1, "/usr/bin/python3 /opt/ros/humble/bin/ros2 launch robot_nav2 "
                 "navigation_bringup.launch.py"),
        101: (100, "/opt/ros/humble/lib/rclcpp_components/component_container_isolated --x"),
        200: (1, "/opt/robot/robot_slam/install/robot_slam/lib/robot_slam/robot_slam"),
        300: (1, "/opt/robot/robot-driver/install/image_decode/lib/image_decode/image_decode_node"),
        400: (1, "/usr/bin/python3 -m nexwar_localizer.persons"),
        401: (1, "/opt/ros/humble/lib/nav2_controller/controller_server --ros-args"),
        500: (1, "./bin/apriltag_localization_node ./config/x.yaml"),
    }
    killed: list = []
    ops = R.Ops(_cfg(tmp_path), run=Rec(), proc=_proc(tmp_path, procs),
                kill=lambda pid, sig: killed.append((pid, sig)))
    out = ops.do("vendor_pause", {})
    assert sorted(p for p, _ in killed) == [100, 101, 200, 500]
    assert {s for _, s in killed} == {signal.SIGSTOP}
    assert sorted(x["pid"] for x in out["paused"]) == [100, 101, 200, 500]
    assert all(len(x["cmd"]) <= 120 for x in out["paused"])
    killed.clear()
    ops.do("vendor_resume", {})
    assert sorted(p for p, _ in killed) == [100, 101, 200, 500]
    assert {s for _, s in killed} == {signal.SIGCONT}
    with pytest.raises(R.OpError):
        ops.do("vendor_pause", {"pids": [400]})


def test_不靠SSH_开机加固定路由_按env里写的_只认网段via网关(tmp_path):
    """第 25 条:视频走的 192.168.234.0/24 经 192.168.168.168,以前每次开机 SSH 手工加。"""
    cfg = _cfg(tmp_path)
    _env(tmp_path, "NEXWAR_PIN=123456\n"
                   "NEXWAR_ROUTES=192.168.234.0/24 via 192.168.168.168;"
                   " 10.8.0.0/16 via 192.168.168.1\n")
    rec = Rec()
    out = R.Ops(cfg, run=rec, etc=tmp_path / "etc").do("routes_apply", {})
    assert ["ip", "route", "replace", "192.168.234.0/24", "via", "192.168.168.168"] in rec.calls
    assert ["ip", "route", "replace", "10.8.0.0/16", "via", "192.168.168.1"] in rec.calls
    assert out["applied"] == ["192.168.234.0/24 via 192.168.168.168",
                              "10.8.0.0/16 via 192.168.168.1"]
    assert out["failed"] == []
    assert "123456" not in json.dumps(out)


def test_不靠SSH_固定路由_没写就什么都不做_写错了整条不加_不许改默认路由(tmp_path):
    cfg = _cfg(tmp_path)
    _env(tmp_path, "NEXWAR_PIN=1\n")
    rec = Rec()
    assert R.Ops(cfg, run=rec, etc=tmp_path / "etc").do("routes_apply", {}) == \
        {"applied": [], "failed": []}
    assert rec.calls == []
    for bad in ("0.0.0.0/0 via 192.168.168.1", "default via 192.168.168.1",
                "192.168.234.0/24 via 192.168.168.168 dev eth0", "192.168.234.0/24 via x;rm",
                "192.168.234.0/33 via 1.2.3.4", "fe80::/64 via fe80::1"):
        _env(tmp_path, f"NEXWAR_ROUTES={bad}\n")
        rec = Rec()
        with pytest.raises(R.OpError):
            R.Ops(cfg, run=rec, etc=tmp_path / "etc").do("routes_apply", {})
        assert rec.calls == [], bad
    with pytest.raises(R.OpError):
        R.Ops(cfg, run=Rec(), etc=tmp_path / "etc").do("routes_apply", {"routes": "x"})


def test_不靠SSH_固定路由_网关一时不通_记成没加上_别的照加(tmp_path):
    cfg = _cfg(tmp_path)
    _env(tmp_path, "NEXWAR_ROUTES=192.168.234.0/24 via 192.168.168.168,10.8.0.0/16 via 10.9.0.1\n")

    def run(argv, timeout=120.0):
        return (2, "Nexthop has invalid gateway") if "10.9.0.1" in argv else (0, "")
    out = R.Ops(cfg, run=run, etc=tmp_path / "etc").do("routes_apply", {})
    assert out["applied"] == ["192.168.234.0/24 via 192.168.168.168"]
    assert out["failed"] == [{"route": "10.8.0.0/16 via 10.9.0.1",
                              "error": "Nexthop has invalid gateway"}]


def _proc_cg(tmp: Path, procs: dict[int, tuple[int, str, str]]) -> Path:
    """``pid → (父 pid, 命令行, 所在的 systemd 单元)``。"""
    (tmp / "proc").mkdir(exist_ok=True)
    root = _proc(tmp, {p: (pp, cmd) for p, (pp, cmd, _u) in procs.items()})
    for p, (_pp, _cmd, unit) in procs.items():
        (root / str(p) / "cgroup").write_text(f"0::/system.slice/{unit}\n")
    return root


class Sys(Rec):
    """假 systemctl:``is-enabled`` 按 ``enabled`` 回。"""

    def __init__(self, enabled=()):
        super().__init__()
        self.enabled = set(enabled)

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "is-enabled"]:
            return (0, "enabled\n") if argv[2] in self.enabled else (1, "disabled\n")
        return 0, ""


def test_第32条_厂家用不上的服务_关自启_只关整个单元都是厂家那几样的_记下来能开回去(tmp_path):
    cfg = _cfg(tmp_path)
    proc = _proc_cg(tmp_path, {
        10: (1, "/opt/robot/robot_slam/install/robot_slam/lib/robot_slam/robot_slam",
             "robot_slam.service"),
        11: (1, "/usr/bin/python3 /opt/ros/humble/bin/ros2 launch robot_nav2 "
                "navigation_bringup.launch.py",
             "robot_nav2.service"),
        12: (11, "/opt/ros/humble/lib/nav2_controller/controller_server", "robot_nav2.service"),
        20: (1, "/opt/robot/perception/install/perception/bin/perception_jobs /x",
             "robot_perception.service"),
        21: (1, "/opt/robot/robot-driver/install/image_decode/lib/image_decode/image_decode_node",
             "robot_perception.service"),
        30: (1, "/usr/bin/python3 -m nexwar_agent.main", "nexwar_agent.service"),
    })
    sysd = Sys(enabled={"robot_slam.service", "robot_nav2.service", "robot_perception.service"})
    ops = R.Ops(cfg, run=sysd, etc=tmp_path / "etc", proc=proc)
    out = ops.do("vendor_autostart", {"on": False})
    assert out["disabled"] == ["robot_nav2.service", "robot_slam.service"]
    assert ["systemctl", "disable", "--now", "robot_slam.service"] in sysd.calls
    assert not any("robot_perception" in " ".join(c) and "disable" in c for c in sysd.calls), \
        "图像解码跟它同一个单元:关了人员检测就没画面"
    assert {"unit": "robot_perception.service", "why": "shared"} in out["skipped"]
    assert not any("nexwar_agent" in " ".join(c) for c in sysd.calls)
    rec = json.loads((tmp_path / "data" / "vendor-autostart-off.json").read_text())
    assert rec["units"] == ["robot_nav2.service", "robot_slam.service"]
    # 再跑一遍(下次开机):已经关了的不重复记,记录不丢
    sysd2 = Sys(enabled=set())
    R.Ops(cfg, run=sysd2, etc=tmp_path / "etc", proc=proc).do("vendor_autostart", {"on": False})
    assert json.loads((tmp_path / "data" / "vendor-autostart-off.json").read_text())["units"] == \
        ["robot_nav2.service", "robot_slam.service"]
    # 开回去:按记录开,记录删掉
    sysd3 = Sys()
    out = R.Ops(cfg, run=sysd3, etc=tmp_path / "etc", proc=proc).do("vendor_autostart",
                                                                     {"on": True})
    assert out["enabled"] == ["robot_nav2.service", "robot_slam.service"]
    assert ["systemctl", "enable", "--now", "robot_slam.service"] in sysd3.calls
    assert not (tmp_path / "data" / "vendor-autostart-off.json").exists()
    for bad in ({}, {"on": "yes"}, {"on": False, "units": ["ssh.service"]}):
        with pytest.raises(R.OpError):
            ops.do("vendor_autostart", bad)


def test_第4条_向站点对时_只在chrony运行时加一个运行时的源_不改厂家的配置文件(tmp_path):
    cfg = _cfg(tmp_path)
    _env(tmp_path, "NEXWAR_SITE_MQTT=mqtts://192.168.234.10:8883\n")
    rec = Rec()
    out = R.Ops(cfg, run=rec, etc=tmp_path / "etc").do("chrony_site", {})
    assert out == {"chrony": "added", "host": "192.168.234.10"}
    assert ["chronyc", "add", "server", "192.168.234.10", "iburst", "prefer", "trust"] in rec.calls
    assert not any(c[:2] == ["systemctl", "restart"] for c in rec.calls)

    def already(argv, timeout):
        return (1, "511 Source already present\n") if argv[0] == "chronyc" else (0, "")
    got = R.Ops(cfg, run=already, etc=tmp_path / "etc").do("chrony_site", {})
    assert got["chrony"] == "present"

    def off(argv, timeout):
        return (3, "inactive\n") if argv[:2] == ["systemctl", "is-active"] else (0, "")
    assert R.Ops(cfg, run=off, etc=tmp_path / "etc").do("chrony_site", {}) == {"chrony": "inactive"}
    _env(tmp_path, "NEXWAR_SITE_MQTT=mqtts://127.0.0.1:8883\n")
    rec = Rec()
    assert R.Ops(cfg, run=rec, etc=tmp_path / "etc").do("chrony_site", {}) == {"chrony": "no_site"}
    assert not any(c[0] == "chronyc" for c in rec.calls), "经隧道的回环地址对时过不去"
    _env(tmp_path, "NEXWAR_SITE_NTP=site.lan;reboot\n")
    with pytest.raises(R.OpError):
        R.Ops(cfg, run=Rec(), etc=tmp_path / "etc").do("chrony_site", {})


def test_开机准备_路由_对时_厂家自启_一起做_没齐每分钟再来_最多十回(tmp_path):
    cfg = _cfg(tmp_path)
    _env(tmp_path, "NEXWAR_ROUTES=192.168.234.0/24 via 192.168.168.168\n"
                   "NEXWAR_SITE_NTP=192.168.234.10\n")
    slept: list[float] = []
    tries = {"n": 0}

    def run(argv, timeout):
        if argv[:2] == ["ip", "route"]:
            tries["n"] += 1
            return (2, "Network is unreachable") if tries["n"] < 3 else (0, "")
        return 0, ""
    proc = _proc_cg(tmp_path, {})
    _env(tmp_path, "NEXWAR_ROUTES=192.168.234.0/24 via 192.168.168.168\n"
                   "NEXWAR_SITE_NTP=192.168.234.10\nNEXWAR_VENDOR_AUTOSTART=1\n")
    R.boot(R.Ops(cfg, run=run, etc=tmp_path / "etc", proc=proc), sleep=slept.append)
    assert tries["n"] == 3 and slept == [60, 60], "路由、对时齐了;厂家自启开着:不用再看"
    # 厂家自启关着(缺省):厂家的进程起得晚,十分钟里每分钟看一回;路由加上了就不再加
    _env(tmp_path, "NEXWAR_ROUTES=192.168.234.0/24 via 192.168.168.168\n"
                   "NEXWAR_SITE_NTP=192.168.234.10\n")
    tries["n"] = 0
    slept.clear()
    R.boot(R.Ops(cfg, run=run, etc=tmp_path / "etc", proc=proc), sleep=slept.append)
    assert tries["n"] == 3 and len(slept) == 9
    # 一直不通:十回后放弃,不卡开机
    tries["n"] = -100
    slept.clear()
    R.boot(R.Ops(cfg, run=run, etc=tmp_path / "etc", proc=proc), sleep=slept.append)
    assert len(slept) == 9 and tries["n"] == -90
