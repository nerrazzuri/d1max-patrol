# W44 第三期：「布置」一页（防区、禁行区、巡逻路线、待命点、摄像头）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本项目用户禁止子代理（2026-10-15）：只能由主会话按 superpowers:executing-plans 一项一项做。用户 2026-10-27 说「直接把所有 4 期都做了，我一次过提交外审」：计划不等审，写完就做。

**Goal:** 「管理 → 布置」一页：一张大地图，上面五个标签（防区、禁行区、巡逻路线、待命点、摄像头），选哪个只能改那一样、别的变淡；右上角「保存」和「历史版本」；保存前检查、每次保存留一版能一键退回、放宽禁行区要再输一次密码、两个人同时改说人话。保安的地图页补上摄像头图层（第一期留下的）。

**Architecture:** 后端加「布置的历史版本」（一张表、按种类记快照、退回走原来的保存路径，规矩不变）、摄像头在图上的位置、路线逐段查走不走得通、网页上放宽禁行区要再输密码。前端新写 `layoutpage.tsx`（外框 + 五个标签），防区、禁行区、路线三个标签从现有 `zonesadmin.tsx`、`routepage.tsx` 拆出来复用（画多边形、拖点、转朝向、确认流程这些已经过外审的逻辑不重写），待命点、摄像头两个标签新写。旧的「地图与防区」「巡检路线」两页删掉。

**Tech Stack:** Python 3（站点）、Preact + Vitest。

**Spec:** `docs/superpowers/specs/2026-10-27-site-console-redesign-design.md` §5.2、§9、§2.4（热像）。

## Global Constraints

- 第二期的 Global Constraints 全部照旧（用词、浅色、权限在后端、站点时区、构建产物、快照、提交结尾）。
- 禁行区的确认规则一条不改（决策 66、W41）：收紧当场生效；放宽要人确认，机器人确认之前照旧守旧的；页面上写「等机器人确认中」。（设计 §5.2、§9）
- 修订号防覆盖照旧（路线、禁行区的 `base_revision`）；冲突时页面说「刚才有人改过，先看一下」，给「看最新的」按钮，不悄悄盖掉。（设计 §5.2 第 5 条）
- 正在巡逻的这一趟不被打断，下一趟用新路线（现有行为：路线存进库，派单时才读）。防区改了当场生效。（设计 §5.2 第 3 条）
- 摄像头口令不回显（现有规矩）；历史版本里也不存口令。
- 热像、量温度：契约的拍照动作现在只认前、后两路（`mission.CAMERAS`），温度读数要等热像实物接 ISAPI。界面上这两样显示出来但灰着，写「热像到货后开放」（设计 §2.4）。
- 「再输一次密码」这一期管：网页上确认放宽禁行区。手机的确认流程这一期不改（手机只改用词），记在没做的里。

## Review Focus

1. **退回一份放宽了的禁行区**：退回走正常保存，放宽的照样要确认、机器人照样守旧的，不能借「退回」绕过确认。测试见 Task 1 `test_退回禁行区_放宽的照样要确认`。
2. **退回路线时别人刚改过**：退回带的是现在的修订号，但退回的内容是旧的 —— 这是有意盖掉，要在页面上确认一次；接口照常记谁退回的。测试见 Task 1 `test_退回路线_记一条新的历史`。
3. **历史里没有口令**：摄像头那一份快照不带口令；退回一台已删掉的摄像头，口令是空的，页面要提示「口令要重新填」。测试见 Task 1 `test_摄像头历史不存口令`。
4. **两点之间走不通**：每个点都在可走的格子上、但两点之间被墙或禁行区隔开 —— 要标出那一段。测试见 Task 3 `test_两点之间走不通_标那一段`。
5. **网页放宽禁行区没输密码**：网页（cookie 会话）确认一份放宽的，没带密码回 403；收紧的、手机（令牌）照旧。测试见 Task 4。

---

### Task 1: 布置的历史版本（后端，库结构 63）

**Files:**
- Modify: `db.py`（`SCHEMA_VERSION = 63`；`layout_history` 表；`cameras` 加 `map_id`、`map_version`、`x`、`y`、`yaw`、`fov_deg` 列）；Create: `tests/fixtures/schema/site-v62.sql`
- Create: `packages/site-node/src/d1max_site/layout_history.py`
- Modify: `api.py`（防区、禁行区、路线、待命点、摄像头保存成功后记一份；`GET /api/layout/history`、`POST /api/layout/history/{id}/restore`）
- Test: `packages/site-node/tests/test_site_layout_history.py`

