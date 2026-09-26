"""升级前检查(W00c6d,决策 9 P0)。老服务的 ``selfcheck`` 七项随 W00c5e 退役了;现在切版本只回一个拒绝
原因、人在切之前看不到清单,装好之后槽里被改坏了也不再核。

- 清单是纯函数(老 ``selfcheck`` 的做法:取值和判断分开),切版本用**同一份**判,第一个拦住的项的原因码
  照旧,回执里带整份清单。
- ``release_precheck {name}``:算一遍、不做任何事,清单放在回执里。
- 定位器、感知、外参三项现在「还没部署」、不拦(W09/W11 接上之后挂到 ``precheck_sources``)。
"""

from __future__ import annotations

import json

from test_runtime_maps import _cmd, _发布台

from d1max_agent.engine import release as rel
from d1max_agent.release_precheck import PrecheckInputs, first_block, precheck

NEW = "2026-09-25-bbbbbb"


def _好(**kw) -> PrecheckInputs:
    base = dict(name=NEW, current="2026-09-20-aaaaaa", installed=True, package_error="",
                can_switch=True, disk_ok=True, battery_pct=80.0, busy="", sources=())
    base.update(kw)
    return PrecheckInputs(**base)


def _项(items) -> dict:
    return {i.name: i for i in items}


def test_全都好_每一项都过_没有拦的():
    items = precheck(_好())
    assert all(i.ok for i in items) and first_block(items) == ""
    names = [i.name for i in items]
    for n in ("busy", "battery", "disk", "version", "installed", "package", "agent_start",
              "localizer", "perception", "extrinsic"):
        assert n in names, n


def test_定位器感知外参_还没部署_写明白_不拦():
    got = _项(precheck(_好()))
    for n in ("localizer", "perception", "extrinsic"):
        assert got[n].ok and not got[n].blocking and "还没部署" in got[n].detail


def test_接上来源之后_不健康就拦():
    from d1max_agent.release_precheck import SourceCheck
    items = precheck(_好(sources=(SourceCheck("localizer", False, "定位器 3 s 没出位姿"),)))
    got = _项(items)
    assert not got["localizer"].ok and got["localizer"].blocking
    assert first_block(items) == "localizer"


def test_每一项不过的原因码_跟以前切版本回的一样():
    cases = [(dict(busy="在跑任务 goto-1"), "busy"),
             (dict(battery_pct=None), "battery_unknown"),
             (dict(battery_pct=29.9), "low_battery"),
             (dict(disk_ok=False), "storage_full"),
             (dict(name="2026-09-20-aaaaaa"), "already_running"),
             (dict(installed=False), "not_installed"),
             (dict(package_error="包的哈希对不上"), "package_corrupt"),
             (dict(can_switch=False), "no_agent_start")]
    for kw, code in cases:
        items = precheck(_好(**kw))
        assert first_block(items) == code, (kw, code)
        bad = [i for i in items if not i.ok]
        assert bad and bad[0].detail, "不过要说清为什么"


def test_好几项不过_原因码按以前的先后_清单里全列出来():
    items = precheck(_好(busy="在录包", battery_pct=10.0, disk_ok=False))
    assert first_block(items) == "busy"
    assert {i.name for i in items if not i.ok} == {"busy", "battery", "disk"}


def test_电量门槛是30():
    assert first_block(precheck(_好(battery_pct=30.0))) == ""


def test_没装的版本_不去核包():
    got = _项(precheck(_好(installed=False, package_error="")))
    assert not got["installed"].ok and "没装" in got["installed"].detail


# ------------------------------------------------------------ 槽里的包现在还对不对


def _槽(tmp_path, name=NEW):
    layout = rel.Layout(root=tmp_path / "opt")
    pkg = tmp_path / "pkgs" / name
    (pkg / "src").mkdir(parents=True)
    (pkg / "src" / "x.py").write_text("x = 1\n")
    (pkg / "release.json").write_text(json.dumps({
        "name": name, "version": "0.9", "content_sha256": rel.tree_sha256(pkg),
        "requires_mission_schema": 1, "built_at": "2026-09-25T00:00:00Z"}))
    rel.stage(layout, pkg, now_ms=1)
    return layout


def test_槽里的包_装好之后建了venv_跑出了pycache_照样认(tmp_path):
    layout = _槽(tmp_path)
    slot = layout.release_dir(NEW)
    (slot / "venv" / "lib").mkdir(parents=True)
    (slot / "venv" / "lib" / "a.py").write_text("a = 1\n")
    (slot / "src" / "__pycache__").mkdir()
    (slot / "src" / "__pycache__" / "x.cpython-310.pyc").write_bytes(b"\0")
    assert rel.verify_slot(layout, NEW).name == NEW


def test_槽里的包被改了一个字节_核不过(tmp_path):
    import pytest
    layout = _槽(tmp_path)
    (layout.release_dir(NEW) / "src" / "x.py").write_text("x = 2\n")
    with pytest.raises(rel.ReleaseError, match="对不上"):
        rel.verify_slot(layout, NEW)


