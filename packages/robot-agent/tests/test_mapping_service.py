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
from d1max_contract.maps import GEOMETRY_FILES, MapRef


class 假编排:
    def __init__(self, bags: object, work) -> None:
        self.bags, self.work = bags, work
        self.fail = ""
        self.made = GEOMETRY_FILES
        self.live = None
        self.package_fail = ""
        self.mode = ""

    async def start_record(self, name, live_map_id=None):
        self.live = live_map_id
        (self.bags / name).mkdir(parents=True)
        (self.bags / name / "metadata.yaml").write_text("x")
        (self.bags / name / f"{name}_0.mcap").write_bytes(b"lidar" * 100)
        return self.bags / name

    async def stop_record(self):
        return sorted(self.bags.iterdir())[-1]

    async def package(self, bag, map_id):
        self.packaged = (bag, map_id)
        if self.package_fail:
            from d1max_patrol.app.mapping import MappingError as OrchError
            raise OrchError(self.package_fail)
        return await self.rebuild(bag, map_id, _mode="live")

    async def rebuild(self, bag, map_id, _mode="bag"):
        self.mode = _mode
        if self.fail:
            raise RuntimeError(self.fail)
        out = self.work / map_id
        out.mkdir(parents=True, exist_ok=True)
        for name in self.made:
            (out / name).write_bytes(name.encode())
        return out


@pytest.fixture
def svc(tmp_path):
    bags, work = tmp_path / "outbox" / "bags", tmp_path / "work"
    return MappingService(假编排(bags, work), bags_root=bags, maps_out=tmp_path / "outbox" / "maps")


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
    assert [f.name for f in ref.files] == list(GEOMETRY_FILES)
    assert (out / "prior.mm").read_bytes() == b"prior.mm"
    assert not (svc.orch.work / "estate-1" / "prior.mm").exists(), "挪过去的,不是拷的(先验几百 MB)"
    with pytest.raises(MappingError):
        await svc.build(bag, "estate-1", "9")        # 同版本狗上已经有一份
    with pytest.raises(MappingError):
        await svc.build("nope", "estate-1", "10")    # 包不在


async def test_重建失败了放开_包照样留着(svc):
    await svc.start("yard")
    await svc.stop()
    bag = svc.last_bag
    svc.orch.fail = "MOLA 起不来"
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


async def test_产物缺一样就算没建成_不登记(svc):
    await svc.start("yard")
    await svc.stop()
    svc.orch.made = GEOMETRY_FILES[:-1]
    with pytest.raises(MappingError, match="build.json"):
        await svc.build(svc.last_bag, "estate-1", "9")
    assert not (svc.maps_out / "estate-1" / "9").exists() and not svc.held


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
    svc = MappingService(假编排(bags, tmp_path / "w"), bags_root=bags, maps_out=maps)
    assert not (maps / ".building").exists()
    assert (bags / "yard-20260925T010000Z" / DONE).exists() and not svc.recording


async def test_盘紧了传完的包不留着备重建(svc):
    """W00c5d 第三部分内部评审:留着的包把发件箱撑满,巡检就被拒 storage_full。盘紧了就让位 ——
    站点上有一份。"""
    from d1max_agent.mapping import storage_pressure
    from d1max_contract.storage import StorageFacts
    await svc.start("yard")
    await svc.stop()
    bag = svc.bags_root / svc.last_bag
    assert not svc.bag_settled(bag), "没重建、不满 7 天:留着"
    facts = {"f": StorageFacts(disk_used_ratio=0.5, outbox_bytes=40, outbox_cap_bytes=100,
                               backlog_files=0, backlog_bytes=0, oldest_backlog_s=None)}
    svc.pressure = lambda: storage_pressure(facts["f"])
    assert not svc.bag_settled(bag)
    facts["f"] = StorageFacts(disk_used_ratio=0.5, outbox_bytes=51, outbox_cap_bytes=100,
                              backlog_files=0, backlog_bytes=0, oldest_backlog_s=None)
    assert svc.bag_settled(bag), "发件箱过了上限的一半:传完的包可以删"
    facts["f"] = StorageFacts(disk_used_ratio=0.8, outbox_bytes=1, outbox_cap_bytes=100,
                              backlog_files=0, backlog_bytes=0, oldest_backlog_s=None)
    assert svc.bag_settled(bag), "盘用到八成:一样"
    svc.held.add(bag.name)
    assert not svc.bag_settled(bag), "正在拿它重建:照样不删"
    assert storage_pressure(None) is False


