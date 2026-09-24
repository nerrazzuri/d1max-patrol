# W00c1 站点骨架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 站点 API → Mosquitto（mTLS + 按 robot_id 的 ACL）→ d1max-agent（sim）端到端派 `goto`/`abort`，冒充与未授权连接被 broker 拒。

**Architecture:** 新包 `packages/site-node`（`d1max_site`），只依赖 `d1max-contract[mqtt]`，不依赖根包（c4 要退役根包的 HTTP 面）。站点自建 CA 走 `openssl` 子进程；状态落一个 SQLite 文件；派遣器在一条共享 MQTT 连接上为每台狗挂一个 `DispatchClient`；HTTP 用标准库 `ThreadingHTTPServer`，异步部分跑在包内自带的事件循环线程里；事件流用 SSE（与老 `/api/events` 同形，手机现成会接）。

**Tech Stack:** Python 3.10 标准库（sqlite3、hashlib.scrypt、http.server、subprocess）、paho-mqtt 2.1、Mosquitto 2.0（测试用 `D1MAX_MOSQUITTO` 指向的二进制或 PATH 里的 `mosquitto`，都没有就跳过真 broker 测试）、openssl。

设计：`docs/superpowers/specs/2026-09-24-W00c-site-node-design.md`（五个决定全 A）。

---

### Task 1: 契约与代理支持 mTLS

**Files:** `packages/contract/src/d1max_contract/paho_transport.py`、`dispatch.py`；`packages/robot-agent/src/d1max_agent/main.py`；`deploy/d1max-agent.service`；测试 `packages/contract/tests/test_paho_tls.py`（新）、`test_dispatch.py`、`packages/robot-agent/tests/test_agent_main.py`、`tests/test_deploy_files.py`。

- [ ] `PahoTransport(url, client_id, *, tls_ca=None, tls_cert=None, tls_key=None)`：`mqtts://` 时 `tls_set(ca_certs, certfile, keyfile)`；只给 cert 不给 key（或反之）抛 `ValueError`。
- [ ] CONNACK 失败（`reason_code.is_failure`）或 `on_connect_fail`：`connect()` 立刻抛 `ConnectionError`（不再把拒绝当成已连上），并 `loop_stop()`。
- [ ] `DispatchClient.attach()`：只登记订阅不连接；`start()` = `connect()` + `attach()`。站点在一条共享连接上挂多台狗。
- [ ] `d1max-agent --tls-ca/--tls-cert/--tls-key`，三个要么都给要么都不给；`mqtts://` 不给就拒绝启动。单元 `ExecStart` 带上 `/etc/d1max/tls/{ca.crt,robot.crt,robot.key}`，仍不 enable。
- [ ] 测试先红：参数校验、失败 CONNACK 抛错（假 paho 客户端回调注入）、`attach` 不调 `connect`。

### Task 2: site-node 包、CA 与注册表

**Files:** `packages/site-node/{pyproject.toml,src/d1max_site/{__init__,ca,registry,db}.py,tests/test_site_ca.py,tests/test_site_registry.py}`；根 `pyproject.toml`（testpaths、known-first-party）；`tests/test_workspace.py`。

- [ ] `ca.SiteCA(root)`：`init(site_id)` 生成 CA 私钥与自签证书、空 CRL；`issue_server(hostnames)`（CN=`site:<site_id>`，SAN 含主机名与 IP）；`issue_robot(robot_id, days)` 返回证书包（ca.crt、robot.crt、robot.key、registration.json；指纹 = 证书 DER 的 sha256）；`revoke(robot_id)` 更新 CRL；私钥文件 0600。
- [ ] `registry.Registry(db)`：`enroll` 写行（robot_id 唯一、不许等于站点 CN、过 `Topics` 的 id 规矩）；`revoke`；`get/list`；`control_epoch`（持久、默认 1）。
- [ ] 测试：openssl 验证链（`openssl verify -CAfile`）、CN 与 SAN、吊销后 `openssl verify -crl_check` 失败、重复 enroll 拒绝、私钥权限。

### Task 3: broker 配置与真 broker 测试

**Files:** `packages/site-node/src/d1max_site/broker_conf.py`；`packages/site-node/tests/{conftest.py,test_site_broker.py}`。

