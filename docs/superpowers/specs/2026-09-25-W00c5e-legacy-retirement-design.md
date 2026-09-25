# W00c5e · 老服务退役 —— 设计稿

日期：2026-09-25 · 工单：W00c5e（L，依赖 W00c5d）
状态：**按推荐（用户 2026-09-25 授权连续推进：「从W00c5a到W00c5e直接做完」）**。七个决定都取推荐项。受决策 7、决策 8 约束：遥控、画面、数据都已经走站点（W00c5a–d），老服务上的同类功能不再有人用。

## 0. 事实（2026-09-25 摸底）

- **狗上老服务** `src/d1max_patrol/app/server.py`（4705 行）：**71 条路由**（工单表写的 76 条是早先的数）+ 静态页（`app/static/` 三个文件）；端口 8095；入口 `d1max-app`（`pyproject.toml`）；单元 `deploy/d1max-patrol.service`（`ExecStartPre` 挂 `release boot-guard` 与 `bundle guard`）。
- **只有老服务在用的模块**：
  - `app/`：`auth`（除了 `identity` 借用的两个名字）、`control`、`gridmap`、`teleop`、`upload_pump`、`video`；
  - `inspect/judge.py`、`inspect/report.py`（站点有自己的一份判读）；
  - `backends/map_bridge.py`、`protocol/map_frames.py`、`tools/ros2_map_bridge.py` 与仿真 `d1max_sim/map_server.py`（建图时往老网页推实时栅格，只有老服务和代理的 `--legacy-http` 用）；`backends/local_nav.py` 生产上只有老服务用 —— **但它留下**（实施时改的）：它是自建图那一路的导航后端，真狗要载入自建的图时（另开工单）用得上，契约测试也拿它当参照实现；
  - 引擎：`backup`、`baselines`、`lease`、`schedule`（空壳）、`export`（另一个用它的 `d1max_console` 没有入口，是孤儿）；
  - 部分函数：`retention` 的扫盘、预告（发件箱只用 `is_settled`）；`selfcheck` 的 precheck、postcheck、`restart_plan`；`privileged` 的 `restart`；狗上的任务包落包、回滚、开机守卫（`engine/bundle.py` 的狗侧部分、`cli bundle guard`）。
- **要留下的**：`app/bridge.py`（代理用）、`app/mapping.py` 与 `app/procs.py`（代理 `--mapping` 用）、`app/identity.py`（命令行 `release activate` 取 SN）、`backends/base.py`、`backends/sidecar_device.py`、`backends/vendor_nav.py`、`config/*`、`cli.py`（`release boot-guard` 两个单元都挂着）、引擎里代理用的那些（archive、datadir、homing、http_sink、machine、preflight、release、removable、safety、storage、uploader、upload_queue 等）、`d1max_sim` 除 `map_server` 以外的部分。
- **代理的 `--legacy-http`**（`main.py`）：连同 `--pin`、`--sn`，拉进老服务的整套上下文。
- **部署**：
  - `install.sh` 5/7 装老单元、生成 PIN、`enable d1max-patrol`；代理单元**装而不 enable**；6/7 用 `curl :8095` 验；7/7 PIN 为空就不往下走，切版本后停、搬数据、起老服务。
  - 特权助手：`install-unit`（装老单元）、`restart`（经 `d1max-restart-now` 重启老服务）、`reboot` 只有老服务在用；`check` 只有它们用。**代理一条都不用**（W00c5d 第三部分评审修复之后，切版本不装单元，代理运行时不需要 root）。
  - 命令行 `release activate/rollback` 装的是**老单元**。
  - 代理单元已经指着这一版带的启动脚本 `deploy/d1max-agent-start`，适配器从 env 取（`D1MAX_HAL`，缺省 `sim`），其余参数 `D1MAX_AGENT_ARGS`（W00c5d 第三部分评审修复）。
- **手机直连模式**：首屏二选一（`ModePage`）、名册、直连遥控、控制面板、盘况页、`PatrolClient`、名册与 PIN 存储（`flutter_secure_storage` 只有 PIN 用）。共用的：`widget/joystick.dart`、`widget/live_video.dart` 里的 MJPEG 解析与画面（站点画面在用，但抛的是 `PatrolClient` 的错误类型）、`wire.dart` 里的 `VideoHealth`。
- **测试**：`tests/app/` 44 个文件约 1050 条，绝大多数测老服务；`tests/inspect/` 82 条；引擎里测死模块的几组；部署测试里约 20 条单元校验用老单元当样本。
- **真狗上现在装的是老服务**（2026-09-22 装的）。仓库里删掉不影响它，**重跑 `install.sh` 才换成代理**。

## 1. 决定一：删到什么程度

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | **直接删**（git 历史里留着）：老服务、静态页、上面列的只有它用的模块与函数、`d1max_console`、`d1max-app` 入口与静态文件的打包配置。留下的模块里只剩老服务引用的名字（比如 `identity` 借 `auth` 的两个名字）挪到留下的那一边 |
| B | 挪进 `legacy/` 不打包。没人测、没人跑的代码留在仓库里，会悄悄坏掉，还会被人当成能用的 |

