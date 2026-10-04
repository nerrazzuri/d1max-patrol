"""连续录像在狗上(W18,决策 31):一直录、切好的段挪进录像发件箱、录不了报、攒太多删最旧的。

ffmpeg 换成假的(记下命令行、由测试决定活着还是退了);段文件由测试写进暂存目录。真 ffmpeg 的那条
在最后(有 ffmpeg 才跑)。
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from d1max_agent.recording import STAGING, Recorder


class _假进程:
    def __init__(self, argv, **kw):
        self.argv, self.env = argv, kw.get("env", {})
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, t=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def 录(tmp_path):
    procs: list[_假进程] = []
    events: list[tuple[str, dict]] = []
    t = [100.0]
    disk = [(1000, 100, 900)]

    def popen(argv, **kw):
        p = _假进程(argv, **kw)
        procs.append(p)
        return p
    r = Recorder(tmp_path / "v", ["front", "back"], source=lambda c: ["-i", f"rtsp://x/{c}"],
                 quota_bytes=10_000, emit=lambda k, d: events.append((k, d)),
                 monotonic=lambda: t[0], disk_usage=lambda p: disk[0], popen=popen)
    r.procs, r.events, r.t, r.disk = procs, events, t, disk
    return r


def _段(r, cam, stamp, n=100):
    p = r.root / STAGING / cam / f"{stamp}.mp4"
    p.write_bytes(b"x" * n)
    return p


def _已传(r):
    return sorted(p.relative_to(r.out).as_posix() for p in r.out.rglob("video.mp4"))


def test_命令行_不转码_一分钟一段_分片mp4_UTC(录):
    r = 录
    r.start()
    assert len(r.procs) == 2
    a = r.procs[0].argv
    assert ["-c:v", "copy"] == a[a.index("-c:v"):a.index("-c:v") + 2]
    assert a[a.index("-segment_time") + 1] == "60" and "-strftime" in a
    assert "frag_keyframe" in a[a.index("-segment_format_options") + 1]
    assert a[-1].endswith(f"{STAGING}/front/%Y%m%dT%H%M%SZ.mp4")
    assert r.procs[0].env["TZ"] == "UTC"
    assert r.caps() == {"cameras": ["front", "back"], "segment_s": 60}


def test_切好的段挪进发件箱_正在写的那段不动(录):
    r = 录
    r.start()
    _段(r, "front", "20261004T010000Z")
    _段(r, "front", "20261004T010100Z")                   # 最新的:正在写
    _段(r, "front", "garbage")                            # 名字不合规:扔掉
    _段(r, "back", "20261004T010000Z", 0)                  # 写完了还是空的:扔掉
    _段(r, "back", "20261004T010100Z", 0)                  # 正在写、刚开(空的):不许碰
    r.step()
    assert _已传(r) == ["front/20261004T010000Z/video.mp4"]
    assert (r.root / STAGING / "front" / "20261004T010100Z.mp4").exists()
    assert not (r.root / STAGING / "front" / "garbage.mp4").exists()
    assert not (r.root / STAGING / "back" / "20261004T010000Z.mp4").exists()
    assert (r.root / STAGING / "back" / "20261004T010100Z.mp4").exists(), "ffmpeg 正写着"


def test_ffmpeg退了_报一次_隔10秒重起_收走最后那段_录回来报好了(录):
    r = 录
    r.start()
    _段(r, "front", "20261004T010000Z")
    r.procs[0].returncode = 1                             # 相机不通
    r.step()
    assert _已传(r) == ["front/20261004T010000Z/video.mp4"], "退了:最后那段也写完了"
    assert [k for k, _ in r.events] == ["recording_failed"]
    assert r.events[0][1]["camera"] == "front"
    r.step()
    assert len(r.procs) == 2, "10 秒内不重起"
    r.t[0] += 11
    r.step()
    assert len(r.procs) == 3
    r.procs[2].returncode = 1
    r.step()
    assert [k for k, _ in r.events] == ["recording_failed"], "一直不好:只报一次"
    r.t[0] += 11
    r.step()
    _段(r, "front", "20261004T010200Z")
    _段(r, "front", "20261004T010300Z")
    r.step()
    assert [k for k, _ in r.events] == ["recording_failed", "recording_ok"]


def test_攒太多删最旧的_报一次_不分相机(录):
    r = 录
    r.start()
    for i in range(12):
        _段(r, "front" if i % 2 else "back", f"20261004T01{i:02d}00Z", 1000)
    _段(r, "front", "20261004T015900Z", 1000)
    _段(r, "back", "20261004T015900Z", 1000)
    r.step()
    left = _已传(r)
    assert len(left) == 10, left                          # 配额 10 000 字节
    assert left[0].endswith("20261004T010200Z/video.mp4") or "0102" in "".join(left[:2])
    assert all("T0100" not in x and "T0101" not in x for x in left), "最旧的先删"
    assert [k for k, _ in r.events] == ["recording_dropped"]
    assert r.events[0][1]["count"] == 2 and r.events[0][1]["reason"] == "quota"
    _段(r, "back", "20261004T020000Z", 1000)
    _段(r, "back", "20261004T020100Z", 1000)
    r.step()
    assert [k for k, _ in r.events] == ["recording_dropped"], "一直超:只报一次"


def test_盘紧了先删录像(录):
    r = 录
    r.start()
    _段(r, "front", "20261004T010000Z")
    _段(r, "front", "20261004T010100Z")
    r.step()
    r.disk[0] = (1000, 850, 150)                          # 盘用到 85%
    r.step()
    assert _已传(r) == []
    assert r.events[-1][0] == "recording_dropped" and r.events[-1][1]["reason"] == "disk"


def test_上次被杀掉留下的段_起来就收走(录):
    r = 录
    (r.root / STAGING / "front").mkdir(parents=True)
    _段(r, "front", "20261004T010000Z")
    _段(r, "front", "20261004T010100Z")
    r.start()
    assert len(_已传(r)) == 2, "分片 mp4:被杀掉的那段也放得了"
    r.close()
    assert all(p.returncode is not None for p in r.procs)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="没有 ffmpeg")
def test_真ffmpeg_仿真测试图_切出能放的段(tmp_path):
    from d1max_agent.video_push import lavfi_source
    events = []
    t = [0.0]
    r = Recorder(tmp_path / "v", ["front"], source=lavfi_source, segment_s=1,
                 quota_bytes=2**30, emit=lambda k, d: events.append(k), monotonic=lambda: t[0])
    r.start()
    import time
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and len(list(r.out.rglob("video.mp4"))) < 2:
        r.step()
        time.sleep(0.2)
    r.close()
    segs = sorted(r.out.rglob("video.mp4"))
    assert len(segs) >= 2, events
    for p in segs:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                              "-of", "csv=p=0", str(p)], capture_output=True, text=True)
        assert out.stdout.strip() == "h264", (p, out.stderr)


def test_record参数_auto真狗录前后两路_仿真不录_认不得的相机不起():
    import argparse

    from d1max_agent.main import record_cameras, video_root
    ns = argparse.Namespace
    assert record_cameras(ns(record="auto", hal="d1max")) == ["front", "back"]
    assert record_cameras(ns(record="auto", hal="sim")) == []
    assert record_cameras(ns(record="none", hal="d1max")) == []
    assert record_cameras(ns(record="back", hal="sim")) == ["back"]
    with pytest.raises(SystemExit):
        record_cameras(ns(record="front,side", hal="d1max"))
    from pathlib import Path
    assert video_root(ns(outbox=Path("/data/outbox"))) == Path("/data/outbox")


def test_录像在发件箱里_不算进盘况_攒多了不让狗拒巡检(tmp_path):
    from d1max_agent.outbox import Outbox, OutboxPump
    from d1max_agent.recording import STAGING, SUB, video_classify

    class _Sink:
        def put(self, req):
            from d1max_agent.engine.uploader import SinkError
            raise SinkError("断网")
    ob = tmp_path / "ob"
    runs = Outbox(ob, cap_bytes=10_000, sink=_Sink(), sn="A", now_ms=lambda: 1,
                  not_counted=(SUB, STAGING))
    video = Outbox(ob, cap_bytes=10_000, sink=_Sink(), sn="A", now_ms=lambda: 1, sub=SUB,
                   run_depth=2, classify=video_classify, settled=lambda p: True)
    seg = ob / SUB / "front" / "20261004T010000Z" / "video.mp4"
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"x" * 50_000)                        # 比配额大得多
    (ob / STAGING / "front").mkdir(parents=True)
    (ob / STAGING / "front" / "20261004T010100Z.mp4").write_bytes(b"x" * 50_000)
    (ob / "runs" / "m" / "20261004T010000Z").mkdir(parents=True)
    (ob / "runs" / "m" / "20261004T010000Z" / "events.jsonl").write_bytes(b"y" * 100)
    pump = OutboxPump(runs, uncounted=(video,))
    for b in pump.boxes:
        b.step()
    f = pump.facts()
    assert f.outbox_bytes < 1000 and not f.full(), "录像(100 KB)不算进运行记录的用量"
    assert f.backlog_files == 1, "积压只算运行记录的"
    assert video in pump.boxes, "照样轮着传"


async def test_运行时_报录像能力_每拍走_关的时候停(tmp_path):
    from d1max_adapter_sim.robot import SimRobot
    from d1max_agent.runtime import AgentRuntime
    from d1max_contract.memory_broker import MemoryBroker, MemoryTransport
    from d1max_contract.registration import Registration

    class _录:
        def __init__(self):
            self.calls = []
            self.emit = None

        def start(self):
            self.calls.append("start")
            raise PermissionError("目录建不了")              # 不许连累代理起不来

        def step(self):
            self.calls.append("step")
            if len(self.calls) == 3:
                raise RuntimeError("录像炸了")            # 不许带走这一拍

        def close(self):
            self.calls.append("close")

        def caps(self):
            return {"cameras": ["front"], "segment_s": 60}
    rec = _录()
    clock = [1_800_000_000_000]
    reg = Registration(site_id="s", robot_id="A", credential_fingerprint="sha256:a",
                       issued_at=0, expires_at=10**14)
    rt = AgentRuntime(transport=MemoryTransport(MemoryBroker(), "dogA"), registration=reg,
                      hal=SimRobot(now_ms=lambda: clock[0]), store_dir=tmp_path,
                      now_ms=lambda: clock[0], loaded_map=None, recorder=rec)
    await rt.start()
    assert rec.emit is not None, "事件接到事件簿"
    assert "recording_failed" in (tmp_path / "events.jsonl").read_text(), "起不来报一条"
    for _ in range(3):
        await rt.step(0.1)
    assert rt._extra_tasks()["recording"] == {"cameras": ["front"], "segment_s": 60}
    await rt.close()
    assert rec.calls[0] == "start" and rec.calls.count("step") == 3 and rec.calls[-1] == "close"
