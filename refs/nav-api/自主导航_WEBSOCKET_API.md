# WebSocket JSON 交互协议文档

## 概述

本文档描述了四足机器人设备端与外部 App 之间的 WebSocket JSON 消息交互格式。

**WebSocket 地址**: 
```
192.168.144.100:10010 #D1 Max
```


## 消息结构

所有消息都遵循以下基本结构：

```json
{
  "head": {
    "type": "消息类型",
    "time_stamp": 时间戳（毫秒）,
    "source": "消息来源",
    "frame_count": 帧计数
  },
  "data": {
    // 根据消息类型不同而不同
  }
}
```

### 消息类型 (type)

- `app_req`: App 请求消息
- `app_resp`: App 响应消息
- `app_sub_topic`: 订阅主题消息（设备端推送给 App）
- `app_topic_req`: App 请求订阅主题
- `app_topic_resp`: 主题订阅响应

### 消息来源 (source)

- `app`: 来自 App
- `alg_control_node`: 来自算法控制节点
- `robot_roamerx`: 来自机器人本体
- `cli`: 来自命令行

---

## 1. SLAM 建图相关

### 1.1 开始建图

**请求 (App → 设备端)**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_mapping": 0
    }
  }
}
```

**响应 (设备端 → App)**

成功：
```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "start_mapping",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

失败：
```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "start_mapping",
      "status": "error",
      "msg": "错误信息",
      "data": null
    }
  }
}
```

### 1.2 停止建图

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "stop_mapping": 0
    }
  }
}
```

**响应**

成功：
```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "stop_mapping",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

**停止建图状态通知** (设备端主动推送)

当停止建图后，设备端会推送建图保存完成的通知：

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890125,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "notify_stop_mapping_status",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 1.3 获取建图状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_mapping_status": null
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "get_mapping_status",
      "status": "ok",
      "msg": null,
      "data": "MappingRunning"
    }
  }
}
```

**建图功能状态值说明**:
- `Unknown`: 未知状态
- `Passive`: 功能抑制状态-传感器无数据等
- `InitWaitSensor`: 等待传感器初始化
- `MappingReady`: 建图就绪
- `MappingRunning`: 建图中
- `MappError`: 建图错误
- `MappingSaveBegin`: 开始保存地图
- `MappingSaveEnd`: 地图保存完成

### 1.4 获取 PGM 地图

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_pgm_map": null
    }
  }
}
```

**响应**

成功：
```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "get_pgm_map",
      "status": "ok",
      "msg": null,
      "data": {
        "header": {
          "stamp": {
            "sec": 0,
            "nanosec": 0
          },
          "frame_id": "map"
        },
        "info": {
          "map_load_time": {
            "sec": 0,
            "nanosec": 0
          },
          "resolution": 0.05,
          "width": 1000,
          "height": 1000,
          "origin": {
            "position": {
              "x": 0.0,
              "y": 0.0,
              "z": 0.0
            },
            "orientation": {
              "x": 0.0,
              "y": 0.0,
              "z": 0.0,
              "w": 1.0
            }
          }
        },
        "data": [0, 1, 2, ...]
      }
    }
  }
}
```

### 1.5 获取所有 PGM 地图列表

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_all_pgm_map": null
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "get_all_pgm_map",
      "status": "ok",
      "msg": null,
      "data": {
        "map_id_1": [
          {
            "header": {...},
            "info": {...},
            "data": [...]
          },
          [
            [0, [x, y, z]],
            [1, [x, y, z]],
            ...
          ]
        ],
        "map_id_2": [...]
      }
    }
  }
}
```


### 1.6 删除指定地图

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "remove_map_by_id": ["map_id_1", "map_id_2"]
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "remove_map_by_id",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 1.7 重命名地图

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "rename_map_name": ["old_map_id", "new_map_id"]
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "rename_map_name",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

---

## 2. 导航路径管理

### 2.1 获取指定地图下的所有导航路径

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_all_paths_by_mapid": "map_id"
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "get_all_paths_by_mapid",
      "status": "ok",
      "msg": null,
      "data": [
        "map_id",
        ["path_id_1", "path_id_2"],
        {
          "path_id_1": [
            ["point_name_1", {
              "position": {"x": 1.0, "y": 2.0, "z": 0.0},
              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
            }],
            ["point_name_2", {...}]
          ],
          "path_id_2": [...]
        }
      ]
    }
  }
}
```

