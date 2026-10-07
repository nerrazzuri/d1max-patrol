"""人员检测,代理这一头(W24):有没有人是三种状态(有、确认没有、不知道);三帧里两帧才算有人;
进 5 米算近、退到 6 米外才算不近;连续有效观测 20 秒没人才确认没人 —— 中断不累计、在看的相机停了不判;
截图每张都收;真套接字端到端:握手、能力里的当前状态、截图存成归档。"""

from __future__ import annotations

import asyncio
import json

from test_runtime_obstacles import REG, T, _连

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.persons import GONE_S, PersonView
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_contract.messages import Capabilities
from d1max_contract.persbridge import Hello, Person, Persons, encode
from d1max_patrol.protocol.nav_types import Pose


class 钟:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


_seq = [0]


def _帧(*ranges, camera="front", check="ok", snapshot=""):
    _seq[0] += 1
    return Persons(seq=_seq[0], stamp_ns=0, camera=camera, check=check,
                   reason="" if check == "ok" else "没模型",
                   people=tuple(Person(bearing_deg=5.0, range_m=r, score=0.9) for r in ranges),
                   snapshot=snapshot)


def _台(**kw):
    c, ev = 钟(), []
    v = PersonView(emit=lambda k, d: ev.append((k, d)), monotonic=c, **kw)
    v.on_connect()
    return c, ev, v


def _空帧(c, v, secs, *, cameras=("front",), step=0.5):
    """每个相机每 ``step`` 秒一帧空画面,共 ``secs`` 秒;每帧后 tick。"""
    n = int(secs / step)
    for _ in range(n):
        c.t += step
        for cam in cameras:
            v.on_persons(_帧(camera=cam))
        v.tick()


def test_一帧误检不算_三帧里两帧才算有人_只报一次():
    c, ev, v = _台()
    assert v.present is None, "刚起来:不知道"
    v.on_persons(_帧(8.0))
    v.on_persons(_帧())
    v.on_persons(_帧())
    assert ev == [] and v.present is None, "一帧不算"
    v.on_persons(_帧(8.0))
    v.on_persons(_帧(7.5))
    assert [k for k, _ in ev] == ["person_seen"] and v.present is True
    assert ev[0][1] == {"count": 1, "nearest_m": 7.5}
    v.on_persons(_帧(7.0))
    assert len(ev) == 1, "一直有人不重报"
    assert v.caps() == {"state": "ok", "present": True, "near": False, "count": 1,
                        "nearest_m": 7.0}


def test_进5米算近_退到6米外才算不近():
    c, ev, v = _台()
    for r in (8.0, 8.0, 4.8, 4.5, 5.5, 4.9, 6.5, 4.0):
        v.on_persons(_帧(r))
    assert [k for k, _ in ev] == ["person_seen", "person_near", "person_near"]
    assert v.near is True and v.caps()["near"] is True


def test_连续有效观测20秒没人_确认没人_之后再看到重新算有人():
    c, ev, v = _台()
    v.on_persons(_帧(9.0))
    v.on_persons(_帧(9.0))
    _空帧(c, v, GONE_S + 1)
    assert [k for k, _ in ev] == ["person_seen", "person_gone"] and v.present is False
    v.on_persons(_帧(3.0))
    v.on_persons(_帧(3.0))
    assert [k for k, _ in ev][-2:] == ["person_seen", "person_near"]


def test_没看到过人_有效观测满20秒_确认没人_不报走了():
    c, ev, v = _台()
    _空帧(c, v, GONE_S + 1)
    assert v.present is False and ev == []


def test_外审1_断开60秒再连上_一帧空画面不算人走了_要重新数满20秒():
    c, ev, v = _台()
    v.on_persons(_帧(9.0))
    v.on_persons(_帧(9.0))
    v.on_disconnect()
    c.t += 60
    v.tick()
    v.on_connect()
    v.on_persons(_帧())
    v.tick()
    assert v.present is None and [k for k, _ in ev] == ["person_seen"], \
        "中断:不知道(不是确认没人),不累计没人的时间"
    _空帧(c, v, GONE_S - 2)
    assert v.present is None
    _空帧(c, v, 3)
    assert v.present is False


