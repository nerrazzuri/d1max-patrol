"""W00c2a:真身在契约包 ``nexwar_contract.mission``(站点与狗共用)。这里让旧名字
``nexwar_agent.engine.mission`` 指到**同一个模块对象**,
老调用与 monkeypatch 不受影响。"""

import sys

from nexwar_contract import mission as _real

sys.modules[__name__] = _real