### 2.2 添加新的导航路径

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "add_nav_path": [
        "map_id",
        "path_id",
        [
          ["point_name_1", {
            "position": {"x": 1.0, "y": 2.0, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
          }],
          ["point_name_2", {...}]
        ]
      ]
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "add_nav_path",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 2.3 修改导航路径

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "modify_nav_path": [
        "map_id",
        "old_path_id",
        "new_path_id",
        [
          ["point_name_1", {
            "position": {"x": 1.0, "y": 2.0, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
          }]
        ]
      ]
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "modify_nav_path",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 2.4 删除导航路径

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "remove_nav_path": [
        ["map_id_1", "path_id_1"],
        ["map_id_2", "path_id_2"]
      ]
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "remove_nav_path",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

---

## 3. 导航控制

### 3.1 开始单点导航

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_nav": {
        "position": {"x": 1.0, "y": 2.0, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
      }
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "start_nav",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 3.2 通过路径id 开始多点导航

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_multi_nav": ["map_id", "path_id"]
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "start_multi_nav",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 3.3 通过点列表 开始多点导航

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_multi_nav_by_points": [
        "map_id",
        [
          {
            "position": {"x": 1.0, "y": 2.0, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
          },
          {
            "position": {"x": 3.0, "y": 4.0, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
          }
        ]
      ]
    }
  }
}
```



### 3.4 开始返航

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_nav_return_home": null
    }
  }
}
```

### 3.5 停止导航

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "stop_nav": null
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "stop_nav",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 3.6 获取导航状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_nav_status": null
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "get_nav_status",
      "status": "ok",
      "msg": null,
      "data": "StandBy"
    }
  }
}
```
**导航功能状态值说明**:
- `StandBy`: 就绪-仅在此状态下可启动导航
- `Initializing`: 功能初始化中
- `Active`: 导航激活运行中
- `Pause`: 导航暂停
- `Cancelled`: 导航取消
- `Succeed`: 导航任务成功结束
- `Failed`: 导航任务失败

### 3.7 暂停导航

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "pause_nav": null
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "pause_nav",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 3.8 继续导航

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "continue_nav": null
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "continue_nav",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 3.9 获取当前导航速度配置

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_navigation_speed":{
          "type": "navigation_speed", 
      }
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "AppReponseObjectData": {
        "req_func": "get_navigation_speed",
        "status": "ok",
        "msg": null,
        "data": {
          "x": 0.8,
          "y": 0.4,
          "z": 1.2
        }
      }
    }
  }
}
```
### 3.10 设置当前导航速度配置

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "set_navigation_speed": {
        "type": "navigation_speed",    
        "x": 0.8,
        "y": 0.4,
        "z": 1.2
      }
    }
  }
```
```txt
        如果传(x, y, z) y, z得都用默认的0.5和1.5
        也可以只传 x,那么 y默认 0.5，z默认 1.5
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "AppReponseObjectData": {
        "req_func": "set_navigation_speed",
        "status": "ok",
        "msg": null,
        "data": {
          "x": 0.8,
          "y": 0.4,
          "z": 1.2
        }
      }
    }
  }
}
```


## 4. 定位相关

### 4.1 加载定位地图

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "loc_load_map": "map_id"
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "load_localization_map",
      "status": "ok",
      "msg": null,
      "data": null
    }
  }
}
```

### 4.2 重置定位

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "reset_loc": null
    }
  }
}
```

### 4.3 获取定位状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_loc_status": null
    }
  }
}
```

**响应**

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "get_loc_status",
      "status": "ok",
      "msg": null,
      "data": "状态字符串"
    }
  }
}
```
**定位功能状态值说明**:
- `Init`: 定位功能初始化中
- `MapLoading`: 定位地图加载中
- `InitLocalization`: 定位初始化过程中
- `ContinuousLoc`: 持续定位运行中
- `Error`: 定位节点异常
- `DynamicInitLoc`: 动态定位辅助过程中
- `LocLost`: 定位丢失

---

## 5. 通用响应格式

### 成功响应

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "功能名称",
      "status": "ok",
      "msg": null,
      "data": null  // 或具体数据
    }
  }
}
```

### 错误响应

```json
{
  "head": {
    "type": "app_resp",
    "time_stamp": 1234567890124,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "req_result": {
      "req_func": "功能名称",
      "status": "error",
      "msg": "错误描述信息",
      "data": null
    }
  }
}
```

---

## 6. 注意事项

1. **frame_count**: 请求和响应中的 `frame_count` 应该保持一致，用于匹配请求和响应
2. **time_stamp**: 使用毫秒级时间戳
3. **消息顺序**: WebSocket 消息可能不按顺序到达，应通过 `frame_count` 匹配请求和响应
4. **错误处理**: 所有请求都应该处理错误响应
5. **连接管理**: 连接断开时，所有正在进行的操作可能会失败
6. **数据格式**: Pose 数据中的 orientation 使用四元数表示 (x, y, z, w)
7. **地图数据**: PGM 地图数据中的 `data` 字段是字节数组，表示占用栅格地图的像素值

---

---
## 7. 回充 (ARC) 相关

  **中狗回充仅支持7.3, 7.4, 7.12**,**7.14**

### 7.1 获取当前回充标签 ID

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_cur_arc_tagid": null
    }
  }
}
```


### 7.2 开始回充（带地图）

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_arc_with_map": "map_id"
    }
  }
}
```

### 7.3 停止回充

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "stop_arc": null
    }
  }
}
```

### 7.4 开始回充粗对准

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_arc_align_coarse": null
    }
  }
}
```