async def test_建好的图放进本地库_库那头出错不算没建成(svc):
    """W09c 决定 7:建图的狗激活这一版时不用从站点下回来。"""
    got = []

    class 库:
        def adopt(self, src, ref):
            got.append((sorted(p.name for p in src.iterdir()), ref))   # 这时还没有清单:发件箱不动它
            if len(got) > 1:
                raise OSError("盘满了")

    svc.keeper = 库()
    await svc.start("yard")
    await svc.stop()
    ref = await svc.build(svc.last_bag, "estate-1", "9")
    assert got == [(sorted(GEOMETRY_FILES), ref)]
    ref2 = await svc.build(svc.last_bag, "estate-1", "10")
    assert ref2.version == "10" and (svc.maps_out / "estate-1" / "10" / "map.json").is_file()


async def test_建图用的哪种射线_记下来给事件用(svc):
    """内审应修 4:退回模拟射线只写在 build.json 里,没人看。"""
    await svc.start("yard")
    await svc.stop()
    svc.orch.made = GEOMETRY_FILES
    real = svc.orch.rebuild

    async def 出图(bag, map_id):
        out = await real(bag, map_id)
        (out / "build.json").write_text(json.dumps({"grid": {"rays": "synthetic:没给录包"}}))
        return out
    svc.orch.rebuild = 出图
    await svc.build(svc.last_bag, "estate-1", "9")
    assert svc.last_rays == "synthetic:没给录包"


async def test_边走边建_开录带目标_停下之后打包在线建好的_放进本地库(svc):
    """W09c2:录包的同时在线建;停下之后打包(不在录包上重跑 MOLA),跟重建一样收产物、写清单。"""
    await svc.start("yard", target=("estate-1", "5"))
    assert svc.orch.live == "estate-1" and svc.live == ("estate-1", "5")
    await svc.stop()
    bag = svc.last_bag
    assert svc.pending == (bag, "estate-1", "5") and svc.live is None
    svc.pressure = lambda: True                           # 盘紧了传完的包都能删 —— 这个不行
    assert not svc.bag_settled(svc.bags_root / bag), "要拿它打包(读逐帧扫描):不许删"
    svc.pressure = lambda: False
    ref, mode = await svc.finish()
    assert mode == "live" and svc.orch.packaged[1] == "estate-1" and svc.orch.mode == "live"
    assert ref.version == "5" and map_settled(svc.maps_out / "estate-1" / "5")
    assert svc.pending is None and svc.bag_settled(svc.bags_root / bag)


async def test_在线那份打不成_退回从录包建(svc):
    await svc.start("yard", target=("estate-1", "5"))
    await svc.stop()
    svc.orch.package_fail = "建图没出 traj.tum"
    ref, mode = await svc.finish()
    assert mode == "bag" and svc.orch.mode == "bag" and ref.version == "5"
    assert "traj.tum" in svc.last_fallback


async def test_退回从录包建也不成_这一版没建成_包留着(svc):
    await svc.start("yard", target=("estate-1", "5"))
    await svc.stop()
    svc.orch.package_fail = "x"
    svc.orch.fail = "MOLA 起不来"
    with pytest.raises(RuntimeError):
        await svc.finish()
    assert svc.pending is None and not svc.held
    assert not svc.bag_settled(svc.bags_root / svc.last_bag)


async def test_边走边建的版本狗上已经有一份在传_开录就拒(svc):
    (svc.maps_out / "estate-1" / "5").mkdir(parents=True)
    with pytest.raises(MappingError, match="已经有一份"):
        await svc.start("yard", target=("estate-1", "5"))
    assert not svc.recording


