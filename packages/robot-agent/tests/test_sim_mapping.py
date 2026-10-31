"""W54 演示站点的假建图:接真的录包、打包服务,预览跟着仿真机器人长,建出的图带按路算的 build.json。"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from d1max_agent.mapping import MappingService
from d1max_agent.sim_mapping import SimMappingOrchestrator


def _map_dir(tmp: Path) -> Path:
    d = tmp / "src"
    d.mkdir()
    w, h = 200, 100                                      # 10 m × 5 m,0.05 m 一格
    px = bytearray([254]) * (w * h)
    for c in range(w):
        px[c] = px[(h - 1) * w + c] = 0
    (d / "floor.pgm").write_bytes(f"P5\n{w} {h}\n255\n".encode() + bytes(px))
    (d / "floor.yaml").write_text("image: floor.pgm\nresolution: 0.05\norigin: [-1.0, -2.5, 0.0]\n"
                                  "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    (d / "frames.json").write_text("{}")
    (d / "prior.mm").write_bytes(b"prior")
    return d


class 位置:
    def __init__(self) -> None:
        self.p = (0.0, 0.0, 0.0)

    def __call__(self):
        return self.p


def _svc(tmp: Path, pose) -> tuple[MappingService, SimMappingOrchestrator]:
    orch = SimMappingOrchestrator(_map_dir(tmp), bags_dir=tmp / "outbox" / "bags",
                                  work=tmp / "work", pose=pose, sample_s=0.01, preview_s=0.02,
                                  build_s=0.0)
    return MappingService(orch, bags_root=tmp / "outbox" / "bags",
                          maps_out=tmp / "outbox" / "maps"), orch


async def _walk(pose: 位置, pts) -> None:
    for x, y in pts:
        pose.p = (x, y, 0.0)
        await asyncio.sleep(0.03)


def test_走一圈回到起点_建出一版_闭合了路程对(tmp_path):
    pose = 位置()
    svc, orch = _svc(tmp_path, pose)

    async def go():
        await svc.start("map-1", target=("main", "20261031-1"), task_id="t1")
        await _walk(pose, [(0, 0), (4, 0), (8, 0), (8, 2), (4, 2), (0, 2), (0, 0.2)])
        pv = svc.preview(0)
        assert pv["live"] and pv["seq"] > 0 and "png" in pv
        assert base64.b64decode(pv["png"])[:4] == b"\x89PNG"
        assert pv["pose"][:2] == [0, 0.2] and len(pv["trail"]) >= 5
        await svc.stop()
        return await svc.finish()
    ref, mode = asyncio.run(go())
    assert (ref.map_id, ref.version, mode) == ("main", "20261031-1", "live")
    out = tmp_path / "outbox" / "maps" / "main" / "20261031-1"
    b = json.loads((out / "build.json").read_text())
    lc = b["loop_closure"]
    assert lc["used"] and lc["closures"] == 1 and lc["path_m"] == pytest.approx(19.8, abs=0.5)
    assert lc["max_shift_m"] < 0.3
    cov = json.loads((out / "coverage.json").read_text())
    assert cov["path"][0] == [0.0, 0.0]
    assert (out / "floor.pgm").read_bytes() == (tmp_path / "src" / "floor.pgm").read_bytes()


def test_没走回起点_不闭合(tmp_path):
    pose = 位置()
    svc, _ = _svc(tmp_path, pose)

    async def go():
        await svc.start("map-1", target=("main", "v2"), task_id="t1")
        await _walk(pose, [(0, 0), (3, 0), (6, 0)])
        await svc.stop()
        return await svc.finish()
    asyncio.run(go())
    b = json.loads((tmp_path / "outbox" / "maps" / "main" / "v2" / "build.json").read_text())
    assert not b["loop_closure"]["used"] and b["loop_closure"]["closures"] == 0