## 2. 决定二：狗上跑什么

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | **狗上只有 `d1max-agent.service`**：<br>• `install.sh` 装它、`enable` 它（没有站点签发的 `registration.json` 就不起，`ConditionPathExists` 早就有）；<br>• 老单元若在：停、disable、删单元文件（装机脚本的 `@删除` 对账）；<br>• 适配器从 env 取（启动脚本已经这么做了）：`D1MAX_HAL`，缺省 `sim`，其余参数 `D1MAX_AGENT_ARGS`。**W00d 真机验收（§3b）过了之前不改成 `d1max`**：`sim` 的代理动不了真狗；装机脚本在 `/etc/d1max/env` 里写一行注释好的 `D1MAX_HAL=d1max` 与说明，验收过了由人打开；<br>• 6/7 的验证改成看代理单元状态与日志，不再 `curl :8095`；7/7 切版本后 `systemctl restart d1max-agent` |
| B | 老单元留着不 enable。两套并存，旁路进程同一时刻只该有一个 Python 在开车，谁在开说不清 |

## 3. 决定三：特权助手与命令行

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | **特权助手整个删**：`d1max-privileged`、`d1max-restart-now`、sudoers 那一行、`engine/privileged.py` 都删；装机脚本删掉 `@写盘` 里这几样并清掉已装的（`@删除` 对账）。代理运行时不需要 root：重启靠自己退出、`Restart=always`；单元由装机脚本（root）装一次。<br>命令行 `release activate/rollback` 不再装单元（启动参数在包里的启动脚本里）。槽内数据搬迁留在装机脚本里（以服务用户跑）。<br>升级后的提交已经在代理里（连上站点才提交），老服务的 postcheck 随它删 |
| B | 助手留着备用。狗上多一条没人用的 root 路径 |

## 4. 决定四：手机

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | **直连模式整个删**：打开就是站点列表（没有站点就进「添加站点」）；删首屏二选一、名册、直连遥控、控制面板、盘况页、`PatrolClient`、名册与 PIN 存储、`flutter_secure_storage` 依赖。<br>共用的留下：摇杆、MJPEG 解析与画面（错误类型换成自己的 `VideoError`）、`VideoHealth`。<br>手机上旧版本存的名册、PIN 留在手机里没人读（系统卸载时一起没），不做迁移 |
| B | 直连模式藏起来留着。决策 7 之后直连遥控没有租约与审计，不能留一个能打开的入口 |

## 5. 决定五：测试怎么处理

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 删掉的代码连测试一起删；留下的代码的测试留下（`bridge`、`mapping`、`procs`、`identity` 的 SN 解析、「引擎和判读不 import HTTP」的守卫改成只守引擎）。<br>**删之前逐条对**：老服务那几组测的**行为**在新的一边有没有人测 —— 遥控死人开关与急停链（→ 代理 `test_teleop_task`、站点 `test_site_teleop*`）、挂起（→ `tests/engine/test_machine_suspend.py`）、判读（→ 站点 `test_site_runs`）、上传（→ 发件箱与接收口）。对不上的补到新的一边，对照表写进完工报告。<br>部署测试：单元校验的样本换成代理单元；老单元、PIN、8095 的那些删掉；新加「狗上只有代理单元」「老单元会被清掉」「单元里的适配器从 env 取」 |
| B | 老测试整批删。可能把只在老测试里测过的行为一起丢掉 |

## 6. 决定六：PIN 与老环境变量

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 新装不再生成 PIN、不再写 `D1MAX_CONSOLE_URL/TOKEN`（只有老服务的上传泵用）；**已有的 `/etc/d1max/env` 里这几行不动**（不删人的文件内容），装机脚本提示一句「这几行已经没人读，可以删」。代理删掉 `--legacy-http`、`--pin`、`--sn` |
| B | 装机脚本自己删掉那几行。改人手写的配置文件，出错了难查 |

## 7. 决定七：文档

| 选项 | 内容 |
|---|---|
| **A（推荐，取）** | 现行文档改：`手机app.md`（改成站点模式的说明）、`装机清单.md`（七步里 PIN、8095、老单元的部分）、`鉴权与控制权.md`（PIN、租约、热点那一套标「已退役」，指向站点的账号角色与决策 7）、`值守与告警.md` §6、`内部安全说明.md`、`任务包格式.md`、`真机联调手册.md`、`建图定位与巡检管线.md`、真机清单里用直连与 8095 的步骤、`START-HERE.md`、`mobile/README.md`、工单表的路由数。**历史文档（完工报告、`superpowers/plans`）不动**。钉着文档结构的测试跟着改 |
| B | 只删代码不动文档。下一个上机的人照着文档去连 8095 |

## 8. 分两部分做

1. **狗这头**（决定一、二、三、五、六）：删老服务与只有它用的代码、代理去掉 `--legacy-http`、部署（单元、装机、卸载、助手、命令行）、测试对照与改写、文档。
2. **手机**（决定四）：删直连模式、改首屏、共用件去掉对直连客户端的依赖、测试。

每部分：TDD（新加的部署行为先写测试）、突变、只读内部评审、修复；最后一份完工报告，连同 W00c5 系列一起交外审。

## 9. 验收

- 全仓不再有 `server.py`、`d1max-app`、`--legacy-http`、`:8095`、`D1MAX_PIN` 的活引用（历史文档除外）；打出来的包里没有静态页。
- 代理、站点、契约、仿真、部署几组测试全过；手机测试全过、`flutter analyze` 无问题。
- 装机脚本在测试里（`--root` 假根）：装代理单元并 enable；老单元在就停、disable、删；不生成 PIN；已有 env 里的老行不动。
- 狗上没有特权助手、没有 sudoers 那一行；装机脚本清掉老的。
- 命令行 `release activate/rollback` 不碰单元。
- 手机打开就是站点列表，没有直连入口。
- **真机项**：狗上重跑 `install.sh` 之后，老服务没了、代理在跑（`systemctl status d1max-agent`），站点上看得到这台狗（仿真适配器）；W00d 验收过了再把 `D1MAX_HAL` 改成 `d1max`。