async def test_代理重启_边走边建的这一趟接着收尾_不丢(tmp_path):
    """内审应修 1:待打包、正在在线建的只在内存里,代理重启(升级、崩了)就丢了,站点一直当它「在建」。
    开录时在包目录记一个标记;起来时按它接着收尾(存下的在线那份打得成就打,不成从录包建)。"""
    bags, work = tmp_path / "outbox" / "bags", tmp_path / "work"
    svc = MappingService(假编排(bags, work), bags_root=bags, maps_out=tmp_path / "outbox" / "maps")
    await svc.start("yard", target=("estate-1", "5"))
    bag = svc.last_bag
    assert json.loads((bags / bag / ".live").read_text()) == {"map_id": "estate-1", "version": "5",
                                                            "task_id": ""}
    # 录着的时候代理没了(没停录):重启
    again = MappingService(假编排(bags, work), bags_root=bags,
                           maps_out=tmp_path / "outbox" / "maps")
    assert again.pending == (bag, "estate-1", "5") and bag in again.held
    assert (bags / bag / DONE).is_file(), "录不下去了:当录完"
    ref, mode = await again.finish()
    assert ref.version == "5" and not (bags / bag / ".live").exists()


async def test_停录先打完成标记再记待打包(svc, monkeypatch):
    """内审应修 3:先记待打包、再打完成标记;打标记炸了(盘满)待打包就永远挂着、之后每次开录都拒。"""
    await svc.start("yard", target=("estate-1", "5"))
    from pathlib import Path as P
    real = P.touch

    def 盘满(self, *a, **k):
        if self.name == DONE:
            raise OSError("盘满了")
        return real(self, *a, **k)
    monkeypatch.setattr(P, "touch", 盘满)
    with pytest.raises(OSError):
        await svc.stop()
    assert svc.pending is None and not svc.held


async def test_收尾时正在录就好好停下(svc):
    """代理停服务:录包、在线建图按正常停录收(在线建图 SIGINT 存盘),重启后接着打包。"""
    await svc.start("yard", target=("estate-1", "5"))
    await svc.shutdown()
    assert not svc.recording and svc.pending is not None
    await svc.shutdown()                                  # 没在录:什么都不做


async def test_上一趟还在打包_开录就拒_收尾时版本已在传也算没建成(svc):
    await svc.start("yard", target=("estate-1", "5"))
    await svc.stop()
    with pytest.raises(MappingError, match="还在打包"):
        await svc.start("yard2")
    (svc.maps_out / "estate-1" / "5").mkdir(parents=True)
    with pytest.raises(MappingError, match="已经有一份"):
        await svc.finish()
    assert svc.pending is None and not svc.held


async def test_恢复标记写不进去_回滚停掉录包与在线建图_之后还能再开(svc, monkeypatch):
    """外审阻断 1:原来子进程已经起了、``.live`` 写不进去(盘满、只读重挂)就抛出去,``recording`` 还是
    False —— 停不了、收尾也不停,两个进程一直写盘。"""
    import errno
    from pathlib import Path as P
    stopped = []
    real_stop = svc.orch.stop_record

    async def 停(*a, **k):
        stopped.append(True)
        return await real_stop(*a, **k)
    svc.orch.stop_record = 停
    real = P.write_text

    def 盘满(self, *a, **k):
        if self.name == ".live":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *a, **k)
    monkeypatch.setattr(P, "write_text", 盘满)
    with pytest.raises(OSError):
        await svc.start("yard", target=("estate-1", "5"), task_id="t1")
    assert stopped == [True] and not svc.recording and svc.live is None
    monkeypatch.setattr(P, "write_text", real)
    await svc.start("yard2", target=("estate-1", "5"), task_id="t2")
    assert svc.recording


async def test_边走边建记着开录那条命令的号_重启之后也在(tmp_path):
    """外审阻断 3:站点按命令号认「这一次失败了没有」,狗上发的失败、建好了要带开录那条命令的号。"""
    bags, work = tmp_path / "outbox" / "bags", tmp_path / "work"
    svc = MappingService(假编排(bags, work), bags_root=bags, maps_out=tmp_path / "outbox" / "maps")
    await svc.start("yard", target=("estate-1", "5"), task_id="mapping-s1")
    assert json.loads((bags / svc.last_bag / ".live").read_text())["task_id"] == "mapping-s1"
    await svc.stop()
    assert svc.pending_task == "mapping-s1"
    again = MappingService(假编排(bags, work), bags_root=bags,
                           maps_out=tmp_path / "outbox" / "maps")
    assert again.pending_task == "mapping-s1"


# ------------------------------------------------------------------ 建图预览(W09f)

