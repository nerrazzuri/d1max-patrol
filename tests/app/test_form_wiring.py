"""跑在哪一档:整个进程只有一个出处。

引擎里那道 preflight 才是真拦住这一趟的那道 —— ctx 只是把引擎接到 HTTP 上。
两边各存一份形态,今天都是单机档,所以谁也看不出来;等联网档落地,对不上的
那个组合正好是没人跑过的那个(见 ``engine/form.py`` 开篇那条约束)。
"""

from __future__ import annotations

from d1max_patrol.app.server import _make_engine
from d1max_patrol.engine.form import STANDALONE, Form

from . import conftest as C


def test_没说就是单机档(ctx):
    # 没有服务器的客户装的就是这个。
    assert ctx.form is STANDALONE


def test_ctx_的形态就是引擎的形态(ctx):
    assert ctx.form is ctx.engine.form


def test_生产那个造引擎的函数收得下形态(bridge, tmp_path):
    # 盯的是 `_make_engine` 这条生产路径,不是测试自己另拼一个引擎 ——
    # 形态传不进去的话,现场那道 preflight 用的就永远是构造函数的默认值。
    connected = Form(name="connected")
    engine = bridge.call(lambda: _make_engine(
        C.FakeNav(), C.FakeDevice(), {}, tmp_path / "runs", None,
        form=connected))
    try:
        assert engine.form is connected
    finally:
        bridge.call(engine.aclose, timeout_s=10.0)