**Interfaces:**
- Produces:
  - 表 `layout_history(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, map_id TEXT NOT NULL, map_version TEXT NOT NULL, item TEXT NOT NULL DEFAULT '', body TEXT, note TEXT NOT NULL DEFAULT '', by TEXT NOT NULL, at_ms INTEGER NOT NULL)`；`kind` ∈ `zones`（这张图这一版的全部防区多边形）、`nogo`（禁行区那一份 ZoneSet）、`route`（`item` = 路线名，`body` 是路线定义，删了是 `null`）、`standby`（`item` = 机器人，这张图上它的待命点）、`cameras`（全部摄像头，不带口令）。每个（kind, map, item）最多留 50 份，多的删最旧的。
  - `LayoutHistory(db, *, now_ms)`：`record(kind, map_id, version, item, body, *, by, note="")`、`list(kind, map_id, version, item=None, limit=50)`（新的在前，不带 body，带 `id, kind, item, by, at_ms, note, summary`）、`get(id)`。
  - `restore(site, id, *, by, confirm_password=None) -> dict`（在 `api.py` 里，因为要走各自原来的保存路径）：
    - `zones`：把这张图的防区换成那一份（删多的、改有的、加没有的）。
    - `nogo`：`NavZones.put(那一份的区域, base_revision=现在的)`，放宽的照样要确认。
    - `route`：`put_route(…, base_revision=现在的)`；那一份是删除 → `delete_route`。
    - `standby`：换成那一份的点。
    - `cameras`：按名字换回位置、防区、地址；口令用现在那一台的，没有就空着，回包 `warnings: [{"camera", "problem": "password_missing"}]`。
    - 退回本身也记一份历史，`note = "restored #{id}"`。
  - 接口：`GET /api/layout/history?kind=&map_id=&version=&item=`（`manage`）；`POST …/{id}/restore`（`manage`）。

- [ ] **Step 1: 写测试**

```python
def test_每次保存记一份():             # 防区存两次 → list 两条,新的在前,带谁、几点
def test_只留50份():
def test_退回防区():                   # 加 A、再加 B → 退回第一份 → 只剩 A;历史多一条 restored
def test_退回禁行区_放宽的照样要确认(): # 确认过一份有泳池的 → 删泳池(放宽,没确认)→ 退回删之前那一份是收紧(当场算确认);
                                       # 反过来:退回到没有泳池的那一份 → confirmed_current 是 False,在生效的照旧有泳池
def test_退回路线_记一条新的历史():    # 改路线两次 → 退回第一份 → 路线点是第一份的,修订号 +1
def test_退回删掉的路线():             # 删了的路线退回到删之前 → 又有了
def test_摄像头历史不存口令():         # 存一台带口令的 → 历史 body 里没有 password;删掉再退回 → warnings password_missing
def test_退回待命点():
def test_历史接口权限():               # 保安 403
```

- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 写**（各保存接口成功后调 `record`；失败不记）
- [ ] **Step 4: 跑这个文件 + `test_site_schema_upgrade.py` + 区域、路线、待命点、摄像头的老测试，PASS**
- [ ] **Step 5: 提交** `站点:布置的历史版本、一键退回(W44 第三期)`

---

### Task 2: 摄像头在图上的位置（后端）

**Files:**
- Modify: `cctv_admin.py`（`check_camera`、`save` 收可选的 `placement`；`_row_view` 带回）
- Test: `packages/site-node/tests/test_site_cctv_admin.py`（补）

**Interfaces:**
- Produces: 摄像头一条多 `placement: {"map_id", "map_version", "x", "y", "yaw", "fov_deg"} | null`；`fov_deg` 10–180，默认 90；`yaw` 弧度；`placement: null` 清掉。`GET /api/cameras` 回的每条带它（保安也要读，`view`；口令照旧不回）。

- [ ] **Step 1: 测试** `test_摄像头放到图上`、`test_位置不合规矩`（fov 5、x 是字符串、缺 map_version）、`test_清掉位置`、`test_不改位置时原样留着`（只改地址不带 placement → 位置不动）
- [ ] **Step 2–4: 红、写、绿**
- [ ] **Step 5: 提交** `站点:摄像头在图上的位置(W44 第三期)`

---

### Task 3: 路线逐段查走不走得通（后端）

**Files:**
- Modify: `routes.py`（`problems()` 多查相邻两点之间；`RouteStore` 多收一个 `reach`）、`api.py`、`main.py`（把 `InterceptReach` 接给路线库）
- Test: `packages/site-node/tests/test_site_routes*.py`（补）

