"""``GET /api/upload``,外加「一趟全传完了才打可删」那一下。

**这条路由只读,不改狗,所以不进 ``CONTROLLED``;但要 PIN。** 积压条数会
泄露这台狗最近跑了多少趟、传没传出去 —— 跟值守屏同一个口径,所以它也不进
``OPEN_PATHS``。

底下那半组测的是打「可删」那道闸(``server._该打可删了吗`` 和挂在它上面
的两个触发点):传成功**只打「可删」,不删**(spec §4.4),而且要等两件事
**同时**成立 —— 这一趟排过队的文件全传完了,**并且**这一趟已经收工。

这半组里最要紧的两条:

1. **标记落在哪儿。** 照片的 key 是 ``<任务>/<时刻>/photos/<名字>.jpg``,
   按最后一段切出来的是 ``photos`` 目录而不是 run 目录,而
   ``retention.mark_uploaded()`` 不做任何校验、拿到错路径也照样 ``touch``
   成功。两个后果都不出声:这一趟永远进不了「可删」(真机上盘会满),以及
   ``events.jsonl`` 还在排队就提前打上标记。
2. **什么时候落。** ``Uploader.scan()`` 不问这一趟结束了没有(spec §4.1),
   所以一趟**刚开跑**的巡检也会进队;光看"排过队的都传完了"就打标,等于给
   一趟正在写的归档打上永不撤销的「可删」。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from d1max_patrol.app.control import needs_lease
from d1max_patrol.app.server import AppServer, _run_finished, _run_rel, _补打可删
from d1max_patrol.app.upload_pump import SCAN_EVERY_MS, UploadPump
from d1max_patrol.engine import retention
from d1max_patrol.engine.retention import (
    SETTLE_HOURS,
    UPLOADED_REL,
    plan_sweep,
    scan_runs,
)
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
    """照真服务器的样子, 对自己落盘的那几个字节重算一次 sha256。

    **落盘是真攒着的。** 断点续传那一支(``offset > 0``)只有攒起来才对得上:
    回执里那个哈希算的是服务器上**已有的全部** ``stored`` 个字节, 不是刚收到
    的这一块 —— ``Uploader`` 拿本地同样长度的前缀去跟它对(spec §4.2)。
    """

    def __init__(self) -> None:
        self.盘: dict[tuple[str, str], bytes] = {}

    def put(self, req):
        已有 = self.盘.get((req.run, req.rel), b"")[:req.offset] + req.data
        self.盘[(req.run, req.rel)] = 已有
        return PutReceipt(ok=True, stored=len(已有),
                          sha256=hashlib.sha256(已有).hexdigest())


def 摆一趟(tmp_path: Path, *文件: str, run_rel: str = RUN_REL) -> Path:
    """在 ``<tmp>/runs/<run_rel>/`` 下摆几个文件出来, 返回 run 目录。"""
    run = tmp_path / "runs" / run_rel
    for rel in 文件:
        p = run / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rel.encode() + b"x" * 64)
    return run


def 起个头(run: Path) -> Path:
    """写一份**还没 summary** 的 manifest —— 这一趟还在跑。"""
    run.mkdir(parents=True, exist_ok=True)
    path = run / "manifest.json"
    path.write_text(json.dumps({"mission": {"name": "巡检一"}}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def 收工(run: Path) -> Path:
    """把 ``summary`` 写进 manifest —— 这一趟收工了。

    ``retention.is_settled`` 认的就是这一个字段(另一条出口是目录名老过
    ``SETTLE_HOURS``)。**测试里一律显式写它**, 不许靠"跑测试那天离 RUN_REL
    那个时刻够不够 24 小时" —— 那种测试会在某一天自己变色, 而且是无声的。
    """
    run.mkdir(parents=True, exist_ok=True)
    path = run / "manifest.json"
    path.write_text(
        json.dumps({"mission": {"name": "巡检一"}, "summary": {"result": "OK"}},
                   ensure_ascii=False),
        encoding="utf-8")
    return path


def 装一台(tmp_path: Path, 钟: list[int] | None = None) -> tuple[UploadPump, UploadQueue]:
    """接法跟 ``server.build_pump`` 一模一样, 只把 sink 换成假的。

    **两个触发点都要接上** —— 只接 ``on_done`` 的话, 最后一个文件比收工早传完
    的那一趟永远打不上标记, 而那正是底下两条测试盯着的事。
    """
    runs_root = tmp_path / "runs"
    q = UploadQueue(tmp_path / "queue.jsonl")
    up = Uploader(runs_root, q, 好服务器(), sn="D1MAX-TEST-01",
                  rand=lambda: 0.0)
    钟 = 钟 if 钟 is not None else [0]
    pump = UploadPump(up, clock=lambda: 钟[0],
                      on_done=partial(_run_finished, runs_root, q),
                      on_scan=partial(_补打可删, runs_root, q))
    return pump, q


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

    这一趟**先让它收工**(写了 summary), 免得测的其实是那道收工闸 —— 收工
    这条已经归底下两条管了, 这儿要单独拦住"还有没传完的"那一条。
    """
    run = 摆一趟(tmp_path, "photos/P1__front.jpg", "telemetry.jsonl")
    收工(run)
    pump, q = 装一台(tmp_path)
    pump.tick()                             # 这一拍扫盘 + 传 manifest(二级)
    pump.tick()                             # 这一拍传照片(三级)
    未完 = [i.key for i in q.all() if not i.done]
    assert 未完 == [f"{RUN_REL}/telemetry.jsonl"], f"这会儿该只剩遥测: {未完}"
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
    收工(run)
    pump, _ = 装一台(tmp_path)
    传到收工(pump)
    assert (run / UPLOADED_REL).exists(), \
        "全传完了却没打「可删」—— 水位线永远扫不到东西可删"
    assert not (run / "photos" / UPLOADED_REL).exists(), \
        "标记落进了 photos/ 里"


