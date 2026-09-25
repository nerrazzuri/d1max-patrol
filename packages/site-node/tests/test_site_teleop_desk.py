"""W00c5c 内部检查:遥控台的并发(假派遣、假事件循环,专测线程之间的次序)。

- 接管要等上一位的狗停稳(最多几秒):这段时间里**同一台狗的停车、查询不许被挡住**;
- 同一台狗同时来两个开租约:一个开,另一个当场 409(不是两个都去狗那头要);
- 结束(发零速)之后,另一条线程晚到一步的摇杆帧**不许再发出去**。
"""

from __future__ import annotations

import asyncio
import json
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


class _手机:
    """一台手机的帧:序号从 1 起,发出时刻按它自己的钟(跟站点的钟差多少都行),每帧 100 ms。"""

    def __init__(self) -> None:
        self.seq = 0
        self.t = 5_000_000.0                          # 手机的单调钟(毫秒)
        self.rx = 100.0                               # 站点收到的时刻(秒,站点的单调钟)

    def 帧(self, vx, wz=0, *, late_ms=0.0, seq=None):
        self.seq += 1
        self.t += 100
        self.rx += 0.1
        d = {"seq": self.seq if seq is None else seq, "t": self.t - late_ms, "vx": vx, "wz": wz}
        return json.dumps(d)


def _发(k, s, ph, vx, wz=0, **kw):
    k.on_messages(s, [ph.帧(vx, wz, **kw)], rx=ph.rx)


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