**Interfaces:**
- Consumes: 第一期的 `InterceptReach.path_m(map_id, version, start, goal) -> float | None`（同一份规划、带禁行区）。
- Produces: `problems()` 每条错多一种 `{"index": i, "segment": [i, i+1], "problem": "no_path"}`（i 到 i+1 走不通，含被禁行区隔开）；首尾不连（路线不回起点）。没接 `reach`（测试台子、没地图目录）不查。点本身有错的那一段不再查（免得一个错报两遍）。

- [ ] **Step 1: 测试** `test_两点之间走不通_标那一段`（中间一道墙）、`test_禁行区隔开也算走不通`、`test_都走得通不报`、`test_没接规划不查`
- [ ] **Step 2–4**
- [ ] **Step 5: 提交** `站点:路线存之前逐段查走不走得通(W44 第三期)`

---

### Task 4: 网页上放宽禁行区要再输一次密码（后端）

**Files:**
- Modify: `api.py`（`_zones_confirm`：网页会话（cookie）确认的这一份比在生效的是放宽 → 要 `confirm_password`）
- Test: `packages/site-node/tests/test_site_zones*.py`（补）

**Interfaces:**
- Produces: 判「放宽」用 `tightens(在生效的, 当前这一份)` 为假且两份不一样。没带或错 → 403（文字「放宽禁行区要再输一次密码」/「密码不对」）。手机（令牌）不要（这一期手机不改），记进完工报告。

- [ ] **Step 1: 测试** `test_网页放宽要密码`、`test_网页收紧不要密码`、`test_手机放宽照旧不要`
- [ ] **Step 2–4**
- [ ] **Step 5: 提交** `站点:网页上确认放宽禁行区要再输一次密码(W44 第三期)`

---

### Task 5: 布置页外框（网页）

**Files:**
- Create: `web/console/src/layoutpage.tsx`、`web/console/src/layouthistory.tsx`
- Modify: `shell.tsx`（`AdminPage` 加 `admin/layout`，「布置」组就这一页；`admin/zones`、`admin/routes` 旧地址跳过来）、`console.tsx`、`i18n.ts`、`styles.css`
- Test: `web/console/src/layoutpage.test.tsx`

**Interfaces:**
- Produces:
  - `type LayoutTab = "zones" | "nogo" | "routes" | "standby" | "cameras"`；地址 `#/admin/layout?tab=routes`。
  - 外框：上面选地图（默认在用的那一版，旁边写「在用」）、五个标签；中间一张大地图（`MapCanvas`），当前标签的东西正常画，别的四样淡着画（`opacity: .35`，不能点）；右上「保存」（当前标签有没存的改动才亮）、「历史版本」（抽屉：谁、几点、改了什么，每条「退回到这一版」→ 确认 → 调 restore；路线、禁行区会说「退回也要走确认」）。
  - 每个标签是一个组件，签名 `(props: { map: MapSel; meta: PreviewMeta; dim: DimLayers; onDirty(b: boolean); registerSave(fn: () => Promise<void>) })`；外框持有地图和淡层数据（防区、禁行区、路线、待命点、摄像头各拉一次，标签改完 `reload(kind)`）。
  - 冲突说人话：保存回 409 → 横条「刚才有人改过，先看一下」+「看最新的」（重新拉、丢掉没存的）。
  - 切标签、切图时有没存的改动 → 先问「还没保存，丢掉吗？」

- [ ] **Step 1: 测试** `五个标签、地址带tab`、`别的层淡着画`、`有改动保存才亮`、`切标签先问没存的`、`409说有人改过`、`历史抽屉能退回`、`旧地址跳过来`
- [ ] **Step 2–4**
- [ ] **Step 5: 提交** `网页:布置页外框(五个标签、淡层、保存、历史版本)(W44 第三期)`

---

### Task 6: 防区标签（网页）

**Files:**
- Create: `web/console/src/layoutzones.tsx`（从 `zonesadmin.tsx` 拆防区多边形那一半：画、命名、删）
- Test: `web/console/src/layoutzones.test.tsx`

**Interfaces:**
- Produces: 在图上点几下圈一块 → 填名字；选中一个防区，右栏：名字、哪些摄像头归它管（勾选，存的是摄像头的 `zone`）、「布防时间」跳 `#/admin/arming`、删除。
- [ ] 测试：`画一个防区存下`、`选中改摄像头归属`、`删防区要确认`
- [ ] 提交 `网页:布置 → 防区(W44 第三期)`

---

### Task 7: 禁行区标签（网页）

**Files:**
- Create: `web/console/src/layoutnogo.tsx`（从 `zonesadmin.tsx` 拆导航区域那一半：禁行区、虚拟墙、限速区、临时、确认、橡皮擦擦过的保留在第四期地图编辑）
- Test: `web/console/src/layoutnogo.test.tsx`

