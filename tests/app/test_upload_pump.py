"""上传线程壳。spec §4.3 那句"上传线程绝不阻塞巡检"。

后半段那几条是**最后一道门**的测试:``run_once()`` 抛出任何没预料到的异常,
线程都不许死。``engine/http_sink.py`` 修了五轮才把七条异常泄漏堵上,最后一条是
``MemoryError`` —— 结论不是"现在安全了",是**只要一个异常能弄死这个线程,
我们就是在赌下面没有第八条**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d1max_patrol.app.upload_pump import IDLE_SLEEP_S, UploadPump
from d1max_patrol.engine.upload_queue import BACKOFF_BASE_MS, BACKOFF_JITTER, UploadQueue
from d1max_patrol.engine.uploader import PutReceipt, SinkError, Step, Uploader


class 只会失败的服务器:
    def put(self, req):
        raise SinkError("断网了")


class 好服务器:
    def __init__(self) -> None:
        self.收到 = 0

    def put(self, req):
        import hashlib

        self.收到 += 1
        return PutReceipt(
            ok=True, stored=req.total, sha256=hashlib.sha256(req.data).hexdigest()
        )


class 抛意外的服务器:
    """抛的**不是** SinkError。这是"第八条泄漏"的替身。"""

    def __init__(self, 要抛: BaseException) -> None:
        self.要抛 = 要抛

    def put(self, req):
        raise self.要抛


def 装配(tmp_path: Path, sink, *, sleep=None) -> tuple[Uploader, UploadPump]:
    run = tmp_path / "runs" / "巡检一" / "20260911T101500Z"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_bytes(b'{"kind": "started"}\n')
    q = UploadQueue(tmp_path / "queue.jsonl")
    up = Uploader(tmp_path / "runs", q, sink, sn="D1MAX-TEST-01", rand=lambda: 0.0)
    钟 = [0]
    return up, UploadPump(up, clock=lambda: 钟[0], sleep=sleep)


def 造(tmp_path: Path, sink) -> UploadPump:
    return 装配(tmp_path, sink)[1]


def test_tick一步就把事件流传上去(tmp_path: Path) -> None:
    sink = 好服务器()
    pump = 造(tmp_path, sink)
    step = pump.tick()
    assert step.action == "done"
    assert sink.收到 == 1
    assert pump.stats().backlog == 0
    assert pump.stats().sent_files == 1


def test_断网时backlog不掉_错误留在stats里(tmp_path: Path) -> None:
    pump = 造(tmp_path, 只会失败的服务器())
    step = pump.tick()
    assert step.action == "deferred"
    assert pump.stats().backlog == 1
    assert "断网" in pump.stats().last_error


def test_第一次tick会先扫盘(tmp_path: Path) -> None:
    """新落下来的照片不用等谁来通知,pump 自己会看见。"""
    pump = 造(tmp_path, 好服务器())
    assert pump.stats().backlog == 0
    pump.tick()
    assert pump.stats().sent_files == 1


def test_start之后stop得回来_而且不留线程(tmp_path: Path) -> None:
    """**stop 要真的等线程退出。** 睡眠必须用 Event.wait 才打得断。"""
    import threading

    before = threading.active_count()
    pump = 造(tmp_path, 好服务器())
    pump.start()
    pump.stop(timeout_s=5.0)
    assert threading.active_count() == before


def test_stop之后再tick不炸(tmp_path: Path) -> None:
    pump = 造(tmp_path, 好服务器())
    pump.start()
    pump.stop()
    assert pump.tick().action in {"idle", "done"}


# ---- 最后一道门 ----


def test_run_once抛非SinkError异常时线程不死_这一项退避重排(tmp_path: Path) -> None:
    """**这一步真正的价值。**

    sink 那一层再严,也只能保证"我们现在数得出来的类型"都翻译成了 SinkError。
    pump 这一层不问出了什么事,只保证:出了任何事,这一项退避重排、循环继续、
    下一拍还来。``tick()`` 绝不把异常放出来 —— 放出来就等于线程死了。
    """
    up, pump = 装配(tmp_path, 抛意外的服务器(ValueError("回执里的字段变成了火星文")))
    step = pump.tick()

    assert step.action == "deferred"
    assert "ValueError" in step.detail
    # **值守屏上必须看得见** —— "线程不死"和"把错误吞掉"是两回事。
    assert "ValueError" in pump.stats().last_error
    assert "火星文" in pump.stats().last_error
    assert pump.stats().last_step == "deferred"
    # 退避重排之后 backlog 不许归零(spec §4.3:让值守屏上看得见一个不降的积压)。
    assert pump.stats().backlog == 1
    条 = up.queue.get("巡检一/20260911T101500Z/events.jsonl")
    assert 条.done is False
    assert 条.attempts == 1


def test_意外退避用的是backoff_ms_没新造时间常量(tmp_path: Path) -> None:
    up, pump = 装配(tmp_path, 抛意外的服务器(MemoryError("Content-Length 说有 100 GB")))
    pump.tick()
    条 = up.queue.get("巡检一/20260911T101500Z/events.jsonl")
    # backoff_ms(1) 的取值区间:基数下拉一点抖动,不越顶。
    assert BACKOFF_BASE_MS * (1 - BACKOFF_JITTER) <= 条.next_ms <= BACKOFF_BASE_MS


def test_意外之后下一拍还来_而且积压不降(tmp_path: Path) -> None:
    """出事的那一拍不许把循环带走,也不许把积压洗掉。"""
    up, pump = 装配(tmp_path, 抛意外的服务器(RuntimeError("下面第八条")))
    assert pump.tick().action == "deferred"
    # 同一个钟点:这一条还在退避里,所以这一拍没活干 —— 但它还排在队里。
    assert pump.tick().action == "idle"
    assert pump.stats().backlog == 1
    assert up.backlog() == 1


def test_扫盘自己炸了也不死(tmp_path: Path) -> None:
    """炸在 scan() 里的时候队列还是空的,没什么可退避的,但线程照样得活着。"""
    up, pump = 装配(tmp_path, 好服务器())

    def 炸() -> int:
        raise OSError("归档盘在扫到一半的时候掉了")

    up.scan = 炸
    step = pump.tick()
    assert step.action == "deferred"
    assert step.key == ""
    assert "OSError" in pump.stats().last_error


def test_KeyboardInterrupt和SystemExit不当成这一项失败(tmp_path: Path) -> None:
    """那是**有人在关进程**,不是一次上传失败。照原样往上走,让线程干净地结束。

    (顺带记一笔:CPython 只把 KeyboardInterrupt 投给主线程,pump 线程实际上
    见不到它 —— 这条测的是"真见到了也不许吞",不是"它会发生"。)
    """
    for 谁 in (KeyboardInterrupt(), SystemExit()):
        up, pump = 装配(tmp_path / type(谁).__name__, 抛意外的服务器(谁))
        with pytest.raises(type(谁)):
            pump.tick()
        # 没被当成"这一项失败了"去退避。
        assert up.queue.get("巡检一/20260911T101500Z/events.jsonl").attempts == 0


def test_loop那一层也兜住_连退避重排都炸了线程照样活(tmp_path: Path) -> None:
    """第二道门:``tick()`` 自己炸了(盘满,queue.jsonl 写不进去)也不许死。

    **不靠 time.sleep 等线程** —— 注入的 sleep 一被调用就把停止位置上,
    ``_loop()`` 在测试线程里同步跑完一圈。
    """
    箱: list[UploadPump] = []
    睡了: list[float] = []

    def 假睡(秒: float) -> None:
        睡了.append(秒)
        箱[0].stop()

    _up, pump = 装配(tmp_path, 好服务器(), sleep=假睡)
    箱.append(pump)

    打了 = []

    def 炸() -> Step:
        打了.append("tick")
        raise RuntimeError("连退避重排那一步都炸了")

    pump.tick = 炸
    pump._loop()  # 不抛就算过 —— 真跑的时候这就是"线程没死"

    assert 打了 == ["tick"]
    assert 睡了 == [IDLE_SLEEP_S]
    assert "这一拍整个失败" in pump.stats().last_error
