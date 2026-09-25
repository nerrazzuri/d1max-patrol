"""W00c5d:发件箱(决策 8)。一趟的文件边跑边传;站点确认一整趟都收到了、这一趟也结束了,狗上就删掉;
没确认的一个字节都不删。盘况(水位、积压)随遥测上站点。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from d1max_agent.engine.uploader import PutReceipt, SinkError
from d1max_agent.outbox import Outbox

STAMP = "20260925T010000Z"


class 假站点:
    """按块收、按自己存下的字节回哈希(跟真站点一个规矩)。``down`` 为真时发不出去。"""

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytearray] = {}
        self.down = False
        self.puts = 0

    def put(self, req):
        if self.down:
            raise SinkError("站点连不上")
        self.puts += 1
        buf = self.files.setdefault((req.run, req.rel), bytearray())
        if req.offset > len(buf):
            return PutReceipt(ok=True, stored=len(buf), sha256=hashlib.sha256(buf).hexdigest())
        del buf[req.offset:]
        buf += req.data
        return PutReceipt(ok=True, stored=len(buf), sha256=hashlib.sha256(buf).hexdigest())


def _一趟(root: Path, mission="巡检一", stamp=STAMP, *, finished=True, photos=2) -> Path:
    run = root / "runs" / mission / stamp
    (run / "photos").mkdir(parents=True)
    (run / "events.jsonl").write_text('{"kind":"start"}\n')
    (run / "state.json").write_text("{}")                     # 本地恢复用,不传
    for i in range(photos):
        (run / "photos" / f"P{i}__front.jpg").write_bytes(os.urandom(3000 + i))
    (run / "manifest.json").write_text(json.dumps(
        {"fingerprint": {}, "summary": {"result": "done"} if finished else {}}))
    return run


@pytest.fixture
def 箱(tmp_path):
    site = 假站点()
    clock = {"ms": int(time.time() * 1000)}
    box = Outbox(tmp_path / "outbox", cap_bytes=10 * 2**20, sink=site, sn="A",
                 now_ms=lambda: clock["ms"])
    yield box, site, clock
    box.close()


def _跑到空(box, clock, n=200):
    for _ in range(n):
        box.step()
        clock["ms"] += 1000


def test_传完而且一趟结束了_狗上就删掉(箱):
    box, site, clock = 箱
    run = _一趟(box.root)
    _跑到空(box, clock, 5)
    assert not run.exists(), "站点确认了、这一趟结束了:狗上不留"
    assert ("巡检一/" + STAMP, "photos/P1__front.jpg") in site.files
    assert ("巡检一/" + STAMP, "state.json") not in site.files, "state.json 不传"
    assert box.queue.all() == [], "删掉的一趟,队列里也不留"


def test_没结束的一趟不删_正在写的那一趟不删(箱):
    box, site, clock = 箱
    run = _一趟(box.root, finished=False)
    live = _一趟(box.root, stamp="20260925T020000Z")
    box.active = lambda: {live}
    _跑到空(box, clock, 5)
    assert run.exists() and live.exists()
    assert ("巡检一/" + STAMP, "events.jsonl") in site.files, "没结束也边跑边传"


def test_断网期间不删_恢复后续传再删(箱):
    box, site, clock = 箱
    site.down = True
    run = _一趟(box.root)
    _跑到空(box, clock, 5)
    assert run.exists() and site.files == {}
    f = box.facts()
    assert f.backlog_files == 4 and f.backlog_bytes > 6000
    assert f.oldest_backlog_s is not None
    site.down = False
    _跑到空(box, clock, 400)                          # 退避最长 5 min
    assert not run.exists()
    assert box.facts().backlog_files == 0 and box.facts().oldest_backlog_s is None


def test_传完之后又改了的文件_重新传完才删(箱):
    box, site, clock = 箱
    run = _一趟(box.root, finished=False)
    _跑到空(box, clock, 3)
    site.down = True
    (run / "events.jsonl").write_text('{"kind":"start"}\n{"kind":"end"}\n')
    m = json.loads((run / "manifest.json").read_text())
    (run / "manifest.json").write_text(json.dumps(m | {"summary": {"result": "done"}}))
    _跑到空(box, clock, 3)
    assert run.exists(), "新写的还没传上去"
    site.down = False
    _跑到空(box, clock, 400)
    assert not run.exists()
    assert bytes(site.files[("巡检一/" + STAMP, "events.jsonl")]).count(b"\n") == 2


def test_崩在半路的残骸_过了安定期传完就删(箱):
    box, site, clock = 箱
    run = _一趟(box.root, stamp="20200101T000000Z", finished=False)
    _跑到空(box, clock, 5)
    assert not run.exists()


def test_不认识的目录不碰(箱):
    box, site, clock = 箱
    odd = box.root / "runs" / "巡检一" / "not-a-stamp"
    odd.mkdir(parents=True)
    (odd / "events.jsonl").write_text("x\n")
    _跑到空(box, clock, 3)
    assert odd.exists()


def test_盘况_发件箱多大_满没满(箱, monkeypatch):
    box, site, clock = 箱
    site.down = True
    _一趟(box.root, photos=3)
    box.step()
    f = box.facts()
    assert 0.0 <= f.disk_used_ratio <= 1.0
    assert f.outbox_bytes >= 9000 and f.outbox_cap_bytes == 10 * 2**20
    assert not f.full()
    small = Outbox(box.root.parent / "small", cap_bytes=5000, sink=site, sn="A",
                   now_ms=lambda: clock["ms"])
    _一趟(small.root, photos=3)
    small.step()
    assert small.facts().full(), "发件箱到上限算满"
    small.close()


def test_狗传的跟站点收的是同一张单子():
    """代理的上传白名单与站点的接收白名单:狗在代理模式下产生的文件,站点都得收(不然永远传不完、
    永远删不掉);站点不收的(判读、复核、报告)代理模式下根本不产生。"""
    from d1max_agent.engine.upload_queue import classify
    from d1max_contract.intake import dog_may_upload
    for rel in ("manifest.json", "events.jsonl", "telemetry.jsonl", "photos/P1__front__t.jpg"):
        assert classify(rel) is not None and dog_may_upload(rel), rel


def test_传完之后原地改写_大小没变_下一次扫盘之前也不删(箱):
    """队列里记着「传完了」,但盘上那份已经被原地改写(大小一样,修改时间变了),扫盘还没来得及看见:
    这时候删,新写的那份就丢了。"""
    box, site, clock = 箱
    run = _一趟(box.root, finished=False)
    _跑到空(box, clock, 3)
    assert run.exists()
    m = run / "manifest.json"
    new = json.dumps({"fingerprint": {}, "summary": {"r": 1}})
    short = json.dumps({"fingerprint": {}, "summary": {}})
    old = short[:-1] + " " * (len(new) - len(short)) + "}"   # 没跑完的清单,补空白到同样长
    assert len(old) == len(new) and json.loads(old)["summary"] == {}
    m.write_text(old)
    _跑到空(box, clock, 20)                           # 传上去了,但没跑完,不删
    assert run.exists()
    import os as _os
    m.write_text(new)
    st = m.stat()
    _os.utime(m, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert m.stat().st_size == len(old)
    box._prune()                                     # 扫盘之前
    assert run.exists(), "盘上的已经不是传上去的那一份了"
