# Nexus Warden 四足机器人安防巡逻系统

四足机器人（当前适配智元 D1 Max）在园区、商场里按路线和日程巡逻，看见人就报警、留证、派机器人过去处置。
当前版本 **1.0.0-rc.1**，改了什么见 [`CHANGELOG.md`](CHANGELOG.md)。

## 由哪几部分组成

| 目录 | 是什么 |
|---|---|
| `packages/site-node` | 站点服务：值班、报警、地图、路线、日程、录像、远程运维；网页值班台也由它提供 |
| `packages/robot-agent` | 机器人上的代理：执行任务、避障、低电量回充，跟站点双向证书连接 |
| `packages/localizer` | 建图与定位（Super-LIO）、Nav2 对接、人员检测 |
| `packages/adapter-d1max` | D1 Max 的运动、电池、上装适配层 |
| `packages/adapter-sim` | 仿真机器人，没有真机也能跑全套 |
| `packages/contract` | 站点与机器人之间的协议和产品版本号 |
| `mobile/` | 手机 App（Flutter，安卓）：值守、遥控、建图向导、配网 |
| `web/console` | 网页值班台的前端源码（构建产物提交在站点包里） |
| `motion/` | 机器人上的 C++ 运动旁路 |
| `deploy/` | 安装脚本、systemd 单元、配置样例 |

## 文档入口

- 正式发行与安装：[`docs/正式发行手册.md`](docs/正式发行手册.md)
- 远程运维：[`docs/远程运维手册.md`](docs/远程运维手册.md)
- 版本兼容与升级顺序：[`docs/版本兼容与升级顺序.md`](docs/版本兼容与升级顺序.md)
- 真机待验清单：[`docs/真机验证.md`](docs/真机验证.md)
- 现场联调（给现场笔记本上的 Claude）：[`START-HERE.md`](START-HERE.md)
- 进度与分卷：[`docs/分卷与进度.md`](docs/分卷与进度.md)

## 开发

Python 3.10，Ubuntu 22.04。

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]" -e "packages/contract[mqtt]" -e packages/adapter-sim \
  -e packages/adapter-d1max -e "packages/localizer[build]" -e packages/robot-agent -e packages/site-node
```

- **测试**：`env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest -p no:cacheprovider --tb=short`
  （装了 ROS 2 的机器要去掉这三个环境变量，否则 ROS 的 pytest 插件会混进来。全量约 13 分钟。）
- **代码检查**：以 `.github/workflows/ci.yml` 里 lint 那一步的 `ruff check …` 命令为准，提交前原样跑一遍。
- **网页前端**：`cd web/console && npm ci && npm test && npm run build`，构建产物要一起提交（CI 会核对）。
- **手机 App**：`cd mobile && flutter test --concurrency=1`。
- **演示站点**（不用真机）：`.venv/bin/python tools/site_demo.py`，会起站点、消息服务和一台仿真机器人，并打印地址和账号。

`master` 受保护：开分支 → PR → CI 全绿 → rebase 合并。

## 许可

专有软件，保留所有权利，见 [`LICENSE`](LICENSE)。第三方软件声明见发行包里的 `third-party-notices.txt`。
