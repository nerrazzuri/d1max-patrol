# D1 Max 真机一致性巡检报告

- 开始时间(UTC): `2026-09-01T04:15:57.754227+00:00`
- 导航地址: `ws://192.168.168.100:10010`
- 允许运动阶段: 否(仅只读)
- 原始帧录像: `runs/frames/nav-20260901T041557Z.jsonl` (共 215 行) **← 最重要的产出,务必带回**

## 阶段 0 · 网络可达性

| 链路 | 地址 | 结果 | 说明 |
|---|---|---|---|
| nav | `192.168.168.100:10010` | 通 | TCP 可达 |
| nav-144 | `192.168.144.100:10010` | 通 | TCP 可达 |
| sdk-wired | `192.168.168.168:8081` | 通 | TCP 可达 |
| sdk-wifi | `192.168.234.1:8081` | 通 | TCP 可达 |

## 阶段 1 · 只读接口扫描

共 5 条调用,成功 4,失败 1。

| 接口 | 结果 | 耗时(s) | 返回/错误 |
|---|---|---|---|
| `nav_status` | ok | 0.004 | `<NavStatus.STANDBY: 'StandBy'>` |
| `loc_status` | ok | 0.005 | `None` |
| `mapping_status` | ok | 0.004 | `<MappingStatus.MAPPING_READY: 'MappingReady'>` |
| `get_speed` | ok | 0.017 | `{'x': 0.6, 'y': 0.5, 'z': 1.5}` |
| `list_maps` | **失败** | 5.002 | `NavTimeoutError: get_all_pgm_map 超过 5.0s 未收到响应` |

### 现场事实

- 机器上没有任何地图: list_paths / get_map_grid 未能执行。若需要覆盖它们,先按手册手动建一张小图再重跑本命令。

## 字段级对拍(真机响应 vs golden fixture)

### `get_loc_status`

对拍样本: `048.json`

文档有、真机没有:

- `data.req_result.data`
- `data.req_result.msg`
- `data.req_result.req_func`
- `data.req_result.status`

真机有、文档没写:

- `data.req_result.AppResponse.data`
- `data.req_result.AppResponse.error_code`
- `data.req_result.AppResponse.msg`
- `data.req_result.AppResponse.req_func`
- `data.req_result.AppResponse.status`

### `get_mapping_status`

对拍样本: `009.json`

文档有、真机没有:

- `data.req_result.data`
- `data.req_result.msg`
- `data.req_result.req_func`
- `data.req_result.status`

真机有、文档没写:

- `data.req_result.AppResponse.data`
- `data.req_result.AppResponse.error_code`
- `data.req_result.AppResponse.msg`
- `data.req_result.AppResponse.req_func`
- `data.req_result.AppResponse.status`

### `get_nav_status`

对拍样本: `035.json`

文档有、真机没有:

- `data.req_result.data`
- `data.req_result.msg`
- `data.req_result.req_func`
- `data.req_result.status`

真机有、文档没写:

- `data.req_result.AppResponse.data`
- `data.req_result.AppResponse.error_code`
- `data.req_result.AppResponse.msg`
- `data.req_result.AppResponse.req_func`
- `data.req_result.AppResponse.status`

### `get_navigation_speed`

对拍样本: `041.json`

**一致。**

