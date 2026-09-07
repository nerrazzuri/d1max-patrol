"""版本那五条路由。装机、升级、回滚,加上改上装属性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_patrol.app.identity import CONFIRM_PHRASE
from d1max_patrol.app.server import AppServer
from d1max_patrol.engine.release import (
    MANIFEST_NAME,
    Layout,
    commit,
    current_name,
    read_pending,
    tree_sha256,
)

from .conftest import get_err, get_json, make_ctx, post


def _pkg(root: Path, name: str) -> Path:
    where = root / name
    (where / "bin").mkdir(parents=True, exist_ok=True)
    (where / "bin" / "run.py").write_text("print(1)\n", encoding="utf-8")
    (where / MANIFEST_NAME).write_text(json.dumps({
        "name": name, "version": "0.2.0", "content_sha256": tree_sha256(where),
        "requires_mission_schema": 1, "built_at": "2026-09-20T03:11:00Z",
    }), encoding="utf-8")
    return where


@pytest.fixture
def rel_server(bridge, tmp_path):
    """一台已经记过「没装上装」、盘上装着一版的机器。"""
    from d1max_patrol.app.identity import write_payload

    payload_file = tmp_path / "payload.json"
    write_payload(has=False, by="装机", now_ms=1, path=payload_file,
                  confirm=CONFIRM_PHRASE)
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt",
                   payload_file=payload_file)
    重启记录: list = []
    ctx.restart = 重启记录.append
    s = AppServer(ctx, port=0)
    s.start()
    s.重启记录 = 重启记录
    s.payload_file = payload_file
    yield s
    s.stop()


def test_刚装完什么都没有时也答得出来(rel_server):
    got = get_json(rel_server, "/api/release")
    assert got["current"] == "" and got["installed"] == [] and got["pending"] is None


def test_装包之后列得出来(rel_server, tmp_path):
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    assert post(rel_server, "/api/release/install",
                {"package": str(pkg)}) == 200
    got = get_json(rel_server, "/api/release")
    assert got["installed"] == ["2026-09-20-77b2de"]


def test_装一个坏包要409而不是500(rel_server, tmp_path):
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    (pkg / "bin" / "run.py").write_text("偷偷改了\n", encoding="utf-8")
    err = get_err(rel_server, "/api/release/install", 409,
                  method="POST", payload={"package": str(pkg)})
    assert "哈希对不上" in json.dumps(err, ensure_ascii=False)


def test_装机路径不许指到别处(rel_server):
    """包路径是外面给的。这条路由不许被当成一个任意目录的读取器。"""
    get_err(rel_server, "/api/release/install", 400,
            method="POST", payload={"package": 123})


def test_切版本前先跑升级前自检(rel_server, tmp_path):
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    post(rel_server, "/api/release/install", {"package": str(pkg)})
    got = get_json(rel_server, "/api/release/activate",
                   method="POST", payload={"name": "2026-09-20-77b2de"})
    assert got["precheck"]["ok"] is True
    assert got["restart"]["kind"] == "service"
    layout = Layout(root=rel_server._ctx.release_root)
    assert current_name(layout) == "2026-09-20-77b2de"
    assert read_pending(layout) is not None
    assert len(rel_server.重启记录) == 1


def test_自检没过就不许切而且链一动不动(rel_server, tmp_path):
    """电量不到一半。这台狗的假设备默认电量在别处设,这里直接把它调下去。"""
    rel_server._ctx.device.batt = 12.0
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    post(rel_server, "/api/release/install", {"package": str(pkg)})
    err = get_err(rel_server, "/api/release/activate", 409,
                  method="POST", payload={"name": "2026-09-20-77b2de"})
    assert "battery" in json.dumps(err, ensure_ascii=False)
    layout = Layout(root=rel_server._ctx.release_root)
    assert current_name(layout) == ""
    assert read_pending(layout) is None
    assert rel_server.重启记录 == []


def test_没记过上装属性的机器不许升(bridge, tmp_path):
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt",
                   payload_file=tmp_path / "没记过.json")
    ctx.restart = lambda _plan: None
    s = AppServer(ctx, port=0)
    s.start()
    try:
        pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
        post(s, "/api/release/install", {"package": str(pkg)})
        err = get_err(s, "/api/release/activate", 409,
                      method="POST", payload={"name": "2026-09-20-77b2de"})
        assert "payload" in json.dumps(err, ensure_ascii=False)
    finally:
        s.stop()


def test_装了上装的机器重启方案是整机重启(rel_server, tmp_path):
    from d1max_patrol.app.identity import write_payload
    write_payload(has=True, by="老王", now_ms=2, path=rel_server.payload_file,
                  confirm=CONFIRM_PHRASE)
    get_json(rel_server, "/api/identity/payload", method="PUT",
            payload={"has_payload": True, "by": "老王", "confirm": CONFIRM_PHRASE})
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    post(rel_server, "/api/release/install", {"package": str(pkg)})
    got = get_json(rel_server, "/api/release/activate",
                   method="POST", payload={"name": "2026-09-20-77b2de"})
    assert got["restart"]["kind"] == "machine"


def test_装了上装就不许自动升(rel_server, tmp_path):
    get_json(rel_server, "/api/identity/payload", method="PUT",
            payload={"has_payload": True, "by": "老王", "confirm": CONFIRM_PHRASE})
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    post(rel_server, "/api/release/install", {"package": str(pkg)})
    err = get_err(rel_server, "/api/release/activate", 409, method="POST",
                  payload={"name": "2026-09-20-77b2de", "auto": True})
    assert "自动" in json.dumps(err, ensure_ascii=False)


def test_人工回滚退回上一版(rel_server, tmp_path):
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        post(rel_server, "/api/release/install",
             {"package": str(_pkg(tmp_path / name, name))})
    post(rel_server, "/api/release/activate", {"name": "2026-09-06-a3f9c1"})
    commit(Layout(root=rel_server._ctx.release_root))    # 把装机那次坐实
    post(rel_server, "/api/release/activate", {"name": "2026-09-20-77b2de"})
    got = get_json(rel_server, "/api/release/rollback", method="POST",
                   payload=None)
    assert got["rolled_back_to"] == "2026-09-06-a3f9c1"
    layout = Layout(root=rel_server._ctx.release_root)
    assert current_name(layout) == "2026-09-06-a3f9c1"


def test_改上装属性要确认语(rel_server):
    get_err(rel_server, "/api/identity/payload", 400,
            method="PUT", payload={"has_payload": True, "by": "谁",
                                   "confirm": "嗯"})
    assert get_json(rel_server, "/api/identity")["has_payload"] is False


def test_改完之后身份立刻跟着变(rel_server):
    get_json(rel_server, "/api/identity/payload", method="PUT",
            payload={"has_payload": True, "by": "老王", "confirm": CONFIRM_PHRASE})
    got = get_json(rel_server, "/api/identity")
    assert got["has_payload"] is True and got["payload_by"] == "老王"