def _等(cond, timeout=3.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("等不到")
        time.sleep(0.01)


def test_结束之后晚到的摇杆帧不再发出去(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    ph = _手机()
    _发(k, s, ph, 0.3)
    inside = threading.Event()
    go = threading.Event()

    def _hook(frame):
        if frame.vx == 0.3 and frame.seq == 2:       # 第二帧发到一半:另一条线程来结束
            inside.set()
            go.wait(2)
    d.frame_hook = _hook
    t = threading.Thread(target=_发, args=(k, s, ph, 0.3))
    t.start()
    assert inside.wait(2)
    closer = threading.Thread(target=k.close, args=(s, "disconnected"))
    closer.start()
    time.sleep(0.1)
    go.set()
    t.join(2)
    closer.join(2)
    _发(k, s, ph, 0.3)                               # 已经结束:丢掉
    seqs = [f.seq for f in d.frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    assert d.frames[-1].vx == 0.0 and d.frames[-1].wz == 0.0, "最后一帧必须是零速"


def test_查过没结束_还没发出去就被结束了_这一帧也不发(desk):
    k, d = desk
    d.status.task = None
    s = k.open("A", _u("gina", "guard"))
    ph = _手机()
    checked = threading.Event()
    go = threading.Event()

    def _卡住():                                     # 手机那条线程:过了「结束没有」,在画面门里
        checked.set()
        go.wait(2)
    d.video_hook = _卡住
    t = threading.Thread(target=_发, args=(k, s, ph, 0.3))
    t.start()
    assert checked.wait(2)
    d.video_hook = None
    k.close(s, "disconnected")
    go.set()
    t.join(2)
    assert [(f.vx, f.wz) for f in d.frames] == [(0.0, 0.0)], "结束之后不许再有运动帧"


def test_没画面不转发_在动的话只发一次零速_画面回来要先松手(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    ph = _手机()
    told = []
    s.ws = SimpleNamespace(send_text=told.append, close=lambda *a: None)
    _发(k, s, ph, 0.3)
    d.video = False
    for _ in range(3):
        _发(k, s, ph, 0.3)
    assert [(f.vx, f.wz) for f in d.frames] == [(0.3, 0.0), (0.0, 0.0)]
    assert told == ['{"kind": "video", "ok": false}']
    d.video = True
    _发(k, s, ph, 0.3)                               # 杆值还卡在画面断之前:不转
    _发(k, s, ph, 0.3)
    assert [(f.vx, f.wz) for f in d.frames] == [(0.3, 0.0), (0.0, 0.0)], \
        "画面回来之后要先收到一帧零速(松手),才转发运动"
    assert told[-1] == '{"kind": "video", "ok": true}'
    _发(k, s, ph, 0.0)
    _发(k, s, ph, 0.3)
    assert [(f.vx, f.wz) for f in d.frames][-2:] == [(0.0, 0.0), (0.3, 0.0)]


def test_手机那一段也判帧_乱序积压的不转_一批里只转最新的(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    ph = _手机()
    for _ in range(5):
        _发(k, s, ph, 0.1)                           # 基线:在途稳定
    n = len(d.frames)
    _发(k, s, ph, 0.4, late_ms=400)                  # 4G 憋住的旧帧:比基线多 400 ms
    _发(k, s, ph, 0.4, seq=2)                        # 序号回退
    assert len(d.frames) == n and s.phone_dropped == {"seq": 1, "late": 1, "rate": 0}
    batch = [ph.帧(0.1), ph.帧(0.2), ph.帧(0.3)]     # 一次收下来三帧:只转最新的
    k.on_messages(s, batch, rx=ph.rx)
    assert [f.vx for f in d.frames][n:] == [0.3]
    for bad in ('{"vx": 0.2, "wz": 0}', '{"seq": 99, "vx": 0.2, "wz": 0}',
                '{"seq": 99, "t": "x", "vx": 0.2, "wz": 0}'):
        k.on_messages(s, [bad], rx=ph.rx + 1)        # 没序号、没时刻的不收
    assert [f.vx for f in d.frames][n:] == [0.3]


def test_运动帧太密的不转_停的那一帧不压(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    ph = _手机()
    _发(k, s, ph, 0.3)
    k.on_messages(s, [ph.帧(0.3)], rx=ph.rx - 0.09)  # 离上一帧才 10 ms
    k.on_messages(s, [ph.帧(0.0)], rx=ph.rx - 0.19)  # 停:不压
    assert [f.vx for f in d.frames] == [0.3, 0.0] and s.phone_dropped["rate"] == 1


def test_站点自己夹一次限速_看不懂的帧丢掉(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    ph = _手机()
    _发(k, s, ph, 3.0, -9)
    k.on_messages(s, ['{"seq": 50, "t": 1, "vx": true, "wz": 0}'], rx=ph.rx + 1)
    k.on_messages(s, ['{"seq": 51, "t": 1, "vx": NaN, "wz": 0}'], rx=ph.rx + 2)
    k.on_messages(s, ['[1, 2]'], rx=ph.rx + 3)
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
    _等(lambda: s.ended.is_set())
    assert s.end_reason == "lease_lost"


def test_狗掉线_遥控结束(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    d.status.online = False
    k.step()
    _等(lambda: bool(d.leases))
    assert s.end_reason == "robot_offline"
    assert d.leases[-1].action == "release"


def test_halt先收遥控再发_回执超时也已经收了(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))

    async def 超时(robot_id, *, issued_by):
        raise TimeoutError("狗没回话")
    d.halt = 超时
    with pytest.raises(TimeoutError):
        k.halt("A", _u("olga", "owner"))
    _等(lambda: bool(d.leases))                      # 收尾在别的线程:等它放完租
    assert s.end_reason == "halt" and d.frames[-1].vx == 0.0


def test_开租约的过程中有人按了停_这个租约不给(desk):
    k, d = desk
    d.grant_gate.clear()
    got = {}

    def _开():
        try:
            k.open("A", _u("gina", "guard"))
        except TeleopRefused as exc:
            got["refused"] = (exc.status, exc.message)
    t = threading.Thread(target=_开)
    t.start()
    time.sleep(0.2)
    k.halt("A", _u("olga", "owner"))
    d.grant_gate.set()
    t.join(5)
    assert got["refused"][0] == 409 and "停车" in got["refused"][1]
    assert k.active("A") is None and d.leases[-1].action == "release"


def test_授予没等到回执_追发放租(desk):
    k, d = desk

    async def 超时(robot_id, **kw):
        raise TimeoutError("狗没回话")
    d.teleop_grant = 超时
    with pytest.raises(TeleopRefused) as e:
        k.open("A", _u("gina", "guard"))
    assert e.value.status == 502
    _等(lambda: bool(d.leases))
    assert (d.leases[-1].action, d.leases[-1].lease_epoch) == ("release", 1)


def test_同一个人刚放开又来_等狗那头上一趟停稳再给(desk):
    k, d = desk
    s = k.open("A", _u("gina", "guard"))
    k.close(s, "released")                           # status.task 还是 teleop-1(狗在停)
    threading.Timer(0.3, lambda: setattr(d.status, "task", None)).start()
    t0 = time.monotonic()
    s2 = k.open("A", _u("gina", "guard"))
    assert time.monotonic() - t0 >= 0.25 and s2.epoch == 2


def test_站点重启_上次没收尾的租约行记成site_restart_没接管不记理由(tmp_path):
    db = SiteDB(tmp_path / "site.db")
    d = _Disp(db)
    k = TeleopDesk(d, _Loop(), audit=None, now_ms=lambda: 1, video_ok=lambda r: True,
                   renew_s=60)
    k.open("A", _u("gina", "guard"), takeover_reason="没人可接管")
    rows = db.query("SELECT takeover_reason, ended_at FROM teleop_leases")
    assert rows[0]["takeover_reason"] is None and rows[0]["ended_at"] is None
    TeleopDesk(d, _Loop(), audit=None, now_ms=lambda: 2, video_ok=lambda r: True)
    rows = db.query("SELECT end_reason, ended_at FROM teleop_leases")
    assert (rows[0]["end_reason"], rows[0]["ended_at"]) == ("site_restart", 2)
    k.close_all()
