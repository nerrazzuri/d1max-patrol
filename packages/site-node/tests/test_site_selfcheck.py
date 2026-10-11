"""站点装完自检(RC 加固 H6):装对了全 PASS;密钥缺、服务没起、探活不通、证书过期是 FAIL;
没管理员、没备份、没推送、读不到日志是 WARN。探针都是注入的:不用 root、不用真起服务。"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from d1max_site import main as site_main
from d1max_site import selfcheck as sc

GOOD_RUN = {
    ("id",): (0, "d1max-site systemd-journal"),
    ("systemctl", "is-active"): (0, "active"),
    ("systemctl", "is-enabled"): (0, "enabled"),
    ("openssl",): (0, ""),
}


def _runner(over=None):
    table = dict(GOOD_RUN) | (over or {})

    def run(cmd):
        for n in (3, 2, 1):
            if tuple(cmd[:n]) in table:
                return table[tuple(cmd[:n])]
        return (127, "")
    return run


@pytest.fixture
def 装好的(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "estate-1",
                           "--hostname", "localhost"]) == 0
    monkeypatch.setenv("D1MAX_SITE_PASSWORD", "correct-horse-battery")
    assert site_main.main(["--home", str(home), "add-admin", "root"]) == 0
    keys = tmp_path / "keys"
    keys.mkdir()
    for name in ("secrets.key", "backup.key", "evidence.key"):
        (keys / name).write_bytes(os.urandom(32))
        (keys / name).chmod(0o600)
    (tmp_path / "bak").mkdir()
    (tmp_path / "release-pub.pem").write_text("pem", "utf-8")
    cfg = json.loads((home / "site.json").read_text())
    cfg |= {"backup_dir": str(tmp_path / "bak"), "push": {"fcm": {}}}
    return home, cfg, keys, tmp_path


def _check(装好的, *, cfg_over=None, run=None, tcp=lambda h, p: "",
           healthz=lambda port, ca: (200, {"ok": True, "version": "1.0"}), keys=None, pub=None,
           free=0.5):
    home, cfg, keyroot, tmp = 装好的
    return sc.selfcheck(home, cfg | (cfg_over or {}), run=run or _runner(), tcp=tcp,
                        healthz=healthz, keys_root=keys or keyroot,
                        release_pub=pub or tmp / "release-pub.pem", disk_free=lambda p: free)


def _by(checks):
    return {c.name: c for c in checks}


def test_装对了_没有FAIL没有WARN(装好的):
    checks = _check(装好的)
    assert [c for c in checks if c.level != "PASS"] == [], sc.render(checks)
    assert sc.render(checks).endswith("FAIL 0 条,WARN 0 条:能用了")
    names = set(_by(checks))
    assert {"站点身份", "站点证书", "管理员", "备份", "推送", "读日志", "狗连的端口", "探活",
            "对时服务", "盘", "服务 d1max-site", "服务 d1max-mosquitto", "发行公钥"} <= names


def test_密钥缺了_长度不对_FAIL(装好的):
    home, cfg, keys, tmp = 装好的
    (keys / "backup.key").unlink()
    (keys / "secrets.key").write_bytes(b"short")
    by = _by(_check(装好的))
    assert by["密钥 backup"].level == "FAIL" and by["密钥 secrets"].level == "FAIL"
    assert by["密钥 evidence"].level == "PASS"


def test_服务没起是FAIL_没设自启是WARN(装好的):
    down = _runner({("systemctl", "is-active", "d1max-site"): (3, "inactive")})
    by = _by(_check(装好的, run=down))
    assert by["服务 d1max-site"].level == "FAIL"
    assert "systemctl start" in by["服务 d1max-site"].detail
    assert by["服务 d1max-mosquitto"].level == "PASS"
    by = _by(_check(装好的, run=_runner({("systemctl", "is-enabled"): (1, "disabled")})))
    assert by["服务 d1max-site"].level == "WARN"


def test_探活_不通_回503_端口没开_都是FAIL(装好的):
    by = _by(_check(装好的, healthz=lambda p, ca: (0, "Connection refused")))
    assert by["探活"].level == "FAIL" and "不通" in by["探活"].detail
    by = _by(_check(装好的, healthz=lambda p, ca: (503, {"ok": False})))
    assert by["探活"].level == "FAIL" and "体检项没过" in by["探活"].detail
    by = _by(_check(装好的, healthz=lambda p, ca: (200, {"ok": False})))
    assert by["探活"].level == "FAIL"
    by = _by(_check(装好的, tcp=lambda h, p: "refused"))
    assert by["狗连的端口"].level == "FAIL" and "8883" in by["狗连的端口"].detail


def test_证书_过期FAIL_快到期WARN_没有FAIL(装好的):
    home, *_ = 装好的

    def expired(cmd):
        return (1, "") if cmd[0] == "openssl" else _runner()(cmd)

    def soon(cmd):
        if cmd[0] == "openssl":
            return (0, "") if cmd[-1] == "0" else (1, "")
        return _runner()(cmd)
    assert _by(_check(装好的, run=expired))["站点证书"].level == "FAIL"
    assert _by(_check(装好的, run=soon))["站点证书"].level == "WARN"
    (home / "ca" / "server" / "server.crt").unlink()
    assert _by(_check(装好的))["站点证书"].level == "FAIL"


def test_少一块的是WARN_没管理员_没备份_没推送_读不到日志_没发行公钥_对时没起(装好的, tmp_path):
    home, cfg, keys, tmp = 装好的
    import sqlite3
    con = sqlite3.connect(home / "site.db")
    con.execute("UPDATE accounts SET disabled=1")
    con.commit()
    con.close()
    checks = _check(装好的, cfg_over={"backup_dir": None, "push": None}, pub=tmp / "无",
                    run=_runner({("id",): (0, "d1max-site"),
                                 ("systemctl", "is-active", "chrony"): (3, "inactive")}))
    by = _by(checks)
    for name in ("管理员", "备份", "推送", "读日志", "发行公钥", "对时服务"):
        assert by[name].level == "WARN", name
    assert not [c for c in checks if c.level == "FAIL"]
    assert "WARN 6 条:能用了,WARN 的看一眼" in sc.render(checks)


def test_备份目录配了但不在_没有站点用户_库打不开_是FAIL(装好的, tmp_path):
    home, *_ = 装好的
    by = _by(_check(装好的, cfg_over={"backup_dir": str(tmp_path / "没挂上")},
                    run=_runner({("id",): (1, "no such user")})))
    assert by["备份"].level == "FAIL" and by["站点用户"].level == "FAIL"
    shutil.move(home / "site.db", home / "site.db.moved")
    assert _by(_check(装好的))["站点库"].level == "FAIL"


def test_盘快满了是WARN_量不了也是WARN(装好的):
    assert _by(_check(装好的, free=0.14))["盘"].level == "WARN"
    assert _by(_check(装好的, free=0.15))["盘"].level == "PASS"
    home, cfg, keys, tmp = 装好的

    def boom(p):
        raise OSError("gone")
    by = _by(sc.selfcheck(home, cfg, run=_runner(), tcp=lambda h, p: "", keys_root=keys,
                          healthz=lambda port, ca: (200, {"ok": True}), disk_free=boom,
                          release_pub=tmp / "release-pub.pem"))
    assert by["盘"].level == "WARN" and "量不了" in by["盘"].detail


def test_命令行_有FAIL退出码1_这台开发机上服务没装所以一定有(装好的, capsys):
    home, *_ = 装好的
    assert site_main.main(["--home", str(home), "selfcheck", "--api-port", "1"]) == 1
    shown = capsys.readouterr().out
    assert "[FAIL] 探活" in shown and "[PASS] 站点身份: estate-1" in shown


def test_装机脚本的收尾提示_叫人跑自检_用开通码_导托管包():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[3] / "deploy/site/install-site.sh").read_text("utf-8")
    assert text.count("selfcheck") >= 2, "正常装完、恢复模式装完都要提"
    assert "enroll <robot_id> --code" in text and "keys-export" in text
    assert "docs/备份恢复与灾备演练.md" in text and "W30-完工报告" not in text.split("装好了。")[1]
