"""W74 二期:跟人(设计 §五、§六)。真引擎 + 规划后端 + 仿真狗;人是假的(按机器人此刻的位姿算方向、
距离)。

人往前走 → 机器人跟上、保持 3 m 左右;人停 → 机器人在人后方停;人消失 10 秒 → 回最后看见人的地方、朝人
离开的方向停、任务结束(result lost);跟到上限按跟丢办;被中止照常停稳再终态;路上发轨迹事件。
"""

from __future__ import annotations

import math

import pytest
from test_planned_engine import _一拍, 台子  # noqa: F401  (pytest 夹具)

from nexwar_agent.tasks.follow import FollowTask
from nexwar_contract.errors import ContractError
from nexwar_contract.follow import FollowRequest, parse_follow
from nexwar_contract.messages import TaskState


class 人:
    """一个人在地图上 (x, y);``seen`` False = 看不到。按机器人位姿给 ``(方向°, 距离 m)``。"""

    def __init__(self, r, x, y):
        self.r, self.x, self.y, self.seen = r, x, y, True

    def target(self):
        if not self.seen:
            return None
        dx, dy = self.x - self.r.x, self.y - self.r.y
        b = math.degrees(math.atan2(dy, dx) - self.r.yaw)
        b = (b + 180) % 360 - 180
        return (b, math.hypot(dx, dy))


def _任务(台, p, *, others=(), **kw):
    c, r, parts = 台
    emits = []

    class 簿:
        def emit(self, kind, data):
            emits.append((kind, data))

    t = FollowTask(task_id="follow-1", req=FollowRequest(**kw), parts=parts, events=簿(),
                   now_ms=lambda: int(c.mono * 1000), wall_ms=c,
                   targets=lambda: [x for x in (p.target(), *(o.target() for o in others))
                                    if x is not None],
                   here=lambda: (r.x, r.y, r.yaw), map_ref=("m", "v"), priority=80)
    return t, emits


async def _跑(台, t, secs, each=None):
    c, r, parts = 台
    for _ in range(int(secs * 10)):
        await t.step(0.1)
        await _一拍(c, r, parts)
        if each is not None:
            each()
        if t.done:
            return


def test_契约_默认_越界拒():
    assert parse_follow(None) == FollowRequest()
    assert parse_follow({"keep_m": 4}).keep_m == 4.0
    for bad in ({"keep_m": 0.5}, {"max_s": 1.5}, {"x": 1}, {"lost_s": float("nan")}, []):
        with pytest.raises(ContractError):
            parse_follow(bad)


async def test_人往前走_机器人跟上_保持3米左右_路上发轨迹(台子):  # noqa: F811
    c, r, parts = 台子
    p = 人(r, 3.0, 3.0)
    t, emits = _任务(台子, p)
    await t.start()

    def 走():
        if p.y < 7.0:
            p.y += 0.05                                   # 0.5 m/s 往北
    await _跑(台子, t, 20, 走)
    assert t.state is TaskState.RUNNING
    d = math.hypot(p.x - r.x, p.y - r.y)
    assert r.y > 2.0 + 1.0, "跟上去了"
    assert 2.0 <= d <= 4.5, d
    track = [d for k, d in emits if k == "follow_track"]
    assert track and track[-1]["task_id"] == "follow-1"
    pt = track[-1]["points"][-1]
    assert abs(pt["x"] - p.x) < 0.6 and abs(pt["y"] - p.y) < 0.6


async def test_人消失10秒_回最后看见的地方_朝人走的方向_结束说跟丢(台子):  # noqa: F811
    c, r, parts = 台子
    p = 人(r, 4.0, 2.0)
    t, emits = _任务(台子, p)
    await t.start()

    def 走():
        if p.seen:
            p.y += 0.04                                   # 往北走
            if p.y > 4.0:
                p.seen = False
    await _跑(台子, t, 80, 走)
    assert t.state is TaskState.DONE, t.detail
    assert t.detail["result"] == "lost"
    last = t.detail["last"]
    assert abs(last["x"] - 4.0) < 0.5 and abs(last["y"] - 4.0) < 0.6
    assert abs(t.detail["heading_deg"] - 90) < 30, "人往北走的"
    assert math.dist((r.x, r.y), (last["x"], last["y"])) < 0.5, "回到最后看见人的地方"
    assert abs(math.degrees(r.yaw) - 90) < 25, "朝人离开的方向"
    assert t.detail["track"]


