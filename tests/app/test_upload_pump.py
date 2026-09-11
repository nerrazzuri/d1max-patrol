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


# ---- 返修第 1 轮:日志这条路自己抛的时候 ----


class _坏流:
    """底下的流 write() 自己抛 RecursionError —— logging 对 RecursionError

    是 ``except RecursionError: raise``,不走 handleError,``logging.raiseExceptions``
    那套兜不住。这是"日志这条路自己炸了"的替身。
    """

    def write(self, s: str) -> None:
        raise RecursionError("模拟 handler 自己抛")

    def flush(self) -> None:
        pass


def _装一个坏handler():
    import logging

    logger = logging.getLogger("d1max_patrol.app.upload_pump")
    旧 = (logger.handlers[:], logger.propagate, logger.level)
    logger.handlers = [logging.StreamHandler(_坏流())]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, 旧


def _还原坏handler(logger, 旧) -> None:
    handlers, propagate, level = 旧
    logger.handlers = handlers
    logger.propagate = propagate
    logger.setLevel(level)


def test_日志handler自己抛的时候线程不死_last_error写下来了(tmp_path: Path) -> None:
    """兜底门(第二道)自己第一句就是 log.exception —— 日志这条路自己抛的时候,

    没有任何人接。这条测的是修完之后:状态先落、日志后记,线程不许死。
    """
    import threading
    import time

    up, pump = 装配(tmp_path, 抛意外的服务器(ValueError("回执里的字段变成了火星文")))
    logger, 旧 = _装一个坏handler()
    try:
        pump.start()
        for _ in range(50):
            if pump.stats().last_error:
                break
            time.sleep(0.05)
        assert pump._thread is not None
        assert pump._thread.is_alive()
        assert pump.stats().last_error != ""
    finally:
        pump.stop(timeout_s=2.0)
        _还原坏handler(logger, 旧)
    # 收尾干净:stop() 已经把线程收回去了,别给后面的测试留一条挂着的线程。
    assert not any(t.name == "d1max-upload" for t in threading.enumerate())


def test_第一道门里日志抛了_退避照样打上(tmp_path: Path) -> None:
    """(b) 那处顺序:先退避重排,再记日志。日志自己抛(handler 坏了)的话,

    不能把退避这一步跳过去 —— 这条测的就是这个顺序,不是"tick 不许抛"。
    """
    up, pump = 装配(tmp_path, 抛意外的服务器(ValueError("回执里的字段变成了火星文")))
    logger, 旧 = _装一个坏handler()
    try:
        with pytest.raises(RecursionError):
            pump.tick()
    finally:
        _还原坏handler(logger, 旧)

    条 = up.queue.get("巡检一/20260911T101500Z/events.jsonl")
    assert 条.attempts == 1
    assert 条.next_ms > 0


def test_stop超时没收回线程就不清句柄_start不会起第二条(tmp_path: Path) -> None:
    """join 超时之后如果照样把 _thread 清成 None, 下一次 start() 就会起出

    第二条线程,两条一起往非线程安全的 queue.jsonl 里写。
    """
    import threading

    卡住了 = threading.Event()
    放行 = threading.Event()

    class 会卡住的服务器:
        def put(self, req):
            卡住了.set()
            放行.wait()
            raise SinkError("放行之后照样失败,反正测的是 stop 的行为")

    up, pump = 装配(tmp_path, 会卡住的服务器())
    try:
        pump.start()
        assert 卡住了.wait(timeout=5.0)
        pump.stop(timeout_s=0.05)
        # 没收回来:句柄不许清。
        assert pump._thread is not None

        before = sum(
            1 for t in threading.enumerate() if t.name == "d1max-upload" and t.is_alive()
        )
        assert before == 1

        pump.start()
        after = sum(
            1 for t in threading.enumerate() if t.name == "d1max-upload" and t.is_alive()
        )
        # 句柄没清, start() 那句 `if self._thread is not None: return` 拦住了它 ——
        # 活线程数还是 1, 不是 2。
        assert after == 1
    finally:
        放行.set()
        pump.stop(timeout_s=5.0)
