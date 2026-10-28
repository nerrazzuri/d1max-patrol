"""W40/W41 外审 R1(2026-10-28):警灯换色、打闪时一路失败不许混出别的颜色、不许留半个颜色。"""

from __future__ import annotations

import contextlib

import pytest


class _坏板:
    """两路线圈的假继电器板;``fail(coil, on)`` 为真就抛。记下每次写完板子的状态。"""

    def __init__(self):
        self.state = {0: False, 1: False}
        self.fail = lambda coil, on: False
        self.history = []

    def write_coil(self, coil, on):
        from d1max_adapter_d1max.payload import PayloadError
        if self.fail(coil, on):
            raise PayloadError("注入:线圈写失败")
        self.state[coil] = on
        self.history.append(dict(self.state))

    def close(self):
        pass


def _红绿(relay, blink=0.0):
    from d1max_adapter_d1max.payload import Payload, PayloadConfig
    return Payload(PayloadConfig(coils={"red": 0, "green": 1},
                                 strobe_colors={"red": ("red",), "green": ("green",),
                                                "yellow": ("red", "green")},
                                 strobe_default="red", blink_s=blink), relay=relay)


async def test_外审R1_旧颜色关不掉_不开新颜色_不混色():
    from d1max_adapter_d1max.payload import PayloadError
    r = _坏板()
    p = _红绿(r)
    await p.start()
    try:
        await p.set("strobe", True, 60, color="red")
        r.fail = lambda coil, on: coil == 0 and not on          # 红关不掉
        r.history.clear()
        with pytest.raises(PayloadError):
            await p.set("strobe", True, 60, color="green")
        assert not any(s[0] and s[1] for s in r.history), r.history
        assert r.state[1] is False, "绿不许开"
        assert p._until["green"] is None, "这一下要开的当「要关」"
    finally:
        r.fail = lambda *a: False
        await p.close()


async def test_外审R1_打闪重新亮的时候一路没开成_整组撤掉_不留半个颜色():
    import asyncio
    r = _坏板()
    p = _红绿(r, blink=0.5)
    await p.start()
    try:
        await p.set("strobe", True, 60, color="yellow")
        p._guard.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await p._guard
        loop = asyncio.get_running_loop()
        p._blink_from = loop.time() - 0.6                 # 灭的半拍
        for c in p.cfg.coils:
            await p._reconcile(c)
        assert r.state == {0: False, 1: False}
        r.fail = lambda coil, on: coil == 1 and on         # 亮的半拍:绿开不了
        p._blink_from = loop.time()
        for c in p.cfg.coils:
            await p._reconcile(c)
        assert all(p._until[c] is None for c in p.cfg.coils), p._until
        for c in p.cfg.coils:                             # 下一拍:整组关掉
            await p._reconcile(c)
        assert r.state == {0: False, 1: False}, r.state
    finally:
        r.fail = lambda *a: False
        await p.close()
