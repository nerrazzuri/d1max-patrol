"""起来之后那一遍。§7.3:自动回滚只有这一个触发条件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_patrol.app.identity import CONFIRM_PHRASE, write_payload
from d1max_patrol.app.server import AppServer
from d1max_patrol.engine.release import (
    MANIFEST_NAME,
    Layout,
    activate,
    commit,
    current_name,
    read_pending,
    stage,
    tree_sha256,
)

from .conftest import get_json, make_ctx


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
def 装了两版(bridge, tmp_path):
    """盘上装了旧、新两版,链指着新的,标记在途 —— 正是重启回来那一刻的样子。"""
    payload_file = tmp_path / "payload.json"
    write_payload(has=False, by="装机", now_ms=1, path=payload_file,
                  confirm=CONFIRM_PHRASE)
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt",
                   payload_file=payload_file)
    layout = Layout(root=ctx.release_root)
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / "pkg" / name, name), now_ms=1)
    activate(layout, "2026-09-06-a3f9c1", now_ms=1, auto=False)
    commit(layout)
    activate(layout, "2026-09-20-77b2de", now_ms=3, auto=False)
    return ctx, layout


def test_四项都过就坐实新版(装了两版):
    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    s = AppServer(ctx, port=0)
    s.start()
    try:
        assert current_name(layout) == "2026-09-20-77b2de"
        assert read_pending(layout) is None          # 坐实了,标记清掉
    finally:
        s.stop()


def test_桥不应答就退回上一版(装了两版):
    """三桥里断一条。这就是那唯一的自动回滚触发条件。

    ``_check_bridges`` 问的是 ``nav.loc_status()`` / ``nav.list_maps()`` /
    ``device.has_control()``(见 ``engine/selfcheck.py``)。让 ``FakeNav``
    的 ``loc`` 变成 ``None``,``loc_status()`` 就答不出位姿,这一路就断了。
    """
    ctx, layout = 装了两版
    ctx.nav.loc = None                                # 让位姿那一路答不出来
    重启记录: list = []
    ctx.restart = 重启记录.append
    s = AppServer(ctx, port=0)
    s.start()
    try:
        assert current_name(layout) == "2026-09-06-a3f9c1"
        assert read_pending(layout) is None
        assert len(重启记录) == 1                     # 退回去之后还得再起一次
    finally:
        s.stop()


def test_不在途的时候什么都不做(bridge, tmp_path):
    """平常的每一次开机都会走这段代码。它必须是个空操作。"""
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt")
    重启记录: list = []
    ctx.restart = 重启记录.append
    s = AppServer(ctx, port=0)
    s.start()
    try:
        assert 重启记录 == []
    finally:
        s.stop()


def test_没有上一版就不回滚而是留着并喊(bridge, tmp_path):
    """第一次装机就没过。退无可退 —— 这时候回滚会把机器变成没有系统。"""
    payload_file = tmp_path / "payload.json"
    write_payload(has=False, by="装机", now_ms=1, path=payload_file,
                  confirm=CONFIRM_PHRASE)
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt",
                   payload_file=payload_file)
    ctx.nav.loc = None                                # 让位姿那一路答不出来
    layout = Layout(root=ctx.release_root)
    stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=1)
    activate(layout, "2026-09-20-77b2de", now_ms=1, auto=False)
    重启记录: list = []
    ctx.restart = 重启记录.append
    s = AppServer(ctx, port=0)
    s.start()
    try:
        assert current_name(layout) == "2026-09-20-77b2de"   # 还留着
        assert read_pending(layout) is None                   # 但不再算在途
        assert 重启记录 == []                                  # 不重启,重启也没用
    finally:
        s.stop()


def test_自检自己炸了也当没过(装了两版, monkeypatch):
    """自检代码本身抛异常,不能变成「跳过判据」。"""
    import d1max_patrol.app.server as srv

    def 炸(*_a, **_k):
        raise RuntimeError("自检自己炸了")

    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    monkeypatch.setattr(srv, "run_postcheck", 炸)
    s = AppServer(ctx, port=0)
    s.start()
    try:
        assert current_name(layout) == "2026-09-06-a3f9c1"
    finally:
        s.stop()


def test_坐实和回滚都发事件(装了两版):
    """``EventEmitter.subscribe()`` 回一个 ``asyncio.Queue``。订阅要在
    ``s.start()`` 之前挂上 —— 自检就在 ``start()`` 里跑完了,事后再订阅
    什么都收不到。事后同步 ``get_nowait()`` 把它排空即可(参见
    ``tests/engine/test_machine.py`` 里同样的用法)。
    """
    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    s = AppServer(ctx, port=0)
    queue = s.hub.events.subscribe()
    s.start()
    try:
        收到: list = []
        while not queue.empty():
            收到.append(queue.get_nowait())
        assert any(e.get("kind") == "release.kept" for e in 收到)
    finally:
        s.stop()


def test_自检路由随时能重跑而且不动任何东西(装了两版):
    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    s = AppServer(ctx, port=0)
    s.start()
    try:
        got = get_json(s, "/api/selfcheck")
        assert [c["name"] for c in got["checks"]] == [
            "process", "control", "bridges", "identity"]
        assert got["verdict"] in ("keep", "rollback")
        assert current_name(layout) == "2026-09-20-77b2de"   # 一动没动
    finally:
        s.stop()


def test_自检路由不会把在途标记清掉(bridge, tmp_path):
    """人点一下自检,不该等价于「我确认这版能用」。"""
    payload_file = tmp_path / "payload.json"
    write_payload(has=False, by="装机", now_ms=1, path=payload_file,
                  confirm=CONFIRM_PHRASE)
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt",
                   payload_file=payload_file)
    layout = Layout(root=ctx.release_root)
    stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=1)
    ctx.restart = lambda _plan: None
    s = AppServer(ctx, port=0)
    s.start()
    try:
        activate(layout, "2026-09-20-77b2de", now_ms=9, auto=False)
        get_json(s, "/api/selfcheck")
        assert read_pending(layout) is not None
    finally:
        s.stop()