def test_外审1_后相机停了_前相机一直报空_不算人走了():
    c, ev, v = _台()
    v.on_persons(_帧(camera="back"))
    v.on_persons(_帧(9.0, camera="front"))
    v.on_persons(_帧(9.0, camera="front"))
    assert v.present is True
    _空帧(c, v, 60, cameras=("front",))                   # 后相机一帧都不报了
    assert v.present is None and [k for k, _ in ev] == ["person_seen"], "不知道,不是走了"


def test_外审1_检测节点报no_model_中断_不数():
    c, ev, v = _台()
    v.on_persons(_帧(9.0))
    v.on_persons(_帧(9.0))
    _空帧(c, v, 10)
    v.on_persons(_帧(check="no_model"))
    v.tick()
    _空帧(c, v, 12)
    assert v.present is None, "no_model 那一下打断了,重新数还不满 20 秒"


def test_检测节点不在_能力里说清楚():
    c, ev, v = _台()
    assert v.state() == "stale"
    v.on_persons(_帧(check="no_model"))
    assert v.caps() == {"state": "no_model", "present": None, "near": False, "reason": "没模型"}
    v.on_disconnect()
    assert v.caps() == {"state": "off", "present": None, "near": False}


def test_外审5_每张截图都收_不只第一下_存不下的留着(tmp_path):
    saved = []
    c, ev, v = _台(snapshot_dir=tmp_path,
                   save_snapshot=lambda p, cam: saved.append(p.name) or "incident-x")
    v.on_persons(_帧(9.0, snapshot="a.jpg"))              # 第一帧带截图,还没确认有人
    v.on_persons(_帧(9.0))                                # 第二帧确认有人,没截图(节点限频)
    v.on_persons(_帧(9.0, snapshot="b.jpg"))              # 持续有人
    assert saved == ["a.jpg", "b.jpg"]
    assert [k for k, _ in ev].count("person_snapshot") == 2

    def 炸(p, cam):
        raise OSError("盘满")
    v.save_snapshot = 炸
    v.on_persons(_帧(9.0, snapshot="c.jpg"))
    assert v.present is True, "截图存不下不影响判人"


class 耳朵:
    def __init__(self):
        self.caps, self.events = [], []

    async def __call__(self, m):
        kind = T.parse(m.topic)[2]
        if kind == "capabilities":
            self.caps.append(json.loads(m.payload))
        elif kind == "event":
            self.events.append(json.loads(m.payload))


async def test_端到端(tmp_path):
    """真套接字:能力里是当前状态、截图存成归档、确认没人。"""
    broker = MemoryBroker()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    c = 钟()
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    (snaps / "s1.jpg").write_bytes(b"\xff\xd8jpg\xff\xd9")
    r = SimRobot(now_ms=lambda: 1_000_000)
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG, hal=r,
                      store_dir=tmp_path / "agent", now_ms=lambda: 1_000_000,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      persons="bridge", persons_socket=tmp_path / "p.sock",
                      persons_snapshots=snaps, monotonic=c)
    await rt.start()

    async def 跑(n):
        for _ in range(n):
            await rt.step(0.1)
            await asyncio.sleep(0.01)
            await broker.drain()

    def 能力():
        return Capabilities.from_wire(ears.caps[-1]).tasks["persons"]
    try:
        await broker.drain()
        assert 能力() == {"state": "off", "present": None, "near": False}
        rd, w = await _连(tmp_path / "p.sock")
        w.write(encode(Hello(proto=1, name="fake")))
        await w.drain()
        assert json.loads(await rd.readline())["t"] == "hello"
        w.write(encode(_帧(4.0, snapshot="s1.jpg")))
        w.write(encode(_帧(4.0)))
        await w.drain()
        await 跑(10)
        assert 能力()["present"] is True and 能力()["near"] is True
        kinds = [e["kind"] for e in ears.events]
        assert {"person_seen", "person_near", "person_snapshot"} <= set(kinds), kinds
        root = rt.parts.engine._runs_root / "persons"
        photos = list(root.glob("*/photos/person__front__*.jpg"))
        assert len(photos) == 1 and photos[0].read_bytes() == b"\xff\xd8jpg\xff\xd9"
        assert not (snaps / "s1.jpg").exists(), "存进归档了,截图目录不留"
        for _ in range(45):                                # 一直报空帧:20 秒后确认没人
            c.t += 0.5
            w.write(encode(_帧()))
            await w.drain()
            await 跑(1)
        assert 能力()["present"] is False
        assert "person_gone" in [e["kind"] for e in ears.events]
        w.close()
    finally:
        await rt.close()


