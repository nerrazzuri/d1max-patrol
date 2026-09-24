"""机器人注册表(SQLite)。与 CA 分开:CA 管证书,注册表管「站点认不认这台狗」。"""

from __future__ import annotations

import pytest

from d1max_site.db import SiteDB
from d1max_site.registry import Registry, RegistryError

T0 = 1_800_000_000_000


@pytest.fixture
def reg(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    yield Registry(db, site_id="estate-1")
    db.close()


def test_登记_查询_列表(reg):
    reg.enroll("A", fingerprint="sha256:aa", issued_at=T0, expires_at=T0 + 1000)
    reg.enroll("B", fingerprint="sha256:bb", issued_at=T0, expires_at=T0 + 1000)
    a = reg.get("A")
    assert a.robot_id == "A" and a.fingerprint == "sha256:aa" and not a.revoked
    assert [r.robot_id for r in reg.list()] == ["A", "B"]
    assert reg.get("nope") is None


def test_有效期与吊销决定active(reg):
    reg.enroll("A", fingerprint="sha256:aa", issued_at=T0, expires_at=T0 + 1000)
    assert reg.active("A", now_ms=T0 + 1)
    assert not reg.active("A", now_ms=T0 + 1000), "到期那一刻就不认了"
    reg.revoke("A")
    assert not reg.active("A", now_ms=T0 + 1) and reg.get("A").revoked
    assert not reg.active("nope", now_ms=T0)


def test_没吊销不许重登_吊销后可以换新证书重登(reg):
    reg.enroll("A", fingerprint="sha256:aa", issued_at=T0, expires_at=T0 + 1000)
    with pytest.raises(RegistryError):
        reg.enroll("A", fingerprint="sha256:a2", issued_at=T0, expires_at=T0 + 1000)
    reg.revoke("A")
    reg.enroll("A", fingerprint="sha256:a2", issued_at=T0, expires_at=T0 + 1000)
    assert reg.get("A").fingerprint == "sha256:a2" and not reg.get("A").revoked


def test_保留名与不合主题规矩的id拒绝(reg):
    for bad in ("site:estate-1", "site:x", "a/b", ""):
        with pytest.raises(RegistryError):
            reg.enroll(bad, fingerprint="sha256:x", issued_at=T0, expires_at=T0 + 1)


def test_control_epoch默认1_持久(reg, tmp_path):
    reg.enroll("A", fingerprint="sha256:aa", issued_at=T0, expires_at=T0 + 1000)
    assert reg.control_epoch("A") == 1
    reg.db.close()
    db2 = SiteDB(tmp_path / "site.db")
    assert Registry(db2, site_id="estate-1").control_epoch("A") == 1
    assert Registry(db2, site_id="estate-1").get("A").fingerprint == "sha256:aa"
    db2.close()


def test_吊销不存在的狗报错(reg):
    with pytest.raises(RegistryError):
        reg.revoke("ghost")


def test_注册表也只认安全字符(reg):
    for bad in ("a\tb", "a=b", "狗", "a" * 65):
        with pytest.raises(RegistryError):
            reg.enroll(bad, fingerprint="sha256:x", issued_at=T0, expires_at=T0 + 1)
