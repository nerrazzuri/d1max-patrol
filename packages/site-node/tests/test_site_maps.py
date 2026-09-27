"""W00c5d 第二部分:站点的地图目录。同一个地图号 + 版本只登记一次;狗传上来的收齐、哈希对上才登记。"""

from __future__ import annotations

import hashlib
import json

import pytest

from d1max_site.db import SiteDB
from d1max_site.maps import MapCatalog, MapError


@pytest.fixture
def cat(tmp_path):
    return MapCatalog(tmp_path, SiteDB(tmp_path / "site.db"), now_ms=lambda: 1000)


def _files(d, **files):
    d.mkdir(parents=True, exist_ok=True)
    for n, data in files.items():
        (d / n).write_bytes(data)
    return d


def test_导入一个目录_登记_文件带哈希_同版本不许再来(cat, tmp_path):
    src = _files(tmp_path / "src", **{"estate-1.pgm": b"P5 map", "estate-1.yaml": b"res: 0.05"})
    ref = cat.import_dir(src, map_id="estate-1", version="8")
    assert [f.name for f in ref.files] == ["estate-1.pgm", "estate-1.yaml"]
    assert ref.files[0].sha256 == hashlib.sha256(b"P5 map").hexdigest()
    assert cat.get("estate-1", "8") == ref
    assert cat.list()[0]["source"] == "import"
    assert cat.file_path("estate-1", "8", "estate-1.pgm").read_bytes() == b"P5 map"
    with pytest.raises(MapError):
        cat.import_dir(src, map_id="estate-1", version="8")
    for bad in (("estate-1", "8", "map.json"), ("estate-1", "8", "../site.db"),
                ("estate-1", "9", "estate-1.pgm"), ("../x", "8", "a")):
        with pytest.raises(MapError):
            cat.file_path(*bad)


def test_狗传上来的图_收齐而且哈希对上才登记(cat):
    d = cat.incoming / "A" / "estate-1" / "9"
    pgm = b"P5 new map"
    ref = {"map_id": "estate-1", "version": "9",
           "files": [{"name": "estate-1.pgm", "size": len(pgm),
                      "sha256": hashlib.sha256(pgm).hexdigest()}]}
    assert cat.take_uploaded("A", "estate-1", "9") is None, "清单还没到"
    _files(d, **{"map.json": json.dumps(ref).encode()})
    assert cat.take_uploaded("A", "estate-1", "9") is None, "文件还没到"
    _files(d, **{"estate-1.pgm": pgm[:-1]})
    assert cat.take_uploaded("A", "estate-1", "9") is None, "还在传(大小不对)"
    _files(d, **{"estate-1.pgm": pgm})
    got = cat.take_uploaded("A", "estate-1", "9")
    assert got is not None and cat.list()[0]["source"] == "A"
    assert cat.file_path("estate-1", "9", "estate-1.pgm").read_bytes() == pgm
    wrong = cat.incoming / "A" / "estate-1" / "10"
    _files(wrong, **{"map.json": json.dumps(ref).encode()})
    with pytest.raises(MapError):
        cat.take_uploaded("A", "estate-1", "10")


def test_站点没让它建的图不收_版本撞了或文件对不上永远不收(cat):
    import hashlib

    from d1max_site.evidence import PathRefused
    pgm = b"P5 new"
    ref = {"map_id": "estate-1", "version": "9",
           "files": [{"name": "estate-1.pgm", "size": len(pgm),
                      "sha256": hashlib.sha256(pgm).hexdigest()}]}
    body = json.dumps(ref).encode()
    with pytest.raises(PathRefused):
        cat.put_map_chunk("A", "estate-1/9", "map.json", offset=0, data=body, total=len(body))
    with cat.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority) VALUES ('c1','t1','A','map_build',?, 'alice', 1, 0)",
                  (json.dumps({"bag": "b", "map_id": "estate-1", "version": "9"}),))
    assert cat.build_in_flight("estate-1", "9")
    with cat.db.tx() as c:                                    # 狗说这一次建失败了:这个版本号放开
        c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                  "received_at) VALUES ('A','b',1,'e1','map_build_failed',?,1,1)",
                  (json.dumps({"task_id": "t1", "map_id": "estate-1", "version": "9",
                               "reason": "x"}),))
    assert not cat.build_in_flight("estate-1", "9")
    with cat.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority, ack_result) VALUES ('c2','t2','B','map_build',?, 'alice',"
                  " 2, 0, 'rejected')",
                  (json.dumps({"bag": "b", "map_id": "estate-1", "version": "11"}),))
    assert not cat.build_in_flight("estate-1", "11"), "狗没接的那次不算在建"
    cat.put_map_chunk("A", "estate-1/9", "estate-1.pgm", offset=0, data=b"P5 bad",
                      total=len(b"P5 bad"))                   # 大小一样、内容不对
    with pytest.raises(PathRefused):
        cat.put_map_chunk("A", "estate-1/9", "map.json", offset=0, data=body, total=len(body))
    assert cat.list() == [], "不登记"


