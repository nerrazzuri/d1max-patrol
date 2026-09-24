"""起来之后那一遍。§7.3:自动回滚只有这一个触发条件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_agent.engine.release import (
    MANIFEST_NAME,
    Layout,
    activate,
    commit,
    current_name,
    read_pending,
    stage,
    tree_sha256,
)
from d1max_patrol.app.identity import CONFIRM_PHRASE, write_payload
from d1max_patrol.app.server import AppServer

from .conftest import get_json, make_ctx


def 假睡本(记录: list | None = None):
    """一个不会真等的 ``sleep``。

    B2 给自检的 ``bridges`` 那一项加了有界重试(3 次探测、每次之间等
    ``BRIDGE_WAIT_S`` 秒)。桥断掉的用例要是走真 ``asyncio.sleep``,这个文件
    每跑一次就白等 4 秒 —— §8.5 的「时间必须可注入」正是为了这个,所以
    ``AppServer`` 收一个 ``postcheck_sleep``,测试里一律注入这个。
    """
    async def 睡(秒: float) -> None:
        if 记录 is not None:
            记录.append(秒)
    return 睡


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
    等过: list = []
    s = AppServer(ctx, port=0, postcheck_sleep=假睡本(等过))
    queue = s.hub.events.subscribe()                  # 订阅要在 start() 之前挂上
    s.start()
    try:
        assert current_name(layout) == "2026-09-06-a3f9c1"
        assert len(等过) == 2                         # 重试的两次间隔,都是假的
        assert read_pending(layout) is None
        assert len(重启记录) == 1                     # 退回去之后还得再起一次
        收到: list = []
        while not queue.empty():
            收到.append(queue.get_nowait())
        rolled = [e for e in 收到 if e.get("kind") == "release.rolled_back"]
        assert len(rolled) == 1
        assert rolled[0]["rolled_back_to"] == "2026-09-06-a3f9c1"
        assert rolled[0]["name"] == "2026-09-20-77b2de"
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
    s = AppServer(ctx, port=0, postcheck_sleep=假睡本())
    queue = s.hub.events.subscribe()                  # 订阅要在 start() 之前挂上
    s.start()
    try:
        assert current_name(layout) == "2026-09-20-77b2de"   # 还留着
        assert read_pending(layout) is None                   # 但不再算在途
        assert 重启记录 == []                                  # 不重启,重启也没用
        收到: list = []
        while not queue.empty():
            收到.append(queue.get_nowait())
        stuck = [e for e in 收到 if e.get("kind") == "release.stuck"]
        assert len(stuck) == 1
        assert stuck[0]["name"] == "2026-09-20-77b2de"
        assert stuck[0]["previous"] == ""                     # 装机那一次没有上一版
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


def test_坐实会发kept事件(装了两版):
    """``EventEmitter.subscribe()`` 回一个 ``asyncio.Queue``。订阅要在
    ``s.start()`` 之前挂上 —— 自检就在 ``start()`` 里跑完了,事后再订阅
    什么都收不到。事后同步 ``get_nowait()`` 把它排空即可(参见
    ``tests/engine/test_machine.py`` 里同样的用法)。

    ``release.rolled_back``/``release.stuck`` 两种事件分别在
    ``test_桥不应答就退回上一版``/``test_没有上一版就不回滚而是留着并喊``
    里断言过了,这条只管 ``release.kept`` 这一种。
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
        kept = [e for e in 收到 if e.get("kind") == "release.kept"]
        assert len(kept) == 1
        assert kept[0]["name"] == "2026-09-20-77b2de"
        assert kept[0]["previous"] == "2026-09-06-a3f9c1"
    finally:
        s.stop()


def test_落盘或发事件炸了标记还留着(装了两版, monkeypatch):
    """坐实/回滚那一段(``commit``/``clear_pending``/``rollback``/``emit``/
    重启)挪进了同一次过桥的 ``_run()`` 协程里,外面套了一层
    ``except Exception``。这条证明两件事:

    1. 那一段炸了,``_boot_postcheck()`` 不把异常带出来——``start()`` 照样
       起完,不会把整个进程带崩。
    2. **炸了不许把 pending 标记清掉。** 这里没有主动调 ``clear_pending``
       去"看起来干净",所以标记原样留着,等下一次开机让 ``boot_guard`` 按
       ``attempts`` 接着判——这正是两层回滚设计里的第二层。
    """
    import d1max_patrol.app.server as srv

    def 炸(_layout):
        raise OSError("落盘的时候盘满了")

    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    monkeypatch.setattr(srv, "commit", 炸)
    before = read_pending(layout)
    assert before is not None
    s = AppServer(ctx, port=0)
    s.start()                                          # 不许抛出来
    try:
        after = read_pending(layout)
        assert after is not None                        # 标记原样留着
        assert after.to == before.to
        assert after.src == before.src
        assert current_name(layout) == "2026-09-20-77b2de"  # commit 没坐实成
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


