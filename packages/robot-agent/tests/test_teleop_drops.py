"""POC 第 3 项:遥控指令被丢的原因。没画面的帧记 ``no_video``;画面断过又回来,要先收到一帧摇杆回中的
才接着走(之前的记 ``video_recovered``:操作员手可能还推着)。
续租回执带 ``drops``(累计数、最近一条、时刻)。"""

from __future__ import annotations

from test_teleop_task import _帧, _走, 台  # noqa: F401  (台 是 fixture)

from nexwar_agent.tasks.teleop import TeleopTask


async def test_没画面的帧记no_video_画面回来要先回中才走(台):  # noqa: F811
    c, r, t, video = 台
    assert t.drops_note() is None, "没丢过不说"
    video["live"] = False
    await _走(c, r, t, 3, frame=lambda: _帧(c, vx=0.4))
    n = t.drops_note()
    assert n["counts"]["no_video"] == 3 and n["last"] == "no_video" and n["at_ms"] == c.ms - 100
    video["live"] = True
    await _走(c, r, t, 3, frame=lambda: _帧(c, vx=0.4))   # 手还推着
    assert (await r.odometry()).x == 0.0, "画面刚回来、杆没回中:不走"
    assert t.drops_note()["last"] == "video_recovered"
    assert t.drops_note()["counts"]["video_recovered"] == 3
    await _走(c, r, t, 1, frame=lambda: _帧(c))           # 松手一帧
    await _走(c, r, t, 5, frame=lambda: _帧(c, vx=0.4))
    assert (await r.odometry()).x > 0.0, "回中之后照常走"


async def test_画面断了没发帧_也要回中才走(台):  # noqa: F811
    c, r, t, video = 台
    video["live"] = False
    await _走(c, r, t, 2)                                  # 一帧没来,只是画面断过
    video["live"] = True
    await _走(c, r, t, 3, frame=lambda: _帧(c, vx=0.4))
    assert (await r.odometry()).x == 0.0
    assert t.drops_note()["last"] == "video_recovered"


async def test_老的三种也算进去(台):  # noqa: F811
    c, r, t, _ = 台
    t.on_frame(_帧(c, vx=0.2, epoch=9), rx_ms=c.ms)
    assert t.drops_note()["counts"]["epoch"] == 1 and t.drops_note()["last"] == "epoch"


def test_丢帧原因码齐全():
    assert {"epoch", "seq", "late", "no_video", "video_recovered"} <= set(TeleopTask.DROP_CODES)
