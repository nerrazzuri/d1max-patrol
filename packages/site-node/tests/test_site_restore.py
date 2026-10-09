"""备份校验、恢复、灾备演练(商业化 A2,决策 53):备份里有站点身份(site.json、CA)、能校验、能一条命令
恢复成一模一样的站点(证书指纹不变、狗不用重登记)、改过的文件查得出、每周自动抽查、演练记一笔。"""

from __future__ import annotations

import json
import os

import pytest

from d1max_site import main as site_main
from d1max_site.backup import SEALED, SiteBackup
from d1max_site.db import SiteDB
from d1max_site.evidence import EvidenceStore
from d1max_site.restore import drill, restore, verify, verify_home
from d1max_site.sealbox import SealBox

NOW = 1_800_000_000_000
RUN = "巡检一/20261009T010000Z"


@pytest.fixture
def 站(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: (tmp_path).stat().st_uid)
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "estate-1",
                           "--hostname", "localhost"]) == 0
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 0
    key = tmp_path / "keys" / "backup.key"
    key.parent.mkdir()
    key.write_bytes(os.urandom(32))
    dest = tmp_path / "bak"
    cfg = json.loads((home / "site.json").read_text())
    cfg["backup_dir"], cfg["backup_key"] = str(dest), str(key)
    (home / "site.json").write_text(json.dumps(cfg))
    db = SiteDB(home / "site.db")
    store = EvidenceStore(home / "evidence", db, now_ms=lambda: NOW)
    for rel, data in (("events.jsonl", b"{}\n"), ("photos/P1__front__20261009T010000Z.jpg",
                                                  b"PHOTO")):
        store.put("A", RUN, rel, offset=0, data=data, total=len(data))
    b = SiteBackup(db, store.root, dest, now_ms=lambda: NOW, box=SealBox(key.read_bytes()),
                   more={"maps": home / "maps", "ca": home / "ca", "broker": home / "broker"},
                   files={"site.json": home / "site.json"})
    assert b.run_once(), b.last_error
    yield home, dest, key, db, b
    db.close()


def test_备份里有站点身份_全都加密(站):
    home, dest, key, db, b = 站
    for need in ("config/site.json", "ca/ca.key", "ca/server/server.key", "ca/server/server.crt"):
        assert (dest / (need + SEALED)).is_file(), need
    assert not [p for p in dest.rglob("*") if p.is_file() and not p.name.endswith(SEALED)]
    assert b"BEGIN" not in (dest / ("ca/ca.key" + SEALED)).read_bytes()


def test_校验_全核_过(站):
    home, dest, key, db, b = 站
    r = verify(dest, key)
    assert r.ok, r.problems
    assert r.counts["runs"] == 1 and r.counts["robots"] == 1 and r.checked_files >= 6


def test_校验_改过一个字节_没给密钥_缺了CA_缺了一趟_都查得出(站, tmp_path):
    home, dest, key, db, b = 站
    p = next(dest.joinpath("evidence").rglob("*.jpg" + SEALED))
    raw = bytearray(p.read_bytes())
    raw[20] ^= 1
    p.write_bytes(bytes(raw))
    r = verify(dest, key)
    assert not r.ok and any("对不上" in x for x in r.problems)
    assert not verify(dest, None).ok, "加密的没给密钥:不算过"
    other = tmp_path / "other.key"
    other.write_bytes(os.urandom(32))
    assert not verify(dest, other).ok, "不是这把密钥"
    (dest / ("ca/ca.key" + SEALED)).unlink()
    import shutil
    shutil.rmtree(dest / "evidence" / "A")
    probs = ";".join(verify(dest, key).problems)
    assert "ca/ca.key" in probs and "没有目录" in probs


def test_恢复_一条命令_证书指纹不变_库完整_证据都在(站, tmp_path, capsys):
    home, dest, key, db, b = 站
    fp_before = site_main.main(["--home", str(home), "fingerprint"])
    before = capsys.readouterr().out
    new = tmp_path / "new-site"
    assert site_main.main(["--home", str(new), "restore", str(dest), "--key", str(key)]) == 0
    capsys.readouterr()
    assert verify_home(new).ok
    assert site_main.main(["--home", str(new), "fingerprint"]) == fp_before
    assert capsys.readouterr().out == before, "同一张站点证书:狗不用重登记、手机不用重加"
    photo = new / "evidence" / "A" / "巡检一" / "20261009T010000Z" / "photos" / \
        "P1__front__20261009T010000Z.jpg"
    assert photo.read_bytes() == b"PHOTO"
    assert oct((new / "ca" / "ca.key").stat().st_mode)[-3:] == "600"


