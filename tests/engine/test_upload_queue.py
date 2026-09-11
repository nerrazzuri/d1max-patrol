"""盘上上传队列:分级、重放、去重。spec §4.3。"""

from __future__ import annotations

from pathlib import Path

import pytest

from d1max_patrol.engine.upload_queue import (
    PRIORITY_ALERT,
    PRIORITY_EVENTS,
    PRIORITY_PHOTO,
    PRIORITY_TELEMETRY,
    QueueItem,
    UploadQueue,
    backoff_ms,
    classify,
)


@pytest.mark.parametrize(
    ("rel", "want"),
    [
        ("alerts.jsonl", PRIORITY_ALERT),
        ("events.jsonl", PRIORITY_EVENTS),
        ("manifest.json", PRIORITY_EVENTS),
        ("photos/P1__front__20260911T101500Z.jpg", PRIORITY_PHOTO),
        ("report.md", PRIORITY_PHOTO),
        ("telemetry.jsonl", PRIORITY_TELEMETRY),
    ],
)
def test_四级优先按文件名分(rel: str, want: int) -> None:
    assert classify(rel) == want


def test_state_json_不入队() -> None:
    """崩溃恢复是本地用的,传上去没有意义,还会每次状态迁移都重传一遍。"""
    assert classify("state.json") is None


def test_没见过的文件名不入队() -> None:
    """白名单,不是黑名单。将来加文件要有人显式决定它是几级。"""
    assert classify("随便什么.bin") is None


def test_退避封顶五分钟() -> None:
    """spec §4.3 定死的。抖动只许把它往下拉,不许越顶。"""
    for attempts in range(1, 40):
        assert backoff_ms(attempts, rand=lambda: 1.0) <= 300_000


def test_退避不放弃() -> None:
    """**不做"重试 N 次后放弃"** —— 放弃意味着悄悄丢证据。
    所以这个函数没有"返回 None 表示别再试了"这条出口。
    """
    assert backoff_ms(9999, rand=lambda: 0.5) > 0


def test_offer_同一个key第二次返回False(tmp_path: Path) -> None:
    q = UploadQueue(tmp_path / "queue.jsonl")
    assert q.offer("run-a/events.jsonl", PRIORITY_EVENTS, size=100) is True
    assert q.offer("run-a/events.jsonl", PRIORITY_EVENTS, size=400) is False
    assert q.backlog() == 1
    q.close()


def test_offer_长了的追加流会把size改大(tmp_path: Path) -> None:
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("run-a/events.jsonl", PRIORITY_EVENTS, size=100)
    q.offer("run-a/events.jsonl", PRIORITY_EVENTS, size=400)
    item = q.get("run-a/events.jsonl")
    assert item is not None
    assert item.size == 400
    q.close()


def test_offer_长了也不许把offset抹掉(tmp_path: Path) -> None:
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("run-a/events.jsonl", PRIORITY_EVENTS, size=100)
    q.advance("run-a/events.jsonl", offset=100)
    q.offer("run-a/events.jsonl", PRIORITY_EVENTS, size=400)
    assert q.get("run-a/events.jsonl").offset == 100
    q.close()


def test_重放_断电后队列还在(tmp_path: Path) -> None:
    """这条是这个模块存在的全部理由。"""
    path = tmp_path / "queue.jsonl"
    q = UploadQueue(path)
    q.offer("run-a/events.jsonl", PRIORITY_EVENTS, size=100)
    q.offer("run-a/photos/P1.jpg", PRIORITY_PHOTO, size=9000)
    q.close()

    again = UploadQueue(path)
    assert again.backlog() == 2
    assert again.get("run-a/photos/P1.jpg").size == 9000
    again.close()