def test_收图不在请求里重算哈希_改名不拷贝(cat, monkeypatch):
    """W09c 决定 9:几百 MB 的先验,最后一块的请求里再算一遍哈希、再拷一份,60 s 的请求超时不够稳。
    收块时已经算过整个文件的哈希(狗也拿它核过),登记时用它;同一块盘上改名。"""
    import os

    import d1max_site.maps as M
    with cat.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority) VALUES ('c1','t1','A','map_build',?, 'alice', 1, 0)",
                  (json.dumps({"bag": "b", "map_id": "estate-1", "version": "9"}),))
    prior = os.urandom(5000)
    for off in range(0, len(prior), 1000):
        cat.put_map_chunk("A", "estate-1/9", "prior.mm", offset=off, data=prior[off:off + 1000],
                          total=len(prior))
    ino = os.stat(cat.incoming / "A" / "estate-1" / "9" / "prior.mm").st_ino
    body = json.dumps({"map_id": "estate-1", "version": "9", "files": [
        {"name": "prior.mm", "size": len(prior),
         "sha256": hashlib.sha256(prior).hexdigest()}]}).encode()

    def 不许(p):
        raise AssertionError(f"重算了 {p}")
    monkeypatch.setattr(M, "_sha256", 不许)
    cat.put_map_chunk("A", "estate-1/9", "map.json", offset=0, data=body, total=len(body))
    p = cat.file_path("estate-1", "9", "prior.mm")
    assert p.read_bytes() == prior and os.stat(p).st_ino == ino
    assert [f.name for f in cat.get("estate-1", "9").files] == ["prior.mm"]
    assert not (cat.incoming / "A" / "estate-1" / "9").exists()


def test_文件传了一半又重传_记下的哈希作废(cat):
    with cat.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority) VALUES ('c1','t1','A','map_build',?, 'alice', 1, 0)",
                  (json.dumps({"bag": "b", "map_id": "estate-1", "version": "9"}),))
    from d1max_site.evidence import PathRefused
    cat.put_map_chunk("A", "estate-1/9", "x.pgm", offset=0, data=b"good", total=4)
    cat.put_map_chunk("A", "estate-1/9", "x.pgm", offset=0, data=b"ba", total=4)   # 从头重传,没传完
    (cat.incoming / "A" / "estate-1" / "9" / "x.pgm").write_bytes(b"badd")        # 盘上内容变了
    body = json.dumps({"map_id": "estate-1", "version": "9", "files": [
        {"name": "x.pgm", "size": 4, "sha256": hashlib.sha256(b"good").hexdigest()}]}).encode()
    with pytest.raises(PathRefused):
        cat.put_map_chunk("A", "estate-1/9", "map.json", offset=0, data=body, total=len(body))


def _floor(d, rows, *, origin=(0.0, 0.0), res=1.0):
    """从上往下的行:``.`` 可通行、``#`` 墙、``?`` 没扫到。"""
    v = {".": 254, "#": 0, "?": 205}
    h, w = len(rows), len(rows[0])
    (d / "floor.pgm").write_bytes(f"P5\n{w} {h}\n255\n".encode()
                                  + bytes(v[c] for r in rows for c in r))
    (d / "floor.yaml").write_text(f"image: floor.pgm\nresolution: {res}\n"
                                  f"origin: [{origin[0]}, {origin[1]}, 0.0]\nnegate: 0\n"
                                  "occupied_thresh: 0.65\nfree_thresh: 0.196\nmode: trinary\n")


