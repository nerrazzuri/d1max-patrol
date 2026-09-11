"""``GET /api/upload``,外加「一趟全传完了才打可删」那一下。

**这条路由只读,不改狗,所以不进 ``CONTROLLED``;但要 PIN。** 积压条数会
泄露这台狗最近跑了多少趟、传没传出去 —— 跟值守屏同一个口径,所以它也不进
``OPEN_PATHS``。

底下那半组测的是 ``server._run_finished``:传成功**只打「可删」,不删**
(spec §4.4),而且要等这一趟排过队的文件**全**传完。**这半组里最要紧的一
条是标记落在哪儿** —— 照片的 key 是 ``<任务>/<时刻>/photos/<名字>.jpg``,
按最后一段切出来的是 ``photos`` 目录而不是 run 目录,而
``retention.mark_uploaded()`` 不做任何校验、拿到错路径也照样 ``touch``
成功。两个后果都不出声:这一趟永远进不了「可删」(真机上盘会满),以及
``events.jsonl`` 还在排队就提前打上标记。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from d1max_patrol.app.control import needs_lease
from d1max_patrol.app.server import AppServer, _run_finished, _run_rel
from d1max_patrol.app.upload_pump import UploadPump
from d1max_patrol.engine.retention import UPLOADED_REL
from d1max_patrol.engine.upload_queue import UploadQueue
from d1max_patrol.engine.uploader import PutReceipt, Uploader

from .conftest import get_json, make_ctx, request

UPLOAD = "/api/upload"

#: 队列里这一趟叫什么。**两段**:任务名 + 时刻,后面全是 rel。
RUN_REL = "巡检一/20260911T101500Z"


def make_ctx_with_console(bridge, tmp_path):
    """一份配了回传地址的上下文。**地址指向 127.0.0.1:1 —— 永远连不上。**

    连不上正是我们要的:这几条测的是「路由报得出形状」和「线程收得干净」,
    不是「真的传上去了」。真传上去那件事在底下那半组的假服务器上测。

    **不许在这儿写一个真地址。** 每个客户的服务器都不一样, 代码和交付件里
    一个具体地址都不该有。
    """
    return make_ctx(bridge, tmp_path, console_url="http://127.0.0.1:1/")


# ------------------------------------------------------------------ 路由


def test_没配回传地址时_enabled是false(server):
    """单机档:客户没买服务器。**这一档不是错误,是一种正常形态。**"""
    份 = get_json(server, UPLOAD)
    assert 份["enabled"] is False
    # backlog 是 None 不是 0 —— 跟 watch_summary 那一档同一个口径:
    # 0 是「查过了没积压」, None 是「这台狗没有这个能力」。
    assert 份["backlog"] is None
    # 光秃秃一个 null 摆在那儿, 看的人分不清是没查还是查了没事。
    assert "不是「没有积压」" in 份["detail"]


def test_没配回传地址时_夹具服务器一条上传线程都不起(server):
    """**这一条钉的是装配条件, 不是路由形状。**

    夹具默认不配回传地址 => ``ctx.upload is None`` => 现有那一大堆 app 测试
    (含 ``test_wire_fixtures`` 里 ``upload_backlog is None`` 那条)一个字都
    不用改。哪天有人把装配条件写成「无条件装」, 这一条当场红。
    """
    assert server.ctx.upload is None


def test_配了回传地址时_报得出积压(bridge, tmp_path):
    c = make_ctx_with_console(bridge, tmp_path)
    s = AppServer(c, port=0)
    s.start()
    try:
        份 = get_json(s, UPLOAD)
    finally:
        s.stop()
    assert 份["enabled"] is True
    assert 份["backlog"] == 0
    assert set(份) >= {"backlog", "last_step", "last_ok_ms", "last_error",
                       "sent_files", "enabled"}


def test_这条路由不在受控表里(server):
    """它不改狗。进了受控表, 值守屏看一眼积压就得先抢控制权。"""
    assert not needs_lease("GET", UPLOAD)


def test_这条路由是只读的(server):
    """挂个写方法上去要 405, 不是 200。"""
    码, body, _ = request(server, UPLOAD, method="PUT", payload={})
    assert 码 == 405, body


def test_带PIN的服务上_没token进不来(bridge, tmp_path):
    """积压条数会泄露这台狗最近跑了多少趟, 跟值守屏同一个口径。"""
    c = make_ctx_with_console(bridge, tmp_path)
    s = AppServer(c, port=0, pin="428913")
    s.start()
    try:
        码, _, _ = request(s, UPLOAD)
    finally:
        s.stop()
    assert 码 == 401


def test_服务停下的时候pump也停(bridge, tmp_path):
    """**这一条是防线不是形式。** 关服务的时候还在往 queue.jsonl 里写,
    下次开机重放到的就是半行 —— 那条半行队列那一头兜得住, 但兜得住不等于
    该发生。
    """
    import threading

    before = threading.active_count()
    c = make_ctx_with_console(bridge, tmp_path)
    s = AppServer(c, port=0)
    s.start()
    s.stop()
    assert threading.active_count() == before


def test_接了回传之后值守屏那一格是个数不是null(bridge, tmp_path):
    """路由和心跳报的是同一个口径 —— 两处分岔的那天, 现场没人知道信哪个。"""
    c = make_ctx_with_console(bridge, tmp_path)
    s = AppServer(c, port=0)
    s.start()
    try:
        份 = get_json(s, "/api/watch/summary")
    finally:
        s.stop()
    assert 份["upload_backlog"] == 0
    assert "upload_backlog" not in 份["detail"]


# ------------------------------------------- 一趟全传完了才打「可删」(§4.4)


class 好服务器:
    """照真服务器的样子, 对自己落盘的那几个字节重算一次 sha256。"""

    def put(self, req):
        return PutReceipt(ok=True, stored=req.total,
                          sha256=hashlib.sha256(req.data).hexdigest())


def 摆一趟(tmp_path: Path, *文件: str) -> Path:
    """在 ``<tmp>/runs/<RUN_REL>/`` 下摆几个文件出来, 返回 run 目录。"""
    run = tmp_path / "runs" / RUN_REL
    for rel in 文件:
        p = run / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rel.encode() + b"x" * 64)
    return run


def 装一台(tmp_path: Path) -> tuple[UploadPump, UploadQueue]:
    """接法跟 ``server.build_pump`` 一模一样, 只把 sink 换成假的。"""
    from functools import partial

    runs_root = tmp_path / "runs"
    q = UploadQueue(tmp_path / "queue.jsonl")
    up = Uploader(runs_root, q, 好服务器(), sn="D1MAX-TEST-01",
                  rand=lambda: 0.0)
    return UploadPump(up, clock=lambda: 0,
                      on_done=partial(_run_finished, runs_root, q)), q


def 传到收工(pump: UploadPump, 上限: int = 50) -> list[str]:
    """一直 tick 到队列空, 返回每一拍的 action。"""
    走过 = []
    for _ in range(上限):
        step = pump.tick()
        走过.append(step.action)
        if step.action == "idle":
            return 走过
    raise AssertionError(f"{上限} 拍还没传完: {走过}")


def test_key的前两段才是一趟_照片那一支不许按最后一段切():
    """``rel`` 是会带斜杠的 —— 照片就在 ``photos/`` 底下。"""
    assert _run_rel(f"{RUN_REL}/photos/P1__front.jpg") == RUN_REL
    assert _run_rel(f"{RUN_REL}/events.jsonl") == RUN_REL


def test_一趟里还有没传完的_就不许打可删(tmp_path: Path):
    """**这一条钉的是「标记提前落进 photos 目录」那个 bug。**

    照片(三级)排在 ``telemetry.jsonl``(四级)前面, 所以照片先传完。按最后
    一段切出来的前缀是 ``<run>/photos``, 拿它去数「还剩几个没传完」只看得见
    照片自己 —— 于是遥测还在队里排着, 这一趟就被打上了「可删」, 而且标记还
    落在 ``photos/`` 里。
    """
    run = 摆一趟(tmp_path, "photos/P1__front.jpg", "telemetry.jsonl")
    pump, q = 装一台(tmp_path)
    pump.tick()                             # 这一拍扫盘 + 传照片
    assert [i.key for i in q.all() if i.done] == [f"{RUN_REL}/photos/P1__front.jpg"], \
        "先传完的该是照片(三级)"
    assert not (run / "photos" / UPLOADED_REL).exists(), \
        "「可删」标记落进了 photos/ —— run 目录是 key 的前两段, 不是去掉最后一段"
    assert not (run / UPLOADED_REL).exists(), \
        "遥测还在队里排着就打了「可删」"


def test_一趟全传完之后_标记恰好落在run目录下(tmp_path: Path):
    """照片和事件流一趟里都有, **标记只许有一个, 而且在 run 目录下。**

    落错地方是不出声的:``retention.mark_uploaded()`` 不做任何校验, 而
    ``scan_runs()`` 查的是 ``<run>/`` 底下那个标记 —— 查不到就永远不进
    「可删」, 水位线扫不到东西删, 真机上盘会满。
    """
    run = 摆一趟(tmp_path, "photos/P1__front.jpg", "events.jsonl")
    pump, _ = 装一台(tmp_path)
    传到收工(pump)
    assert (run / UPLOADED_REL).exists(), \
        "全传完了却没打「可删」—— 水位线永远扫不到东西可删"
    assert not (run / "photos" / UPLOADED_REL).exists(), \
        "标记落进了 photos/ 里"


def test_传成功只打可删_不删(tmp_path: Path):
    """**§4.4:真删由水位线驱动, 不由上传驱动。**"""
    run = 摆一趟(tmp_path, "events.jsonl")
    pump, _ = 装一台(tmp_path)
    传到收工(pump)
    assert (run / "events.jsonl").exists(), "上传这条线把证据删了"
    assert (run / UPLOADED_REL).exists()


def test_打可删这一下炸了_也不许把这个文件改判成没传上去(tmp_path: Path):
    """文件**确实**传上去了, 改判成「退避重排」会让同一份字节再传一遍。

    但也不许静悄悄:打不上标记的后果是盘会满, 所以值守屏上得看得见。
    """
    摆一趟(tmp_path, "events.jsonl")
    runs_root = tmp_path / "runs"
    q = UploadQueue(tmp_path / "queue.jsonl")
    up = Uploader(runs_root, q, 好服务器(), sn="D1MAX-TEST-01", rand=lambda: 0.0)

    def 打标的时候炸(_key: str) -> None:
        raise OSError("盘满了")

    pump = UploadPump(up, clock=lambda: 0, on_done=打标的时候炸)
    step = pump.tick()
    assert step.action == "done", "文件确实传上去了, 不许改判成退避重排"
    assert pump.stats().last_step == "done"
    # **但不许静悄悄。** 打不上「可删」的后果是水位线永远扫不到东西删, 盘会满。
    assert "盘满了" in pump.stats().last_error