def test_外审1_断开马上重连_中间没走拍_也不累计():
    c, ev, v = _台()
    v.on_persons(_帧(9.0))
    v.on_persons(_帧(9.0))
    _空帧(c, v, 15)                                       # 已经数了 15 秒
    v.on_disconnect()
    v.on_connect()                                        # 马上连回来,中间没 tick
    _空帧(c, v, 10)
    assert v.present is None, "断开那一下就重新数:10 秒不够"


def test_外审1_后相机停着时不开始数_它回来以后从头数():
    c, ev, v = _台()
    v.on_persons(_帧(camera="back"))
    v.on_persons(_帧(9.0, camera="front"))
    v.on_persons(_帧(9.0, camera="front"))
    for _ in range(60):                                   # 后相机停了 30 秒,只来前相机的空帧
        c.t += 0.5
        v.on_persons(_帧(camera="front"))
    v.on_persons(_帧(camera="back"))                      # 后相机回来了
    v.tick()
    assert v.present is None, "停着那段不算,刚回来不够 20 秒"



# ------------------------------------------------------------ W24 复查


def test_复查1_断开重连_只有前相机回来_持续空画面_不确认没人():
    c, ev, v = _台()
    v.on_persons(_帧(camera="front"))
    v.on_persons(_帧(9.0, camera="back"))
    v.on_persons(_帧(9.0, camera="back"))
    assert v.present is True
    v.on_disconnect()
    v.on_connect()
    _空帧(c, v, 60, cameras=("front",))                   # 后相机一直没回来
    assert v.present is None and "person_gone" not in [k for k, _ in ev]
    _空帧(c, v, GONE_S + 1, cameras=("front", "back"))    # 后相机回来了、一起空满 20 秒
    assert v.present is False


def test_复查1_中断之后旧人数距离近都作废():
    c, ev, v = _台()
    v.on_persons(_帧(3.0))
    v.on_persons(_帧(3.0))
    assert v.caps()["near"] is True
    v.on_disconnect()
    assert v.caps() == {"state": "off", "present": None, "near": False}
    assert v.count == 0 and v.nearest_m is None


def test_复查2_前相机近_后相机远_后相机最后报也照样算近():
    c, ev, v = _台()
    for _ in range(4):
        v.on_persons(_帧(3.0, camera="front"))
        v.on_persons(_帧(9.0, camera="back"))
    caps = v.caps()
    assert caps["near"] is True and caps["nearest_m"] == 3.0 and caps["count"] == 2
    assert [k for k, _ in ev] == ["person_seen", "person_near"]


def test_复查2_前相机看到的人走出画面_近就不算了():
    c, ev, v = _台()
    for _ in range(3):
        v.on_persons(_帧(3.0, camera="front"))
        v.on_persons(_帧(9.0, camera="back"))
    for _ in range(3):
        v.on_persons(_帧(camera="front"))
        v.on_persons(_帧(9.0, camera="back"))
    caps = v.caps()
    assert caps["near"] is False and caps["nearest_m"] == 9.0 and caps["present"] is True


def test_复查二1_后相机确认远处的人_前相机一帧误检3米_不算近():
    c, ev, v = _台()
    for _ in range(3):
        v.on_persons(_帧(9.0, camera="back"))
    v.on_persons(_帧(3.0, camera="front"))                # 前相机一帧误检
    caps = v.caps()
    assert caps["near"] is False and caps["nearest_m"] == 9.0 and caps["count"] == 1
    assert "person_near" not in [k for k, _ in ev]
    v.on_persons(_帧(camera="front"))
    v.on_persons(_帧(3.0, camera="front"))                # 三帧里只有两帧、这一帧有:确认了
    assert v.caps()["near"] is True and v.caps()["count"] == 2
