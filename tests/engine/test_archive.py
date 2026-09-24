"""归档目录:事件流、原子状态文件、照片命名、manifest 快照。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from d1max_agent.engine.archive import (
    RunArchive,
    list_runs,
    read_events,
    read_manifest,
    read_state,
    read_telemetry,
)

AT = datetime(2026, 9, 1, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def archive(tmp_path, sample_mission):
    a = RunArchive(tmp_path, sample_mission, started_at=AT)
    yield a
    a.close()


def test_目录名是任务名加UTC时间戳(archive, tmp_path, sample_mission):
    assert archive.path == tmp_path / sample_mission.mission / "20260901T101500Z"
    assert archive.path.is_dir()


def test_照片目录一开始就建好(archive):
    assert (archive.path / "photos").is_dir()


def test_事件是一行一条的jsonl(archive):
    archive.append_event("state", frm="IDLE", to="PREFLIGHT")
    archive.append_event("nav", waypoint="P1")
    events = read_events(archive.path)
    assert [e["kind"] for e in events] == ["state", "nav"]
    assert events[0]["frm"] == "IDLE"
    assert events[1]["waypoint"] == "P1"


def test_每条事件都自带时间戳而且是递增的(archive):
    for i in range(5):
        archive.append_event("tick", i=i)
    ts = [e["ts_ms"] for e in read_events(archive.path)]
    assert ts == sorted(ts)


def test_事件立刻落盘而不是等关闭(archive):
    """崩溃恢复全靠这个 —— 攒在缓冲里的事件等于没写。"""
    archive.append_event("state", to="RUNNING")
    assert read_events(archive.path), "还没 close 就该能读到"


def test_事件里的中文不会被转义(archive):
    archive.append_event("note", reason="定位丢了")
    assert "定位丢了" in (archive.path / "events.jsonl").read_text(encoding="utf-8")


def test_遥测走的是另一条流(archive):
    archive.append_event("state", to="RUNNING")
    archive.append_telemetry(x=1.0, y=2.0, battery=88.0)
    assert len(read_events(archive.path)) == 1
    assert read_telemetry(archive.path)[0]["battery"] == pytest.approx(88.0)


def test_坏行不静默跳过而是报出第几行(archive):
    archive.append_event("ok", n=1)
    with (archive.path / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("这不是 json\n")
    with pytest.raises(ValueError, match="第 2 行"):
        read_events(archive.path)


def test_状态文件是整个换掉而不是就地改(archive):
    archive.write_state({"state": "RUNNING", "waypoint": 0})
    archive.write_state({"state": "DONE", "waypoint": 3})
    assert read_state(archive.path) == {"state": "DONE", "waypoint": 3}
    assert not list(archive.path.glob("*.tmp")), "临时文件要收干净"


def test_状态文件任何时刻读出来都是完整的json(archive):
    """断电正好断在写一半上,读出来必须要么是旧的、要么是新的,不能是半个。"""
    for i in range(50):
        archive.write_state({"state": "RUNNING", "i": i})
        json.loads((archive.path / "state.json").read_text(encoding="utf-8"))


def test_照片名带点位名和相机名和时间(archive):
    p = archive.photo_path("P1_transformer", "front", at=AT)
    assert p.name == "P1_transformer__front__20260901T101500Z.jpg"
    assert p.parent == archive.path / "photos"


def test_存照片存的就是原始字节(archive):
    p = archive.save_photo("P1_transformer", "front", b"\xff\xd8jpegdata", at=AT)
    assert p.read_bytes() == b"\xff\xd8jpegdata"


def test_manifest_里有任务快照和环境指纹(archive, sample_mission):
    archive.write_manifest({"proto_version": 1, "sdk": "0.1.1"})
    data = read_manifest(archive.path)
    assert data["fingerprint"]["sdk"] == "0.1.1"
    assert data["mission"]["mission"] == sample_mission.mission
    assert data["mission"]["waypoints"], "路线要固化成快照 —— 设计 spec §6.1"
    assert data["started_at"] == "20260901T101500Z"


def test_任务定义快照跟着报告走而不是引用外面的文件(archive, sample_mission):
    """别人改了 missions/*.yaml,历史报告不该跟着变。"""
    archive.write_manifest({})
    names = [w["name"] for w in read_manifest(archive.path)["mission"]["waypoints"]]
    assert names == [w.name for w in sample_mission.waypoints]


def test_收尾会补上汇总但不丢指纹(archive):
    archive.write_manifest({"sdk": "0.1.1"})
    archive.finish({"succeeded": 2, "failed": 0})
    data = read_manifest(archive.path)
    assert data["summary"]["succeeded"] == 2
    assert data["fingerprint"]["sdk"] == "0.1.1", "收尾不该把指纹冲掉"


def test_同一秒起两次不会写进同一个目录(tmp_path, sample_mission):
    """现场手抖点两下,两次运行的数据混在一起最难查。"""
    a = RunArchive(tmp_path, sample_mission, started_at=AT)
    b = RunArchive(tmp_path, sample_mission, started_at=AT)
    assert a.path != b.path
    a.close()
    b.close()


def test_列运行记录是按时间倒着来的(tmp_path, sample_mission):
    made = [RunArchive(tmp_path, sample_mission, started_at=AT + timedelta(minutes=i))
            for i in range(3)]
    for m in made:
        m.close()
    assert [p.name for p in list_runs(tmp_path)] == [m.path.name for m in reversed(made)]


def test_列运行记录会跨任务名收齐(tmp_path, sample_mission):
    from dataclasses import replace as dc_replace
    a = RunArchive(tmp_path, sample_mission, started_at=AT)
    b = RunArchive(tmp_path, dc_replace(sample_mission, mission="另一个任务"),
                   started_at=AT + timedelta(minutes=1))
    assert {p for p in list_runs(tmp_path)} == {a.path, b.path}
    a.close()
    b.close()


def test_没跑过的目录读状态返回None(tmp_path):
    assert read_state(tmp_path / "nope") is None
    assert read_manifest(tmp_path / "nope") is None
    assert read_events(tmp_path / "nope") == []
    assert list_runs(tmp_path / "nope") == []


def test_关了之后再写会重新打开文件(archive):
    """收尾之后补一条事件是合法的,不该炸。"""
    archive.append_event("a")
    archive.close()
    archive.append_event("b")
    assert [e["kind"] for e in read_events(archive.path)] == ["a", "b"]
