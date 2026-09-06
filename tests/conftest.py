"""整个测试套件共用的零件。放这儿是因为它要能被 ``tests`` 下所有子包看到
(``tests/app``、``tests/engine`` 都要用)。
"""

from __future__ import annotations


class NoDisks:
    """测试里的探针替身:永远认不到盘。

    默认那个 ``DEFAULT_PROBE`` 扫的是真机上的 /media 和 /mnt —— 测试的结果
    就会取决于跑测试的这台机器上此刻插着什么盘。那种红在别人机器上复现不了。
    """

    async def scan(self) -> tuple[()]:
        return ()
