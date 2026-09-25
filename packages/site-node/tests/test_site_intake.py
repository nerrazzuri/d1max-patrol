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
        from d1max_site.maps import MapCatalog
        self.maps = MapCatalog(tmp_path / "site", self.db, now_ms=lambda: NOW)
        ctx = server_context(cert=ca.srv[0], key=ca.srv[1], ca=ca.ca_cert, crl=ca.crl)
        self.intake = IntakeServer(host="127.0.0.1", port=0, ctx=ctx, db=self.db,
                                   store=self.store, now_ms=lambda: NOW, maps=self.maps)
        self.intake.start()

    def close(self):
        self.intake.stop()


@pytest.fixture
def 站点(tmp_path, ca):
    s = 站(tmp_path, ca)
    yield s
    s.close()


def _ctx(ca, bundle, *, ca_cert=None):
    ctx = ssl.create_default_context(cafile=str(ca_cert or ca.ca_cert))
    ctx.load_cert_chain(str(bundle.dir / "robot.crt"), str(bundle.dir / "robot.key"))
    return ctx


def _sink(site, ca, bundle, *, ca_cert=None, sub=""):
    return HttpSink(site.intake.url + sub, ssl_context=_ctx(ca, bundle, ca_cert=ca_cert))


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
    assert not isinstance(e.value, urllib.error.HTTPError), \
        "要在 TLS 握手那一步就断,不是回个 HTTP 错"


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



# ------------------------------------------------------------ W00c5d 第二部分:图与录包