def test_start带postcheck假时不跑自检main要用这条时序(装了两版):
    """§7.3:自动回滚只有"重启后自检没过"这一个触发条件,不该被开机时序自己
    触发。``main()`` 里 ``server.start()`` 排在三个后端 ``connect()`` 之前
    (见 ``server.py`` 的 ``main()``),那时候问 ``control``/``bridges`` 两项
    几乎必然假失败——所以 ``main()`` 传 ``postcheck=False``,等三个
    ``connect()`` 都跑完了才补一次 ``server._boot_postcheck()``。

    这条不跑 ``main()``(不真的连后端、不真的起子进程),只证明
    ``AppServer`` 这一侧的两个承诺:``postcheck=False`` 时 ``start()`` 本身
    是空操作,以及事后显式调一次 ``_boot_postcheck()`` 才真的会跑判据。
    """
    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    s = AppServer(ctx, port=0)
    queue = s.hub.events.subscribe()
    s.start(postcheck=False)
    try:
        # start() 里没跑自检:链没坐实、标记还在、没有 release.* 事件。
        assert current_name(layout) == "2026-09-20-77b2de"
        assert read_pending(layout) is not None
        收到: list = []
        while not queue.empty():
            收到.append(queue.get_nowait())
        assert not any(str(e.get("kind", "")).startswith("release.")
                       for e in 收到)

        # 事后显式补一次,判据才真的跑:四项都过,坐实。
        s._boot_postcheck()
        assert current_name(layout) == "2026-09-20-77b2de"
        assert read_pending(layout) is None
        收到 = []
        while not queue.empty():
            收到.append(queue.get_nowait())
        assert any(e.get("kind") == "release.kept" for e in 收到)
    finally:
        s.stop()


def test_桥好着的时候一次都不等(装了两版):
    """有界重试不该给「桥本来就好」的绝大多数启动添一次等待。

    这条同时钉住注入口本身:``AppServer`` 要是没把 ``postcheck_sleep`` 往
    ``run_postcheck`` 里传,上面那两条断桥的用例会各自真等 4 秒,而这条会
    悄悄照过 —— 所以断桥那两条断言了"等过几次",这条断言"一次没等"。
    """
    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    等过: list = []
    s = AppServer(ctx, port=0, postcheck_sleep=假睡本(等过))
    s.start()
    try:
        assert read_pending(layout) is None            # 四项都过,坐实了
        assert 等过 == []
    finally:
        s.stop()


def test_自检路由那一遍也走注入进来的sleep(装了两版):
    """``_boot_postcheck`` 和 ``/api/selfcheck`` 是两个各自调 ``run_postcheck``
    的地方。只补第一个的话,人点一下自检就会在断桥的机器上卡 4 秒。
    """
    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    等过: list = []
    s = AppServer(ctx, port=0, postcheck_sleep=假睡本(等过))
    s.start(postcheck=False)                           # 先别跑开机那一遍
    try:
        ctx.nav.loc = None                             # 起来之后桥再断掉
        got = get_json(s, "/api/selfcheck")
        assert got["verdict"] == "rollback"             # 断了就是断了,重试救不回来
        assert len(等过) == 2                           # 而且等的是假的
    finally:
        s.stop()


def test_自检路由给自检的是它自己的超时预算而不是默认10秒(装了两版, monkeypatch):
    """W01c 真机:没有旁路进程时 ``/api/selfcheck`` 10 s 就 504 「后端没在规定
    时间内回话」,而自检本身还在跑 —— 人得到的是一句超时,不是四项里哪项没过。"""
    from d1max_agent.engine.selfcheck import POSTCHECK_TIMEOUT_S

    ctx, layout = 装了两版
    ctx.restart = lambda _plan: None
    s = AppServer(ctx, port=0, postcheck_sleep=假睡本())
    s.start(postcheck=False)
    看到的: list[float] = []
    原 = s._call

    def 记下(factory, timeout_s=10.0):
        看到的.append(timeout_s)
        return 原(factory, timeout_s=timeout_s)

    monkeypatch.setattr(s, "_call", 记下)
    try:
        got = get_json(s, "/api/selfcheck")
        assert got["verdict"] in ("keep", "rollback")
    finally:
        s.stop()
    assert 看到的 == [POSTCHECK_TIMEOUT_S]


def test_开机自检没过回滚时也把上一版的单元装回去(装了两版, tmp_path):
    """W01b:回滚三条路(人工、开机自检、CLI)都要对齐单元,不然退回去的代码
    配着新版的单元跑。"""
    import subprocess

    from d1max_agent.engine.privileged import Privileged

    ctx, layout = 装了两版
    ctx.nav.loc = None
    ctx.restart = lambda _plan: None
    helper = tmp_path / "d1max-privileged"
    helper.write_text("#!/bin/bash\n", encoding="utf-8")
    调用: list[str] = []

    def 假sudo(argv, **kw):
        调用.append(" ".join(argv[3:]))
        return subprocess.CompletedProcess(argv, 0, stdout="installed\n", stderr="")

    ctx.privileged = Privileged(helper=helper, runner=假sudo)
    s = AppServer(ctx, port=0, postcheck_sleep=假睡本())
    s.start()
    try:
        assert current_name(layout) == "2026-09-06-a3f9c1"
        assert 调用 == ["install-unit 2026-09-06-a3f9c1"]
    finally:
        s.stop()
