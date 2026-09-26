"""建图进程日志(W00c6g):站点经 ``proc_log`` 取狗上录包、重建子进程日志的列表与尾巴(以前只能 SSH
上狗看)。
尾巴放在回执里走 MQTT:站点 broker 单包上限 256 KB(``max_packet_size``,超了狗会被断开),所以尾巴最多
128 KiB,而且按编码之后的大小再裁一次(控制字符在 JSON 里一个字节变六个)。"""

from __future__ import annotations

import json
import os

import pytest
from test_runtime_maps import REG, T, _cmd, 耳朵, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.runtime import AgentRuntime
from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
from d1max_patrol.protocol.nav_types import Pose


class 假录包:
    def __init__(self, log_dir):
        self.recording = False
        self.last_bag = ""
        self.log_dir = log_dir


async def _台(tmp_path, *, mapper=True):
    broker, c = MemoryBroker(), 钟()
    ears = 耳朵()
    st = MemoryTransport(broker, "site")
    await st.connect()
    await st.subscribe(f"{T.prefix}/#", ears)
    logs = tmp_path / "logs"
    logs.mkdir()
    rt = AgentRuntime(transport=MemoryTransport(broker, "dog"), registration=REG,
                      hal=SimRobot(now_ms=c), store_dir=tmp_path / "agent", now_ms=c,
                      loaded_map=("m", "1"), boot_id="b", home=Pose.from_xy_yaw(0, 0, 0),
                      monotonic=lambda: c.mono, mapper=假录包(logs) if mapper else None)
    await rt.start()
    await broker.drain()
    return broker, c, ears, rt, logs


def _ack(ears, cid):
    return [a for a in ears.by["cmd/ack"] if a["command_id"] == cid][-1]


async def test_列日志_按修改时间倒序_只列日志目录里的(tmp_path):
    broker, c, ears, rt, logs = await _台(tmp_path)
    assert "proc_log" in ears.by["capabilities"][-1]["tasks"]
    for i, n in enumerate(("bagrecord", "slam_offline", "map_saver")):
        (logs / f"{n}.log").write_text(f"{n}\n")
        os.utime(logs / f"{n}.log", (1000 + i, 1000 + i))
    (logs / "notes.txt").write_text("x")
    (logs / "sub").mkdir()
    (logs / "sub" / "x.log").write_text("x")
    await rt._on_cmd(_cmd("proc_log", {}, "l1", c))
    await broker.drain()
    a = _ack(ears, "l1")
    assert a["result"] == "accepted", a
    names = [x["name"] for x in a["data"]["logs"]]
    assert names == ["map_saver", "slam_offline", "bagrecord"]
    assert a["data"]["logs"][0]["size"] == len("map_saver\n")
    await rt.close()


async def test_取尾巴_默认_指定_上限_从行首截_坏字节替换(tmp_path):
    broker, c, ears, rt, logs = await _台(tmp_path)
    lines = [f"line {i:05d} " + "x" * 40 for i in range(4000)]          # 约 190 KB
    (logs / "slam_offline.log").write_bytes(("\n".join(lines) + "\n").encode() + b"\xff\xfe end\n")
    await rt._on_cmd(_cmd("proc_log", {"name": "slam_offline"}, "t1", c))
    await broker.drain()
    d = _ack(ears, "t1")["data"]
    assert d["name"] == "slam_offline" and d["truncated"] is True
    assert d["bytes"] <= 64 * 1024 and d["size"] > 180_000
    assert d["text"].startswith("line "), "从一行的开头截,不留半行"
    assert d["text"].endswith(" end\n") and "�" in d["text"], "坏字节替换,不炸"
    await rt._on_cmd(_cmd("proc_log", {"name": "slam_offline", "bytes": 1000}, "t2", c))
    await broker.drain()
    assert _ack(ears, "t2")["data"]["bytes"] <= 1000
    await rt._on_cmd(_cmd("proc_log", {"name": "slam_offline", "bytes": 10**9}, "t3", c))
    await broker.drain()
    assert _ack(ears, "t3")["data"]["bytes"] <= 128 * 1024
    (logs / "short.log").write_text("a\nb\n")
    await rt._on_cmd(_cmd("proc_log", {"name": "short"}, "t4", c))
    await broker.drain()
    d = _ack(ears, "t4")["data"]
    assert d["text"] == "a\nb\n" and d["truncated"] is False
    await rt.close()


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "", "x" * 60, "A B", 3])
async def test_名字不像话_穿越_拒(tmp_path, bad):
    broker, c, ears, rt, logs = await _台(tmp_path)
    await rt._on_cmd(_cmd("proc_log", {"name": bad}, "b1", c))
    await broker.drain()
    assert _ack(ears, "b1")["reason"].startswith("payload"), _ack(ears, "b1")
    await rt.close()


