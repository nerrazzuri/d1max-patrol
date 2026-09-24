"""上传器:切块、前缀哈希、回执核对。spec §4.1/§4.2。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from d1max_agent.engine.upload_queue import UploadQueue
from d1max_agent.engine.uploader import (
    PutReceipt,
    PutRequest,
    SinkError,
    Step,
    Uploader,
    sha256_prefix,
)


class 假服务器:
    """把收到的块拼起来,像真服务器那样**自己重算**落盘内容的 sha256。

    ``坏掉的哈希`` 打开时,它回一个对不上的哈希 —— 模拟"服务器那边落盘落坏了"。
    ``抛`` 打开时,它抛 SinkError —— 模拟断网。
    """

    def __init__(self) -> None:
        self.盘: dict[str, bytearray] = {}
        self.收到: list[PutRequest] = []
        self.坏掉的哈希 = False
        self.抛 = False

    def put(self, req: PutRequest) -> PutReceipt:
        if self.抛:
            raise SinkError("断网了")
        self.收到.append(req)
        key = f"{req.sn}/{req.run}/{req.rel}"
        buf = self.盘.setdefault(key, bytearray())
        if req.offset > len(buf):
            # 服务器比我们以为的少 —— 告诉它我这儿到底有多少,让狗自己退回去。
            return PutReceipt(ok=False, stored=len(buf), sha256="", message="偏移对不上")
        del buf[req.offset :]
        buf.extend(req.data)
        if self.坏掉的哈希:
            return PutReceipt(ok=True, stored=len(buf), sha256="0" * 64)
        return PutReceipt(ok=True, stored=len(buf), sha256=hashlib.sha256(buf).hexdigest())


@pytest.fixture
def 一趟(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    (run / "photos").mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b'{"kind": "started"}\n')
    (run / "telemetry.jsonl").write_bytes(b'{"v": 1}\n')
    (run / "state.json").write_bytes(b"{}")
    (run / "photos" / "P1__front__20260911T101500Z.jpg").write_bytes(b"\xff\xd8" + b"x" * 500)
    return tmp_path / "runs"


def 造(runs_root: Path, tmp_path: Path, sink: 假服务器) -> Uploader:
    q = UploadQueue(tmp_path / "queue.jsonl")
    return Uploader(runs_root, q, sink, sn="D1MAX-TEST-01", rand=lambda: 0.0)


def test_前缀哈希只算前n个字节(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"abcdefgh")
    assert sha256_prefix(p, 3) == hashlib.sha256(b"abc").hexdigest()
    assert sha256_prefix(p, 8) == hashlib.sha256(b"abcdefgh").hexdigest()
    assert sha256_prefix(p, 0) == hashlib.sha256(b"").hexdigest()


def test_scan_把该传的都入队_state_json除外(一趟: Path, tmp_path: Path) -> None:
    sink = 假服务器()
    up = 造(一趟, tmp_path, sink)
    assert up.scan() == 3
    keys = {i.key for i in up.queue.all()}
    assert keys == {
        "巡检一/20260911T101500Z/events.jsonl",
        "巡检一/20260911T101500Z/telemetry.jsonl",
        "巡检一/20260911T101500Z/photos/P1__front__20260911T101500Z.jpg",
    }


def test_scan_没结束的一趟照样传(tmp_path: Path) -> None:
    """§4.1:RunArchive.finish() 有可能永远不被调用。没有 manifest.json、
    没有 report.md 的半趟,是最需要传上去的那一趟。
    """
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b"half\n")
    sink = 假服务器()
    up = 造(tmp_path / "runs", tmp_path, sink)
    assert up.scan() == 1


def test_一步一步传完事件流_哈希对上才算完(一趟: Path, tmp_path: Path) -> None:
    sink = 假服务器()
    up = 造(一趟, tmp_path, sink)
    up.scan()
    step = up.run_once(now_ms=0)
    assert step.key == "巡检一/20260911T101500Z/events.jsonl"
    assert step.action == "done"
    assert sink.盘["D1MAX-TEST-01/巡检一/20260911T101500Z/events.jsonl"] == bytearray(
        b'{"kind": "started"}\n'
    )


def test_先传事件流再传照片最后遥测(一趟: Path, tmp_path: Path) -> None:
    """spec §4.3 四级优先。这里没有告警文件,所以从第 2 级开始。"""
    sink = 假服务器()
    up = 造(一趟, tmp_path, sink)
    up.scan()
    走过的 = []
    for _ in range(3):
        走过的.append(up.run_once(now_ms=0).key.rsplit("/", 1)[-1])
    assert 走过的 == ["events.jsonl", "P1__front__20260911T101500Z.jpg", "telemetry.jsonl"]
    assert up.run_once(now_ms=0).action == "idle"


def test_哈希对不上就整个重来_不算传成功(一趟: Path, tmp_path: Path) -> None:
    """§4.2 的全部要害。服务器说 ok=True,我们照样判它没传成功。"""
    sink = 假服务器()
    sink.坏掉的哈希 = True
    up = 造(一趟, tmp_path, sink)
    up.scan()
    step = up.run_once(now_ms=0)
    assert step.action == "rewound"
    item = up.queue.get("巡检一/20260911T101500Z/events.jsonl")
    assert item.offset == 0
    assert item.done is False
    assert item.attempts == 1


def test_断网就退避_不放弃(一趟: Path, tmp_path: Path) -> None:
    sink = 假服务器()
    sink.抛 = True
    up = 造(一趟, tmp_path, sink)
    up.scan()
    step = up.run_once(now_ms=1_000)
    assert step.action == "deferred"
    item = up.queue.get("巡检一/20260911T101500Z/events.jsonl")
    assert item.attempts == 1
    assert item.next_ms == 1_000 + 5_000
    assert item.done is False
    # 没到点就轮不到它,但下一级(照片)照样走 —— 一条堵住不许堵住全部。
    assert up.run_once(now_ms=1_000).key.endswith(".jpg")


def test_run目录还在_单个文件没了就退避_不销账(一趟: Path, tmp_path: Path) -> None:
    """修复轮 1:run 目录还在,单独这一个文件不见了不是 retention 的形状,是异常。

    **退避,不许销账。** 这条会一直排着、``backlog()`` 不归零 —— 这正是 §4.3
    要的那一侧:让值守屏上看得见一个不降的积压,好过悄悄丢掉一份证据。
    (修复前的版本在这里判 ``gone`` 直接销账,而 ``UploadQueue.offer()`` 的规矩
    是"新 size <= 老 size 就不重开",文件其实没动过大小的话以后永远捡不回来。)
    """
    sink = 假服务器()
    up = 造(一趟, tmp_path, sink)
    up.scan()
    backlog_before = up.backlog()
    (一趟 / "巡检一" / "20260911T101500Z" / "events.jsonl").unlink()
    step = up.run_once(now_ms=0)
    assert step.action == "deferred"
    assert up.queue.get("巡检一/20260911T101500Z/events.jsonl").done is False
    assert up.backlog() == backlog_before


def test_runs_root整个不在就退避_不销账(一趟: Path, tmp_path: Path) -> None:
    """修复轮 1:盘没挂上(``runs_root`` 短暂掉线或者还没 mount 完)不是水位线删除,

    最现实的触发场景不是杀毒软件,是这一卷短暂掉线。**退避,绝不销账** —— 盘一旦
    回来,这一条还得在队列里等着。
    """
    sink = 假服务器()
    up = 造(一趟, tmp_path, sink)
    up.scan()
    backlog_before = up.backlog()
    key = "巡检一/20260911T101500Z/events.jsonl"
    up.runs_root = tmp_path / "runs-掉线了"
    step = up.run_once(now_ms=0)
    assert step.action == "deferred"
    assert up.queue.get(key).done is False
    assert up.backlog() == backlog_before


def test_run目录整个被删就销账(tmp_path: Path) -> None:
    """修复轮 1:``retention.py`` 是整趟 ``rmtree`` 删的 —— 这才是水位线真的

    删掉了它,才允许 ``finish()`` 销账。
    """
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b"half\n")
    sink = 假服务器()
    up = 造(tmp_path / "runs", tmp_path, sink)
    up.scan()
    shutil.rmtree(run)
    step = up.run_once(now_ms=0)
    assert step.action == "gone"
    assert up.queue.get("巡检一/20260911T101500Z/events.jsonl").done is True


def test_续传_只发没发过的那一段(tmp_path: Path) -> None:
    """§4.1 按字节偏移续传。第二轮发出去的块,offset 必须接着第一轮。"""
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    p = run / "events.jsonl"
    p.write_bytes(b"A" * 10)
    sink = 假服务器()
    up = 造(tmp_path / "runs", tmp_path, sink)
    up.scan()
    assert up.run_once(now_ms=0).action == "done"

    p.write_bytes(b"A" * 10 + b"B" * 7)
    up.scan()
    assert up.run_once(now_ms=0).action == "done"
    assert [(r.offset, len(r.data)) for r in sink.收到] == [(0, 10), (10, 7)]
    落盘 = sink.盘["D1MAX-TEST-01/巡检一/20260911T101500Z/events.jsonl"]
    assert bytes(落盘) == b"A" * 10 + b"B" * 7


def test_服务器说它那儿更少就退回去重发(tmp_path: Path) -> None:
    """服务器换过盘、回滚过备份。**以服务器说的为准** —— 它才是要长期存的那一份。"""
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b"A" * 10)
    sink = 假服务器()
    up = 造(tmp_path / "runs", tmp_path, sink)
    up.scan()
    up.queue.advance("巡检一/20260911T101500Z/events.jsonl", offset=6)
    step = up.run_once(now_ms=0)
    assert step.action == "rewound"
    assert up.queue.get("巡检一/20260911T101500Z/events.jsonl").offset == 0


def test_大文件分块(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z" / "photos"
    run.mkdir(parents=True)
    (run / "P1.jpg").write_bytes(b"z" * (1_048_576 + 5))
    sink = 假服务器()
    up = 造(tmp_path / "runs", tmp_path, sink)
    up.scan()
    assert up.run_once(now_ms=0).action == "sent"
    assert up.run_once(now_ms=0).action == "done"
    assert [len(r.data) for r in sink.收到] == [1_048_576, 5]


def test_空队列返回idle(tmp_path: Path) -> None:
    (tmp_path / "runs").mkdir()
    up = 造(tmp_path / "runs", tmp_path, 假服务器())
    assert up.run_once(now_ms=0) == Step(key="", action="idle", detail="队列空")


# ---- 第二次读文件,跟第一次一个待遇 ----


class 传完就把盘抽走的服务器(假服务器):
    """``put()`` 正常返回,但在返回之前动一下盘。

    模拟的是**第一次读和第二次读之间**那个窗口:``run_once()`` 里读文件读了
    两次,中间隔着一整个来回的网络请求 —— 几十秒里 U 盘可以被拔掉、归档盘可以
    掉线、水位线可以把整趟删掉。第二次读照样会抛 ``OSError``,它必须跟第一次
    一个待遇,走 ``_missing()`` 那三分支,而不是直接穿出 ``run_once()``。
    """

    def __init__(self, 动手) -> None:
        super().__init__()
        self.动手 = 动手

    def put(self, req: PutRequest) -> PutReceipt:
        回执 = super().put(req)
        self.动手()
        return 回执


def test_第二次读_文件在发包期间没了就退避_不销账(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b"A" * 10)
    sink = 传完就把盘抽走的服务器(lambda: (run / "events.jsonl").unlink())
    up = 造(tmp_path / "runs", tmp_path, sink)
    up.scan()
    step = up.run_once(now_ms=0)
    assert step.action == "deferred"
    assert up.queue.get("巡检一/20260911T101500Z/events.jsonl").done is False
    assert up.backlog() == 1


def test_第二次读_整趟在发包期间被水位线删了就销账(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b"A" * 10)
    sink = 传完就把盘抽走的服务器(lambda: shutil.rmtree(run))
    up = 造(tmp_path / "runs", tmp_path, sink)
    up.scan()
    step = up.run_once(now_ms=0)
    assert step.action == "gone"
    assert up.queue.get("巡检一/20260911T101500Z/events.jsonl").done is True


def test_第二次读_归档盘在发包期间掉了就退避_不销账(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b"A" * 10)
    盒: list = []
    sink = 传完就把盘抽走的服务器(lambda: shutil.rmtree(盒[0].runs_root))
    up = 造(tmp_path / "runs", tmp_path, sink)
    盒.append(up)
    up.scan()
    step = up.run_once(now_ms=0)
    assert step.action == "deferred"
    assert "归档盘不在" in step.detail
    assert up.queue.get("巡检一/20260911T101500Z/events.jsonl").done is False


def test_scan_改过的报告会被重新入队(一趟: Path, tmp_path: Path) -> None:
    """W02 端到端:传完 → 报告被判读重写成同样大小 → 下一轮 scan 重新打开、从 0 传。"""
    import os
    sink = 假服务器()
    up = 造(一趟, tmp_path, sink)
    (一趟 / "巡检一" / "20260911T101500Z" / "report.md").write_bytes(b"OLD REPORT")
    up.scan()
    key = "巡检一/20260911T101500Z/report.md"
    for _ in range(20):
        up.run_once(now_ms=1_000)
        if up.queue.get(key).done:
            break
    assert up.queue.get(key).done is True
    p = 一趟 / "巡检一" / "20260911T101500Z" / "report.md"
    p.write_bytes(b"NEW REPORT")                       # 同样 10 个字节
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert up.scan() == 0                              # 不是新条目
    item = up.queue.get(key)
    assert (item.done, item.offset) == (False, 0)
