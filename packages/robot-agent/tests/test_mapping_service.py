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


async def test_录包_停了重建过才算安定_重建出一张图_清单最后写(svc):
    await svc.start("yard")
    bag = svc.last_bag
    assert svc.recording and bag.startswith("yard-2") and bag.endswith("Z"), "包名带录的时刻"
    with pytest.raises(MappingError):
        await svc.start("again")
    assert not svc.bag_settled(svc.bags_root / bag), "还在录"
    await svc.stop()
    assert not svc.bag_settled(svc.bags_root / bag), "录完了没重建:留着当原料"
    ref = await svc.build(bag, "estate-1", "9")
    assert svc.bag_settled(svc.bags_root / bag), "重建过了:传完就可以删"
    out = svc.maps_out / "estate-1" / "9"
    assert map_settled(out)
    assert MapRef.from_wire(json.loads((out / "map.json").read_text())) == ref
    assert sorted(f.name for f in ref.files) == ["estate-1.pgm", "estate-1.posegraph",
                                                 "estate-1.yaml"]
    with pytest.raises(MappingError):
        await svc.build(bag, "estate-1", "9")        # 同版本狗上已经有一份
    with pytest.raises(MappingError):
        await svc.build("nope", "estate-1", "10")    # 包不在


async def test_重建失败了放开_包照样留着(svc):
    await svc.start("yard")
    await svc.stop()
    bag = svc.last_bag
    svc.orch.fail = "slam 起不来"
    with pytest.raises(RuntimeError):
        await svc.build(bag, "estate-1", "9")
    assert not svc.held and not svc.bag_settled(svc.bags_root / bag), "没重建成:原料留着"
    assert not (svc.maps_out / "estate-1" / "9").exists()
    import os as _os
    old = (svc.bags_root / bag / DONE).stat().st_mtime - 8 * 86400
    _os.utime(svc.bags_root / bag / DONE, (old, old))
    assert svc.bag_settled(svc.bags_root / bag), "放够天数:传完就删"


def test_分类_点开头的不传_清单排在最后():
    assert bag_classify("yard_0.mcap") == 5 and bag_classify(DONE) is None
    assert map_classify("estate-1.pgm") == 4 and map_classify("map.json") == 5
    assert map_classify(".x") is None


async def test_包和图经发件箱传_攒到一半的不传(svc, tmp_path):
    from test_outbox import 假站点
    site = 假站点()
    await svc.start("yard")
    await svc.stop()
    bag = svc.last_bag
    (svc.bags_root / bag / ".built").touch()
    (svc.maps_out / ".building" / "x@1").mkdir(parents=True)
    (svc.maps_out / ".building" / "x@1" / "x.pgm").write_bytes(b"half")
    bags = Outbox(tmp_path / "outbox", cap_bytes=2**30, sink=site, sn="A", now_ms=lambda: 1,
                  sub="bags", run_depth=1, classify=bag_classify, settled=svc.bag_settled)
    maps = Outbox(tmp_path / "outbox", cap_bytes=2**30, sink=site, sn="A", now_ms=lambda: 1,
                  sub="maps", run_depth=2, classify=map_classify, settled=map_settled)
    for _ in range(3):
        bags.step()
        maps.step()
    assert (bag, f"{bag}_0.mcap") in site.files and not (svc.bags_root / bag).exists()
    assert not any(r.startswith(".building") for r, _ in site.files), "攒到一半的不传"
    bags.close()
    maps.close()


async def test_重建期间这个包算没安定(svc):
    await svc.start("yard")
    await svc.stop()
    name = svc.last_bag
    seen = []
    real = svc.orch.rebuild

    async def 看一眼(bag, map_id):
        seen.append(svc.bag_settled(svc.bags_root / name))
        return await real(bag, map_id)
    svc.orch.rebuild = 看一眼
    await svc.build(name, "estate-1", "9")
    assert seen == [False], "正在拿它重建:发件箱不许删"
    assert svc.bag_settled(svc.bags_root / name)
    await svc.build(name, "estate-1", "10")                 # 重建过的包再拿来重建一次
    assert seen == [False, False], "重建过(有 .built)也一样:正在用就不删"


async def test_上一次同名的产物不混进这一次(svc):
    await svc.start("yard")
    await svc.stop()
    svc.work_dir.mkdir(parents=True, exist_ok=True)
    (svc.work_dir / "estate-1.data").write_bytes(b"old")   # 上一次留下的,这一次不会出
    ref = await svc.build(svc.last_bag, "estate-1", "9")
    assert "estate-1.data" not in {f.name for f in ref.files}


async def test_发件箱删包跟重建占包是同一把锁(svc, tmp_path):
    import threading
    import time as _t

    from test_outbox import 假站点
    await svc.start("yard")
    await svc.stop()
    bag = svc.bags_root / svc.last_bag
    (bag / ".built").touch()
    bags = Outbox(tmp_path / "outbox", cap_bytes=2**30, sink=假站点(), sn="A", now_ms=lambda: 1,
                  sub="bags", run_depth=1, classify=bag_classify, settled=svc.bag_settled,
                  delete_lock=svc.lock)
    svc.lock.acquire()
    t = threading.Thread(target=lambda: [bags.step() for _ in range(3)])
    t.start()
    _t.sleep(0.5)
    assert bag.exists(), "重建那头拿着锁(查过在、正要占住):发件箱不许删"
    svc.lock.release()
    t.join(10)
    assert not bag.exists()
    bags.close()


def test_起来时收拾_攒到一半的图扔掉_录到一半的包补上完成标记(tmp_path):
    bags, maps = tmp_path / "outbox" / "bags", tmp_path / "outbox" / "maps"
    (maps / ".building" / "x@1").mkdir(parents=True)
    (bags / "yard-20260925T010000Z").mkdir(parents=True)
    svc = MappingService(假编排(bags, tmp_path / "w"), bags_root=bags, maps_out=maps,
                         work_dir=tmp_path / "w")
    assert not (maps / ".building").exists()
    assert (bags / "yard-20260925T010000Z" / DONE).exists() and not svc.recording


async def test_重建只收这张图的那几个后缀_不捡同前缀别的图(svc):
    await svc.start("yard")
    await svc.stop()
    svc.work_dir.mkdir(parents=True, exist_ok=True)
    (svc.work_dir / "estate-1.v2.pgm").write_bytes(b"other")
    ref = await svc.build(svc.last_bag, "estate-1", "9")
    assert "estate-1.v2.pgm" not in {f.name for f in ref.files}
