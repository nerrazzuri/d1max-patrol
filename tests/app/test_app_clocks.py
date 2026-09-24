"""必修 4:``app/`` 这一层到底有几口钟, 哪一口该注入, 哪一口不该。

这个文件钉的是一条**分类**, 不是一个值:

* **``ctx.clock()`` 这一侧** —— app 自己盖、app 自己读回来的时间戳(原点的
  ``marked_at_ms``、备份的 ``last_sync_ms``、版本的落槽/切换时刻、上装登记
  的时刻)。这些原来全是 ``int(time.time() * 1000)``, 注入点形同虚设:
  拨了钟的测试拿不到自己拨的那个数, 而真机上这些数字将来要跟服务器对时。
* **``_归档钟()`` 这一侧** —— 归档目录名上那个时间戳的纪元。它是
  ``engine/archive.py`` 的 ``_now()`` 盖的, **外壳注不进去**。保留期、清盘、
  导出、镜像算的都是"这一趟存了多久", 被减数和减数必须同一口钟。
  ``plan_sweep`` 后面接的是 ``shutil.rmtree`` —— 按错纪元算出来的"过期"是
  直接删盘。
* **``AppContext.clock`` 的默认值仍然是墙钟** —— 挂起闸门读的就是它
  (见 ``test_suspend_e2e.py`` 的 ``test_闸门读的钟必须还是墙钟``)。这里
  不碰那条, 只把"注入了就得生效"补上。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from d1max_agent.engine.homing import load_home
from d1max_patrol.app import server as S
from d1max_patrol.app.server import AppContext, AppServer, _wall_ms, _归档钟

from .conftest import make_ctx, request

#: 一个一眼就认得出来的假时刻(2025-09-04 前后), 跟真墙钟差着好几年 ——
#: 万一哪一处漏了注入, 断言里是"差了几亿毫秒", 不是"差了几十毫秒"。
_T0 = 1_757_000_000_000


class 假墙钟:
    def __init__(self, t: int = _T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def 钟():
    return 假墙钟()


@pytest.fixture
def 拨过钟的服务(bridge, tmp_path, 钟):
    import contextlib

    ctx = make_ctx(bridge, tmp_path, clock=钟)
    s = AppServer(ctx, port=0)
    s.start()
    yield s
    s.stop()
    with contextlib.suppress(Exception):
        bridge.call(ctx.engine.aclose, timeout_s=10.0)


# ---------------------------------------------------- 该注入的那一侧真注进去了


def test_原点的时刻走注入钟(拨过钟的服务):
    """``PUT /api/maps/<id>/home`` 盖的 ``marked_at_ms`` 原来是墙钟。

    这个数是**留给人看的**:"这个原点是什么时候标的"。它跟别的记录一样要
    跟第 9 卷接上的时间服务器对得起来, 而不是各盖各的。
    """
    s = 拨过钟的服务
    code, body, _ = request(s, quote("/api/maps/map_test/home", safe="/"),
                            method="PUT",
                            payload={"x": 1.0, "y": 2.0, "yaw": 0.0})
    assert code == 200, body
    盘上 = load_home(s._ctx.mapping.maps_dir, "map_test")
    assert 盘上 is not None and 盘上.marked_at_ms == _T0


def test_上装登记的时刻走注入钟(bridge, tmp_path, 钟):
    """``PUT /api/identity/payload`` 同理。

    这一条格外要紧:装没装上装决定了升级时是"重启服务"还是"重启整机"
    (§7.1), 而"什么时候登记的"是出了事之后第一个要对的时间。
    """
    import contextlib

    from d1max_patrol.app.identity import CONFIRM_PHRASE, read_payload, write_payload

    payload_file = tmp_path / "payload.json"
    write_payload(has=False, by="装机", now_ms=1, path=payload_file,
                  confirm=CONFIRM_PHRASE)
    ctx = make_ctx(bridge, tmp_path, clock=钟, payload_file=payload_file)
    s = AppServer(ctx, port=0)
    s.start()
    try:
        code, body, _ = request(s, "/api/identity/payload", method="PUT",
                                payload={"has_payload": True, "by": "老王",
                                         "confirm": CONFIRM_PHRASE})
        assert code == 200, body
    finally:
        s.stop()
        with contextlib.suppress(Exception):
            bridge.call(ctx.engine.aclose, timeout_s=10.0)
    assert read_payload(payload_file).at_ms == _T0


def test_server里除了那口墙钟没有第二处读time(拨过钟的服务):
    """**这一条是围栏, 不是断言。**

    上面两条只钉住了两条具体路由。``app/server.py`` 里原来有十处
    ``int(time.time() * 1000)``, 散在原点、备份、版本、上装那几摊, 一条条钉
    要写十条测试, 而下一个人新加第十一处的时候一条都不会红。这里直接读源码:
    ``time.time()`` 只许出现在 ``_wall_ms`` 的函数体里(以及讲它的注释里)。
    """
    源码 = Path(S.__file__).read_text(encoding="utf-8")
    代码行 = [行.strip() for 行 in 源码.splitlines()
              if "time.time()" in 行 and not 行.lstrip().startswith("#")
              and "``" not in 行]
    assert 代码行 == ["return int(time.time() * 1000)"], 代码行


# --------------------------------------------- 不该注入的那一侧仍然是墙钟


def test_归档钟是墙钟而且故意不可注入(拨过钟的服务):
    """**这不是漏注入。**

    ``_归档钟()`` 的被减数是归档目录名上那个时间戳, 而那个是
    ``engine/archive.py`` 的 ``_now()`` 盖的 —— 外壳注不进去。两个数不同纪元
    的话, ``plan_sweep`` 算出来的"过期"就是错的, 而它后面接的是
    ``shutil.rmtree``。

    这里连着断三件事:它读的是真墙钟、它跟注入钟无关、``engine`` 那一侧盖的
    确实是同一口钟。第三条要是哪天变了(``engine/archive.py`` 变成可注入),
    这条会红 —— 那正是"该把这摊也接上注入"的信号。
    """
    from d1max_agent.engine import archive

    前 = datetime.now(timezone.utc)
    得到 = _归档钟()
    后 = datetime.now(timezone.utc)
    assert 前 <= 得到 <= 后
    assert 得到.tzinfo is timezone.utc
    # 跟注入钟不是一口钟 —— 注入钟被拨到了好几年前。
    assert abs(得到.timestamp() * 1000 - 拨过钟的服务._ctx.clock()) > 60_000
    # engine 那一侧盖的是同一口。
    assert abs((archive._now() - 得到).total_seconds()) < 5.0


def test_归档那四条路由读的都是归档钟():
    """保留期、清盘、导出、镜像 —— 一条都不许换成 ``ctx.clock()``。

    同样是围栏:``datetime.now(timezone.utc)`` 在 ``server.py`` 里只许出现在
    ``_归档钟`` 的函数体里, 别处一律走这个函数。这样"该不该注入"这件事在
    源码里只剩一个决策点, 而那个点上写着为什么。
    """
    源码 = Path(S.__file__).read_text(encoding="utf-8")
    代码行 = [行.strip() for 行 in 源码.splitlines()
              if "datetime.now(" in 行 and not 行.lstrip().startswith("#")
              and "``" not in 行]
    assert 代码行 == ["return datetime.now(timezone.utc)"], 代码行
    调用 = [行 for 行 in 源码.splitlines()
            if "_归档钟()" in 行 and not 行.lstrip().startswith("def ")]
    assert len(调用) == 4, 调用


def test_过期判定读的是真墙钟不是注入钟(拨过钟的服务):
    """把注入钟拨到一年前, 过期判定不许跟着变。

    这条是上面那条围栏的**行为面**:围栏防的是源码被改回去, 这条防的是
    "换了个写法绕过围栏"。

    局面是刻意挑的:摆一趟**按真墙钟算已经存了 400 天**的归档, 保留期 90 天
    —— 按真墙钟它早过期了, 该进预告名单。而注入钟停在一年多以前, 按它算这
    趟才存了不到 90 天, 名单会是空的。所以这条测试分得出这一摊读的是哪口钟。
    """
    from d1max_agent.engine.archive import STAMP_FMT

    ctx = 拨过钟的服务._ctx
    assert ctx.clock() == _T0, "注入钟没接上, 这条测试就什么也没测"
    真现在 = datetime.now(timezone.utc)
    assert 真现在.timestamp() * 1000 - _T0 > 200 * 86400 * 1000, (
        "注入钟得比真墙钟早得够多, 不然两口钟判出来的过期是一样的")
    戳 = (真现在 - timedelta(days=400)).strftime(STAMP_FMT)
    run = Path(ctx.runs_root) / "m1" / 戳
    (run / "photos").mkdir(parents=True)
    (run / "photos" / f"P0__front__{戳}.jpg").write_bytes(b"jpegfake" * 64)
    (run / "manifest.json").write_text(json.dumps({
        "mission": {"mission": "m1", "map_id": "map_test", "waypoints": [],
                    "policy": {"retention_days": 90}},
        "started_at": 戳, "fingerprint": {},
        "summary": {"state": "COMPLETED", "total": 1},
    }, ensure_ascii=False), encoding="utf-8")

    code, body, _ = request(拨过钟的服务, "/api/storage")
    assert code == 200, body
    名单 = json.loads(body)["forecast"]["runs"]
    assert [r["mission"] for r in 名单] == ["m1"], (
        "按真墙钟这趟早过期了。名单是空的, 说明保留期这摊被接到注入钟上了 "
        "—— 而 plan_sweep 后面接的是 shutil.rmtree")


# ------------------------------------------------------- 默认值一个字都没动


def test_上下文默认还是墙钟(拨过钟的服务):
    """``test_suspend_e2e.py`` 的 ``test_闸门读的钟必须还是墙钟`` 钉的就是
    这一条。这里再钉一遍, 是因为必修 4 改的正是它周围那一圈 —— 改崩了要在
    这个文件里也看得见, 而不是只在一个讲挂起的文件里。
    """
    assert AppContext.__dataclass_fields__["clock"].default is _wall_ms
    assert abs(_wall_ms() - int(time.time() * 1000)) < 5_000