def test_传成功只打可删_不删(tmp_path: Path):
    """**§4.4:真删由水位线驱动, 不由上传驱动。**

    两头都得钉住, 缺一条这个测试就测不出东西来:标记**确实打上了**(少了这
    一条, 把 ``mark_uploaded`` 整个拆掉它照样绿), 而盘上的证据**一个字节都
    没动**(少了这一条, 哪天有人把打标改成 rmtree 它也照样绿)。
    """
    run = 摆一趟(tmp_path, "events.jsonl", "photos/P1__front.jpg")
    收工(run)
    原样 = {p.relative_to(run).as_posix(): p.read_bytes()
            for p in sorted(run.rglob("*")) if p.is_file()}
    pump, _ = 装一台(tmp_path)
    传到收工(pump)
    assert (run / UPLOADED_REL).exists(), \
        "全传完了却没打「可删」—— 水位线永远扫不到东西可删"
    现在 = {p.relative_to(run).as_posix(): p.read_bytes()
            for p in sorted(run.rglob("*")) if p.is_file()}
    assert {k: v for k, v in 现在.items() if k != UPLOADED_REL} == 原样, \
        "上传这条线动了盘上的证据 —— 它只许打标, 真删是水位线的事"


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


# ------------------------------------------- 收工了才许打标, 而且一定要打上


def test_一趟还在跑的时候就把盘上那几个传完了_收工之后也不许判成可删(
        tmp_path: Path, monkeypatch):
    """**这一条就是应修 1 那个 bug 的原型。**

    ``Uploader.scan()`` 不问这一趟结束了没有(spec §4.1), 所以巡检**刚开跑**、
    盘上只有 manifest 和刚起头的 events 时它们就进了队。传完那一下, "这个 run
    前缀下没有还没 done 的条目"是成立的 —— 只凭这一条打标, 就是给一趟**正在
    写**的归档打上了「可删」, 而 ``.uploaded`` 是 ``touch`` 出来的, **永不撤销**。

    后面照片才落盘(下面那行就是它), 网断着, 一个字节都没传出去。等这一趟
    写完 summary, ``plan_sweep`` 在"有服务器"这一档下只看 ``uploaded`` 一个
    字段 —— 整趟连同那张没传出去的照片一起 rmtree。
    """
    此刻 = datetime(2026, 9, 11, 10, 20, tzinfo=timezone.utc)      # 开跑五分钟
    monkeypatch.setattr(retention, "_utcnow", lambda: 此刻)
    run = 摆一趟(tmp_path, "events.jsonl")
    起个头(run)                                  # manifest 在, 但还没 summary
    pump, q = 装一台(tmp_path)
    传到收工(pump)
    assert all(i.done for i in q.all()), "盘上那两个确实都传完了"
    assert not (run / UPLOADED_REL).exists(), \
        "一趟还在跑就打了「可删」—— 后面才落盘的照片会被连带删掉"

    (run / "photos").mkdir(parents=True, exist_ok=True)
    (run / "photos" / "P1__front.jpg").write_bytes(b"x" * 4096)
    收工(run)

    之后 = 此刻 + timedelta(minutes=30)
    runs = scan_runs(tmp_path / "runs", now=之后)
    assert [i.path.name for i in runs] == [Path(RUN_REL).name]
    assert runs[0].settled, "写了 summary 就该算收工 —— 不然这一条测了个寂寞"
    sweep = plan_sweep(runs, now=之后, has_upload=True, need_bytes=10 ** 9)
    assert sweep.delete == (), \
        "照片一个字节都没传出去, 这一趟却被水位线放行了 —— 崩溃那一趟就是这么没的"


