"""W00c5d:狗专用接收口,端到端(真 openssl 签的站点 CA 与证书、真 mTLS、代理自己的发件箱与分块上传)。

- 狗的身份是证书:没证书连不上,别的 CA 签的连不上,吊销了、登记表里指纹对不上的,站点不认;
- 路径里的狗号由站点按证书填,狗 A 写不进狗 B;
- 站点对自己存下的字节算哈希,狗对上了才算传到;传完、这一趟结束,狗上这一趟就没了;
- 只收狗能产生的那几种文件。
"""

from __future__ import annotations

import json
import os
import ssl
import time
from pathlib import Path

import pytest

from d1max_agent.engine.http_sink import HttpSink
from d1max_agent.outbox import Outbox
from d1max_site.ca import SiteCA
from d1max_site.db import SiteDB
from d1max_site.evidence import EvidenceStore
from d1max_site.intake import IntakeServer, server_context
from d1max_site.registry import Registry

NOW = int(time.time() * 1000)
STAMP = "20260925T010000Z"


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    root = tmp_path_factory.mktemp("ca")
    c = SiteCA(root / "ca")
    c.init("estate-1")
    c.srv = c.issue_server(["127.0.0.1"])
    c.a = c.issue_robot("A", days=30, now_ms=NOW)
    c.b = c.issue_robot("B", days=30, now_ms=NOW)
    other = SiteCA(root / "other")
    other.init("estate-1")
    c.stranger = other.issue_robot("A", days=30, now_ms=NOW)
    return c


class 站:
    def __init__(self, tmp_path, ca) -> None:
        self.db = SiteDB(tmp_path / "site.db")
        self.reg = Registry(self.db, site_id="estate-1")
        for b in (ca.a, ca.b):
            self.reg.enroll(b.robot_id, fingerprint=b.fingerprint, issued_at=NOW - 1000,
                            expires_at=NOW + 10**9)
        self.store = EvidenceStore(tmp_path / "evidence", self.db, now_ms=lambda: NOW)
        ctx = server_context(cert=ca.srv[0], key=ca.srv[1], ca=ca.ca_cert, crl=ca.crl)
        self.intake = IntakeServer(host="127.0.0.1", port=0, ctx=ctx, db=self.db,
                                   store=self.store, now_ms=lambda: NOW)
        self.intake.start()

    def close(self):
        self.intake.stop()


@pytest.fixture
def 站点(tmp_path, ca):
    s = 站(tmp_path, ca)
    yield s
    s.close()


def _sink(site, ca, bundle, *, ca_cert=None):
    ctx = ssl.create_default_context(cafile=str(ca_cert or ca.ca_cert))
    ctx.load_cert_chain(str(bundle.dir / "robot.crt"), str(bundle.dir / "robot.key"))
    return HttpSink(site.intake.url, ssl_context=ctx)


def _一趟(root: Path, *, photos=2, stamp=STAMP) -> Path:
    run = root / "runs" / "巡检一" / stamp
    (run / "photos").mkdir(parents=True)
    (run / "events.jsonl").write_text('{"kind":"start"}\n')
    for i in range(photos):
        (run / "photos" / f"P{i}__front__{stamp}.jpg").write_bytes(os.urandom(1500 + i))
    (run / "manifest.json").write_text(json.dumps(
        {"fingerprint": {}, "summary": {"result": "done"},
         "mission": {"waypoints": [{"name": "P0", "check": "门关好了没有"}]}}))
    return run


def _跑(box, n=20):
    for _ in range(n):
        box.step()