**Interfaces:**
- Produces:
  - 三种：禁行区（圈一块）、虚拟墙（画一条线）、限速区（圈一块，填限速）；「临时的」勾上选几天（1、3、7、30）→ 站点按 `for_s`。
  - 状态一行人话：「已生效」/「等你确认：你删掉或缩小了禁行区，确认之前机器人照旧绕开原来的范围」/「等机器人确认中」（站点在生效的那一份还没被机器人报回来）。
  - 确认那一句换成人话：「水池、台阶边、陡坡、花坛这些机器人不能去的地方，都已经画成禁行区了」，勾上才能确认；放宽的确认弹 `ConfirmPassword`。
- [ ] 测试：`画禁行区_收紧当场生效`、`删禁行区_写等确认_确认要密码`、`临时的带天数`、`虚拟墙两个点就行`
- [ ] 提交 `网页:布置 → 禁行区(W44 第三期)`

---

### Task 8: 巡逻路线标签（网页）

**Files:**
- Create: `web/console/src/layoutroutes.tsx`（复用 `routes.ts` 的点运算和 `routepage.tsx` 的画法、排程表）
- Delete: `routepage.tsx`（排程表挪进来）
- Test: `web/console/src/layoutroutes.test.tsx`；`routes.test.tsx` 里页面那几条挪过来

**Interfaces:**
- Produces:
  - 左栏路线列表 +「新路线」；按顺序在图上点巡检点，自动连线；站点回的错：点标红、走不通的那一段画红色虚线（Task 3 的 `segment`）。
  - 选中一个点：停多久（秒）、拍不拍照（前 / 后）、朝哪边看（拖箭头，不填角度）；「拍热像」「量温度、超过多少度报警」两项灰着写「热像到货后开放」。
  - 下面排程：几点出发、多久巡一趟、哪几天、派哪台（现有排程接口）。
- [ ] 测试：`点图加点自动连线`、`走不通那一段标红`、`热像两项灰着`、`改排程`
- [ ] 提交 `网页:布置 → 巡逻路线(W44 第三期)`

---

### Task 9: 待命点标签（网页）

**Files:**
- Create: `web/console/src/layoutstandby.tsx`
- Test: `web/console/src/layoutstandby.test.tsx`

**Interfaces:**
- Consumes: `GET/POST /api/robots/{id}/standby`（现有：`{name, map_id, map_version, x, y, yaw, default}`，`{name, remove: true}`）、`/api/chargers`。
- Produces: 每台机器人的待命点画成图标（默认的实心）；拖图标改位置、拖把手改朝向；点空白处「在这里加一个待命点」→ 选哪台、名字；选中可设默认、删除。充电桩显示成另一个图标（只看，改充电桩还在机器人页 / 手机上）。
- [ ] 测试：`拖待命点存下`、`加一个待命点`、`设默认`
- [ ] 提交 `网页:布置 → 待命点(W44 第三期)`

---

### Task 10: 摄像头标签（网页）+ 地图页摄像头图层（保安）

**Files:**
- Create: `web/console/src/layoutcams.tsx`；Modify: `camadmin.tsx`（表单挪进来复用）、`mappage.tsx`（`cameras` 图层画出来：扇形 + 名字，点了看实时画面）、`maplayers.ts`
- Delete: 「系统」页底下的摄像头那一块（第二期暂挂的）
- Test: `web/console/src/layoutcams.test.tsx`、`mappage.test.tsx`（补）

**Interfaces:**
- Produces: 摄像头列表；「放到图上」→ 点图放图标，拖扇形边改朝向和张角；右栏名字、防区、连接地址、账号口令（不回显，空着 = 不改）、旁边实时画面（现有 `/api/cameras/{name}/live`）对照朝向。地图页摄像头图层默认开，点扇形弹实时画面。
- [ ] 测试：`放到图上_拖朝向_存下`、`口令空着不改`、`地图页画出摄像头扇形_点了看画面`
- [ ] 提交 `网页:布置 → 摄像头,地图页摄像头图层(W44 第三期)`

---

### Task 11: 删旧页、全量、截图、文档

- Delete: `zonesadmin.tsx`（拆完）、旧的 `admin/zones`、`admin/routes` 路由（跳 `admin/layout`）、用不上的 i18n 词条不删（无害）。
- 构建产物；ruff；Python 四组（约 20 分钟）；vitest；flutter。
- 演示站点截图：布置五个标签、历史抽屉、地图页摄像头图层。
- `docs/W44-第三期-完工报告.md`；真机项 §3ac（放宽禁行区机器人确认前照旧绕、路线改了正在走的不打断、摄像头扇形跟实际朝向对不对得上）；台账。
- PR、九项检查、合并。
