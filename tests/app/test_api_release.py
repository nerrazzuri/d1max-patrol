"""版本那五条路由。装机、升级、回滚,加上改上装属性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d1max_agent.engine.lease import LEASE_TTL_MS
from d1max_agent.engine.release import (
    MANIFEST_NAME,
    Layout,
    commit,
    current_name,
    read_pending,
    tree_sha256,
)
from d1max_patrol.app.identity import CONFIRM_PHRASE
from d1max_patrol.app.server import AppServer

from .conftest import get_err, get_json, make_ctx, post, request


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


_RELPIN = "428913"


class _假墙钟:
    """给"租约到期"这两条测试拨钟用的——不能真等 30 秒(§8.5 第 2 条)。"""

    def __init__(self, t: int = 1_757_000_000_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def rel_server_带钟(bridge, tmp_path):
    """跟 ``rel_server`` 一样的机器,只是墙钟可以拨、设了 PIN。

    设 PIN 是必须的:``lease_active`` 现在用 ``ControlDesk.sweep()`` 结
    算,而 ``sweep()`` 会把"token 已经不在 ``Guard.live_refs()`` 里"的租约
    当死租约释放掉(见 ``app/server.py`` 的 ``_gather_precheck`` 注释)。
    没有真实会话就没有活着的 token,直接在 ``book`` 上塞一个编出来的
    ref 会被 ``sweep()`` 秒杀,测不出"busy"——所以这里要真解锁一次拿一个
    活着的 ``sess.ref``。
    """
    from d1max_patrol.app.identity import write_payload

    payload_file = tmp_path / "payload.json"
    write_payload(has=False, by="装机", now_ms=1, path=payload_file,
                  confirm=CONFIRM_PHRASE)
    钟 = _假墙钟()
    ctx = make_ctx(bridge, tmp_path, release_root=tmp_path / "opt",
                   payload_file=payload_file, clock=钟)
    重启记录: list = []
    ctx.restart = 重启记录.append
    s = AppServer(ctx, port=0, pin=_RELPIN)
    s.start()
    s.重启记录 = 重启记录
    s.钟 = 钟
    yield s
    s.stop()


def _rel解锁(server) -> tuple[str, dict[str, str]]:
    code, body, _ = request(server, "/api/auth", method="POST",
                            payload={"pin": _RELPIN, "operator": "张三"})
    assert code == 200, body
    tok = json.loads(body)["token"]
    return tok, {"Authorization": f"Bearer {tok}"}


def test_持有租约时切版本会被busy挡住(rel_server_带钟, tmp_path):
    """docs/第2卷待办.md 第 50 条:``busy`` 判据要接到真实的、结算过的租约状态。

    这里直接摸 ``control.book``——``/api/control/acquire`` 这条路由是后面的
    任务才接,这一卷只保证"只要 book 里有人握着,precheck 就看得见"。租约
    绑的是真解锁拿到的 ``sess.ref``,不是编出来的字符串,这样它才是
    ``sweep()`` 眼里"活着"的租约。
    """
    tok, hdr = _rel解锁(rel_server_带钟)
    sess = rel_server_带钟.auth.session_of(tok)
    assert sess is not None
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    code, body, _ = request(rel_server_带钟, "/api/release/install",
                            method="POST", payload={"package": str(pkg)},
                            headers=hdr)
    assert code == 200, body
    rel_server_带钟.control.book.acquire(sess.ref, sess.operator,
                                        now_ms=rel_server_带钟.钟.t)
    err = get_err(rel_server_带钟, "/api/release/activate", 409,
                  method="POST", payload={"name": "2026-09-20-77b2de"},
                  headers=hdr)
    assert "租约" in json.dumps(err, ensure_ascii=False)
    layout = Layout(root=rel_server_带钟._ctx.release_root)
    assert current_name(layout) == ""
    assert rel_server_带钟.重启记录 == []


def test_租约过期后busy又通过了(rel_server_带钟, tmp_path):
    """同一把租约,拨过 TTL 之后——不用等,也不用有人来"通知"——busy 该放行了。"""
    tok, hdr = _rel解锁(rel_server_带钟)
    sess = rel_server_带钟.auth.session_of(tok)
    assert sess is not None
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    code, body, _ = request(rel_server_带钟, "/api/release/install",
                            method="POST", payload={"package": str(pkg)},
                            headers=hdr)
    assert code == 200, body
    rel_server_带钟.control.book.acquire(sess.ref, sess.operator,
                                        now_ms=rel_server_带钟.钟.t)
    get_err(rel_server_带钟, "/api/release/activate", 409,
            method="POST", payload={"name": "2026-09-20-77b2de"},
            headers=hdr)

    rel_server_带钟.钟.t += LEASE_TTL_MS
    got = get_json(rel_server_带钟, "/api/release/activate",
                   method="POST", payload={"name": "2026-09-20-77b2de"},
                   headers=hdr)
    assert got["precheck"]["ok"] is True
    layout = Layout(root=rel_server_带钟._ctx.release_root)
    assert current_name(layout) == "2026-09-20-77b2de"


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


# ------------------------------------------------------------ W01b:单元随包装、重启走助手

import subprocess  # noqa: E402
from functools import partial  # noqa: E402

from d1max_agent.engine.privileged import Privileged, PrivilegedError  # noqa: E402
from d1max_agent.engine.selfcheck import RestartPlan  # noqa: E402
from d1max_patrol.app import server as server_mod  # noqa: E402


class _假sudo:
    """记下每次 sudo 的子命令;按剧本回退出码。"""

    def __init__(self, 剧本=None):
        self.剧本 = 剧本 or {}
        self.调用: list[str] = []

    def __call__(self, argv, **kw):
        子命令 = " ".join(argv[3:])
        self.调用.append(子命令)
        默认 = (0, "installed\n" if argv[3] == "install-unit" else "", "")
        rc, out, err = self.剧本.get(argv[3], 默认)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


def _有助手的(tmp_path, 剧本=None):
    helper = tmp_path / "d1max-privileged"
    helper.write_text("#!/bin/bash\n", encoding="utf-8")
    假 = _假sudo(剧本)
    return Privileged(helper=helper, runner=假), 假


def _两版装好(server, tmp_path):
    for name in ("2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        post(server, "/api/release/install", {"package": str(_pkg(tmp_path / name, name))})


def test_切版本先装单元再切链再重启(rel_server, tmp_path):
    priv, 假 = _有助手的(tmp_path)
    rel_server._ctx.privileged = priv
    pkg = _pkg(tmp_path / "pkg", "2026-09-20-77b2de")
    post(rel_server, "/api/release/install", {"package": str(pkg)})
    got = get_json(rel_server, "/api/release/activate", method="POST",
                   payload={"name": "2026-09-20-77b2de"})
    assert got["unit"] == "installed"
    assert 假.调用 == ["check", "install-unit 2026-09-20-77b2de"]
    assert current_name(Layout(root=rel_server._ctx.release_root)) == "2026-09-20-77b2de"
    assert len(rel_server.重启记录) == 1


def test_单元装不上就不切链(rel_server, tmp_path):
    priv, 假 = _有助手的(
        tmp_path, {"install-unit": (1, "", "d1max-privileged: 第 8 行:User 只能是 robot\n")})
    rel_server._ctx.privileged = priv
    post(rel_server, "/api/release/install",
         {"package": str(_pkg(tmp_path / "pkg", "2026-09-20-77b2de"))})
    err = get_err(rel_server, "/api/release/activate", 409, method="POST",
                  payload={"name": "2026-09-20-77b2de"})
    assert "单元" in json.dumps(err, ensure_ascii=False)
    assert "User 只能是 robot" in json.dumps(err, ensure_ascii=False)
    layout = Layout(root=rel_server._ctx.release_root)
    assert current_name(layout) == ""
    assert read_pending(layout) is None
    assert rel_server.重启记录 == []


def test_重启命令发不出去就不切(rel_server, tmp_path):
    """助手在、sudo 不通:切了链却重启不了,机器就挂在「在途」里等人。所以在切之前拦。"""
    priv, 假 = _有助手的(tmp_path, {"check": (1, "", "sudo: a password is required\n")})
    rel_server._ctx.privileged = priv
    post(rel_server, "/api/release/install",
         {"package": str(_pkg(tmp_path / "pkg", "2026-09-20-77b2de"))})
    err = get_err(rel_server, "/api/release/activate", 409, method="POST",
                  payload={"name": "2026-09-20-77b2de"})
    assert "重启命令发不出去" in json.dumps(err, ensure_ascii=False)
    assert current_name(Layout(root=rel_server._ctx.release_root)) == ""
    assert "install-unit 2026-09-20-77b2de" not in 假.调用
    assert rel_server.重启记录 == []


def test_没有特权助手的开发机照旧切(rel_server, tmp_path):
    假 = _假sudo()
    rel_server._ctx.privileged = Privileged(helper=tmp_path / "不存在", runner=假)
    post(rel_server, "/api/release/install",
         {"package": str(_pkg(tmp_path / "pkg", "2026-09-20-77b2de"))})
    got = get_json(rel_server, "/api/release/activate", method="POST",
                   payload={"name": "2026-09-20-77b2de"})
    assert got["unit"].startswith("skipped:")
    assert 假.调用 == []
    assert len(rel_server.重启记录) == 1


def test_回滚把上一版的单元装回去(rel_server, tmp_path):
    priv, 假 = _有助手的(tmp_path)
    rel_server._ctx.privileged = priv
    _两版装好(rel_server, tmp_path)
    post(rel_server, "/api/release/activate", {"name": "2026-09-06-a3f9c1"})
    commit(Layout(root=rel_server._ctx.release_root))
    post(rel_server, "/api/release/activate", {"name": "2026-09-20-77b2de"})
    假.调用.clear()
    got = get_json(rel_server, "/api/release/rollback", method="POST", payload=None)
    assert got["rolled_back_to"] == "2026-09-06-a3f9c1"
    assert 假.调用 == ["install-unit 2026-09-06-a3f9c1"]


def test_回滚时单元装不回去也照样退回去(rel_server, tmp_path, caplog):
    """回到能跑的代码比单元一致更要紧;单元向后兼容(只引用 current)。"""
    priv, 假 = _有助手的(tmp_path)
    rel_server._ctx.privileged = priv
    _两版装好(rel_server, tmp_path)
    post(rel_server, "/api/release/activate", {"name": "2026-09-06-a3f9c1"})
    commit(Layout(root=rel_server._ctx.release_root))
    post(rel_server, "/api/release/activate", {"name": "2026-09-20-77b2de"})
    假.剧本["install-unit"] = (1, "", "d1max-privileged: 源不存在\n")
    got = get_json(rel_server, "/api/release/rollback", method="POST", payload=None)
    assert got["rolled_back_to"] == "2026-09-06-a3f9c1"
    assert current_name(Layout(root=rel_server._ctx.release_root)) == "2026-09-06-a3f9c1"
    assert any("单元没装回去" in r.getMessage() for r in caplog.records)


def test_spawn_restart探到没权限就抛而不是Popen(tmp_path, monkeypatch):
    import d1max_agent.engine.privileged as priv_mod
    起过: list = []
    monkeypatch.setattr(priv_mod.subprocess, "Popen",
                        lambda argv, **kw: 起过.append(tuple(argv)))
    plan = RestartPlan("service", ("systemctl", "restart", "d1max-patrol.service"), "没上装")
    priv, _ = _有助手的(tmp_path, {"check": (1, "", "sudo: a password is required\n")})
    with pytest.raises(PrivilegedError):
        server_mod._spawn_restart(plan, privileged=priv)
    assert 起过 == []
    priv, 假 = _有助手的(tmp_path)
    server_mod._spawn_restart(plan, privileged=priv)
    assert 起过 == [], "有助手时 restart 同步跑、等结果,不 Popen"
    assert 假.调用[-1] == "restart"


def test_check通了但restart排队失败_接口不得说成功(rel_server, tmp_path):
    """外部审核阻断项:原来 Popen 完就回 200,systemd-run 失败、单元名冲突、助手炸了
    全都看不见。现在同步等助手的 restart 退 0;非零 → 500 说实话。"""
    priv, 假 = _有助手的(tmp_path, {"restart": (1, "", "Failed to start transient service unit\n")})
    rel_server._ctx.privileged = priv
    rel_server._ctx.restart = partial(server_mod._spawn_restart, privileged=priv)
    post(rel_server, "/api/release/install",
         {"package": str(_pkg(tmp_path / "pkg", "2026-09-20-77b2de"))})
    err = get_err(rel_server, "/api/release/activate", 500, method="POST",
                  payload={"name": "2026-09-20-77b2de"})
    assert "重启命令发不出去" in json.dumps(err, ensure_ascii=False)
    assert "transient" in json.dumps(err, ensure_ascii=False)
    assert 假.调用 == ["check", "install-unit 2026-09-20-77b2de", "check", "restart"]


def test_没注入restart时默认绑的是ctx里的privileged(bridge, tmp_path):
    """``_spawn_restart`` 不许自己再 new 一个 ``Privileged()``:测试注入的 runner
    要管得到重启那条路,生产上也别多发一次 sudo check。"""
    from functools import partial

    ctx = make_ctx(bridge, tmp_path)
    assert isinstance(ctx.restart, partial)
    assert ctx.restart.func is server_mod._spawn_restart
    assert ctx.restart.keywords["privileged"] is ctx.privileged
