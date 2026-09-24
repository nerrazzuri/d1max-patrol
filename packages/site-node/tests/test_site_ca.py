"""站点自建 CA(设计决定三 A:调系统 openssl)。狗证书 CN = robot_id(broker 拿它当用户名),
站点服务证书带 SAN;吊销进 CRL。"""

from __future__ import annotations

import json
import stat
import subprocess

import pytest

from d1max_contract.registration import Registration
from d1max_site.ca import CAError, SiteCA

NOW = 1_800_000_000_000


def _ossl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl", *args], capture_output=True, text=True)


@pytest.fixture
def ca(tmp_path):
    c = SiteCA(tmp_path / "ca")
    c.init("estate-1")
    return c


def test_init生成CA与空CRL_私钥只有自己能读(ca):
    assert ca.site_id == "estate-1"
    assert ca.ca_cert.is_file() and ca.crl.is_file()
    assert stat.S_IMODE(ca.ca_key.stat().st_mode) == 0o600
    txt = _ossl("x509", "-in", str(ca.ca_cert), "-noout", "-subject").stdout
    assert "estate-1" in txt


def test_init两次拒绝_不覆盖已有CA(ca):
    with pytest.raises(CAError):
        ca.init("estate-1")


def test_狗证书CN是robot_id_能被CA验证_证书包齐全(ca, tmp_path):
    b = ca.issue_robot("D1MAX-001", days=30, now_ms=NOW)
    for f in ("ca.crt", "robot.crt", "robot.key", "registration.json"):
        assert (b.dir / f).is_file(), f
    assert stat.S_IMODE((b.dir / "robot.key").stat().st_mode) == 0o600
    subj = _ossl("x509", "-in", str(b.dir / "robot.crt"), "-noout", "-subject").stdout
    assert "CN = D1MAX-001" in subj or "CN=D1MAX-001" in subj
    v = _ossl("verify", "-CAfile", str(ca.ca_cert), str(b.dir / "robot.crt"))
    assert v.returncode == 0, v.stdout + v.stderr
    ext = _ossl("x509", "-in", str(b.dir / "robot.crt"), "-noout", "-ext", "extendedKeyUsage")
    assert "Client Authentication" in ext.stdout
    reg = Registration.load(b.dir / "registration.json")
    assert (reg.site_id, reg.robot_id) == ("estate-1", "D1MAX-001")
    assert reg.credential_fingerprint == b.fingerprint == ca.fingerprint(b.dir / "robot.crt")
    assert reg.issued_at == NOW and reg.expires_at == NOW + 30 * 86_400_000
    assert json.loads((b.dir / "registration.json").read_text())["schema"] == "1.0"


def test_站点服务证书带SAN_能当服务端(ca):
    crt, key = ca.issue_server(["site.local", "127.0.0.1"])
    san = _ossl("x509", "-in", str(crt), "-noout", "-ext", "subjectAltName").stdout
    assert "DNS:site.local" in san and "IP Address:127.0.0.1" in san
    eku = _ossl("x509", "-in", str(crt), "-noout", "-ext", "extendedKeyUsage").stdout
    assert "Server Authentication" in eku and "Client Authentication" in eku
    subj = _ossl("x509", "-in", str(crt), "-noout", "-subject").stdout
    assert "site:estate-1" in subj, "站点自己连 broker 也用这张,CN 是保留名 site:<site_id>"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_吊销之后CRL验不过(ca):
    b = ca.issue_robot("D1MAX-001", days=30, now_ms=NOW)
    ca.revoke("D1MAX-001")
    v = _ossl("verify", "-crl_check", "-CAfile", str(ca.ca_cert), "-CRLfile", str(ca.crl),
              str(b.dir / "robot.crt"))
    assert v.returncode != 0 and "revoked" in (v.stdout + v.stderr).lower()


def test_robot_id不合主题规矩或是保留名_拒签(ca):
    for bad in ("a/b", "x+", "has space", "", "site:estate-1"):
        with pytest.raises((CAError, ValueError)):
            ca.issue_robot(bad, days=1, now_ms=NOW)


def test_没吊销就重签同一台_拒绝(ca):
    ca.issue_robot("D1MAX-001", days=30, now_ms=NOW)
    with pytest.raises(CAError):
        ca.issue_robot("D1MAX-001", days=30, now_ms=NOW)
    ca.revoke("D1MAX-001")
    b2 = ca.issue_robot("D1MAX-001", days=30, now_ms=NOW)          # 吊销后可以重签
    assert b2.fingerprint


def test_站点证书总带本机地址_站点自己连127001不会被主机名校验挡住(ca):
    """serve 默认连 mqtts://127.0.0.1;安装示例只给了局域网名字与 IP。"""
    crt, _ = ca.issue_server(["site.local"])
    san = _ossl("x509", "-in", str(crt), "-noout", "-ext", "subjectAltName").stdout
    assert "DNS:site.local" in san and "IP Address:127.0.0.1" in san and "DNS:localhost" in san


def test_robot_id只许安全字符(ca):
    for bad in ("a\tb", "a\nb", "a\\b", "CN=x", "狗1", "a" * 65, "a,b"):
        with pytest.raises(CAError):
            ca.issue_robot(bad, days=1, now_ms=NOW)


def test_主机名里有逗号换行_拒绝(ca):
    for bad in ("a,DNS:evil", "a\nb", "a b", ""):
        with pytest.raises(CAError):
            ca.issue_server([bad])
