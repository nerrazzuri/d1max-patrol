# W00c · site-node 最小版 —— 设计稿

日期：2026-09-24 · 工单：`docs/庄园安防-差距核查与工单.md` W00c（L，依赖 W00b）· 上游：总设计 §1、§3.5、§3.6、§5、§7；W00、W00b 设计与报告
状态：**已确认**（2026-09-24 用户回复「按推荐」：五个决定全选 A；同一句话确认决策 5 按建议改为「可下发任务级指令，不可遥控」）。

## 0. 工单原文与范围

> broker、注册、派遣器（吸收 W06/W14/W16）、账号、站点 API；手机改连站点

事实（决定的依据）：

- **这张单比 W00b 大好几倍。** 它同时包含：MQTT broker 与设备认证（§3.6）、派遣器（W06 排程执行器已在老 `server.py` 里做完，W14 抢占与待命点、W16 外部事件派遣都没做）、账号与角色（W20 的前置）、站点 API、手机改连。老 `server.py` 有 4949 行、68 个 `/api/*` 路由；手机端 `patrol_client.dart` 按这些路由写，四个主屏合计约 150 KB。
- 总设计 §5 的警告：「**不要拆包、改协议、改鉴权、换客户端同时进行**」。W00c 原文恰好把后三件放在了一张单里。
- 已有可复用的：契约包的 `DispatchClient`（站点侧派单、去重、回执）、`Registration`（注册文件）、`TopicAcl`（按 `robot_id` 的主题白名单）、`PahoTransport`（目前 `mqtts://` 只调了 `tls_set()`，**没有客户端证书**，mTLS 要补）；老 `app/auth.py`（1237 行，PIN、会话、操作人）、`app/control.py`（控制权租约与审计）、`d1max_console/store.py`（证据库起点，198 行）。
- 开发机上**没有**装 Mosquitto；`openssl` 有。站点主机的硬件还没定（总设计 §7 风险 3：狗上网方案须在真机接站点前定）。
- 狗上 `d1max-agent.service` 已装未启用；`/etc/d1max/registration.json` 不存在时单元不起（W00b）。

## 1. 决定一：W00c 拆成四张子单，一张一张过外审

| 选项 | 内容 |
|---|---|
| **A（推荐）** | 拆四张，顺序做，每张各走设计 → 实现 → 外审：<br>**W00c1 站点骨架**：`packages/site-node`、broker + mTLS + ACL、设备注册与证书签发、最小派遣（`goto`/`abort` 经 `DispatchClient`，状态与事件落库）、最小站点 API（登录、机器人列表与状态、派 `goto`/`abort`、事件流）。验收：站点 API → broker（mTLS）→ agent（sim）端到端，冒充别的狗被 ACL 拒。<br>**W00c2 派遣器**：排程执行器从老 `server.py` 迁到站点（W06）、抢占与待命点（W14）、外部事件派遣入口（W16，需决策 5）；`schedule/alerts/baselines` 从 robot-agent 搬到 site-node，删别名壳。<br>**W00c3 账号与角色**：业主/保安/管理员，已认证用户绑定到命令、租约、审计（总设计 §5 `app/{auth,control,identity}` 那一行）；遥控通路手机 → 站点 → 代理（§3.5）。<br>**W00c4 手机改连站点**：`patrol_client.dart` 换 base URL 与鉴权，四屏逐屏迁；老 `server.py` 的退役路径与路由测试改写。 |
| B | 一张大单做完。外审一次看的改动量在万行级，§5 警告的「几件事同时进行」全部发生 |

推荐 A 的理由：每张子单都能单独演示、单独回滚；c1 先证明「站点 ↔ 狗」这一层成立（总设计 §7 第 4 步），后面三张都压在它上面。拆分同时改写工单表：W00c 一行拆四行，W06/W14/W16 的去向写进 c2。

**以下第 2–5 节只针对 W00c1。** c2–c4 各自出设计稿时再拍它们的决定。

## 2. 决定二：broker 用什么

| 选项 | 内容 |
|---|---|
| **A（推荐）** | **Mosquitto 2.x**（Ubuntu 官方源 `apt install mosquitto`）。mTLS：`require_certificate true` + `use_identity_as_username true`，证书 CN 就是 `robot_id`；ACL 用 `pattern`，按 `%u` 只许一台狗写自己的 `status/event/…`、只许读自己的 `cmd`，与契约的 `TopicAcl` 一一对应（由同一张表生成，有对账测试）。吊销走 `crlfile`。 |
| B | 纯 Python 内嵌 broker（`amqtt`）。不用 apt，但 mTLS 与 ACL 要自己在插件里写，维护者少 |
| C | EMQX。功能最全，但是 Erlang 大件，对单站点过重 |

A 的代价：开发机要装一次 `sudo apt install mosquitto`（需要你在本机执行）。没装时真 broker 的端到端测试自动跳过，派遣逻辑仍走 W00 的 `MemoryBroker` 测；装了之后测试起一个临时端口的 Mosquitto 进程，跑 mTLS 与 ACL。

## 3. 决定三：设备证书怎么签

