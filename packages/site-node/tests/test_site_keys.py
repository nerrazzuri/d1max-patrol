"""站点密钥托管与自检、发行钥匙换钥匙(商业化 A3)。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from d1max_site import keyvault
from d1max_site import main as site_main

PASS = "correct horse battery"


def _钥(root: Path, *, jpush=True):
    root.mkdir(parents=True, exist_ok=True)
    for k in ("secrets", "backup", "evidence"):
        (root / f"{k}.key").write_bytes(os.urandom(32))
        (root / f"{k}.key").chmod(0o440)
    if jpush:
        (root / "jpush.secret").write_text("ms-secret\n")
        (root / "jpush.secret").chmod(0o440)
    return {f"{k}_key": str(root / f"{k}.key") for k in ("secrets", "backup", "evidence")} | (
        {"push": {"app_key": "k", "secret_file": str(root / "jpush.secret")}} if jpush else {})


def test_自检_都在_短指纹_推送没配不算毛病(tmp_path):
    cfg = _钥(tmp_path / "etc", jpush=False)
    rows = {r["name"]: r for r in keyvault.status(cfg)}
    assert all(not r["problem"] for r in rows.values()), rows
    assert len(rows["backup"]["fingerprint"]) == 16 and not rows["jpush"]["present"]


def test_自检_少了_长度不对_权限太宽(tmp_path):
    cfg = _钥(tmp_path / "etc")
    Path(cfg["secrets_key"]).unlink()
    Path(cfg["backup_key"]).chmod(0o600)
    Path(cfg["backup_key"]).write_bytes(b"short")
    Path(cfg["evidence_key"]).chmod(0o444)
    rows = {r["name"]: r["problem"] for r in keyvault.status(cfg)}
    assert "没有" in rows["secrets"] and "长度不对" in rows["backup"] \
        and "权限太宽" in rows["evidence"] and rows["jpush"] == ""


def test_托管包_来回一样_文件0600_里面看不到密钥_口令不对解不开(tmp_path):
    cfg = _钥(tmp_path / "etc")
    out = tmp_path / "usb" / "keys.d1keys"
    assert keyvault.export(cfg, out, PASS) == ["backup", "evidence", "jpush", "secrets"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    blob = out.read_bytes()
    for k in ("secrets", "backup", "evidence"):
        raw = Path(cfg[f"{k}_key"]).read_bytes()
        assert raw not in blob and raw.hex().encode() not in blob
    assert b"ms-secret" not in blob
    with pytest.raises(keyvault.KeyError_, match="口令不对"):
        keyvault.read_bundle(out, "wrong passphrase!")
    got = keyvault.read_bundle(out, PASS)
    assert got["backup"] == Path(cfg["backup_key"]).read_bytes()
    assert got["jpush"] == b"ms-secret\n"


def test_导回_空目录放回0440_一样的跳过_不一样的不盖_force才盖(tmp_path):
    cfg = _钥(tmp_path / "etc")
    out = tmp_path / "keys.d1keys"
    keyvault.export(cfg, out, PASS)
    new = tmp_path / "new-etc"
    assert sorted(keyvault.import_(cfg, out, PASS, root=new)) == \
        ["backup", "evidence", "jpush", "secrets"]
    for k in ("secrets", "backup", "evidence"):
        assert (new / f"{k}.key").read_bytes() == Path(cfg[f"{k}_key"]).read_bytes()
        assert stat.S_IMODE((new / f"{k}.key").stat().st_mode) == 0o440
    assert keyvault.import_(cfg, out, PASS, root=new) == [], "一样的:跳过"
    (new / "backup.key").chmod(0o600)
    (new / "backup.key").write_bytes(os.urandom(32))
    with pytest.raises(keyvault.KeyError_, match="不盖"):
        keyvault.import_(cfg, out, PASS, root=new)
    assert keyvault.import_(cfg, out, PASS, root=new, force=True) == ["backup"]


def test_托管包_口令太短不打_缺了必需的不打(tmp_path):
    cfg = _钥(tmp_path / "etc")
    with pytest.raises(keyvault.KeyError_, match="至少"):
        keyvault.export(cfg, tmp_path / "x", "short")
    Path(cfg["evidence_key"]).unlink()
    with pytest.raises(keyvault.KeyError_, match="evidence"):
        keyvault.export(cfg, tmp_path / "x", PASS)
    assert not (tmp_path / "x").exists()


def test_命令行_自检_导出_导回(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid)
    home = tmp_path / "site"
    site_main.main(["--home", str(home), "init", "--site-id", "e", "--hostname", "localhost"])
    cfg = json.loads((home / "site.json").read_text())
    cfg.update(_钥(tmp_path / "etc"))
    (home / "site.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("D1MAX_KEYS_PASSPHRASE", PASS)
    assert site_main.main(["--home", str(home), "keys-status"]) == 0
    out = tmp_path / "k.d1keys"
    assert site_main.main(["--home", str(home), "keys-export", "--out", str(out)]) == 0
    Path(cfg["backup_key"]).unlink()
    assert site_main.main(["--home", str(home), "keys-status"]) == 1
    assert site_main.main(["--home", str(home), "keys-import", str(out)]) == 0
    assert "backup" in capsys.readouterr().out
    assert site_main.main(["--home", str(home), "keys-status"]) == 0


def test_体检里有密钥一项(tmp_path):
    from d1max_site.db import SiteDB
    from d1max_site.health import HealthDesk
    cfg = _钥(tmp_path / "etc")
    db = SiteDB(tmp_path / "s.db")
    try:
        h = HealthDesk(db, home=tmp_path, now_ms=lambda: 0, cert_end=lambda p: None,
                       keys=lambda: keyvault.status(cfg))
        assert h.checks()["keys"]["ok"]
        Path(cfg["secrets_key"]).unlink()
        assert not h.checks()["keys"]["ok"] and "secrets" in h.checks()["keys"]["detail"]
    finally:
        db.close()


def test_发行钥匙_换钥匙_新钥匙放进点d目录就认_删掉就是吊销(tmp_path):
    from d1max_contract import relsign
    etc = tmp_path / "etc"
    relsign.keygen(tmp_path / "old.key", etc / "release-pub.pem")
    relsign.keygen(tmp_path / "new.key", tmp_path / "new.pem")
    m = {"name": "r1", "version": "1", "content_sha256": "ab", "requires_mission_schema": 1}
    m["signature"] = relsign.sign(m, tmp_path / "new.key")
    with pytest.raises(relsign.SignError, match="对不上"):
        relsign.verify(m, etc / "release-pub.pem")
    (etc / "release-pub.d").mkdir()
    (etc / "release-pub.d" / "2026.pem").write_bytes((tmp_path / "new.pem").read_bytes())
    relsign.verify(m, etc / "release-pub.pem")                  # 新钥匙签的认了
    old = dict(m, signature=relsign.sign(m, tmp_path / "old.key"))
    relsign.verify(old, etc / "release-pub.pem")                # 旧的也还认(换的过程中)
    (etc / "release-pub.pem").unlink()                          # 旧公钥删掉 = 吊销
    with pytest.raises(relsign.SignError):
        relsign.verify(old, etc / "release-pub.pem")
    relsign.verify(m, etc / "release-pub.pem"), "只剩 .d 里的也认"
    assert len(relsign.key_id(tmp_path / "new.pem")) == 16


def test_站点装机脚本_恢复模式不生成密钥_托管包在装好软件以后导回():
    s = (Path(__file__).resolve().parents[3] / "deploy" / "site" / "install-site.sh").read_text()
    gen = s.index('head -c 32 /dev/urandom')
    loop = s[s.rindex("for k in secrets backup evidence; do", 0, gen):gen]
    assert "[[ $RECOVER -eq 1 ]] && continue" in loop, "恢复模式:没有的不生成"
    assert s.index("pip\" install") < s.index("keys-import \"$KEYS_FILE\""), "装好软件再导回"
    assert "--keys)" in s
