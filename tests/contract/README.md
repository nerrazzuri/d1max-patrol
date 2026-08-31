# 契约测试

这里的测试定义 `NavBackend` 的行为约定,与任何具体实现无关。

## 规矩

- 只能 import `d1max_patrol.backends.base` 和 `d1max_patrol.protocol.nav_types`
- 不许 import `vendor_nav`、不许 import `d1max_sim`、不许碰 `sim.faults`
- 不许断言任何实现独有的属性(`fallback_matches`、`_pending`、…)

违反以上任何一条,这套测试就不再是契约测试,换后端时会假通过。

## 加一个新后端

在 `conftest.py` 的 `BACKEND_FACTORIES` 里加一行工厂函数即可,本目录其余文件
不用动。改不动说明抽象漏了 —— 该修的是 `base.py`。