def _snap(svc, map_id, **kw):
    """编排的预览进程写的快照(``d1max_localizer.livemap.Snapshots`` 的格式)。"""
    d = svc.orch.preview_dir(map_id)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"seq": 3, "run": "yard-x", "res": 0.1, "origin": [-2.0, -1.0], "width": 40,
           "height": 30, "pose": [1.0, 2.0, 0.5], "trail": [[0.0, 0.0], [1.0, 2.0]],
           "frames": 12, "dropped": 30, "written_at": 1000.0, "png": "iVBORw0KGgo="} | kw
    (d / "preview.json").write_text(json.dumps(doc))
    return doc


def _preview_orch(svc, tmp_path):
    svc.orch.preview_dir = lambda map_id: tmp_path / "work" / ".w" / map_id / "preview"
    svc.orch.preview_error = ""


async def test_预览_没在边走边建是_live_false(svc, tmp_path, monkeypatch):
    _preview_orch(svc, tmp_path)
    assert svc.preview(0) == {"live": False}
    await svc.start("yard")                                   # 只录包
    assert svc.preview(0) == {"live": False}


async def test_预览_边走边建还没第一张_有了带图_since_相同不带图(svc, tmp_path, monkeypatch):
    import d1max_agent.mapping as M
    monkeypatch.setattr(M.time, "time", lambda: 1004.26)
    _preview_orch(svc, tmp_path)
    await svc.start("yard", ("estate-1", "9"))
    d = svc.preview(0)
    assert d["live"] is True and d["seq"] == 0 and d["recording"] is True
    assert (d["map_id"], d["version"]) == ("estate-1", "9") and "png" not in d
    doc = _snap(svc, "estate-1")
    d = svc.preview(0)
    assert d["png"] == doc["png"] and d["seq"] == 3 and d["age_s"] == 4.3
    for k in ("run", "res", "origin", "width", "height", "pose", "trail", "frames", "dropped"):
        assert d[k] == doc[k], k
    assert "written_at" not in d, "给的是多久没更新,不是狗的钟"
    d = svc.preview(3)
    assert "png" not in d and d["seq"] == 3 and d["trail"] == doc["trail"], "没新图:说明照给"
    assert "png" in svc.preview(2)


async def test_预览_停录之后打包之前照给最后一张_打完了没有(svc, tmp_path):
    _preview_orch(svc, tmp_path)
    await svc.start("yard", ("estate-1", "9"))
    _snap(svc, "estate-1")
    await svc.stop()
    d = svc.preview(0)
    assert d["live"] is True and d["recording"] is False and d["seq"] == 3 and "png" in d
    await svc.finish()
    assert svc.preview(0) == {"live": False}


async def test_预览_图太大不带_说太大(svc, tmp_path):
    import d1max_agent.mapping as M
    _preview_orch(svc, tmp_path)
    await svc.start("yard", ("estate-1", "9"))
    _snap(svc, "estate-1", png="A" * (M.PREVIEW_PNG_MAX_B64 + 4))
    d = svc.preview(0)
    assert "png" not in d and d["too_big"] is True


@pytest.mark.parametrize("bad", ["不是 JSON", "[1, 2]", '{"seq": "3"}', '{"seq": true}'])
async def test_预览_快照坏了当没有(svc, tmp_path, bad):
    _preview_orch(svc, tmp_path)
    await svc.start("yard", ("estate-1", "9"))
    d = svc.orch.preview_dir("estate-1")
    d.mkdir(parents=True)
    (d / "preview.json").write_text(bad)
    got = svc.preview(0)
    assert got["live"] is True and got["seq"] == 0 and "png" not in got


async def test_预览_进程没起来带上原因(svc, tmp_path):
    _preview_orch(svc, tmp_path)
    svc.orch.preview_error = "mapview 起不来"
    await svc.start("yard", ("estate-1", "9"))
    assert svc.preview(0)["preview_error"] == "mapview 起不来"


async def test_预览_没写时刻不给多久_别的照给(svc, tmp_path):
    _preview_orch(svc, tmp_path)
    await svc.start("yard", ("estate-1", "9"))
    d = svc.orch.preview_dir("estate-1")
    d.mkdir(parents=True)
    (d / "preview.json").write_text('{"seq": 2, "png": "QQ=="}')
    got = svc.preview(0)
    assert got["seq"] == 2 and got["png"] == "QQ==" and "age_s" not in got
