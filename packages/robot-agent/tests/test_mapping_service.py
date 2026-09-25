"""W00c5d 第二部分:录包与重建接到发件箱上(假的建图编排,不起 ROS)。"""

from __future__ import annotations

import json

import pytest

from d1max_agent.mapping import (
    DONE,
    MappingError,
    MappingService,
    bag_classify,
    map_classify,
    map_settled,
)
from d1max_agent.outbox import Outbox
from d1max_contract.maps import MapRef


class 假编排:
    def __init__(self, bags: object, work) -> None:
        self.bags, self.work = bags, work
        self.fail = ""

    async def start_record(self, name):
        (self.bags / name).mkdir(parents=True)
        (self.bags / name / "metadata.yaml").write_text("x")
        (self.bags / name / f"{name}_0.mcap").write_bytes(b"lidar" * 100)
        return self.bags / name

    async def stop_record(self):
        return sorted(self.bags.iterdir())[-1]

    async def rebuild(self, bag, map_id):
        if self.fail:
            raise RuntimeError(self.fail)
        self.work.mkdir(parents=True, exist_ok=True)
        for ext, data in ((".pgm", b"P5"), (".yaml", b"res"), (".posegraph", b"g")):
            (self.work / f"{map_id}{ext}").write_bytes(data)
        return self.work / map_id


@pytest.fixture
def svc(tmp_path):
    bags, work = tmp_path / "outbox" / "bags", tmp_path / "work"
    return MappingService(假编排(bags, work), bags_root=bags, maps_out=tmp_path / "outbox" / "maps",
                          work_dir=work)


async def test_录包_停了才算安定_重建出一张图_清单最后写(svc):
    await svc.start("yard")
    assert svc.recording and svc.last_bag == "yard"
    with pytest.raises(MappingError):
        await svc.start("again")
    assert not svc.bag_settled(svc.bags_root / "yard"), "还在录"
    await svc.stop()
    assert svc.bag_settled(svc.bags_root / "yard")
    ref = await svc.build("yard", "estate-1", "9")
    out = svc.maps_out / "estate-1" / "9"
    assert map_settled(out)
    assert MapRef.from_wire(json.loads((out / "map.json").read_text())) == ref
    assert sorted(f.name for f in ref.files) == ["estate-1.pgm", "estate-1.posegraph",
                                                 "estate-1.yaml"]
    with pytest.raises(MappingError):
        await svc.build("yard", "estate-1", "9")     # 同版本狗上已经有一份
    with pytest.raises(MappingError):
        await svc.build("nope", "estate-1", "10")    # 包不在


async def test_重建期间包不删_失败了放开(svc):
    await svc.start("yard")
    await svc.stop()
    svc.orch.fail = "slam 起不来"
    with pytest.raises(RuntimeError):
        await svc.build("yard", "estate-1", "9")
    assert svc.bag_settled(svc.bags_root / "yard") and not svc.held
    assert not (svc.maps_out / "estate-1" / "9").exists()


def test_分类_点开头的不传_清单排在最后():
    assert bag_classify("yard_0.mcap") == 5 and bag_classify(DONE) is None
    assert map_classify("estate-1.pgm") == 4 and map_classify("map.json") == 5
    assert map_classify(".x") is None


async def test_包和图经发件箱传_攒到一半的不传(svc, tmp_path):
    from test_outbox import 假站点
    site = 假站点()
    await svc.start("yard")
    await svc.stop()
    (svc.maps_out / ".building" / "x@1").mkdir(parents=True)
    (svc.maps_out / ".building" / "x@1" / "x.pgm").write_bytes(b"half")
    bags = Outbox(tmp_path / "outbox", cap_bytes=2**30, sink=site, sn="A", now_ms=lambda: 1,
                  sub="bags", run_depth=1, classify=bag_classify, settled=svc.bag_settled)
    maps = Outbox(tmp_path / "outbox", cap_bytes=2**30, sink=site, sn="A", now_ms=lambda: 1,
                  sub="maps", run_depth=2, classify=map_classify, settled=map_settled)
    for _ in range(3):
        bags.step()
        maps.step()
    assert ("yard", "yard_0.mcap") in site.files and not (svc.bags_root / "yard").exists()
    assert not any(r.startswith(".building") for r, _ in site.files), "攒到一半的不传"
    bags.close()
    maps.close()


async def test_重建期间这个包算没安定(svc):
    await svc.start("yard")
    await svc.stop()
    seen = []
    real = svc.orch.rebuild

    async def 看一眼(bag, map_id):
        seen.append(svc.bag_settled(svc.bags_root / "yard"))
        return await real(bag, map_id)
    svc.orch.rebuild = 看一眼
    await svc.build("yard", "estate-1", "9")
    assert seen == [False], "正在拿它重建:发件箱不许删"
    assert svc.bag_settled(svc.bags_root / "yard")