async def test_没有这个日志_拒_字节数不像话拒(tmp_path):
    broker, c, ears, rt, logs = await _台(tmp_path)
    await rt._on_cmd(_cmd("proc_log", {"name": "nope"}, "n1", c))
    await broker.drain()
    assert _ack(ears, "n1")["reason"] == "no_such_log"
    (logs / "a.log").write_text("x")
    await rt._on_cmd(_cmd("proc_log", {"name": "a", "bytes": 0}, "n2", c))
    await broker.drain()
    assert _ack(ears, "n2")["reason"].startswith("payload")
    await rt.close()


async def test_没有录包能力_不报这项(tmp_path):
    broker, c, ears, rt, logs = await _台(tmp_path, mapper=False)
    assert "proc_log" not in ears.by["capabilities"][-1]["tasks"]
    await rt.close()


async def test_满是控制字符的日志_回执编码之后也装得进一个包(tmp_path):
    """带颜色的日志(ESC[31m……):JSON 里每个控制字符变成 6 个字节。裁到回执整个编码之后不到
    broker 的单包上限(256 KB),留足余量。"""
    broker, c, ears, rt, logs = await _台(tmp_path)
    line = b"\x1b[31m" + b"\x01\x02\x03" * 20 + b"\x1b[0m\n"
    (logs / "slam.log").write_bytes(line * 5000)                       # 约 400 KB
    await rt._on_cmd(_cmd("proc_log", {"name": "slam", "bytes": 128 * 1024}, "j1", c))
    await broker.drain()
    a = _ack(ears, "j1")
    assert a["result"] == "accepted", a
    assert len(json.dumps(a, ensure_ascii=False).encode()) < 200_000
    d = a["data"]
    assert d["truncated"] is True and d["text"].startswith("\x1b[31m"), "照样从行首截"
    await rt.close()