def test_哪里有图_离走过的路太远或不在可通行格子上就说清楚_老版本不查(cat, tmp_path):
    """W09c 决定 5。栅格 20×3 格(1 m 一格),原点 (-1, -1);建图时沿 y=0 从 x=0 走到 x=3。"""
    src = _files(tmp_path / "v1")
    _floor(src, ["..#................?",
                 "......#.............",
                 "...................."], origin=(-1.0, -1.0))
    (src / "coverage.json").write_text(json.dumps(
        {"version": 1, "step_m": 0.5, "path": [[0.5 * i, 0.0] for i in range(7)]}))
    cat.import_dir(src, map_id="yard", version="1")
    ok = cat.reach_problem("yard", "1", [("门口", 1.0, 0.0), ("远一点", 7.5, 0.5)])
    assert ok == ""
    far = cat.reach_problem("yard", "1", [("门口", 1.0, 0.0), ("仓库", 11.0, 0.0)])
    assert "仓库" in far and "8.0 m" in far and "先把那里建进图" in far
    assert "墙" in cat.reach_problem("yard", "1", [("柱子", 5.5, 0.5)])
    assert "墙" in cat.reach_problem("yard", "1", [("上面那排", 1.5, 1.5)]), "PGM 第一行是 y 最大的"
    assert cat.reach_problem("yard", "1", [("下面那排", 1.5, -0.5)]) == ""
    assert "墙" in cat.reach_problem("yard", "1", [("图外", 3.0, 5.0)])
    old = _files(tmp_path / "v0", **{"yard.pgm": b"P5\n1 1\n255\n\x00", "yard.yaml": b"x"})
    cat.import_dir(old, map_id="yard", version="0")
    assert cat.reach_problem("yard", "0", [("随便", 999.0, 0.0)]) == "", "老版本没有 coverage.json"
    assert cat.reach_problem("nope", "1", [("随便", 0.0, 0.0)]) == "", "站点不认识的版本:不挡"


def _cov_version(cat, tmp_path, version, cov=None, yaml_image="floor.pgm"):
    src = _files(tmp_path / f"cv{version}")
    _floor(src, ["....", "....", "...."], origin=(-1.0, -1.0))
    if yaml_image != "floor.pgm":
        (src / "floor.yaml").write_text((src / "floor.yaml").read_text().replace(
            "image: floor.pgm", f"image: {yaml_image}"))
    (src / "coverage.json").write_text(json.dumps(cov or {"version": 1, "path": [[0.0, 0.0]]}))
    cat.import_dir(src, map_id="yard", version=version)


def test_哪里有图_文件太大不读_图名不在清单里不读_缓存有上限(cat, tmp_path, monkeypatch):
    """内审应修 2:狗传上来的 coverage.json、栅格整个读进内存,没有上限;缓存从不清。"""
    import d1max_site.maps as M
    _cov_version(cat, tmp_path, "1", cov={"version": 1, "path": [[0.0, 0.0]] * 50})
    monkeypatch.setattr(M, "MAX_COVERAGE_BYTES", 100)
    with pytest.raises(MapError, match="太大"):
        cat.reach_problem("yard", "1", [("p", 0.0, 0.0)])
    monkeypatch.undo()
    _cov_version(cat, tmp_path, "2", yaml_image="../../site.db")
    with pytest.raises(MapError, match="没有"):
        cat.reach_problem("yard", "2", [("p", 0.0, 0.0)])
    for v in range(3, 3 + M.REACH_CACHE + 3):
        _cov_version(cat, tmp_path, str(v))
        assert cat.reach_problem("yard", str(v), [("p", 0.0, 0.0)]) == ""
    assert len(cat._reaches) <= M.REACH_CACHE


def test_收完之后盘上的字节又变了_记下的哈希不作数(cat):
    """内审小 6:旁注在块锁外面写;记下「大小:修改时刻:哈希」,对不上就现算。"""
    import os
    import time as _t

    from d1max_site.evidence import PathRefused
    with cat.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority) VALUES ('c1','t1','A','map_build',?, 'alice', 1, 0)",
                  (json.dumps({"bag": "b", "map_id": "estate-1", "version": "9"}),))
    cat.put_map_chunk("A", "estate-1/9", "x.pgm", offset=0, data=b"good", total=4)
    f = cat.incoming / "A" / "estate-1" / "9" / "x.pgm"
    _t.sleep(0.01)
    f.write_bytes(b"badd")                                   # 同样大小
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000))
    body = json.dumps({"map_id": "estate-1", "version": "9", "files": [
        {"name": "x.pgm", "size": 4, "sha256": hashlib.sha256(b"good").hexdigest()}]}).encode()
    with pytest.raises(PathRefused):
        cat.put_map_chunk("A", "estate-1/9", "map.json", offset=0, data=body, total=len(body))