def test_恢复_只往空目录里放(站, tmp_path):
    home, dest, key, db, b = 站
    with pytest.raises(Exception, match="不是空的"):
        restore(dest, key, home)


def test_演练_恢复到临时目录_校验_对数_删掉_记一笔(站):
    home, dest, key, db, b = 站
    got = site_main.cmd_backup_drill(home, str(key))
    assert got["ok"] and got["counts"]["runs"] == got["live"]["runs"] == 1, got
    assert json.loads(db.query("SELECT value FROM meta WHERE key='backup_drill'")[0]["value"])["ok"]
    assert b.status()["drill"]["ok"]
    bad = drill(home / "site.db", dest, None, now_ms=lambda: NOW)
    assert not bad["ok"]


def test_每周自动抽查_没过报P2_修好了自动解决(站):
    from test_site_charging import 假告警台
    home, dest, key, db, b = 站
    b.alerts = 假告警台()
    resolved = []
    b.alerts.resolve_all = lambda *a, **kw: resolved.append(a)
    (dest / ("config/site.json" + SEALED)).unlink()
    rep = b.verify_step(key)
    assert not rep["ok"] and b.alerts.raised[0]["kind"] == "backup_verify_failed"
    assert b.verify_step(key) is None, "一周一次"
    b._now = lambda: NOW + 8 * 86_400_000
    assert b.run_once()                                   # 下一次备份把 site.json 补上
    assert b.verify_step(key)["ok"] and resolved
    assert b.status()["verify"]["ok"]


def test_没加密的备份_不放CA私钥(tmp_path):
    from pathlib import Path
    assert "ca" in site_main.backup_dirs(Path("/h"), encrypted=True)
    plain = site_main.backup_dirs(Path("/h"), encrypted=False)
    assert "ca" not in plain and "broker" not in plain and "maps" in plain


# ------------------------------------------------------------ A2 外审(R1–R4)


def test_R1_删掉一张登记了的照片_校验和恢复都不过(站, tmp_path):
    home, dest, key, db, b = 站
    next(dest.joinpath("evidence").rglob("*.jpg" + SEALED)).unlink()
    r = verify(dest, key)
    assert not r.ok and any("清单上" in x for x in r.problems) \
        and any("照片没有文件" in x for x in r.problems), r.problems
    with pytest.raises(Exception, match="不恢复"):
        restore(dest, key, tmp_path / "new")


def test_R1_恢复出来的站点目录缺照片_缺MQTT配置_verify_home查得出(站, tmp_path):
    home, dest, key, db, b = 站
    new = tmp_path / "new"
    assert restore(dest, key, new).ok
    next(new.joinpath("evidence").rglob("*.jpg")).unlink()
    (new / "broker" / "acl").unlink()
    probs = ";".join(verify_home(new).problems)
    assert "照片没有文件" in probs and "broker/acl" in probs


def test_R1_删掉整个MQTT配置_校验和恢复都不过(站, tmp_path):
    import shutil
    home, dest, key, db, b = 站
    shutil.rmtree(dest / "broker")
    probs = ";".join(verify(dest, key).problems)
    assert "broker/mosquitto.conf" in probs and "broker/acl" in probs
    with pytest.raises(Exception, match="broker/acl"):
        restore(dest, key, tmp_path / "new")


def test_R1_明文备份不带站点身份_不过(站, tmp_path):
    home, dest, key, db, b = 站
    plain = tmp_path / "plain-bak"
    pb = SiteBackup(db, home / "evidence", plain, now_ms=lambda: NOW,
                    more=site_main.backup_dirs(home, encrypted=False),
                    files={"site.json": home / "site.json"})
    assert pb.run_once()
    r = verify(plain, None)
    assert not r.ok and any("ca/ca.key" in x for x in r.problems), r.problems


def test_R1_做到一半断了的那次不算_用上一次做完了的_老备份没有清单不过(站, tmp_path):
    home, dest, key, db, b = 站
    (dest / "db" / ("site-20991231T000000Z.db" + SEALED)).write_bytes(
        (dest / "db").glob("site-*.db" + SEALED).__next__().read_bytes())  # 只有快照,没清单
    r = verify(dest, key)
    assert r.ok and r.snapshot != "site-20991231T000000Z.db" + SEALED, r.problems
    for m in (dest / "db").glob("manifest-*"):
        m.unlink()
    assert any("没有做完的备份" in x for x in verify(dest, key).problems)


