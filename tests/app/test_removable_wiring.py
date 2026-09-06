"""外插盘探针:测试套件不能悄悄掉回真的那个。

`MissionEngine` 的 `removable` 是可注入的,唯一的理由是离机测试要能躲开真机
上此刻插着的盘。这条钉住 `ctx` 用的确实是测试替身,不是 `DEFAULT_PROBE` ——
后者会去扫真机上的 `/media`、`/mnt`,一旦以后有人新起一处引擎构造忘了传
`removable=`,它会悄悄继承那个默认值,在这台机器上不出事,换一台插着盘的
机器(或者 WSL,`/mnt/c` 天生就是个挂载点)就会红,而且没人能复现。
"""

from __future__ import annotations

from d1max_patrol.engine.removable import DEFAULT_PROBE


def test_ctx的引擎用的不是真探针(ctx):
    assert ctx.engine.removable is not DEFAULT_PROBE
