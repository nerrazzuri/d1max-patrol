"""D1 Max 巡检与例检系统。"""

import os as _os

# 改名兼容一个版本(同 nexwar_contract.oldenv):老的 D1MAX_X 照抄成 NEXWAR_X,新键已有的不动
for _k in [k for k in _os.environ if k.startswith("D1MAX_")]:
    _os.environ.setdefault("NEXWAR_" + _k[6:], _os.environ[_k])

__version__ = "1.0.0-rc.1"
