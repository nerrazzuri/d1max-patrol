"""确认包结构与测试框架可用。"""

import asyncio

import d1max_patrol
import d1max_sim


def test_包可导入且带版本号():
    assert d1max_patrol.__version__ == "0.1.0"
    assert d1max_sim.__version__ == "0.1.0"


async def test_异步测试模式已开启():
    """asyncio_mode=auto 生效时,这个不加装饰器的协程测试应被真正执行。"""
    await asyncio.sleep(0)
    assert True