def 一直跑(pump: UploadPump, 钟: list[int], 拍数: int = 8) -> None:
    """每一拍把钟往前拨一个 ``SCAN_EVERY_MS``, 跑够拍数。

    拨这么大一步是为了让**每一拍都重扫一次盘**, 顺带跨过队列的退避
    (``BACKOFF_BASE_MS`` 比它小)—— 这样"最终会不会打上标记"这件事就不取决于
    正好踩在第几拍上。
    """
    for _ in range(拍数):
        钟[0] += SCAN_EVERY_MS
        pump.tick()


def test_最后一个文件在summary写下之前就传完_最终还是会打上可删(
        tmp_path: Path, monkeypatch):
    """**坑二:光有 on_done 那一声不够。**

    真机上的次序就是这样:照片传完了, 狗还在往回走, summary 是后写的。传完
    最后一个文件的那一刻这一趟还没收工, 打标那道闸拦住了它 —— 对的。但从那
    以后如果再没人回头看一眼, 这一趟就**永远**打不上「可删」, 水位线永远扫
    不到东西删, 盘照样会满, 只不过这次是从另一头满的。
    """
    此刻 = datetime(2026, 9, 11, 10, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(retention, "_utcnow", lambda: 此刻)
    run = 摆一趟(tmp_path, "photos/P1__front.jpg")
    起个头(run)
    钟 = [0]
    pump, q = 装一台(tmp_path, 钟)
    传到收工(pump)
    assert all(i.done for i in q.all()), "盘上那两个都传完了"
    assert not (run / UPLOADED_REL).exists(), "还没写 summary 就打了「可删」"

    收工(run)                                    # 狗这会儿才写完 summary
    一直跑(pump, 钟)
    assert (run / UPLOADED_REL).exists(), \
        "最后一个文件比 summary 早传完的那一趟一直没打上「可删」—— 盘会满"


def test_崩在半路那一趟老化成收工之后_有人回头把可删补上(
        tmp_path: Path, monkeypatch):
    """**这一条钉的是第二个触发点本身:重扫那一拍。**

    崩在半路的一趟没有 summary, 它是靠目录名老过 ``SETTLE_HOURS`` 才变成
    「收工」的 —— 而老化这件事**盘上一个字节都不会变**:不会有新文件进队, 更
    不会再有第二声 ``done``。所以这一趟能不能打上标记, 只可能来自重扫那一拍
    (``on_scan``);下面那一句 ``step.action == "idle"`` 和 ``sent_files`` 没
    涨, 钉的就是"这一拍没有任何文件在传", 标记只能是重扫那一下打的。
    """
    此刻 = [datetime(2026, 9, 11, 10, 20, tzinfo=timezone.utc)]
    monkeypatch.setattr(retention, "_utcnow", lambda: 此刻[0])
    run = 摆一趟(tmp_path, "photos/P1__front.jpg")   # 连 manifest 都没来得及写
    钟 = [0]
    pump, q = 装一台(tmp_path, 钟)
    传到收工(pump)
    传完了几个 = pump.stats().sent_files
    assert all(i.done for i in q.all()), "盘上那一个传完了"
    assert not (run / UPLOADED_REL).exists(), "还在安定期里就打了「可删」"

    此刻[0] += timedelta(hours=SETTLE_HOURS + 1)     # 老化成"没人在写它了"
    钟[0] = SCAN_EVERY_MS                            # 正好踩在重扫那一拍上
    step = pump.tick()

    assert step.action == "idle", f"这一拍不该有文件在传: {step}"
    assert pump.stats().sent_files == 传完了几个, "这一拍传了新文件, 证明不了什么"
    assert (run / UPLOADED_REL).exists(), \
        "老化成收工之后没人回头补这一下 —— 这一趟永远不可删, 真机上盘会满"


def test_补打可删这一下炸了_也不许把这一拍的上传带走(tmp_path: Path):
    """重扫盘那件事本身已经办成了, 上传照旧要往下走。

    但同样不许静悄悄:这条路断了的后果跟 ``on_done`` 那条一样是**盘会满**,
    而且这一种更阴 —— 上传一路顺风, 值守屏上什么都看不出来。
    """
    摆一趟(tmp_path, "events.jsonl")
    runs_root = tmp_path / "runs"
    q = UploadQueue(tmp_path / "queue.jsonl")
    up = Uploader(runs_root, q, 好服务器(), sn="D1MAX-TEST-01", rand=lambda: 0.0)

    def 补打的时候炸() -> None:
        raise OSError("盘掉了")

    pump = UploadPump(up, clock=lambda: 0, on_scan=补打的时候炸)
    step = pump.tick()
    assert step.action == "done", "重扫之后那一下炸了, 不许把这一拍的上传也带走"
    # 这一拍上传是成功的 —— 成功那一支会把 last_error 清空。这句话还在,
    # 钉的就是"回调那一声排在清空之后"。
    assert "盘掉了" in pump.stats().last_error