async def test_指到日志目录外面的链接_当没有(tmp_path):
    broker, c, ears, rt, logs = await _台(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("key")
    (logs / "evil.log").symlink_to(secret)
    await rt._on_cmd(_cmd("proc_log", {"name": "evil"}, "s1", c))
    await broker.drain()
    assert _ack(ears, "s1")["reason"] == "no_such_log"
    await rt._on_cmd(_cmd("proc_log", {}, "s2", c))
    await broker.drain()
    assert "evil" not in [x["name"] for x in _ack(ears, "s2")["data"]["logs"]], "列表里也不列"
    await rt.close()


def test_日志目录_从进程管理器经编排报到录包服务(tmp_path):
    from d1max_agent.mapping import MappingService
    from d1max_patrol.app.procs import ProcManager
    pm = ProcManager(tmp_path / "logs")
    assert pm.log_dir == tmp_path / "logs"

    class 编排:
        log_dir = tmp_path / "logs"
    svc = MappingService(编排(), bags_root=tmp_path / "b", maps_out=tmp_path / "m",
                         work_dir=tmp_path / "w")
    assert svc.log_dir == tmp_path / "logs"
    assert MappingService(object(), bags_root=tmp_path / "b", maps_out=tmp_path / "m",
                          work_dir=tmp_path / "w").log_dir is None


async def test_不进幂等记录_重投的再查一次(tmp_path):
    """回执带着上百 KB 的日志:记进幂等记录的话代理起来要全量重放。重投同一条命令就再查一次。"""
    broker, c, ears, rt, logs = await _台(tmp_path)
    (logs / "slam.log").write_text("第一次\n")
    await rt._on_cmd(_cmd("proc_log", {"name": "slam"}, "i1", c))
    await broker.drain()
    (logs / "slam.log").write_text("第二次\n")
    await rt._on_cmd(_cmd("proc_log", {"name": "slam"}, "i1", c))
    await broker.drain()
    a = _ack(ears, "i1")
    assert a["result"] == "accepted" and a["data"]["text"] == "第二次\n", a
    idem = tmp_path / "agent" / "idempotency.jsonl"
    assert not idem.exists() or "第一次" not in idem.read_text(encoding="utf-8")
    await rt.close()


async def test_尾巴封顶128KiB_列表最多50个(tmp_path):
    """普通的 150 KiB 日志(编码后没胀过线):照样只给最后 128 KiB。55 个日志只列最新的 50 个。"""
    broker, c, ears, rt, logs = await _台(tmp_path)
    (logs / "slam.log").write_bytes((b"y" * 99 + b"\n") * 1536)          # 150 KiB
    await rt._on_cmd(_cmd("proc_log", {"name": "slam", "bytes": 10**6}, "c1", c))
    await broker.drain()
    d = _ack(ears, "c1")["data"]
    assert 120 * 1024 <= d["bytes"] <= 128 * 1024, d["bytes"]
    os.utime(logs / "slam.log", (1000, 1000))
    for i in range(55):
        (logs / f"p{i:02d}.log").write_text("x")
        os.utime(logs / f"p{i:02d}.log", (2000 + i, 2000 + i))
    await rt._on_cmd(_cmd("proc_log", {}, "c2", c))
    await broker.drain()
    names = [x["name"] for x in _ack(ears, "c2")["data"]["logs"]]
    assert len(names) == 50 and names[0] == "p54", names[:3]
    await rt.close()


async def test_大回执在锁外发_弱网上传的时候别的命令照样处理(tmp_path):
    """内审应修 1:回执的发送要等 broker 收完整包(弱网上上百 KB 要好几秒)。读类命令的回执以前在命令
    锁里发,这几秒里遥控续租、监护心跳都排在锁后面,租约会过期。"""
    import asyncio

    from test_runtime_maps import _abort
    broker, c, ears, rt, logs = await _台(tmp_path)
    (logs / "slam.log").write_text("x\n" * 1000)
    gate, stuck = asyncio.Event(), asyncio.Event()
    real = rt.transport.publish

    async def 慢(topic, payload, **k):
        if b'"command_id":"big"' in payload:
            stuck.set()
            await gate.wait()                          # 弱网:这个包一直在传
        return await real(topic, payload, **k)
    rt.transport.publish = 慢
    big = asyncio.get_running_loop().create_task(
        rt._on_cmd(_cmd("proc_log", {"name": "slam"}, "big", c)))
    await asyncio.wait_for(stuck.wait(), 2.0)
    await asyncio.wait_for(rt._on_cmd(_abort("nope", "ab1", c)), 2.0)   # 不许等那个大包
    await broker.drain()
    assert [a for a in ears.by["cmd/ack"] if a["command_id"] == "ab1"]
    gate.set()
    await asyncio.wait_for(big, 2.0)
    await broker.drain()
    assert _ack(ears, "big")["result"] == "accepted"
    await rt.close()


async def test_列表时文件没了_读不了_照样回执(tmp_path, monkeypatch):
    """内审应修 2:列表那一路读目录出错以前没人接,命令不回回执(站点等到 504)。"""
    from d1max_agent import proc_logs
    broker, c, ears, rt, logs = await _台(tmp_path)
    (logs / "a.log").write_text("x")
    (logs / "b.log").write_text("y")
    real = os.lstat

    def 没了(p, *a, **k):
        if str(p).endswith("a.log"):
            raise FileNotFoundError(2, "没了", str(p))
        return real(p, *a, **k)
    monkeypatch.setattr(proc_logs.os, "lstat", 没了)
    await rt._on_cmd(_cmd("proc_log", {}, "l1", c))
    await broker.drain()
    assert [x["name"] for x in _ack(ears, "l1")["data"]["logs"]] == ["b"], "那一个跳过"

    def 炸(*a, **k):
        raise PermissionError(13, "Permission denied " + "/很长的路径" * 80)
    monkeypatch.setattr(proc_logs, "list_logs", 炸)
    await rt._on_cmd(_cmd("proc_log", {}, "l2", c))
    await broker.drain()
    r = _ack(ears, "l2")["reason"]
    assert r.startswith("read_failed") and len(r) <= 200, r
    monkeypatch.setattr(proc_logs, "tail", 炸)
    await rt._on_cmd(_cmd("proc_log", {"name": "b"}, "l3", c))
    await broker.drain()
    r = _ack(ears, "l3")["reason"]
    assert r.startswith("read_failed") and len(r) <= 200, r
    await rt.close()


async def test_管道_硬链接_当没有_不会卡住(tmp_path):
    """内审小问题 1:检查之后打开之前被换成管道(FIFO),以前在线程里 open 永远卡住、命令锁永远占着;
    日志目录里放一个硬链接指到别处的文件,以前照读。现在按句柄核:只认普通文件、只有一个名字。"""
    import asyncio
    broker, c, ears, rt, logs = await _台(tmp_path)
    os.mkfifo(logs / "pipe.log")
    secret = tmp_path / "secret.txt"
    secret.write_text("key")
    os.link(secret, logs / "hard.log")
    for name, cid in (("pipe", "f1"), ("hard", "f2")):
        await asyncio.wait_for(rt._on_cmd(_cmd("proc_log", {"name": name}, cid, c)), 3.0)
        await broker.drain()
        assert _ack(ears, cid)["reason"] == "no_such_log", _ack(ears, cid)
    await rt._on_cmd(_cmd("proc_log", {}, "f3", c))
    await broker.drain()
    assert _ack(ears, "f3")["data"]["logs"] == [], "列表里也不列"
    await rt.close()


async def test_编码预算真的管用_整包在broker上限以内(tmp_path):
    """内审小问题 2:以前的测试把预算放到 300 KiB 也是绿的。四分之一是控制字符的日志,128 KiB 原样
    编码要约 290 KB,超过 broker 单包上限(262144):必须按预算裁。"""
    from d1max_agent.runtime import _dumps
    broker, c, ears, rt, logs = await _台(tmp_path)
    line = b"\x01" * 25 + b"a" * 74 + b"\n"
    (logs / "slam.log").write_bytes(line * 1400)                         # 140 KB
    await rt._on_cmd(_cmd("proc_log", {"name": "slam", "bytes": 128 * 1024}, "e1", c))
    await broker.drain()
    a = _ack(ears, "e1")
    assert len(_dumps(a)) < 200_000, len(_dumps(a))
    assert a["data"]["truncated"] is True and a["data"]["text"].startswith("\x01")
    await rt.close()


async def test_过期的_要重启了_都不查(tmp_path):
    import json

    from test_runtime_maps import T

    from d1max_contract.messages import Command
    from d1max_contract.transport import Message
    broker, c, ears, rt, logs = await _台(tmp_path)
    (logs / "a.log").write_text("x")
    old = Command(command_id="x1", task_id="t", kind="proc_log", issued_at=c.ms - 90_000,
                  expires_at=c.ms - 30_000, control_epoch=1, payload={})
    await rt._on_cmd(Message(T.cmd, json.dumps(old.to_wire()).encode(), 1, False))
    await broker.drain()
    assert _ack(ears, "x1")["result"] == "expired"
    rt._restarting = True
    await rt._on_cmd(_cmd("proc_log", {}, "x2", c))
    await broker.drain()
    assert _ack(ears, "x2")["reason"] == "restarting"
    rt._restarting = False
    await rt.close()
