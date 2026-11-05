"""W74 四期:机器人被遮挡(设计 §八)。雷达被挡 + 至少一路相机全黑 / 几乎单色,连着 3 秒才算;
揭开了报恢复。只有一样不算(暗处、对着白墙、雷达被人挡一下)。"""

from __future__ import annotations

from nexwar_agent.cover import HOLD_S, CoverWatch, blind
from nexwar_contract.locbridge import LIDAR_BLOCKED_REASON

W, H = 64, 48
DARK = bytes(W * H)
FLAT = bytes([200]) * (W * H)
SCENE = bytes((x * 7 + y * 13) % 256 for y in range(H) for x in range(W))


def test_一帧黑不黑():
    assert blind(DARK) and blind(FLAT) and not blind(SCENE)


class 台:
    def __init__(self, front=SCENE, back=SCENE):
        self.t = 0.0
        self.lidar = False
        self.frames = {"front": front, "back": back}
        self.events: list[tuple[str, dict]] = []
        self.flips: list[bool] = []

        def grab(cam):
            async def g():
                f = self.frames[cam]
                if isinstance(f, Exception):
                    raise f
                return f
            return g
        self.w = CoverWatch({c: grab(c) for c in self.frames}, lidar_blocked=lambda: self.lidar,
                            monotonic=lambda: self.t, emit=lambda k, d: self.events.append((k, d)),
                            on_change=self.flips.append)

    async def 跑(self, secs):
        for _ in range(int(secs * 10)):
            self.t += 0.1
            if self.w.due():
                await self.w.step()


async def test_雷达挡了_相机黑了_连着3秒才报_揭开报恢复():
    s = 台()
    s.lidar = True
    s.frames["back"] = DARK
    await s.跑(HOLD_S - 0.5)
    assert s.events == [] and not s.w.covered
    await s.跑(1.5)
    assert s.w.covered and s.flips == [True]
    [(k, d)] = s.events
    assert k == "robot_covered" and d["cameras"] == ["back"]
    await s.跑(3)
    assert len(s.events) == 1, "只报一次"
    s.lidar = False
    await s.跑(1)
    assert not s.w.covered and s.events[-1][0] == "robot_uncovered" and s.flips == [True, False]


async def test_只有一样不算():
    s = 台(front=DARK, back=DARK)
    await s.跑(6)
    assert s.events == [], "相机黑但雷达好好的:暗处"
    s = 台()
    s.lidar = True
    await s.跑(6)
    assert s.events == [], "雷达挡了但相机看得见"


async def test_不该一直取图_雷达没挡不取_取不到图不算黑():
    s = 台()
    calls = []
    s.w.grabs = {"front": lambda: calls.append(1) or _boom()}
    await s.跑(3)
    assert calls == [], "雷达没挡:不起 ffmpeg"
    s.lidar = True
    await s.跑(5)
    assert calls and s.events == []


async def _boom():
    raise OSError("取不到")


def test_定位器的原因开头():
    from nexwar_agent.bridge_localizer import BridgeLocalizer
    from nexwar_contract.locbridge import State
    b = BridgeLocalizer(monotonic=lambda: 0.0)
    assert not b.lidar_blocked()
    b.on_state(State(state="lost", reason=f"{LIDAR_BLOCKED_REASON}(一帧有效点只有 12)", seq=1))
    assert b.lidar_blocked()
    b.on_state(State(state="lost", reason="匹配在来回跳", seq=2))
    assert not b.lidar_blocked()


async def test_运行时遮挡(tmp_path):
    """被遮挡:撤会动的任务、不收新的;揭开了照常。"""
    from test_runtime_maps import _cmd
    from test_standoff import CENTER, _拍, _真台
    broker, c, ears, dog, rt = await _真台(tmp_path)
    try:
        s = {"lidar": False}

        async def dark():
            return DARK
        rt.attach_cover(CoverWatch({"front": dark}, lidar_blocked=lambda: s["lidar"],
                                   monotonic=lambda: c.mono))
        await rt._on_cmd(_cmd("standoff", {"max_s": 60, "leash_m": 6.0,
                                           "center": CENTER.to_wire()}, "s1", c))
        await _拍(rt, broker, dog, c, 3, person=None)
        assert rt.processor.current is not None and rt.processor.current.kind == "standoff"
        s["lidar"] = True
        await _拍(rt, broker, dog, c, 45, person=None)
        assert any(e["kind"] == "robot_covered" for e in ears.by["event"])
        assert ears.by["capabilities"][-1]["tasks"]["cover"] == {"state": "covered"}, "站点按它对账"
        cur = rt.processor.current
        assert cur is None or cur.done, "被遮挡:会动的任务撤了"
        await rt._on_cmd(_cmd("standoff", {"max_s": 60, "leash_m": 6.0,
                                           "center": CENTER.to_wire()}, "s2", c))
        await broker.drain()
        ack = ears.by["cmd/ack"][-1]
        assert ack["result"] == "rejected" and "被遮挡" in str(ack["reason"]), ack
        s["lidar"] = False
        await _拍(rt, broker, dog, c, 5, person=None)
        assert any(e["kind"] == "robot_uncovered" for e in ears.by["event"])
        assert ears.by["capabilities"][-1]["tasks"]["cover"] == {"state": "ok"}
        await rt._on_cmd(_cmd("standoff", {"max_s": 60, "leash_m": 6.0,
                                           "center": CENTER.to_wire()}, "s3", c))
        await broker.drain()
        assert ears.by["cmd/ack"][-1]["result"] == "accepted"
    finally:
        await rt.close()
