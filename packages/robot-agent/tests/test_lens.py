"""W46:热像镜头被挡(雨天水膜、脏东西)。画面换成造出来的灰度小图:有细节的场景 / 糊成一片。"""

from __future__ import annotations

import random

import pytest

from d1max_agent.lens import H, LensWatch, W, detail


def _景(seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(40, 220) if (x // 8 + y // 6) % 2 else rng.randrange(0, 60)
                 for y in range(H) for x in range(W))


def _糊(level: int = 128) -> bytes:
    rng = random.Random(level)
    return bytes(max(0, min(255, level + rng.randrange(-2, 3))) for _ in range(W * H))


def test_细节_有景的远大于糊的():
    assert detail(_景(1)) > 10 * max(detail(_糊()), 0.1)
    with pytest.raises(ValueError):
        detail(b"\x00" * 10)


async def _跑(w, frames, t):
    for f in frames:
        w._frame = f
        t[0] += w.every_s
        await w.step()


def _watch():
    t = [0.0]
    events: list = []
    w = LensWatch(None, emit=lambda k, d: events.append((k, d)), mono=lambda: t[0])

    async def grab():
        return w._frame
    w._grab = grab
    return w, t, events


async def test_先攒基线_连着几次糊了才报_好了再清():
    w, t, events = _watch()
    await _跑(w, [_景(i) for i in range(12)], t)
    assert events == [] and w.state == "clear"
    await _跑(w, [_糊()] * 4, t)
    assert events == [], "糊了 4 次(4 分钟):还不报,刚扫过一面白墙也会这样"
    await _跑(w, [_糊()], t)
    assert [k for k, _ in events] == ["thermal_lens_blocked"]
    assert events[0][1]["camera"] == "thermal" and events[0][1]["baseline"] > 0
    h = w.health()
    assert h["id"] == "lens:thermal" and h["state"] == "degraded" and "挡" in h["detail"]
    await _跑(w, [_糊()] * 5, t)
    assert len(events) == 1, "一直挡着只报一次"
    await _跑(w, [_景(50), _景(51)], t)
    assert len(events) == 1, "好了两次还不清"
    await _跑(w, [_景(52)], t)
    assert [k for k, _ in events][-1] == "thermal_lens_clear"
    assert w.health()["state"] == "online"


async def test_基线没攒够不判_糊的不进基线():
    w, t, events = _watch()
    await _跑(w, [_糊()] * 20, t)
    assert events == [] and w.health()["state"] == "online" and "基线" in w.health()["detail"]
    await _跑(w, [_景(i) for i in range(12)], t)
    assert w.baseline() > 10, "糊的那 20 张不算进基线"


async def test_取不到图_不算被挡_报取不到():
    w, t, events = _watch()

    async def bad():
        raise OSError("相机不通")
    w._grab = bad
    t[0] += w.every_s
    await w.step()
    assert events == [] and "相机不通" in w.health()["detail"]
    assert w.health()["state"] == "online", "取不到图归录像、网络那几条管,这里不重复报故障"


async def test_没到点不取():
    w, t, events = _watch()
    n = [0]

    async def grab():
        n[0] += 1
        return _景(1)
    w._grab = grab
    t[0] += w.every_s
    await w.step()
    await w.step()
    assert n[0] == 1


async def test_到点了才due():
    w, t, events = _watch()
    w._frame = _景(1)
    assert w.due(), "起来先取一帧"
    await w.step()
    assert not w.due()
    t[0] += w.every_s
    assert w.due()


async def test_外审R9_开机就糊_攒不出基线_半小时后报一次看不清_好了照常攒基线():
    w, t, events = _watch()
    await _跑(w, [_糊()] * 29, t)
    assert events == []
    await _跑(w, [_糊()], t)
    assert [k for k, _ in events] == ["thermal_lens_blocked"]
    assert events[0][1]["baseline"] == 0
    h = w.health()
    assert h["state"] == "degraded" and "开机" in h["detail"]
    await _跑(w, [_糊()] * 10, t)
    assert len(events) == 1, "一直糊着只报一次"
    await _跑(w, [_景(i) for i in range(13)], t)
    assert [k for k, _ in events][-1] == "thermal_lens_clear"
    assert w.health()["state"] == "online"
