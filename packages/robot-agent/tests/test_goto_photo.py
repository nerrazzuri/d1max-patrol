"""goto 到了拍一张(W17:事件派遣的现场照片)。照片跟巡检点位的一样进这一趟的归档;拍不成不算没到。"""

from __future__ import annotations

from test_patrol_task import _跑, 钟

from d1max_adapter_sim.robot import SimRobot
from d1max_agent.assembly import build_engine
from d1max_agent.bridges.sim_media import PLACEHOLDER_JPEG, sim_media
from d1max_agent.events import EventBook
from d1max_agent.tasks.engine_goto import EngineGotoTask
from d1max_contract.geometry import Pose
from d1max_contract.messages import MapPose, TaskState


async def _goto(tmp_path, *, media, photo):
    c = 钟()
    r = SimRobot(now_ms=c, max_vx=1.0, max_wz=1.5, stop_latency_s=0.2)
    await r.connect()
    await r.acquire_control()
    parts = build_engine(r, runs_root=tmp_path / "runs", now_ms=c, monotonic=lambda: c.mono,
                         map_id="m", home=Pose.from_xy_yaw(0.0, 0.0), media=media(c))
    book = EventBook(tmp_path / "ev.jsonl", boot_id="b1", now_ms=c)
    t = EngineGotoTask(task_id="incident-1", target=MapPose(map_id="m", map_version="1",
                                                            frame_id="map", x=1.0, y=0.0, yaw=0.0),
                       max_speed_mps=None, parts=parts, events=book, now_ms=c, photo=photo)
    await t.start()
    await _跑(c, r, parts, t, 500)
    await parts.engine.aclose()
    return t, parts


async def test_到了拍一张_照片进这一趟的归档(tmp_path):
    t, parts = await _goto(tmp_path, media=sim_media, photo="front")
    assert t.state is TaskState.DONE, (t.state, t.detail)
    shots = list((tmp_path / "runs").rglob("*.jpg"))
    assert len(shots) == 1 and shots[0].read_bytes() == PLACEHOLDER_JPEG
    assert "front" in parts.engine.cameras


async def test_没要照片就不拍(tmp_path):
    t, _ = await _goto(tmp_path, media=sim_media, photo=None)
    assert t.state is TaskState.DONE
    assert not list((tmp_path / "runs").rglob("*.jpg"))


async def test_拍不成也算到了_记一笔(tmp_path):
    """相机没配(或取图失败、盘满):狗到了就是到了。巡检的拍照点照旧按失败算(见 test_patrol_task)。"""
    t, parts = await _goto(tmp_path, media=lambda c: {}, photo="front")
    assert t.state is TaskState.DONE, (t.state, t.detail)
    assert not list((tmp_path / "runs").rglob("*.jpg"))
    assert parts.engine.cameras == []
    log = "".join(p.read_text() for p in (tmp_path / "runs").rglob("*.jsonl"))
    assert "photo_failed" in log, log[-500:]
