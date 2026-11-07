"""D1 Max 车身尺寸的三层口径(导航 V1,``docs/导航V1-设计.md`` 「车身尺寸」)。只在这里定义。

| 层 | 长 × 宽 | 谁用 |
|---|---|---|
| 物理车身 | 0.93 × 0.48 m | 仿真判真实碰撞 |
| + 基础安全余量 0.05 m/边 | 1.03 × 0.58 m | 守卫、Nav2 footprint |
| + 路线检查余量 0.20 m/边 | 1.43 × 0.98 m | Route Validator |

0.98 m 只是直通道的最小宽度,转弯看扫过的面积。路线检查余量 0.20 m 是临时值,以后按定位质量加。
"""

from __future__ import annotations

BODY_LEN = 0.93
BODY_WID = 0.48
BASE_MARGIN = 0.05
ROUTE_MARGIN = 0.20
BASE_HALF_LEN = BODY_LEN / 2 + BASE_MARGIN
BASE_HALF_WID = BODY_WID / 2 + BASE_MARGIN
#: 直通道按有效车身要多宽(米)。
ROUTE_MIN_WIDTH = 2 * (BASE_HALF_WID + ROUTE_MARGIN)
