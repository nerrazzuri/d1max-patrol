"""POC 11-05(用户听过 4 段,「可以,挺好的」):开机就绪播报。

- 连上站点、拿到运控、定位可信、Nav2 就绪都齐了 → 放 ``ready``,开机只放一次;
- 开机 5 分钟还没齐 → 按缺的那一样放提示(连不上站点 ``not_ready_site`` 先于定位
  ``not_ready_location``),每 5 分钟最多一次;一拍只放一段;齐了以后放 ``ready``、不再提示;
- 钟不对时代理还没起来(``nexwar_wait_clock`` 在等),``not_ready_time`` 由那个脚本放,不在这里;
- 机器人上没有那段话术就不放、不报错;没有 ``ready`` 这段,齐了也就不再管。"""

from __future__ import annotations

from test_autovoice import _台

from nexwar_agent.bootvoice import HINT_AFTER_S, HINT_EVERY_S, BootVoice

CLIPS = ("ready", "not_ready_site", "not_ready_location", "not_ready_time")


def _开机(clips=CLIPS):
    hal, c, ev, desk, _ = _台(clips)
    return hal, c, desk, BootVoice(desk, monotonic=c)


async def _拍(bv, desk, **kw):
    desk._sound = None                                    # 上一段放完了
    await bv.tick(**({"site": True, "control": True, "loc": True, "nav2": True} | kw))


async def test_都齐了放ready_只放一次():
    hal, c, desk, bv = _开机()
    await _拍(bv, desk, loc=False)
    assert hal.played == []
    c.t += 30
    await _拍(bv, desk)
    assert hal.played == ["ready"]
    c.t += 600
    await _拍(bv, desk, site=False)
    await _拍(bv, desk)
    assert hal.played == ["ready"], "开机只报一次;之后掉线不归这里管"


async def test_5分钟还没齐_按缺的放提示_站点先于定位_每5分钟最多一次():
    hal, c, desk, bv = _开机()
    await _拍(bv, desk, site=False, loc=False)
    c.t += HINT_AFTER_S - 1
    await _拍(bv, desk, site=False, loc=False)
    assert hal.played == []
    c.t += 1
    await _拍(bv, desk, site=False, loc=False)
    assert hal.played == ["not_ready_site"], "一拍只放一段,站点先"
    await _拍(bv, desk, site=False, loc=False)
    assert hal.played == ["not_ready_site", "not_ready_location"]
    c.t += 60
    await _拍(bv, desk, site=False, loc=False)
    assert len(hal.played) == 2, "5 分钟内不重复"
    c.t += HINT_EVERY_S
    await _拍(bv, desk, loc=False)
    assert hal.played[-1] == "not_ready_location"
    await _拍(bv, desk)
    assert hal.played[-1] == "ready"


async def test_运控_Nav2没好_不报齐_也没有专门的提示():
    hal, c, desk, bv = _开机()
    c.t += HINT_AFTER_S + 1
    await _拍(bv, desk, control=False)
    await _拍(bv, desk, nav2=False)
    assert hal.played == []


async def test_机器人上没有这几段_不放不报错():
    hal, c, desk, bv = _开机(clips=("blocked",))
    c.t += HINT_AFTER_S + 1
    await _拍(bv, desk, site=False)
    await _拍(bv, desk)
    assert hal.played == []


async def test_正在放更要紧的_ready等它放完再放():
    hal, c, desk, bv = _开机()
    desk._sound = (60, c.t + 30)
    await bv.tick(site=True, control=True, loc=True, nav2=True)
    assert hal.played == []
    await _拍(bv, desk)
    assert hal.played == ["ready"]