- [ ] `render(site_id, paths, port)` → `mosquitto.conf`（`listener`、`cafile/certfile/keyfile/crlfile`、`require_certificate true`、`use_identity_as_username true`、`allow_anonymous false`、`acl_file`、`persistence`）与 acl 文本：站点用户 `site:<id>` 可写 `…/robot/+/cmd`、可读 `…/robot/+/#`；`pattern write` 逐条对 `PUBLISH_KINDS`、`pattern read …/robot/%u/cmd`。
- [ ] 对账测试：ACL 里的狗侧条目与 `TopicAcl` 由同一张 `PUBLISH_KINDS` 生成。
- [ ] conftest：找 Mosquitto（`D1MAX_MOSQUITTO` 或 PATH），临时目录起 CA、enroll A/B、渲染配置、空闲端口起进程、等端口可连；找不到就 `pytest.skip`。
- [ ] 真 broker 测试：A 发自己的 status 站点收得到；A 发 B 的 status、A 订 B 的 cmd 收不到站点发给 B 的命令；无证书连不上；别的 CA 签的证书连不上；`revoke A` + 重启 broker 后 A 连不上。

### Task 4: 派遣器与落库

**Files:** `packages/site-node/src/d1max_site/{db,dispatcher}.py`；`packages/site-node/tests/test_site_dispatcher.py`。

- [ ] SQLite 表：`robots`、`accounts`、`sessions`、`commands`（command_id、task_id、robot_id、kind、payload、issued_by、issued_at、ack_result、ack_reason）、`events`（robot_id、boot_id、seq 唯一、kind、data、stamp、received_at）、`robot_state`（最新 status/capabilities JSON）。
- [ ] `Dispatcher(transport, db, site_id, now_ms)`：`start()` 连一次，按注册表给每台未吊销的狗 `attach` 一个 `DispatchClient`；`enroll` 之后 `add_robot` 动态挂；status/caps/event/reconcile 落库并推给订阅者（SSE 用）。
- [ ] `goto(robot_id, target, max_speed, *, issued_by)`、`abort(robot_id, task_id, *, issued_by)`：派遣条件（注册且未吊销、online、`last_seen` 新鲜 ≤ 90 s、`ready` 全真，abort 只要求注册）不满足抛 `DispatchRefused(reason)`；命令先落库再发，回执回写。
- [ ] 测试（MemoryBroker + `AgentRuntime` + SimRobot，同 W00 验收的驱动方式）：goto 到 done、abort 到 aborted、离线拒派、未注册拒派、吊销拒派、`issued_by` 落库、事件去重落库。

### Task 5: 账号与站点 API

**Files:** `packages/site-node/src/d1max_site/{accounts,loop,api}.py`；`packages/site-node/tests/test_site_api.py`。

- [ ] `accounts`：`add_admin(name, password)`（scrypt，盐 16 字节）、`login` → 随机令牌（存 sha256）、闲置 30 min / 绝对 12 h 过期、`logout`；登录失败按用户名限流。
- [ ] `loop.LoopThread`：后台事件循环线程，`call(coro_factory, timeout)`。
- [ ] `api.SiteApi(host, port, dispatcher, accounts)`：`POST /api/login`、`POST /api/logout`、`GET /api/robots`、`GET /api/robots/<id>`、`POST /api/robots/<id>/goto`、`POST /api/robots/<id>/abort`、`GET /api/events`（SSE，第一帧全量）；除 login 外都要 `Authorization: Bearer`；派遣拒绝 → 409 带理由；body 上限 64 KB；JSON 错 → 400。
- [ ] 测试：未登录 401、错口令 401 且限流、goto 经 API 到 done 且 SSE 看得到、409 理由、body 超限。

### Task 6: 入口、部署文件与端到端验收

**Files:** `packages/site-node/src/d1max_site/main.py`（`d1max-site serve|init-ca|enroll|revoke|add-admin`）；`deploy/site/{d1max-site.service,mosquitto-d1max.conf.README.md,install-site.sh}`；`packages/site-node/tests/test_site_acceptance.py`；`tests/test_deploy_files.py`。

- [ ] 端到端（真 Mosquitto，子进程 `d1max-agent`）：设计 §6 验收 1–5 全部在一个测试文件里。
- [ ] `install-site.sh`：站点主机用，装包、写单元、`init-ca`、渲染 broker 配置到 `/etc/mosquitto/conf.d/`；**不进**狗的 `install.sh`。部署文件测试守形状。

### Task 7: 收尾

- [ ] 突变检查、内部只读评审、`docs/W00c1-完工报告.md`、工单表、真机待测文档（狗上启用 agent 归「狗上网方案」定之后）、全量测试、推送。
