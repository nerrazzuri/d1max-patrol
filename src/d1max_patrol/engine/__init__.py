"""``d1max_patrol.engine`` 的**别名壳**(W00b,决定 1)。

引擎的代码在 ``d1max_agent.engine``(robot-agent 包)。这里只把每个子模块的名字指到
**同一个模块对象**上:``import d1max_patrol.engine.machine`` 与
``import d1max_agent.engine.machine`` 拿到的是同一个东西 —— 私有名、monkeypatch、
``isinstance`` 全都一致。81 个测试文件、``app/``、``cli.py`` 因此一字不改。

**注意**:因为是同一个对象,logger 名、``__module__``、``__file__`` 都是
``d1max_agent.engine.*``;按旧名字 ``caplog`` 的地方要改。

这层壳在 W00c(手机改连站点、``server.py`` 退役)时删掉;那之前不要往这里加任何逻辑。
"""

from __future__ import annotations

import importlib
import sys

_MODULES = (
    "alerts", "archive", "backup", "baselines", "bundle", "datadir", "export", "form",
    "homing", "http_sink", "lease", "machine", "mission", "preflight", "privileged",
    "release", "removable", "retention", "safety", "schedule", "selfcheck", "storage",
    "uploader", "upload_queue",
)

for _name in _MODULES:
    _mod = importlib.import_module(f"d1max_agent.engine.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _mod
    globals()[_name] = _mod

__all__ = list(_MODULES)
