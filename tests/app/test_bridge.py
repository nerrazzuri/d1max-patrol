"""HTTP 线程与 asyncio 线程之间那道门。

这些测试**故意不是 async 的**:调用方就是 HTTP 线程,同步到底。桥要是
只在 async 测试里成立,那它就没解决任何问题。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from d1max_patrol.app.bridge import LoopBridge
from d1max_patrol.backends.base import EventEmitter


@pytest.fixture
def bridge():
    b = LoopBridge()
    b.start()
    yield b
    b.stop()


async def _add(a: int, b: int) -> int:
    await asyncio.sleep(0)
    return a + b


async def _boom() -> None:
    raise ValueError("炸了")


async def _echo(value: int) -> int:
    await asyncio.sleep(0.001)
    return value


async def _emit_soon(emitter: EventEmitter[int], value: int) -> None:
    await asyncio.sleep(0.01)
    emitter.emit(value)


# ------------------------------------------------------------------ 调用


def test_从别的线程调协程拿得到结果(bridge):
    assert bridge.call(lambda: _add(1, 2)) == 3


def test_跑协程的不是调用线程(bridge):
    """这正是这道门存在的理由 —— 认错了就等于没有门。"""
    seen = {}

    async def _who():
        seen["loop"] = threading.current_thread().name

    bridge.call(lambda: _who())
    assert seen["loop"] != threading.current_thread().name


def test_协程抛的异常原样传回调用线程(bridge):
    with pytest.raises(ValueError, match="炸了"):
        bridge.call(lambda: _boom())


def test_超时了抛TimeoutError而不是永远卡住(bridge):
    with pytest.raises(TimeoutError):
        bridge.call(lambda: asyncio.sleep(10), timeout_s=0.2)


def test_超时之后那个协程会被取消(bridge):
    """不取消的话,一个卡住的请求会一直占着后端。"""
    state = {"cancelled": False}

    async def _long():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    with pytest.raises(TimeoutError):
        bridge.call(lambda: _long(), timeout_s=0.2)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not state["cancelled"]:
        time.sleep(0.02)
    assert state["cancelled"]


def test_并发调用互不干扰(bridge):
    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(lambda i: bridge.call(lambda i=i: _echo(i)), range(50)))
    assert sorted(got) == list(range(50))


def test_收的是工厂不是协程对象(bridge):
    """协程对象在 HTTP 线程里造出来就绑错了上下文 —— 这条是接口的核心约束。"""
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _add(1, 1)

    bridge.call(factory)
    assert calls["n"] == 1, "工厂应该在循环线程里被调用一次"


def test_spawn_丢过去就不管了(bridge):
    done = threading.Event()

    async def _work():
        await asyncio.sleep(0.01)
        done.set()

    bridge.spawn(lambda: _work())
    assert done.wait(2)


def test_spawn_里抛的异常不会打死循环(bridge):
    """点一下就走的动作出错,不该把整个 app 拖下水。"""
    bridge.spawn(lambda: _boom())
    time.sleep(0.05)
    assert bridge.call(lambda: _add(2, 2)) == 4


# ------------------------------------------------------------------ 订阅


def test_订阅拿得到循环里emit的事件(bridge):
    em: EventEmitter[int] = EventEmitter()
    it = bridge.subscribe(em)
    bridge.spawn(lambda: _emit_soon(em, 7))
    assert next(it) == 7


def test_两个订阅者都收得到(bridge):
    em: EventEmitter[int] = EventEmitter()
    a, b = bridge.subscribe(em), bridge.subscribe(em)
    time.sleep(0.05)          # 等两条订阅都在循环线程里挂上
    bridge.spawn(lambda: _emit_soon(em, 9))
    assert next(a) == 9 and next(b) == 9


def test_订阅按顺序给事件(bridge):
    em: EventEmitter[int] = EventEmitter()
    it = bridge.subscribe(em)
    time.sleep(0.05)

    async def _burst():
        for i in range(5):
            em.emit(i)

    bridge.spawn(lambda: _burst())
    assert [next(it) for _ in range(5)] == [0, 1, 2, 3, 4]


def test_订阅者迭代结束会退订(bridge):
    """SSE 客户端断了就得退订,否则队列越堆越大。"""
    em: EventEmitter[int] = EventEmitter()
    it = bridge.subscribe(em)
    time.sleep(0.05)
    bridge.spawn(lambda: _emit_soon(em, 1))
    assert next(it) == 1
    it.close()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and bridge.call(lambda: _count(em)) != 0:
        time.sleep(0.02)
    assert bridge.call(lambda: _count(em)) == 0, "断了的订阅者不该留在名单上"


async def _count(emitter: EventEmitter[int]) -> int:
    return len(emitter._subscribers)


def test_订阅在桥停掉之后会收尾(bridge):
    """不收尾的话 SSE 那条 HTTP 线程会永远挂在 next() 上,进程退不出去。"""
    em: EventEmitter[int] = EventEmitter()
    it = bridge.subscribe(em)
    time.sleep(0.05)
    bridge.stop()
    with pytest.raises(StopIteration):
        next(it)


# ------------------------------------------------------------------ 收尾


def test_stop之后再call会被拒(bridge):
    bridge.stop()
    with pytest.raises(RuntimeError):
        bridge.call(lambda: _add(1, 1))


def test_stop会等循环真的停掉(bridge):
    bridge.stop()
    assert not bridge.running


def test_stop两次不会炸(bridge):
    bridge.stop()
    bridge.stop()


def test_没start就stop不会炸():
    LoopBridge().stop()


def test_没start就call会被拒():
    with pytest.raises(RuntimeError):
        LoopBridge().call(lambda: _add(1, 1))


def test_start两次不会起两条线程(bridge):
    bridge.start()
    names = [t.name for t in threading.enumerate() if t.name == "d1max-loop"]
    assert len(names) == 1


def test_能当上下文管理器用():
    with LoopBridge() as b:
        assert b.call(lambda: _add(3, 4)) == 7
    assert not b.running