| 选项 | 内容 |
|---|---|
| **A（推荐）** | 站点自建 CA，**调用系统 `openssl` 命令行**签发。`d1max-site enroll <robot_id>` 产出一个证书包：客户端证书与私钥、CA 证书、`registration.json`（指纹写进去）。用 U 盘或 scp 拷到狗的 `/etc/d1max/`。不引入新 Python 依赖 |
| B | 用 `cryptography` 库在进程内签发。代码更干净，但多一个带本地扩展的依赖，狗与站点都要离线轮子 |
| C | c1 先不上 mTLS，用户名密码 + TLS。§3.6 要求 mTLS，只是推迟 |

注册流程（A 与 B 相同）：管理员在站点执行 enroll → 拿到证书包 → 装到狗上 → 启用 agent 单元。自动配对（扫码、一次性注册码）留给后面；c1 只做手工这一条，但证书包格式定下来。

## 4. 决定四：站点 API 用什么写

| 选项 | 内容 |
|---|---|
| **A（推荐）** | 与老 `server.py` 同一套：标准库 `ThreadingHTTPServer` + 已有的 `websockets`（事件流），加 `LoopBridge`。不加依赖；老代码里的会话、限流、审计写法能直接搬 |
| B | FastAPI + uvicorn。路由声明式、自带 OpenAPI 文档，但新增约十个依赖；与老代码的会话/审计写法不通用，c3、c4 要重写那部分 |

站点状态（机器人注册表、派出的命令与任务、事件、账号）存 **SQLite**（标准库，单文件，带事务）。这一条不列为决定：站点是长期运行的多表服务，JSONL 在这里没有优势。

## 5. 决定五：第一个站点跑在哪

| 选项 | 内容 |
|---|---|
| **A（推荐）** | **独立的 Linux 小主机**（Ubuntu 22.04，x86 或 ARM 都行），`d1max-site.service` + `mosquitto.service`。开发与测试在这台笔记本上跑。狗怎么连到这台主机（4G/CPE 还是站点侧无线客户端接狗热点）是总设计 §7 风险 3，**c1 不需要定**，但狗上启用 agent 单元（真机联通）要等它定 |
| B | 过渡期先住在狗的 Orin 上（站点与代理同机）。能更早在真机上演示，但手机仍然连狗的热点，与 §1 硬规则 1「手机永远不直连狗」冲突，之后还要搬一次 |

## 6. W00c1 的形状（按推荐项）

```
packages/site-node/src/d1max_site/
  ca.py          自建 CA:init、签站点服务证书、签狗证书(CN=robot_id)、吊销与 CRL(openssl 子进程)
  registry.py    机器人注册表(SQLite):robot_id、证书指纹、签发/到期、是否吊销
  broker_conf.py 由注册表与 TopicAcl 生成 mosquitto.conf 片段与 acl 文件
  dispatcher.py  包 DispatchClient:派 goto/abort、control_epoch、命令与任务落库、订 status/event/reconcile
  accounts.py    最小账号:一个管理员账号(scrypt 口令)、会话令牌;角色体系归 c3
  api.py         站点 API:/api/login、/api/robots、/api/robots/<id>、/api/robots/<id>/goto、
                 /api/robots/<id>/abort、/api/events(WebSocket)
  main.py        入口 d1max-site;子命令 serve / init-ca / enroll / revoke
deploy/site/     d1max-site.service、mosquitto 配置片段、安装脚本(站点主机用,不进狗的 install.sh)
```

契约与代理侧的配套改动：`PahoTransport` 支持客户端证书（`mqtts://` 带 `--tls-ca/--tls-cert/--tls-key`）；`d1max-agent` 加这三个参数；`d1max-agent.service` 的 `ExecStart` 跟上，**仍不 enable**。

**验收（端到端，测试里跑）**：
1. `init-ca` → `enroll robot-A` → 起 Mosquitto（mTLS + ACL）→ 起 `d1max-agent`（sim，带 A 的证书）→ 管理员登录站点 API → `POST /api/robots/robot-A/goto` → 事件流里看到 accepted、progress、done → 再派一趟、中途 `abort`，看到 aborted 与停止确认。
2. 用 A 的证书去发 B 的主题、订 B 的 `cmd` → broker 拒绝。
3. 没有证书、证书不是本站 CA 签的 → 连不上。
4. `revoke robot-A` 之后 A 重连被拒。
5. 未登录调站点 API 派单 → 401；每条派出的命令都记下是哪个账号派的。

**不在 c1 内**：排程、告警、抢占规则、外部事件（c2）；角色与遥控（c3）；手机（c4）；狗上启用 agent 与真机联通（等狗上网方案，另见 W00d）。

## 7. 风险

- 真 broker 测试依赖本机装 Mosquitto；审核沙箱未必能起子进程监听端口（W00b 外审环境连 `socketpair` 都不许）。真 broker 那几条会标成「无 Mosquitto 或无端口时跳过」，逻辑部分都有不依赖它的测试。
- 决策 5 已随本设计确认（2026-09-24）：站点可下发任务级指令，不可遥控。
- 实现计划：`docs/superpowers/plans/2026-09-24-W00c1-site-skeleton.md`。事件流按实现计划改用 SSE（与老 `/api/events` 同形），不用 WebSocket。
