"""W30(决策 43):站点上的口令加密、备份加密与恢复、证据留存期与按时间段删(连备份)、登记发布包时验签。
看证据记审计见 ``test_site_runs_api``。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3

import pytest

from d1max_site.backup import SEALED, SiteBackup
from d1max_site.db import SiteDB
from d1max_site.evidence import EvidenceStore
from d1max_site.privacy import DAY_MS, PrivacyDesk
from d1max_site.recordings import RecordingStore
from d1max_site.sealbox import (
    SealBox,
    SealError,
    load_or_create_key,
    migrate_plaintext,
    open_value,
    seal_value,
)

S1, S2 = "20260925T010000Z", "20260926T010000Z"


class 钟:
    def __init__(self):
        self.ms = 1_800_000_000_000

    def __call__(self):
        return self.ms


@pytest.fixture
def 箱(tmp_path):
    return SealBox(load_or_create_key(tmp_path / "keys" / "secrets.key"))


# ------------------------------------------------------------ 加解密


def test_小的值_加密解开_改一个字节_换钥匙_都解不开(箱, tmp_path):
    v = 箱.seal("摄像头口令 p@ss:1")
    assert v.startswith("enc1:") and "p@ss" not in v
    assert 箱.open(v) == "摄像头口令 p@ss:1"
    assert 箱.seal("x") != 箱.seal("x"), "每次随机盐:同一个值加密两次不一样"
    bad = v[:-4] + ("AAAA" if not v.endswith("AAAA") else "BBBB")
    with pytest.raises(SealError):
        箱.open(bad)
    with pytest.raises(SealError):
        SealBox(os.urandom(32)).open(v)
    with pytest.raises(SealError):
        箱.open("明文")


def test_密钥文件_没有就生成0600_长度不对就报错不换(tmp_path):
    k = tmp_path / "k" / "a.key"
    raw = load_or_create_key(k)
    assert len(raw) == 32 and (k.stat().st_mode & 0o777) == 0o600
    assert load_or_create_key(k) == raw, "有了就读,不重新生成"
    k.write_bytes(b"short")
    with pytest.raises(SealError):
        load_or_create_key(k)


def test_文件_加密解开一样_改过的解不开_不写半截(箱, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(os.urandom(2_500_000))
    箱.seal_file(src, tmp_path / "a.enc")
    箱.open_file(tmp_path / "a.enc", tmp_path / "b.bin")
    assert (tmp_path / "b.bin").read_bytes() == src.read_bytes()
    raw = bytearray((tmp_path / "a.enc").read_bytes())
    raw[1000] ^= 1
    (tmp_path / "c.enc").write_bytes(bytes(raw))
    with pytest.raises(SealError):
        箱.open_file(tmp_path / "c.enc", tmp_path / "c.bin")
    assert not (tmp_path / "c.bin").exists() and not (tmp_path / "c.bin.part").exists()


# ------------------------------------------------------------ 库里的口令


def test_摄像头口令_事件源密钥_加密落库_读出来是原文_验签照常(箱, tmp_path):
    from d1max_site.cctv import Camera, add_camera, load_cameras
    from d1max_site.incidents import IncidentDesk
    db = SiteDB(tmp_path / "site.db")
    db.sealbox = 箱
    add_camera(db, Camera(name="gate", onvif_url="http://10.0.0.5", username="admin",
                          password="Cam#Pass1", zone="front"), now_ms=1)
    raw = db.query("SELECT password FROM cameras")[0]["password"]
    assert raw.startswith("enc1:") and "Cam#Pass1" not in raw
    assert load_cameras(db)[0].password == "Cam#Pass1"
    from types import SimpleNamespace
    disp = SimpleNamespace(on_event=lambda cb: None, on_ack=lambda cb: None, clients={})
    desk = IncidentDesk(db, disp, now_ms=lambda: 1_700_000_000_000)
    secret = desk.add_source("nvr")
    stored = db.query("SELECT secret FROM incident_sources")[0]["secret"]
    assert stored.startswith("enc1:") and secret not in stored
    body, stamp = b'{"zone":"front"}', "1700000000000"
    sig = hmac.new(bytes.fromhex(secret), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    desk.verify("nvr", stamp, sig, body)                  # 不抛 = 验过了
    new = desk.rotate_secret("nvr")
    assert db.query("SELECT secret FROM incident_sources")[0]["secret"].startswith("enc1:")
    sig2 = hmac.new(bytes.fromhex(new), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    desk.verify("nvr", stamp, sig2, body)
    db.close()


def test_老库里的明文_起来就地加密_没配密钥读加密值就报错(箱, tmp_path):
    db = SiteDB(tmp_path / "site.db")
    db.query("INSERT INTO cameras(name, onvif_url, username, password, zone, added_ms) "
             "VALUES ('g','http://x','u','old-pass','z',1)")
    db.query("INSERT INTO incident_sources VALUES ('nvr', ?, 1)", ("ab" * 32,))
    assert migrate_plaintext(db) == 0, "没配密钥:不动"
    db.sealbox = 箱
    assert migrate_plaintext(db) == 2 and migrate_plaintext(db) == 0
    pw = db.query("SELECT password FROM cameras")[0]["password"]
    assert pw.startswith("enc1:") and open_value(db, pw) == "old-pass"
    assert seal_value(db, "") == "", "空口令照样空"
    db2 = SiteDB(tmp_path / "site.db")
    with pytest.raises(SealError):
        open_value(db2, db2.query("SELECT secret FROM incident_sources")[0]["secret"]
                   + "x")                                 # 没缓存过、没密钥
    db.close()
    db2.close()


# ------------------------------------------------------------ 备份


def _证据(tmp_path, c):
    db = SiteDB(tmp_path / "site.db")
    store = EvidenceStore(tmp_path / "evidence", db, now_ms=c)
    return db, store


def _传(store, robot="A", stamp=S1, photo=b"jpg-bytes"):
    for rel, data in {f"photos/P1__front__{stamp}.jpg": photo, "manifest.json": b"{}"}.items():
        store.put(robot, f"巡检/{stamp}", rel, offset=0, data=data, total=len(data))
    return [r for r in store.runs(robot_id=robot) if r["stamp"] == stamp][0]


def test_备份加密_库和证据都是密文_增量_以前的明文删掉_解开一样(箱, tmp_path):
    from d1max_site.main import cmd_backup_open
    c = 钟()
    db, store = _证据(tmp_path, c)
    _传(store, photo=b"SECRET-PHOTO")
    for f in store.root.rglob("*"):                       # 证据是早先收的(不在这一秒里)
        if f.is_file():
            os.utime(f, (f.stat().st_atime - 1000, f.stat().st_mtime - 1000))
    dest = tmp_path / "bak"
    plain = dest / "evidence" / "A" / "巡检" / S1 / "manifest.json"   # 上一版留下的明文备份
    plain.parent.mkdir(parents=True)
    plain.write_bytes(b"{}")
    (dest / "db").mkdir(parents=True)
    (dest / "db" / "site-old.db").write_bytes(b"plain db")
    bkey = tmp_path / "keys" / "backup.key"
    b = SiteBackup(db, store.root, dest, now_ms=c, box=SealBox(load_or_create_key(bkey)))
    assert b.run_once(), b.last_error
    files = [p for p in dest.rglob("*") if p.is_file()]
    assert files and all(p.name.endswith(SEALED) for p in files), files
    assert not any(b"SECRET-PHOTO" in p.read_bytes() for p in files)
    assert not plain.exists() and not (dest / "db" / "site-old.db").exists()
    sealed = {p: p.stat().st_mtime_ns for p in (dest / "evidence").rglob(f"*{SEALED}")}
    calls = []
    real = b.box.seal_file
    b.box.seal_file = lambda s, d: (calls.append(s), real(s, d))
    assert b.run_once()
    assert not [s for s in calls if "evidence" in str(s)], "证据没变:不重加密"
    out = tmp_path / "restored"
    n = cmd_backup_open(dest, out, bkey)
    assert n == len([p for p in dest.rglob("*") if p.is_file()])
    photo = out / "evidence" / "A" / "巡检" / S1 / "photos" / f"P1__front__{S1}.jpg"
    assert photo.read_bytes() == b"SECRET-PHOTO"
    [snap] = sorted((out / "db").glob("site-*.db"))[-1:]
    assert sqlite3.connect(snap).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert sealed, "镜像有东西"
    from d1max_site.main import SiteError
    with pytest.raises(SiteError, match="解不开"):
        cmd_backup_open(dest, tmp_path / "r2", tmp_path / "keys" / "secrets-wrong.key"
                        if load_or_create_key(tmp_path / "keys" / "secrets-wrong.key") else bkey)
    db.close()


def test_没配备份密钥_照旧明文备份(tmp_path):
    c = 钟()
    db, store = _证据(tmp_path, c)
    _传(store)
    b = SiteBackup(db, store.root, tmp_path / "bak", now_ms=c)
    assert b.run_once()
    assert list((tmp_path / "bak" / "db").glob("site-*.db"))
    assert not list((tmp_path / "bak").rglob(f"*{SEALED}"))
    db.close()


# ------------------------------------------------------------ 留存期、按时间段删


def _台(tmp_path, keep_days=90):
    c = 钟()
    db, store = _证据(tmp_path, c)
    rec = RecordingStore(db, tmp_path / "recordings", now_ms=c)
    desk = PrivacyDesk(db, store, now_ms=c, recordings=rec, backup_dest=tmp_path / "bak",
                       keep_days=keep_days)
    return c, db, store, rec, desk


def _录像(rec, stamp=S1):
    data = b"mp4"
    rec.put("A", f"front/{stamp}", "video.mp4", offset=0, data=data, total=len(data))
    return rec.list(robot_id="A")[0]


def test_过了留存期删_标了留着的不删_连备份(tmp_path):
    c, db, store, rec, desk = _台(tmp_path)
    old = _传(store, stamp=S1)
    kept = _传(store, stamp=S2)
    desk.set_keep(kept["id"], True)
    SiteBackup(db, store.root, tmp_path / "bak", now_ms=c).run_once()
    bak_old = tmp_path / "bak" / "evidence" / "A" / "巡检" / S1
    assert bak_old.exists()
    c.ms += 89 * DAY_MS
    assert desk.prune() == 0
    c.ms += 2 * DAY_MS
    assert desk.prune() == 1
    left = [r["id"] for r in store.runs()]
    assert left == [kept["id"]] and not store.dir_of(old).exists() and not bak_old.exists()
    assert not db.query("SELECT 1 FROM run_photos WHERE run_id=?", (old["id"],))
    with pytest.raises(KeyError):
        desk.set_keep(9999, True)


def test_按时间段删_运行记录和录像_留着的列出来不删(tmp_path):
    c, db, store, rec, desk = _台(tmp_path)
    r1 = _传(store, stamp=S1)
    held = _传(store, robot="B", stamp=S1)
    desk.set_keep(held["id"], True)
    seg = _录像(rec, S1)
    rec.set_keep(seg["id"], True)
    seg2 = _录像(rec, "20260925T010100Z")
    got = desk.purge(since_ms=c.ms - 1000, until_ms=c.ms + 1000)
    assert got["runs"] == 1 and got["recordings"] == 0, "录像按片段开始时刻算:不在这个窗口里"
    assert got["held_runs"] == [held["id"]] and store.run(held["id"]) is not None
    assert store.run(r1["id"]) is None and rec.get(seg2["id"]) is not None
    start = seg["start_ms"]
    got = desk.purge(since_ms=start, until_ms=start + 120_000, robot_id="A")
    assert got["recordings"] == 1 and got["held_recordings"] == [seg["id"]]
    assert rec.get(seg2["id"]) is None and rec.get(seg["id"]) is not None
    with pytest.raises(ValueError):
        desk.purge(since_ms=5, until_ms=5)


def test_命令行按时间段删_要写原因_记审计(tmp_path):
    from d1max_site.main import SiteError, cmd_privacy_purge
    home = tmp_path / "home"
    home.mkdir()
    (home / "site.json").write_text(json.dumps({"site_id": "s"}))
    db = SiteDB(home / "site.db")
    store = EvidenceStore(home / "evidence", db, now_ms=lambda: 1_790_000_000_000)
    _传(store)
    db.close()
    with pytest.raises(SiteError, match="--reason"):
        cmd_privacy_purge(home, "2026-09-20T00:00+00:00", "2027-01-01T00:00+00:00", None, " ")
    got = cmd_privacy_purge(home, "2026-09-20T00:00+00:00", "2027-01-01T00:00+00:00", None,
                            "当事人 2026-10-08 书面要求")
    assert got["runs"] == 1
    db = SiteDB(home / "site.db")
    [a] = [dict(r) for r in db.query("SELECT * FROM audit WHERE action='privacy purge'")]
    assert "书面要求" in a["detail"]
    db.close()


# ------------------------------------------------------------ 发布包签名


def test_站点配了发行公钥_没签名_签错的不登记_签对的登记(tmp_path):
    from test_site_releases import 做包

    from d1max_contract import relsign
    from d1max_site.releases import ReleaseCatalog, ReleaseCatalogError
    k, pub = tmp_path / "k", tmp_path / "k.pub"
    relsign.keygen(k, pub)
    cat = ReleaseCatalog(tmp_path / "site", SiteDB(tmp_path / "site.db"), now_ms=lambda: 7,
                         pubkey=pub)
    pkg = 做包(tmp_path)
    with pytest.raises(ReleaseCatalogError, match="没有签名"):
        cat.add(pkg)
    m = json.loads((pkg / "release.json").read_text())
    m["signature"] = relsign.sign(dict(m, version="9.9"), k)    # 签的不是这一版
    (pkg / "release.json").write_text(json.dumps(m))
    with pytest.raises(ReleaseCatalogError, match="签名对不上"):
        cat.add(pkg)
    m["signature"] = relsign.sign(m, k)
    (pkg / "release.json").write_text(json.dumps(m))
    assert cat.add(pkg)["name"] == m["name"]