def test_狗建的图和录包传上来_站点收齐登记_别的狗下载核对装上(站点, ca, tmp_path):
    import hashlib

    from d1max_agent.mapping import DONE, bag_classify, map_classify, map_settled
    from d1max_agent.maps import MapKeeper, https_fetch
    from d1max_contract.maps import MapRef
    root = tmp_path / "dogA"
    bag = root / "bags" / "yard"
    bag.mkdir(parents=True)
    (bag / "yard_0.mcap").write_bytes(os.urandom(5000))
    (bag / DONE).touch()
    out = root / "maps" / "estate-1" / "9"
    out.mkdir(parents=True)
    files = {"estate-1.pgm": os.urandom(3000), "estate-1.yaml": b"resolution: 0.05"}
    for n, d in files.items():
        (out / n).write_bytes(d)
    ref = MapRef.from_wire({"map_id": "estate-1", "version": "9", "files": [
        {"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
        for n, d in files.items()]})
    (out / "map.json").write_text(json.dumps(ref.to_wire()))
    bags = Outbox(root, cap_bytes=2**30, sink=_sink(站点, ca, ca.a, sub="/bags"), sn="A",
                  now_ms=lambda: NOW, sub="bags", run_depth=1, classify=bag_classify,
                  settled=lambda p: (p / DONE).is_file())
    maps = Outbox(root, cap_bytes=2**30, sink=_sink(站点, ca, ca.a, sub="/maps"), sn="A",
                  now_ms=lambda: NOW, sub="maps", run_depth=2, classify=map_classify,
                  settled=map_settled)
    for _ in range(4):
        bags.step()
        maps.step()
    assert not bag.exists() and not out.exists(), "站点确认了:狗上不留"
    assert [b["name"] for b in 站点.maps.bags("A")] == ["yard"]
    assert 站点.maps.get("estate-1", "9") == ref and 站点.maps.list()[0]["source"] == "A"
    keeper = MapKeeper(tmp_path / "dogB-maps", fetch=https_fetch(站点.intake.url, _ctx(ca, ca.b)))
    d = keeper.install(ref)
    assert (d / "estate-1.pgm").read_bytes() == files["estate-1.pgm"]
    stranger = MapKeeper(tmp_path / "x", fetch=https_fetch(站点.intake.url, _ctx(ca, ca.stranger)))
    from d1max_agent.maps import MapInstallError
    with pytest.raises(MapInstallError):
        stranger.install(ref)
    站点.reg.revoke("B")                              # TLS 过得去,登记表里吊销了:站点不给
    with pytest.raises(MapInstallError):
        MapKeeper(tmp_path / "y", fetch=https_fetch(站点.intake.url, _ctx(ca, ca.b))).install(ref)
    bags.close()
    maps.close()


def test_图的路径不合规的不收_没登记的图下载不到(站点, ca, tmp_path):
    for run, rel in (("estate-1", "a.pgm"), ("estate-1/../x", "a.pgm"), ("e/9", "../a"),
                     ("e/9", "a/b")):
        with pytest.raises(ValueError):
            站点.maps.put_map_chunk("A", run, rel, offset=0, data=b"x", total=1)
    for run, rel in (("a/b", "x"), ("yard", "../x")):
        with pytest.raises(ValueError):
            站点.maps.put_bag_chunk("A", run, rel, offset=0, data=b"x", total=1)
    import urllib.error
    import urllib.request
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(站点.intake.url + "/maps/estate-1/404/x.pgm",
                               context=_ctx(ca, ca.a), timeout=5)
    assert e.value.code == 404



def test_太大的文件永远不收_站点盘快满了先不收(站点, monkeypatch):
    from d1max_site import evidence as ev
    with pytest.raises(ev.PathRefused):
        站点.store.put("A", "巡检一/" + STAMP, "events.jsonl", offset=0, data=b"x",
                     total=ev.MAX_FILE_BYTES + 1)
    import collections
    usage = collections.namedtuple("u", "total used free")
    monkeypatch.setattr(ev.shutil, "disk_usage", lambda p: usage(100 * 2**30, 99 * 2**30, 2**20))
    with pytest.raises(ev.DiskFull):
        站点.store.put("A", "巡检一/" + STAMP, "events.jsonl", offset=0, data=b"x", total=1)


def test_同时连接数有上限_超了的直接关(tmp_path):
    import socket
    import threading

    from d1max_site.tlsserve import TlsHandlerMixin, TlsThreadingServer
    hold = threading.Event()

    class H(TlsHandlerMixin):
        def do_GET(self):
            hold.wait(5)
            self.send_response(200)
            self.end_headers()
    srv = TlsThreadingServer(("127.0.0.1", 0), H, ctx=None, max_connections=2)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        socks = []
        for _ in range(2):
            s = socket.create_connection(srv.server_address)
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            socks.append(s)
        time.sleep(0.2)
        third = socket.create_connection(srv.server_address)
        third.settimeout(2)
        assert third.recv(10) == b"", "第三条:直接关"
        hold.set()
        for s in socks:
            assert s.recv(20).startswith(b"HTTP/1.0 200")
            s.close()
        third.close()
    finally:
        hold.set()
        srv.shutdown()
        srv.server_close()



def test_狗能产生的名字站点都收(站点, ca, tmp_path):
    """任务名里有冒号、空格(「夜巡 22:00」)、同秒第二趟 ``-2``、随机后缀、长照片名:都得传得上去、
    传完就删(W00c5d 内部评审:站点按 Windows 的规矩拒了,狗就永远传不完)。"""
    from d1max_agent.engine.archive import safe_segment
    box = Outbox(tmp_path / "dog", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: NOW)
    mission = safe_segment("夜巡 22:00 ?*")
    runs = []
    for stamp in ("20260925T010000Z-2", "20260925T010000Z-123456"):
        run = box.runs_root / mission / stamp
        (run / "photos").mkdir(parents=True)
        (run / "events.jsonl").write_text("{}\n")
        (run / "photos" / ("点位" * 38 + "__front__x.jpg")).write_bytes(b"jpg")
        (run / "manifest.json").write_text(json.dumps({"summary": {"result": "done"}}))
        runs.append(run)
    _跑(box, 3)
    assert not any(r.exists() for r in runs), "都传上去、都删了"
    assert sorted(r["stamp"] for r in 站点.store.runs(robot_id="A")) == \
        ["20260925T010000Z-123456", "20260925T010000Z-2"]
    box.close()


def test_站点永远不收的_狗隔离不再重试_这一趟留着_站点出告警(站点, ca, tmp_path):
    got = []
    站点.intake.on_refused = lambda *a: got.append(a)
    clock = {"ms": NOW}
    box = Outbox(tmp_path / "dog", cap_bytes=2**30, sink=_sink(站点, ca, ca.a), sn="A",
                 now_ms=lambda: clock["ms"])
    run = _一趟(box.root)
    (run / "photos" / "坏\x01名.jpg").write_bytes(b"x")     # 站点永远不收的名字
    for _ in range(10):
        box.step()
        clock["ms"] += 600_000                                # 十分钟一拍:按老规矩早该重试好几次了
    assert len(got) == 1 and "控制字符" in got[0][3], "隔离了:不再重试"
    assert run.exists(), "没全确认:这一趟留在狗上"
    items = {i.key.split("/")[-1]: i for i in box.queue.all()}
    assert items["坏\x01名.jpg"].refused and items["events.jsonl"].done
    box.close()