# ------------------------------------------------------------ 代理:命令


def _ack(ears):
    return ears.by["cmd/ack"][-1]


async def test_release_precheck_回执里带清单_什么都不做(tmp_path):
    broker, c, ears, relops, dog, rt = await _发布台(tmp_path)
    await rt.start()
    await broker.drain()
    caps = ears.by["capabilities"][-1]["tasks"]
    assert "release_precheck" in caps, "站点据此知道这台狗会回清单"
    await rt._on_cmd(_cmd("release_precheck", {"name": NEW}, "p1", c))
    await broker.drain()
    ack = _ack(ears)
    assert ack["result"] == "accepted", ack
    d = ack["data"]
    assert d["name"] == NEW and d["ok"] is True and d["blocking"] == []
    assert {x["name"] for x in d["checks"]} >= {"busy", "battery", "package", "localizer"}
    assert relops.calls == [] and not rt._restarting, "只查,不切"
    await rt.close()


async def test_release_precheck_有不过的_照样收下_清单里标出来(tmp_path):
    broker, c, ears, relops, dog, rt = await _发布台(tmp_path)
    await rt.start()
    relops.disk = False
    await rt._on_cmd(_cmd("release_precheck", {"name": NEW}, "p2", c))
    await broker.drain()
    d = _ack(ears)["data"]
    assert d["ok"] is False and d["blocking"] == ["disk"]
    await rt.close()


async def test_切版本被拒_回执里带整份清单_原因码照旧(tmp_path):
    broker, c, ears, relops, dog, rt = await _发布台(tmp_path)
    await rt.start()
    relops.disk = False
    await rt._on_cmd(_cmd("release_activate", {"name": NEW}, "a1", c))
    await broker.drain()
    ack = _ack(ears)
    assert ack["reason"] == "storage_full" and ack["data"]["blocking"] == ["disk"]
    assert relops.calls == []
    await rt.close()


async def test_槽里的包坏了_不切(tmp_path):
    broker, c, ears, relops, dog, rt = await _发布台(tmp_path)
    await rt.start()
    relops.corrupt = "包的哈希对不上: 自述写 aa,算出 bb"
    await rt._on_cmd(_cmd("release_activate", {"name": NEW}, "a2", c))
    await broker.drain()
    ack = _ack(ears)
    assert ack["reason"] == "package_corrupt" and relops.calls == [] and not rt._restarting
    pkg = next(x for x in ack["data"]["checks"] if x["name"] == "package")
    assert "对不上" in pkg["detail"]
    await rt.close()


async def test_切版本都过_照旧收下_不带清单(tmp_path):
    broker, c, ears, relops, dog, rt = await _发布台(tmp_path)
    await rt.start()
    await rt._on_cmd(_cmd("release_activate", {"name": NEW}, "a3", c))
    await broker.drain()
    assert _ack(ears)["result"] == "accepted"
    await rt.close()


async def test_接上健康来源_不健康就拦切版本_来源自己炸了也按不健康(tmp_path):
    """W09/W11 接上之后的样子:运行时的 ``precheck_sources`` 里挂一个来源。"""
    from d1max_agent.release_precheck import SourceCheck
    broker, c, ears, relops, dog, rt = await _发布台(tmp_path)
    await rt.start()

    async def 定位器():
        return SourceCheck("localizer", False, "3 s 没出位姿")
    rt.precheck_sources.append(定位器)
    await rt._on_cmd(_cmd("release_activate", {"name": NEW}, "s1", c))
    await broker.drain()
    ack = _ack(ears)
    assert ack["reason"] == "localizer" and "localizer" in ack["data"]["blocking"]

    async def 炸():
        raise RuntimeError("桥断了")
    炸.check_name = "perception"
    rt.precheck_sources[:] = [炸]
    await rt._on_cmd(_cmd("release_precheck", {"name": NEW}, "s2", c))
    await broker.drain()
    got = {x["name"]: x for x in _ack(ears)["data"]["checks"]}
    assert not got["perception"]["ok"] and "桥断了" in got["perception"]["detail"]
    assert relops.calls == []
    await rt.close()


async def test_退版本_又忙又读不到电量_先说忙(tmp_path):
    """退版本不走清单,但原因码的先后跟切版本一样:忙在电量前面(读电量会让出,收下之前还要再看一眼忙)。"""
    from d1max_contract.messages import MapPose
    broker, c, ears, relops, dog, rt = await _发布台(tmp_path)
    await rt.start()
    goto = {"target": MapPose(map_id="m", map_version="1", frame_id="map", x=5.0, y=0.0,
                              yaw=0.0).to_wire()}
    await rt._on_cmd(_cmd("goto", goto, "b1", c))
    await rt.step(0.1)

    async def 读不到():
        raise RuntimeError("还没收到第一帧")
    dog.battery = 读不到
    await rt._on_cmd(_cmd("release_rollback", {}, "b2", c))
    await broker.drain()
    assert _ack(ears)["reason"] == "busy"
    await rt.close()