def test_登记失败_文件挪回收件目录_下次还能收(cat, monkeypatch):
    """内审小 7:改成挪文件之后,登记炸了文件已经离开收件目录,狗重传清单时站点回「还没收齐」(200),
    狗就把自己那份删了。"""
    with cat.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority) VALUES ('c1','t1','A','map_build',?, 'alice', 1, 0)",
                  (json.dumps({"bag": "b", "map_id": "estate-1", "version": "9"}),))
    cat.put_map_chunk("A", "estate-1/9", "x.pgm", offset=0, data=b"good", total=4)
    body = json.dumps({"map_id": "estate-1", "version": "9", "files": [
        {"name": "x.pgm", "size": 4, "sha256": hashlib.sha256(b"good").hexdigest()}]}).encode()
    real = cat._register

    def 库忙(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(cat, "_register", 库忙)
    with pytest.raises(RuntimeError):
        cat.put_map_chunk("A", "estate-1/9", "map.json", offset=0, data=body, total=len(body))
    assert (cat.incoming / "A" / "estate-1" / "9" / "x.pgm").read_bytes() == b"good"
    assert not (cat.root / "estate-1" / "9").exists()
    monkeypatch.setattr(cat, "_register", real)
    cat.put_map_chunk("A", "estate-1/9", "map.json", offset=0, data=body, total=len(body))
    assert cat.get("estate-1", "9").files[0].name == "x.pgm"


def test_边走边建的狗传上来的图也收(cat):
    """W09c2:站点让它在录包时建这一版(``mapping start`` 带地图号与版本),传上来照样收。"""
    from d1max_site.evidence import PathRefused
    body = json.dumps({"map_id": "estate-1", "version": "5", "files": [
        {"name": "x.pgm", "size": 4, "sha256": hashlib.sha256(b"good").hexdigest()}]}).encode()
    with pytest.raises(PathRefused):
        cat.put_map_chunk("A", "estate-1/5", "x.pgm", offset=0, data=b"good", total=4)
    with cat.db.tx() as c:
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority) VALUES ('c1','t1','A','mapping',?, 'alice', 1, 0)",
                  (json.dumps({"action": "start", "name": "y", "map_id": "estate-1",
                               "version": "5"}),))
    assert cat.build_in_flight("estate-1", "5")
    with cat.db.tx() as c:                                # 停止录包不算让它建(契约也不许带)
        c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, issued_by, "
                  "issued_at, priority) VALUES ('c2','t2','B','mapping',?, 'alice', 1, 0)",
                  (json.dumps({"action": "stop", "map_id": "estate-1", "version": "6"}),))
    assert not cat.build_in_flight("estate-1", "6")
    cat.put_map_chunk("A", "estate-1/5", "x.pgm", offset=0, data=b"good", total=4)
    cat.put_map_chunk("A", "estate-1/5", "map.json", offset=0, data=body, total=len(body))
    assert cat.get("estate-1", "5").files[0].name == "x.pgm"


def test_版本在建没有_按这一次命令的号认失败_历史失败不放开重试(cat):
    """外审阻断 3:原来按(狗、地图号、版本)找任意一条历史失败 —— 第一次失败之后用同一版本重试,
    重试还在跑时第三次请求也被放行,同一版本并发建、并发传。"""
    def 发(cid, task):
        with cat.db.tx() as c:
            c.execute("INSERT INTO commands(command_id, task_id, robot_id, kind, payload, "
                      "issued_by, issued_at, priority) "
                      "VALUES (?,?,'A','map_build',?, 'alice', 1, 0)",
                      (cid, task, json.dumps({"bag": "b", "map_id": "estate", "version": "1"})))

    def 失败(eid, task):
        with cat.db.tx() as c:
            c.execute("INSERT INTO events(robot_id, boot_id, seq, event_id, kind, data, stamp, "
                      "received_at) VALUES ('A','b',?,?,'map_build_failed',?,1,1)",
                      (int(eid[1:]), eid, json.dumps({"task_id": task, "map_id": "estate",
                                                      "version": "1", "reason": "x"})))
    发("c1", "t1")
    assert cat.build_in_flight("estate", "1")
    失败("e1", "t1")
    assert not cat.build_in_flight("estate", "1"), "第一次失败:放开,允许重试"
    发("c2", "t2")
    assert cat.build_in_flight("estate", "1"), "重试还在跑:第三次要拒"
    失败("e2", "t2")
    assert not cat.build_in_flight("estate", "1"), "重试也失败:再放开"