def test_R3_umask022下恢复_私钥都是0600_目录0700(站, tmp_path):
    import stat
    home, dest, key, db, b = 站
    old = os.umask(0o022)
    try:
        new = tmp_path / "new"
        assert restore(dest, key, new).ok
    finally:
        os.umask(old)
    assert (new / "ca" / "issued" / "A" / "robot.key").is_file()
    for p in new.rglob("*"):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o077 == 0, (p, oct(mode))


def test_R4_报P2写失败了_下一拍补上_解决失败了也补(站):
    from d1max_site.alert_store import AlertDesk
    home, dest, key, db, b = 站
    desk = AlertDesk(db, now_ms=lambda: NOW)
    b.alerts = desk
    real_raise, real_resolve = desk.raise_alert, desk.resolve_all
    desk.raise_alert = lambda **kw: (_ for _ in ()).throw(OSError("库锁住了"))
    (dest / ("config/site.json" + SEALED)).unlink()
    with pytest.raises(OSError):
        b.verify_step(key)
    desk.raise_alert = real_raise
    assert b.verify_step(key) is None, "一周一次的扫描不重跑"
    [a] = [a for a in desk.book.open() if a.kind == "backup_verify_failed"]
    b._now = lambda: NOW + 8 * 86_400_000
    assert b.run_once()
    desk.resolve_all = lambda *a, **kw: (_ for _ in ()).throw(OSError("库锁住了"))
    with pytest.raises(OSError):
        b.verify_step(key)
    desk.resolve_all = real_resolve
    b.verify_step(key)
    assert not [a for a in desk.book.open() if a.kind == "backup_verify_failed"]


def test_R2_装机脚本有恢复模式_不生成密钥_不init_不起服务():
    from pathlib import Path
    s = (Path(__file__).resolve().parents[3] / "deploy" / "site" / "install-site.sh").read_text()
    rec = s[s.index('if [[ $RECOVER -eq 1 ]]; then'):]
    head = rec[:rec.index("fi")]
    assert "放回来(不会生成新的)" in head and "exit 2" in head, "密钥没放回就停,不生成"
    gen = s.index('head -c 32 /dev/urandom')
    assert s.index("恢复要先把离线另存的") < gen, "先查密钥、后才会走到生成那一步"
    assert '恢复模式:不 init' in s
    tail = s[s.index("systemctl daemon-reload"):]
    assert tail.index("systemctl enable d1max-mosquitto.service d1max-site.service") \
        < tail.index("exit 0") < tail.index("enable --now")


def test_R3_明文备份恢复也不抄源文件的宽权限(站, tmp_path):
    import stat
    home, dest, key, db, b = 站
    plain = tmp_path / "plain-bak"
    pb = SiteBackup(db, home / "evidence", plain, now_ms=lambda: NOW,
                    more=site_main.backup_dirs(home, encrypted=False),
                    files={"site.json": home / "site.json"})
    assert pb.run_once()
    for p in plain.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    old = os.umask(0o022)
    try:
        restore(plain, None, tmp_path / "new")
    finally:
        os.umask(old)
    files = [p for p in (tmp_path / "new").rglob("*") if p.is_file()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) & 0o077 == 0 for p in files)


# ------------------------------------------------------------ A2 复查(恢复、演练也按清单)


def test_复查_清单上的文件缺了_恢复和演练都不成_一个字节都不写(站, tmp_path):
    home, dest, key, db, b = 站
    next(dest.joinpath("evidence").rglob("events.jsonl" + SEALED)).unlink()
    new = tmp_path / "new"
    with pytest.raises(Exception, match="清单上的 1 个文件不见了"):
        restore(dest, key, new)
    assert not new.exists() or not any(new.iterdir()), "写之前就挡住"
    got = site_main.cmd_backup_drill(home, str(key))
    assert not got["ok"] and "清单" in got["problems"][0]


def test_复查_清单全删了_恢复和演练都不成(站, tmp_path):
    home, dest, key, db, b = 站
    for m in (dest / "db").glob("manifest-*"):
        m.unlink()
    with pytest.raises(Exception, match="没有做完的备份"):
        restore(dest, key, tmp_path / "new")
    assert not site_main.cmd_backup_drill(home, str(key))["ok"]
