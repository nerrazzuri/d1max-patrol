"""W00c5c 内部检查:遥控台的并发(假派遣、假事件循环,专测线程之间的次序)。

- 接管要等上一位的狗停稳(最多几秒):这段时间里**同一台狗的停车、查询不许被挡住**;
- 同一台狗同时来两个开租约:一个开,另一个当场 409(不是两个都去狗那头要);
- 结束(发零速)之后,另一条线程晚到一步的摇杆帧**不许再发出去**。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from d1max_contract.messages import AckResult
from d1max_site.db import SiteDB
from d1max_site.teleop import TeleopDesk, TeleopRefused


class _Loop:
    def call(self, factory, timeout_s=None):
        return asyncio.run(factory())


class _Disp:
    def __init__(self, db) -> None:
        self.db = db
        self.ack_timeout_s = 1.0
        caps = SimpleNamespace(tasks={"teleop": {"max_vx": 0.5, "max_wz": 0.75}})
        self.status = SimpleNamespace(online=True, task=None)
        self.clients = {"A": SimpleNamespace(capabilities=caps, status=self.status)}
        self.frames: list = []
        self.leases: list = []
        self.lease_result = AckResult.ACCEPTED
        self.grant_gate = threading.Event()
        self.grant_gate.set()
        self.grants = 0
        self.halts = 0
        self.frame_hook = None

    async def teleop_grant(self, robot_id, **kw):
        self.grants += 1
        t0 = time.monotonic()
        while not self.grant_gate.is_set() and time.monotonic() - t0 < 2:
            await asyncio.sleep(0.01)
        self.status.task = SimpleNamespace(task_id=f"teleop-{kw['lease_epoch']}")
        return {"ack": {"result": AckResult.ACCEPTED.value}}

    async def teleop_frame(self, robot_id, frame):
        if self.frame_hook is not None:
            self.frame_hook(frame)
        self.frames.append(frame)

    async def teleop_lease(self, robot_id, lease, *, timeout_s):
        self.leases.append(lease)
        return SimpleNamespace(result=self.lease_result)

    async def halt(self, robot_id, *, issued_by):
        self.halts += 1
        return {"ack": {"result": "accepted"}}

    def is_stale(self, robot_id):
        return False


class _U(str):
    role = ""


def _u(name, role):
    u = _U(name)
    u.role = role
    return u


@pytest.fixture
def desk(tmp_path):
    d = _Disp(SiteDB(tmp_path / "site.db"))
    d.video = True
    d.video_hook = None

    def video_ok(robot_id):
        if d.video_hook is not None:
            d.video_hook()
        return d.video
    k = TeleopDesk(d, _Loop(), audit=None, now_ms=lambda: int(time.time() * 1000),
                   video_ok=video_ok, renew_s=60, wait_released_s=1.5)
    yield k, d
    k.close_all()


def test_接管在等上一位停稳_同一台狗的停车与查询不被挡住(desk):
    k, d = desk
    k.open("A", _u("gina", "guard"))                # status.task 一直是 teleop-1:接管会等满
    took = {}

    def _接管():
        try:
            k.open("A", _u("alice", "admin"), takeover_reason="手机没电")
        except TeleopRefused as exc:
            took["refused"] = exc.status
    t = threading.Thread(target=_接管)
    t.start()
    time.sleep(0.2)                                  # 接管线程正在等
    t0 = time.monotonic()
    k.halt("A", _u("olga", "owner"))
    k.active("A")
    assert time.monotonic() - t0 < 0.5, "停车被接管的等待挡住了"
    t.join(5)
    assert took == {"refused": 409}


def test_同一台狗同时开两个_一个开_另一个当场409(desk):
    k, d = desk
    d.grant_gate.clear()                             # 第一个卡在等狗回话
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("s", k.open("A", _u("gina", "guard"))))
    t.start()
    time.sleep(0.2)
    with pytest.raises(TeleopRefused) as e:
        k.open("A", _u("gus", "guard"))
    assert e.value.status == 409
    d.grant_gate.set()
    t.join(5)
    assert got["s"].operator == "gina" and d.grants == 1


def test_结束之后晚到的摇杆帧不再发出去(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    k.on_message(s, '{"vx": 0.3, "wz": 0}')
    inside = threading.Event()
    go = threading.Event()

    def _hook(frame):
        if frame.vx == 0.3 and frame.seq == 2:       # 第二帧发到一半:另一条线程来结束
            inside.set()
            go.wait(2)
    d.frame_hook = _hook
    t = threading.Thread(target=k.on_message, args=(s, '{"vx": 0.3, "wz": 0}'))
    t.start()
    assert inside.wait(2)
    closer = threading.Thread(target=k.close, args=(s, "disconnected"))
    closer.start()
    time.sleep(0.1)
    go.set()
    t.join(2)
    closer.join(2)
    k.on_message(s, '{"vx": 0.3, "wz": 0}')         # 已经结束:丢掉
    seqs = [f.seq for f in d.frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    assert d.frames[-1].vx == 0.0 and d.frames[-1].wz == 0.0, "最后一帧必须是零速"


def test_查过没结束_还没发出去就被结束了_这一帧也不发(desk):
    k, d = desk
    d.status.task = None
    s = k.open("A", _u("gina", "guard"))
    checked = threading.Event()
    go = threading.Event()

    def _卡住():                                     # 手机那条线程:过了「结束没有」,在画面门里
        checked.set()
        go.wait(2)
    d.video_hook = _卡住
    t = threading.Thread(target=k.on_message, args=(s, '{"vx": 0.3, "wz": 0}'))
    t.start()
    assert checked.wait(2)
    d.video_hook = None
    k.close(s, "disconnected")
    go.set()
    t.join(2)
    assert [(f.vx, f.wz) for f in d.frames] == [(0.0, 0.0)], "结束之后不许再有运动帧"


def test_没画面不转发_在动的话只发一次零速_告诉手机(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    told = []
    s.ws = SimpleNamespace(send_text=told.append, close=lambda *a: None)
    k.on_message(s, '{"vx": 0.3, "wz": 0}')
    d.video = False
    for _ in range(3):
        k.on_message(s, '{"vx": 0.3, "wz": 0}')
    assert [(f.vx, f.wz) for f in d.frames] == [(0.3, 0.0), (0.0, 0.0)]
    assert told == ['{"kind": "video", "ok": false}']
    d.video = True
    k.on_message(s, '{"vx": 0.3, "wz": 0}')
    assert d.frames[-1].vx == 0.3 and told[-1] == '{"kind": "video", "ok": true}'


def test_站点自己夹一次限速_看不懂的帧丢掉(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    k.on_message(s, '{"vx": 3.0, "wz": -9}')
    k.on_message(s, '{"vx": true, "wz": 0}')
    k.on_message(s, '{"vx": NaN, "wz": 0}')
    k.on_message(s, '[1, 2]')
    assert [(f.vx, f.wz) for f in d.frames] == [(0.5, -0.75)]
    assert d.frames[0].ttl_ms == 300 and d.frames[0].lease_epoch == 1


def test_结束_先零速再放租_手机放开也放租(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    k.close(s, "disconnected")                       # 没在动也发零速(断开不是本人放开)
    assert [(f.vx, f.wz) for f in d.frames] == [(0.0, 0.0)]
    assert [(x.action, x.lease_epoch) for x in d.leases] == [("release", 1)]
    d.status.task = None
    s = k.open("A", _u("gina", "guard"))
    k.on_message(s, '{"kind": "release"}')
    assert s.end_reason == "released" and d.leases[-1].action == "release"


def test_续租连续两次续不上_结束说续不上(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    d.lease_result = AckResult.REJECTED
    k.step()
    assert not s.ended.is_set(), "一次续不上还不算"
    k.step()
    assert s.end_reason == "lease_lost"


def test_狗掉线_遥控结束(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    d.status.online = False
    k.step()
    assert s.end_reason == "robot_offline"
    assert d.leases[-1].action == "release"
