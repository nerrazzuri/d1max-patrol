"""W23:真身挪到契约包 ``d1max_contract.planning.costmap``(站点设拦截点时也要用同一份规划)。
这里让旧名字 ``d1max_agent.planning.costmap`` 指到**同一个模块对象**,
老调用与 monkeypatch 不受影响。"""

import sys

from d1max_contract.planning import costmap as _real

sys.modules[__name__] = _real