async def test_跟到时限_按跟丢办(台子):  # noqa: F811
    c, r, parts = 台子
    p = 人(r, 3.0, 3.0)
    t, _ = _任务(台子, p, max_s=10)

    def 走():
        p.y = min(p.y + 0.05, 7.0)
    await t.start()
    await _跑(台子, t, 60, 走)
    assert t.state is TaskState.DONE and t.detail["result"] == "limit"


async def test_中止_停稳再终态(台子):  # noqa: F811
    c, r, parts = 台子
    p = 人(r, 5.0, 2.0)
    t, _ = _任务(台子, p)
    await t.start()
    await _跑(台子, t, 2)
    await t.abort("人工")
    await _跑(台子, t, 20)
    assert t.state is TaskState.ABORTED


async def test_一开始就看不到人_也按跟丢办_不乱跑(台子):  # noqa: F811
    c, r, parts = 台子
    p = 人(r, 4.0, 2.0)
    p.seen = False
    t, _ = _任务(台子, p, lost_s=3)
    await t.start()
    await _跑(台子, t, 20)
    assert t.state is TaskState.DONE and t.detail["result"] == "lost"
    assert math.dist((r.x, r.y), (2.0, 2.0)) < 0.3, "没看到过人:原地"


async def test_运行时_能力里有跟人_收命令_能力报在跟(tmp_path):
    from test_planned_engine import 画图
    from test_runtime_maps import _cmd
    from test_standoff import _拍, _真台
    broker, c, ears, dog, rt = await _真台(tmp_path)
    try:
        画图(tmp_path)
        rt.parts.nav.load_grid(tmp_path)
        rt.processor.supported = rt._supported()          # 真的换图时运行时自己刷
        dog.teleport(2.0, 2.0, 0.0)
        await rt._publish_caps()
        await broker.drain()
        caps = ears.by["capabilities"][-1]
        assert caps["tasks"]["follow"] == {"state": "idle"}
        await rt._on_cmd(_cmd("follow", {"keep_m": 20}, "f0", c))
        await broker.drain()
        assert ears.by["cmd/ack"][-1]["result"] != "accepted", "keep_m 越界:拒"
        await rt._on_cmd(_cmd("follow", {"max_s": 60}, "f1", c))
        await broker.drain()
        assert ears.by["cmd/ack"][-1]["result"] == "accepted", str(ears.by["cmd/ack"][-1])
        await _拍(rt, broker, dog, c, 5, person=6.0)
        await rt._publish_caps()
        await broker.drain()
        st = ears.by["capabilities"][-1]["tasks"]["follow"]
        assert st["state"] == "follow" and st["task_id"] == "follow-f1", st
    finally:
        await rt.close()


async def test_外审8_到上限往回收尾_人回头靠近_不往人身上走(台子):  # noqa: F811
    c, r, parts = 台子
    p = 人(r, 2.0, 5.0)
    t, _ = _任务(台子, p, max_s=6)
    await t.start()
    back = {}

    def 走():
        if t.phase == "follow":
            p.y = min(p.y + 0.15, 9.0)                    # 1.5 m/s 往北,机器人跟不上
        elif t._leg_goal is not None:
            back.setdefault("goal", t._leg_goal)
            gy = back["goal"][1] + 1.0                    # 人回头,停在收尾目标前 1 m
            p.y = max(p.y - 0.15, gy)
    await _跑(台子, t, 40, 走)
    assert "goal" in back, "到上限时离人远过 keep_m,要往前收尾"
    assert t.state is TaskState.DONE and t.detail["result"] == "limit"
    d = math.hypot(p.x - r.x, p.y - r.y)
    assert d >= 2.0, f"人回头靠近了还往前走:只剩 {d:.2f} m"


async def test_外审9_两个人_被跟的还看得见_旁边更近的不抢(台子):  # noqa: F811
    c, r, parts = 台子
    p = 人(r, 3.0, 5.5)
    t, _ = _任务(台子, p, others=())
    await t.start()
    await _跑(台子, t, 1)
    q = 人(r, 3.5, 2.0)                                  # 另一个人站在机器人旁边,比被跟的近
    t2_others = [q]
    t._targets = lambda: [x for x in (p.target(), *(o.target() for o in t2_others))
                          if x is not None]

    def 走():
        p.y = min(p.y + 0.05, 8.0)
    await _跑(台子, t, 8, 走)
    assert t._person is not None and math.dist(t._person, (p.x, p.y)) < 1.0, \
        (t._person, (p.x, p.y))