### 7.5 开始回充标定

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_arc_calibration": null
    }
  }
}
```

### 7.6 获取回充蓝牙连接状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_arc_bluetooth_connect_status": null
    }
  }
}
```

### 7.7 获取回充匹配状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_arc_match_status": null
    }
  }
}
```

### 7.8 开始回充建图

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "start_arc_mapping": "map_id"
    }
  }
}
```

### 7.9 停止回充建图

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "stop_arc_mapping": null
    }
  }
}
```

### 7.10 获取回充建图状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_arc_mapping_status": null
    }
  }
}
```

### 7.11 获取回充建图检测标签

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_arc_mapping_detect_tag": null
    }
  }
}
```

### 7.12 获取 ARC 算法状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_arc_alg_status": null
    }
  }
}
```

**回复**

```json
{
    "head": {
        "type": "app_resp",
        "time_stamp": 1763710059813,
        "source": "alg_control_node",
        "frame_count": 1
    },
    "data": {
        "req_result": {
            "AppResponse": {
                "req_func": "get_arc_alg_status",
                "status": "ok",
                "msg": null,
                "data": "StandBy"
            }
        }
    }
}
```

枚举类型:

```txt
"Calibration",
"ChargedExit",
"Charging",
"DockAlignCoarse",
"FailureContact",
"DockAlignFine",
"DockContact",
"FailureSafe",
"Passive",
"RthInitial",
"RthNavigation",
"StandBy",
"Success",
"UnDockReset",
```

仅在StandBy状态下可以开始发送7.4请求start_arc_align_coarse



### 7.13 获取 Dock 状态

**请求**

```json
{
  "head": {
    "type": "app_req",
    "time_stamp": 1234567890123,
    "source": "app",
    "frame_count": 1
  },
  "data": {
    "req_func": {
      "get_arc_dock_status": null
    }
  }
}
```

---

### 7.14 请求出桩

**请求**

```json
{
    "head": {
        "type": "app_req",
        "time_stamp": 1763708841048,
        "source": "app",
        "frame_count": 1
    },
    "data": {
        "req_func": "exit_charging"
    }
}
```

**回复**

```json
{
    "head": {
        "type": "app_resp",
        "time_stamp": 1763706993936,
        "source": "alg_control_node",
        "frame_count": 1
    },
    "data": {
        "req_result": {
            "AppResponse": {
                "req_func": "exit_charging",
                "status": "ok",
                "msg": null,
                "data": null
            }
        }
    }
}
```



## 8. 示例代码

### JavaScript/TypeScript 示例

```typescript
const ws = new WebSocket('ws://0.0.0.0:10010');

// 发送开始建图请求
function startMapping() {
  const msg = {
    head: {
      type: "app_req",
      time_stamp: Date.now(),
      source: "app",
      frame_count: 1
    },
    data: {
      req_func: {
        start_mapping: 0
      }
    }
  };
  ws.send(JSON.stringify(msg));
}

// 接收消息
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  console.log('收到消息:', msg);
  
  if (msg.head.type === 'app_resp') {
    const result = msg.data.req_result;
    console.log(`功能: ${result.req_func}, 状态: ${result.status}`);
    if (result.msg) {
      console.log(`消息: ${result.msg}`);
    }
  } else if (msg.head.type === 'app_sub_topic') {
    // 处理订阅的主题数据
    console.log('收到主题数据:', msg.data);
  }
};
```

### Python 示例

```python
import asyncio
import websockets
import json

async def start_mapping():
    uri = "ws://0.0.0.0:10010"
    async with websockets.connect(uri) as websocket:
        # 发送开始建图请求
        msg = {
            "head": {
                "type": "app_req",
                "time_stamp": int(time.time() * 1000),
                "source": "app",
                "frame_count": 1
            },
            "data": {
                "req_func": {
                    "start_mapping": 0
                }
            }
        }
        await websocket.send(json.dumps(msg))
        
        # 接收响应
        response = await websocket.recv()
        result = json.loads(response)
        print(f"收到响应: {result}")

asyncio.run(start_mapping())
```
---

## 9. 算法故障码
```json
{
  "head": {
    "type": "alg_error_code_notify",
    "time_stamp": 1763697492747,
    "source": "alg_control_node",
    "frame_count": 1
  },
  "data": {
    "items": [
      {
        "code": 13330,
        "description": "navigation blocked",
        "severity": 0,
      },
      {
        "code": 13331,
        "description": "lidar disconnected",
        "severity": 1,
      }
    ]
  }
}
```
---
## 更新日志

- 2026-01-07: 初始版本，包含所有基础 API 文档
- 2026-03-12: 增加3.7暂停导航和3.8继续导航

* 2026-03-16: 增加ch7 回充功能相关接口，主要支持中狗回充。
* 2026-06-02: 增加导航故障码上报。
* 2026-06-11: 增加3.9,3.10导航速度设置接口
