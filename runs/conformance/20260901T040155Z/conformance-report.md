# D1 Max 真机一致性巡检报告

- 开始时间(UTC): `2026-09-01T04:01:55.702173+00:00`
- 导航地址: `ws://127.0.0.1:10011`
- 允许运动阶段: 否(仅只读)
- 原始帧录像: `runs/frames/nav-20260901T040155Z.jsonl` (共 18 行) **← 最重要的产出,务必带回**

## 阶段 0 · 网络可达性

| 链路 | 地址 | 结果 | 说明 |
|---|---|---|---|
| nav | `127.0.0.1:10011` | 通 | TCP 可达 |
| nav-144 | `192.168.144.100:10010` | **不通** | 连接 192.168.144.100:10010 超时(3.0s) —— 网段不通或 IP 没配对 |
| nav-168 | `192.168.168.100:10010` | **不通** | 连接 192.168.168.100:10010 超时(3.0s) —— 网段不通或 IP 没配对 |
| sdk-wired | `192.168.168.168:8081` | **不通** | 连接 192.168.168.168:8081 超时(3.0s) —— 网段不通或 IP 没配对 |
| sdk-wifi | `192.168.234.1:8081` | **不通** | 连接 192.168.234.1:8081 超时(3.0s) —— 网段不通或 IP 没配对 |

## 阶段 1 · 只读接口扫描

共 7 条调用,成功 7,失败 0。

| 接口 | 结果 | 耗时(s) | 返回/错误 |
|---|---|---|---|
| `nav_status` | ok | 0.001 | `<NavStatus.STANDBY: 'StandBy'>` |
| `loc_status` | ok | 0.001 | `<LocStatus.INIT: 'Init'>` |
| `mapping_status` | ok | 0.001 | `<MappingStatus.PASSIVE: 'Passive'>` |
| `get_speed` | ok | 0.001 | `{'x': 0.8, 'y': 0.5, 'z': 1.5}` |
| `list_maps` | ok | 0.001 | `['demo_map']` |
| `list_paths(demo_map)` | ok | 0.001 | `{'demo_route': [Waypoint(name='P1_配电柜', pose=Pose(position=Position(x=2.0, y=0.0, z=0.0), orientation=Orientation(x=0.0, y=0.0, z=0.0, w=1.0))), Waypoint(name='P2_水泵', pose=Pose(position=Position(x=2. …` |
| `get_map_grid(demo_map)` | ok | 0.001 | `{'header': {'frame_id': 'map', 'stamp': {'sec': 0, 'nanosec': 0}}, 'info': {'map_load_time': {'sec': 0, 'nanosec': 0}, 'resolution': 0.05, 'width': 20, 'height': 20, 'origin': {'position': {'x': 0.0,  …` |

## 字段级对拍(真机响应 vs golden fixture)

### `get_all_paths_by_mapid`

**文档里没有这个响应的样本。** 真机发回了文档没写的报文 —— 这条要单独记下来带回去。字段:

- `data.req_result.data`
- `data.req_result.data[]`
- `data.req_result.msg`
- `data.req_result.req_func`
- `data.req_result.status`
- `head.frame_count`
- `head.source`
- `head.time_stamp`
- `head.type`

### `get_all_pgm_map`

**文档里没有这个响应的样本。** 真机发回了文档没写的报文 —— 这条要单独记下来带回去。字段:

- `data.req_result.data.demo_map`
- `data.req_result.data.demo_map[].data`
- `data.req_result.data.demo_map[].data[]`
- `data.req_result.data.demo_map[].header.frame_id`
- `data.req_result.data.demo_map[].header.stamp.nanosec`
- `data.req_result.data.demo_map[].header.stamp.sec`
- `data.req_result.data.demo_map[].info.height`
- `data.req_result.data.demo_map[].info.map_load_time.nanosec`
- `data.req_result.data.demo_map[].info.map_load_time.sec`
- `data.req_result.data.demo_map[].info.origin.orientation.w`
- `data.req_result.data.demo_map[].info.origin.orientation.x`
- `data.req_result.data.demo_map[].info.origin.orientation.y`
- `data.req_result.data.demo_map[].info.origin.orientation.z`
- `data.req_result.data.demo_map[].info.origin.position.x`
- `data.req_result.data.demo_map[].info.origin.position.y`
- `data.req_result.data.demo_map[].info.origin.position.z`
- `data.req_result.data.demo_map[].info.resolution`
- `data.req_result.data.demo_map[].info.width`
- `data.req_result.msg`
- `data.req_result.req_func`
- `data.req_result.status`
- `head.frame_count`
- `head.source`
- `head.time_stamp`
- `head.type`

### `get_loc_status`

对拍样本: `048.json`

**一致。**

### `get_mapping_status`

对拍样本: `009.json`

**一致。**

### `get_nav_status`

对拍样本: `035.json`

**一致。**

### `get_navigation_speed`

对拍样本: `041.json`

**一致。**

### `get_pgm_map`

**文档里没有这个响应的样本。** 真机发回了文档没写的报文 —— 这条要单独记下来带回去。字段:

- `data.req_result.data.data`
- `data.req_result.data.data[]`
- `data.req_result.data.header.frame_id`
- `data.req_result.data.header.stamp.nanosec`
- `data.req_result.data.header.stamp.sec`
- `data.req_result.data.info.height`
- `data.req_result.data.info.map_load_time.nanosec`
- `data.req_result.data.info.map_load_time.sec`
- `data.req_result.data.info.origin.orientation.w`
- `data.req_result.data.info.origin.orientation.x`
- `data.req_result.data.info.origin.orientation.y`
- `data.req_result.data.info.origin.orientation.z`
- `data.req_result.data.info.origin.position.x`
- `data.req_result.data.info.origin.position.y`
- `data.req_result.data.info.origin.position.z`
- `data.req_result.data.info.resolution`
- `data.req_result.data.info.width`
- `data.req_result.msg`
- `data.req_result.req_func`
- `data.req_result.status`
- `head.frame_count`
- `head.source`
- `head.time_stamp`
- `head.type`