def test_狗传上来_站点登记一趟_狗上删掉(站点, ca, tmp_path):
    box = Outbox(tmp_path / "dogA", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    run = _一趟(box.root)
    want = {p.relative_to(run).as_posix(): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    _跑(box, 3)
    assert not run.exists(), "站点确认了、这一趟结束了:狗上不留"
    got = 站点.store.run_dir("A", "巡检一", STAMP)
    for rel, data in want.items():
        assert (got / rel).read_bytes() == data, rel
    rows = 站点.store.runs(robot_id="A")
    assert len(rows) == 1 and rows[0]["photos"] == 2 and rows[0]["finished"] is True
    assert rows[0]["result"] == "done"
    box.close()


def test_狗号按证书填_狗B的证书写不进狗A(站点, ca, tmp_path):
    box = Outbox(tmp_path / "dogB", cap_bytes=2**30, sink=_sink(站点, ca, ca.b), sn="A",
                 now_ms=lambda: NOW)                  # 请求头里冒充 A
    _一趟(box.root)
    _跑(box, 3)
    assert 站点.store.runs(robot_id="A") == []
    assert len(站点.store.runs(robot_id="B")) == 1
    box.close()


def test_别的CA签的_吊销的_都传不上去_狗上一个字节都不删(站点, ca, tmp_path):
    box = Outbox(tmp_path / "x", cap_bytes=2**30, sink=_sink(站点, ca, ca.stranger), sn="A",
                 now_ms=lambda: NOW)
    run = _一趟(box.root)
    _跑(box, 3)
    assert run.exists() and 站点.store.runs() == []
    box.close()
    站点.reg.revoke("A")
    box = Outbox(tmp_path / "y", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    run = _一趟(box.root)
    _跑(box, 3)
    assert run.exists() and 站点.store.runs() == [], "登记表里吊销了:TLS 过得去,站点也不认"
    assert box.facts().backlog_files > 0
    box.close()


def test_没有客户端证书连不上(站点, ca):
    import urllib.error
    import urllib.request
    ctx = ssl.create_default_context(cafile=str(ca.ca_cert))
    req = urllib.request.Request(站点.intake.url + "/api/intake/put", data=b"x", method="POST")
    with pytest.raises((urllib.error.URLError, ssl.SSLError, ConnectionError, OSError)) as e:
        urllib.request.urlopen(req, context=ctx, timeout=5).read()
    assert not isinstance(e.value, urllib.error.HTTPError), "要在 TLS 握手那一步就断,不是回个 HTTP 错"


def test_不收狗产生不了的文件_路径不合规的不收(站点):
    for run, rel in (("巡检一/" + STAMP, "findings.json"), ("巡检一/" + STAMP, "review.json"),
                     ("巡检一/" + STAMP, "../../B/x/manifest.json"), ("巡检一/x", "events.jsonl"),
                     ("../B/" + STAMP, "events.jsonl")):
        with pytest.raises(ValueError):
            站点.store.put("A", run, rel, offset=0, data=b"{}", total=2)


def test_中间缺一段不写_报盘上真实的大小_重传截断再写(站点):
    s = 站点.store
    r = s.put("A", "巡检一/" + STAMP, "events.jsonl", offset=0, data=b"abc", total=9)
    assert r.size == 3
    r = s.put("A", "巡检一/" + STAMP, "events.jsonl", offset=6, data=b"ghi", total=9)
    assert r.size == 3, "中间缺了一段:不写,报盘上真实的"
    r = s.put("A", "巡检一/" + STAMP, "events.jsonl", offset=3, data=b"def", total=9)
    r2 = s.put("A", "巡检一/" + STAMP, "events.jsonl", offset=6, data=b"ghi", total=9)
    import hashlib
    assert r2.size == 9 and r2.sha256 == hashlib.sha256(b"abcdefghi").hexdigest()
    r3 = s.put("A", "巡检一/" + STAMP, "events.jsonl", offset=2, data=b"X", total=3)
    assert r3.size == 3 and r3.sha256 == hashlib.sha256(b"abX").hexdigest(), "重传:截断再写"


def test_握手不做的连接挡不住别的狗(站点, ca, tmp_path):
    """一个连上来却不发 ClientHello 的:握手在它自己的线程里等,接连接的那条线程照样接别人。"""
    import socket
    host, port = 站点.intake.httpd.server_address[:2]
    idle = socket.create_connection((host, port))
    try:
        box = Outbox(tmp_path / "dogA", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                     now_ms=lambda: NOW)
        run = _一趟(box.root)
        t0 = time.monotonic()
        _跑(box, 3)
        assert not run.exists() and time.monotonic() - t0 < 5
        box.close()
    finally:
        idle.close()
