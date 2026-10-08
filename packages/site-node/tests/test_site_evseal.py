"""狗上证据加密(W30b,决策 51),站点这一头:封好的照片、录像收齐了解开、登记;私钥没配、
不对就留着封好的、报 P2,补解;狗报没封报 P2 一台一次;证书包带证据公钥。"""
# ruff: noqa: F811  (站点、ca 是从 test_site_intake 借来的夹具)

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from test_site_charging import 假告警台
from test_site_intake import NOW, STAMP, _sink, _一趟, _跑, ca, 站点  # noqa: F401

from d1max_agent.outbox import Outbox
from d1max_contract import evseal as E
from d1max_site.db import SiteDB
from d1max_site.evidence import EvidencePlainWatch


@pytest.fixture
def 钥匙():
    priv, pub = E.keypair()
    return SimpleNamespace(priv=priv, pub=pub, sealer=E.Sealer(pub))


def _照片(run):
    return {p.name: p.read_bytes() for p in (run / "photos").iterdir()}


def test_狗上补封旧照片_传上来_站点解开登记_跟原来一样_没留封好的(站点, ca, tmp_path, 钥匙):
    站点.store.evidence_key = 钥匙.priv
    box = Outbox(tmp_path / "dogA", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    run = _一趟(box.root)
    want = _照片(run)
    assert box.seal_leftovers(钥匙.sealer, lambda rel: rel.startswith("photos/")) == 2
    assert sorted(p.name for p in (run / "photos").iterdir()) == sorted(
        n + ".d1e" for n in want), "狗上只剩封好的"
    _跑(box, 3)
    assert not run.exists(), "站点确认了:狗上不留"
    got = 站点.store.run_dir("A", "巡检一", STAMP)
    assert _照片(got) == want, "站点解开成原名、原样"
    rows = 站点.store.runs(robot_id="A")
    assert rows[0]["photos"] == 2, "登记的是解开的照片"
    box.close()


def test_升级前明文已经排进上传队列_补封了销账_封好的照样传完删掉(站点, ca, tmp_path, 钥匙):
    """传到一半的从头传封好的那份;明文那一条不销账的话,上传线程会一直等那个不见了的文件。"""
    站点.store.evidence_key = 钥匙.priv
    box = Outbox(tmp_path / "dogA", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    run = _一趟(box.root)
    want = _照片(run)
    box.uploader.scan()                                   # 升级前:明文已经排进队列了
    assert box.seal_leftovers(钥匙.sealer, lambda rel: rel.startswith("photos/")) == 2
    _跑(box, 3)
    assert not run.exists(), "封好的传完、站点确认:整趟删掉"
    assert _照片(站点.store.run_dir("A", "巡检一", STAMP)) == want
    box.close()


def test_站点没配私钥_封好的留着报P2_配好了补解(站点, ca, tmp_path, 钥匙):
    got_unopened = []
    站点.store.on_unopened = lambda *a: got_unopened.append(a)
    box = Outbox(tmp_path / "dogA", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    run = _一趟(box.root)
    want = _照片(run)
    box.seal_leftovers(钥匙.sealer, lambda rel: rel.startswith("photos/"))
    _跑(box, 3)
    assert not run.exists(), "站点照样确认:狗那份删了,站点这份是唯一的"
    d = 站点.store.run_dir("A", "巡检一", STAMP) / "photos"
    assert sorted(p.name for p in d.iterdir()) == sorted(n + ".d1e" for n in want)
    assert len(got_unopened) == 2 and "没配证据私钥" in got_unopened[0][3]
    assert 站点.store.runs(robot_id="A")[0]["photos"] == 0, "没解开的不登记"
    站点.store.evidence_key = 钥匙.priv
    assert 站点.store.open_leftovers() == (2, 0)
    assert _照片(d.parent) == want and 站点.store.runs(robot_id="A")[0]["photos"] == 2
    box.close()


def test_私钥不对_解不开_封好的留着(站点, ca, tmp_path, 钥匙):
    other, _ = E.keypair()
    站点.store.evidence_key = other
    got = []
    站点.store.on_unopened = lambda *a: got.append(a)
    box = Outbox(tmp_path / "dogA", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    _一趟(box.root, photos=1)
    box.seal_leftovers(钥匙.sealer, lambda rel: rel.startswith("photos/"))
    _跑(box, 3)
    assert got and "对不上" in got[0][3]
    d = 站点.store.run_dir("A", "巡检一", STAMP) / "photos"
    assert [p.suffix for p in d.iterdir()] == [".d1e"], "一个字节明文都没写"
    box.close()


def test_封好的录像段_收齐了解开登记_登记的是解开的大小和哈希(tmp_path, 钥匙):
    from d1max_site.evidence import sha256_file
    from d1max_site.recordings import RecordingStore
    rec = RecordingStore(SiteDB(tmp_path / "s.db"), tmp_path / "rec", now_ms=lambda: NOW)
    rec.evidence_key = 钥匙.priv
    seg = tmp_path / "seg.mp4"
    seg.write_bytes(os.urandom(5000))
    钥匙.sealer.seal_file(seg, tmp_path / "seg.mp4.d1e")
    data = (tmp_path / "seg.mp4.d1e").read_bytes()
    stamp = "20261009T010000Z"
    got = rec.put("A", f"front/{stamp}", "video.mp4.d1e", offset=0, data=data, total=len(data))
    assert got.size == len(data)
    [row] = rec.list(robot_id="A")
    assert row["bytes"] == 5000 and row["sha256"] == sha256_file(seg)
    files = list((tmp_path / "rec").rglob("*"))
    assert [p.name for p in files if p.is_file()] == [f"{stamp}.mp4"]


def test_没配私钥的录像段_留着_配好了补解(tmp_path, 钥匙):
    from d1max_site.recordings import RecordingStore
    rec = RecordingStore(SiteDB(tmp_path / "s.db"), tmp_path / "rec", now_ms=lambda: NOW)
    got = []
    rec.on_unopened = lambda *a: got.append(a)
    seg = tmp_path / "seg.mp4"
    seg.write_bytes(os.urandom(3000))
    钥匙.sealer.seal_file(seg, tmp_path / "x.d1e")
    data = (tmp_path / "x.d1e").read_bytes()
    rec.put("A", "front/20261009T010000Z", "video.mp4.d1e", offset=0, data=data,
            total=len(data))
    assert got and rec.list(robot_id="A") == []
    rec.evidence_key = 钥匙.priv
    assert rec.open_leftovers() == (1, 0) and len(rec.list(robot_id="A")) == 1


def test_封好的名字_只认照片和录像段():
    from d1max_contract.intake import dog_may_upload
    assert dog_may_upload("photos/P1__front__x.jpg.d1e")
    assert not dog_may_upload("manifest.json.d1e") and not dog_may_upload("events.jsonl.d1e")
    assert not dog_may_upload("photos/x.exe.d1e")


def _caps(sealed):
    return SimpleNamespace(capabilities=SimpleNamespace(tasks={"evidence": {"sealed": sealed}}))


def test_狗报证据没封_报P2一台一次_装好了清_老代理不报(tmp_path):
    db = SiteDB(tmp_path / "s.db")
    d = SimpleNamespace(clients={"A": _caps(False), "B": SimpleNamespace(
        capabilities=SimpleNamespace(tasks={}))})
    w = EvidencePlainWatch(db, d, now_ms=lambda: NOW)
    w.alerts = 假告警台()
    w.tick()
    w.tick()
    assert [(a["kind"], a["robot"]) for a in w.alerts.raised] == [("evidence_plain", "A")]
    d.clients["A"] = _caps(True)
    w.tick()
    assert not db.query("SELECT 1 FROM evidence_plain")
    w.alerts.raised.clear()
    d.clients["A"] = _caps(False)
    w.tick()
    assert [a["kind"] for a in w.alerts.raised] == ["evidence_plain"]


def test_登记狗_证书包带证据公钥_能补解(tmp_path, monkeypatch, 钥匙):
    import json

    from d1max_site import main as site_main
    home = tmp_path / "site"
    assert site_main.main(["--home", str(home), "init", "--site-id", "estate-1",
                           "--hostname", "localhost"]) == 0
    keyf = tmp_path / "evidence.key"
    keyf.write_bytes(钥匙.priv)
    cfg = json.loads((home / "site.json").read_text())
    cfg["evidence_key"] = str(keyf)
    (home / "site.json").write_text(json.dumps(cfg))
    assert site_main.main(["--home", str(home), "enroll", "A"]) == 0
    pub = home / "ca" / "issued" / "A" / "evidence-pub.key"
    assert E.load_public(pub) == 钥匙.pub
    assert site_main.main(["--home", str(home), "evidence-open"]) == 0