def test_重放_后写的那行赢(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    q = UploadQueue(path)
    q.offer("k", PRIORITY_PHOTO, size=1)
    q.offer("k", PRIORITY_PHOTO, size=2)
    q.close()
    assert UploadQueue(path).get("k").size == 2


def test_重放_容得下半行(tmp_path: Path) -> None:
    """断电正好断在写一行的中间。半行丢掉,前面的行一个不许少。"""
    path = tmp_path / "queue.jsonl"
    q = UploadQueue(path)
    q.offer("好的", PRIORITY_PHOTO, size=1)
    q.close()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"key": "半行", "prior')

    again = UploadQueue(path)
    assert again.backlog() == 1
    assert again.get("好的") is not None
    assert again.get("半行") is None
    again.close()


def test_pending_按优先级再按入队顺序(tmp_path: Path) -> None:
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("c/photos/x.jpg", PRIORITY_PHOTO, size=1)
    q.offer("d/telemetry.jsonl", PRIORITY_TELEMETRY, size=1)
    q.offer("a/events.jsonl", PRIORITY_EVENTS, size=1)
    q.offer("b/events.jsonl", PRIORITY_EVENTS, size=1)
    q.offer("e/alerts.jsonl", PRIORITY_ALERT, size=1)
    keys = [i.key for i in q.pending(now_ms=0)]
    assert keys == [
        "e/alerts.jsonl",
        "a/events.jsonl",
        "b/events.jsonl",
        "c/photos/x.jpg",
        "d/telemetry.jsonl",
    ]
    q.close()


def test_pending_跳过还在退避里的(tmp_path: Path) -> None:
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("a/events.jsonl", PRIORITY_EVENTS, size=1)
    q.defer("a/events.jsonl", next_ms=10_000)
    assert q.pending(now_ms=9_999) == []
    assert [i.key for i in q.pending(now_ms=10_000)] == ["a/events.jsonl"]
    q.close()


def test_advance_把attempts归零(tmp_path: Path) -> None:
    """有进展就不该继续退避 —— 传了一半断了,恢复之后不能还罚站五分钟。"""
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("a/events.jsonl", PRIORITY_EVENTS, size=100)
    q.defer("a/events.jsonl", next_ms=99_999)
    q.advance("a/events.jsonl", offset=50)
    item = q.get("a/events.jsonl")
    assert (item.attempts, item.next_ms, item.offset) == (0, 0, 50)
    q.close()


def test_rewind_把offset清零(tmp_path: Path) -> None:
    """哈希对不上 —— 之前传的那些全不算数,从头再来。"""
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("a/photos/x.jpg", PRIORITY_PHOTO, size=100)
    q.advance("a/photos/x.jpg", offset=100)
    q.rewind("a/photos/x.jpg")
    assert q.get("a/photos/x.jpg").offset == 0
    q.close()


def test_done_不再出现在pending里_也不算积压(tmp_path: Path) -> None:
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("a/events.jsonl", PRIORITY_EVENTS, size=1)
    q.finish("a/events.jsonl")
    assert q.pending(now_ms=0) == []
    assert q.backlog() == 0
    q.close()


def test_done的追加流长了会重新打开(tmp_path: Path) -> None:
    """传完一轮之后巡检还在往 events.jsonl 里写。那是新证据,不是重复。"""
    q = UploadQueue(tmp_path / "queue.jsonl")
    q.offer("a/events.jsonl", PRIORITY_EVENTS, size=100)
    q.advance("a/events.jsonl", offset=100)
    q.finish("a/events.jsonl")
    assert q.backlog() == 0
    q.offer("a/events.jsonl", PRIORITY_EVENTS, size=400)
    item = q.get("a/events.jsonl")
    assert (item.done, item.size, item.offset) == (False, 400, 100)
    assert q.backlog() == 1
    q.close()


def test_紧凑之后重放结果不变_而且文件变小(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    q = UploadQueue(path)
    q.offer("a/events.jsonl", PRIORITY_EVENTS, size=1)
    for n in range(2, 60):
        q.offer("a/events.jsonl", PRIORITY_EVENTS, size=n)
    q.close()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) < 60
    again = UploadQueue(path)
    assert again.backlog() == 1
    assert again.get("a/events.jsonl").size == 59
    again.close()


def test_wire_往返() -> None:
    item = QueueItem(key="k", priority=2, offset=5, size=9, attempts=1, next_ms=7, done=False)
    assert QueueItem.from_wire(item.to_wire()) == item
