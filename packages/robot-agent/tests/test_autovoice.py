"""POC 11-05(用户:接入告警,特定的音频在特定的时候播放):机器人自己按情况放录好的话术。

- 被挡住走不过去(导航报 ``nav_blocked``)→ ``blocked``,挡着每 20 秒再放一次,走通了停;
- 有人进 2 米 → ``distance``(30 秒内不重复);
- 走着的时候看到 3 个人以上 → ``passing``(60 秒内不重复);
- 禁区驱离归站点(第 2 级轮放 ``warn-<语言>``),不在这里。

只放这台机器人有的话术(没拷那段就不放);优先级比手动、驱离都低:正在放更要紧的就不插。"""

from __future__ import annotations

from types import SimpleNamespace

from nexwar_agent.autovoice import AUTO_PRIORITY, AutoVoice
from nexwar_agent.deter import DeterDesk


class 假HAL:
    def __init__(self, clips=("blocked", "passing", "distance", "restricted")) -> None:
        self.clips = tuple(clips)
        self.played: list[str] = []

    def hal_capabilities(self):
        return SimpleNamespace(actuators={"speaker": True})

    def sound_clips(self):
        return self.clips

    async def sound(self, clip, max_s):
        self.played.append(clip)


class 钟:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _台(clips=("blocked", "passing", "distance", "restricted")):
    hal, c, ev = 假HAL(clips), 钟(), []
    desk = DeterDesk(hal, emit=lambda k, d: ev.append((k, d)), monotonic=c)
    return hal, c, ev, desk, AutoVoice(desk, monotonic=c)


async def test_被挡住_放blocked_挡着每20秒再放_走通了停():
    hal, c, ev, desk, av = _台()
    av.on_nav_event("nav_blocked", {"reason": "x"})
    await av.tick(moving=True, persons=0, nearest_m=None)
    assert hal.played == ["blocked"]
    c.t += 10
    desk._sound = None                                   # 放完了
    await av.tick(moving=True, persons=0, nearest_m=None)
    assert hal.played == ["blocked"], "20 秒内不重复"
    c.t += 11
    await av.tick(moving=True, persons=0, nearest_m=None)
    assert hal.played == ["blocked", "blocked"]
    av.on_nav_event("nav_unblocked", {})
    c.t += 30
    desk._sound = None
    await av.tick(moving=True, persons=0, nearest_m=None)
    assert hal.played == ["blocked", "blocked"]
    auto = [d for k, d in ev if k == "deter"]
    assert auto[0]["clip"] == "blocked" and auto[0]["auto"] == "nav_blocked"
    assert auto[0]["priority"] == AUTO_PRIORITY


async def test_停下来了_任务结束_被挡也清掉():
    hal, c, ev, desk, av = _台()
    av.on_nav_event("nav_blocked", {})
    await av.tick(moving=False, persons=0, nearest_m=None)
    c.t += 60
    await av.tick(moving=True, persons=0, nearest_m=None)
    assert hal.played == []


async def test_有人进2米_放distance_30秒内不重复():
    hal, c, ev, desk, av = _台()
    await av.tick(moving=False, persons=1, nearest_m=2.5)
    assert hal.played == []
    await av.tick(moving=False, persons=1, nearest_m=1.8)
    assert hal.played == ["distance"]
    desk._sound = None
    c.t += 20
    await av.tick(moving=False, persons=1, nearest_m=1.0)
    assert hal.played == ["distance"]
    c.t += 11
    await av.tick(moving=False, persons=1, nearest_m=1.0)
    assert hal.played == ["distance", "distance"]


async def test_走着看到3个人以上_放passing_停着不放_60秒内不重复():
    hal, c, ev, desk, av = _台()
    await av.tick(moving=False, persons=5, nearest_m=8.0)
    assert hal.played == []
    await av.tick(moving=True, persons=2, nearest_m=8.0)
    assert hal.played == []
    await av.tick(moving=True, persons=3, nearest_m=8.0)
    assert hal.played == ["passing"]
    desk._sound = None
    c.t += 50
    await av.tick(moving=True, persons=4, nearest_m=8.0)
    assert hal.played == ["passing"]


async def test_被挡比人近要紧_一拍只放一段():
    hal, c, ev, desk, av = _台()
    av.on_nav_event("nav_blocked", {})
    await av.tick(moving=True, persons=4, nearest_m=1.0)
    assert hal.played == ["blocked"]


async def test_机器人上没有那段_不放_也不报错():
    hal, c, ev, desk, av = _台(clips=("warn-zh",))
    av.on_nav_event("nav_blocked", {})
    await av.tick(moving=True, persons=4, nearest_m=1.0)
    assert hal.played == []


async def test_正在放更要紧的_不插():
    hal, c, ev, desk, av = _台()
    desk._sound = (60, c.t + 30)                          # 保安手动在放
    av.on_nav_event("nav_blocked", {})
    await av.tick(moving=True, persons=0, nearest_m=None)
    assert hal.played == []
    c.t += 31                                            # 那段放完了(按时长算)
    await av.tick(moving=True, persons=0, nearest_m=None)
    assert hal.played == ["blocked"]


async def test_自己放的被手动的打断_手动的照常(tmp_path):
    hal, c, ev, desk, av = _台()
    await av.tick(moving=False, persons=1, nearest_m=1.0)
    assert desk.playing()[0] == AUTO_PRIORITY < 60
