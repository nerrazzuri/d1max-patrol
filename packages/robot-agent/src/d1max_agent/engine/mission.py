"""W00c2a:真身在契约包 ``d1max_contract.mission``(站点与狗共用)。这里让旧名字
``d1max_agent.engine.mission``(以及别名壳 ``d1max_patrol.engine.mission``)指到**同一个模块对象**,
老调用与 monkeypatch 不受影响。"""

import sys

from d1max_contract import mission as _real

sys.modules[__name__] = _real
