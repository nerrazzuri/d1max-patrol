# W44 第四期：「高级」五页和地图编辑第一版 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本项目用户禁止子代理（2026-10-15）：主会话按 superpowers:executing-plans 一项一项做。用户 2026-10-27：四期连着做，最后一次外审；计划不等审。

**Goal:** 「管理 → 高级」（默认收起来）五页：地图（列表、版本、重新建图的步骤、发布 / 退回 / 对比、打开编辑）、定位（每台绿黄红、帮它找位置、详细收起来）、诊断（检查单、打包日志）、软件升级（网页上传或下载升级包、等都在充电时装、退回）、系统（站点、网络和 VPN 状态、存储、证书、加密、手机配对、备份）。地图编辑第一版：橡皮擦、画障碍、画笔粗细 / 直线 / 方框、撤销重做、清噪点（现有）、摆正（只转显示）、底图对齐、区域命名、特殊地面；存成新的一版，走现有发布流程。

**Architecture:** 后端：地图编辑沿用「清噪点」的路子（改栅格、另存新版本、原点照抄、区域等走「新版本带过来要人核」），新加 `map_edit.py` 按一串操作改 `floor.pgm`；地图的显示设置（转角、底图、区域名、特殊地面）存进新表 `map_display`，跟着版本走；特殊地面里「放慢」的那几样落成限速区（现有导航区域，不改契约）。升级包网页上传走现有 `ReleaseCatalog.add`（照旧验签）。体检、版本、备份、配对都是现有接口，只换界面。前端：高级五页新写，`mapmgmt.tsx`、`locpage.tsx`、`admin.tsx` 的内容挪进去；地图编辑器 `mapedit.tsx` 一页全屏。

**Tech Stack:** Python 3（站点）、Preact + Vitest。

**Spec:** `docs/superpowers/specs/2026-10-27-site-console-redesign-design.md` §5.3、§6、§2.3、§9、§11 第 8、9、12、16 条。

## Global Constraints

- 第二、三期的 Global Constraints 照旧。
- 高级组每页开头一句「什么时候用得上」。（设计 §5）
- 地图编辑**只改「哪里能走」的平面图（`floor.pgm`），不改认位置用的立体地图（`prior.mm`）**；页面上写明。（设计 §6）
- 摆正只转显示、不改坐标；底图（卫星截图 / 平面图）也只是显示。（设计 §6 第 6 条、§11 第 8 条）
- 特殊地面不挡路、不换步态；楼梯、台阶、斜坡默认放慢（限速区，速度可改、可设不放慢），草地、碎石路默认不放慢只标出来。（设计 §6 第 8 条、§11 第 16 条）
- 区域没命名的用防区名。（设计 §11 第 9 条）
- 再输一次密码：重新建图（换成新图）、换图（发布到机器人）、软件升级。（设计 §5.2 第 4 条）
- 升级包照旧核签名和指纹（现有 `relsign`）；不验签只在 `release_unsigned: true` 的开发站点。（设计 §5.3）
- 站点联网的几样（在线下载升级包、在线卫星图、VPN）这一期**只做界面**，后端在 W45；没联网 / 没装 VPN 时写明「还没开」，不报错。（设计 §2.3）
- 备份目录在站点主机上配，网页只显示和检查。（设计 §11 第 12 条）
- 发行钥匙、设备 PIN 不出现在网页上。（设计 §9）

## Review Focus

1. **编辑的那一版在编辑期间被别人改了 / 删了**：保存带打开时那份栅格的摘要，对不上回 409，不悄悄盖。测试见 Task 1 `test_栅格换了不让存`。
2. **橡皮擦擦掉了墙边一圈**：擦掉的地方改成空地，机器人会走 —— 保存前页面列出「擦掉 N 平方米」并要确认；站点照样只另存新版本，不动在用的。测试见 Task 1 `test_只另存新版本_原版不动`。
3. **特殊地面放慢的限速区跟人手画的限速区混在一起**：放慢的那几块带 `source: "terrain"` 标签，删特殊地面时只删它生成的那几块。测试见 Task 2 `test_删特殊地面只删它生成的限速区`。
4. **网页上传一个没签名 / 签错的升级包**：拒，说人话（「升级包的签名对不上，不是公司发的」）。测试见 Task 3 `test_签名不对拒`。
5. **定位页「帮它找位置」拖的朝向**：跟值班页同一套（`usePosePick`），派出去的就是拖的那个位置和朝向。测试见 Task 6。

---

### Task 1: 地图编辑后端（橡皮擦、画障碍，另存新版本）

**Files:** Create `packages/site-node/src/d1max_site/map_edit.py`；Modify `maps_admin.py`（`edit()`）、`maps_admin_api.py`（`POST /api/admin/maps/<图>/<版本>/edit`）；Test `packages/site-node/tests/test_site_map_edit.py`

**Interfaces:**
- `apply_ops(pgm: bytes, meta: dict, ops: list[dict]) -> tuple[bytes, dict]`：`ops` 按顺序；每个 `{"op": "erase" | "obstacle", "shape": "stroke" | "rect" | "line", "points": [[x, y], ...](米), "width_m": 0.05–2.0}`；`erase` 改成空地灰度（254），`obstacle` 改成占用（0）；回新栅格和统计 `{"erased_m2", "obstacle_m2"}`。最多 500 个操作、每个最多 2000 个点。
- `MapAdmin.edit(map_id, version, *, digest, ops, note, by) -> {"version", "erased_m2", "obstacle_m2", "homes_copied"}`：跟 `denoise` 一样另存新版本（`source="edit"`），原点照抄，区域等走 R2「带过来要人核」。
- 接口要 `manage`，进审计（操作数、面积）。

- [ ] 测试：`test_橡皮擦改成空地`、`test_画障碍改成占用`、`test_直线和方框`、`test_栅格换了不让存`（409）、`test_只另存新版本_原版不动`、`test_操作太多拒`、`test_权限`
- [ ] 红、写、绿；提交 `站点:地图编辑(橡皮擦、画障碍),另存新版本(W44 第四期)`

---

### Task 2: 地图的显示设置、区域命名、特殊地面（后端，库结构 64）

**Files:** `db.py`（`map_display(map_id, map_version, body TEXT, updated_by, updated_ms, PRIMARY KEY(map_id, map_version))`；v63 快照）；Create `map_display.py`；`maps_admin_api.py`（`GET/POST /api/admin/maps/<图>/<版本>/display`、`POST …/basemap`（上传底图 PNG/JPEG，≤ 8 MB）、`GET …/basemap`）；`map_preview` 回包带 `display`（转角、底图、区域名）给所有人看；Test `test_site_map_display.py`

**Interfaces:**
- `display` 一份：`{"rotate_deg": -180–180, "basemap": {"file", "x", "y", "scale", "rotate_deg", "opacity"} | null, "regions": [{"name", "polygon"}], "terrain": [{"id", "kind": "stairs"|"step"|"slope"|"grass"|"gravel", "polygon", "slow_mps": number|null}]}`。
- 存 `terrain` 时：楼梯、台阶、斜坡默认 `slow_mps = 0.3`（可改、`null` = 不放慢）；有 `slow_mps` 的在导航区域里生成限速区 `{"kind": "slow", "label": "楼梯", "source": "terrain", "id": "t-<id>"}`（走现有 `NavZones.put`，收紧当场生效；删特殊地面 = 删它生成的那块，是放宽，照样要确认）。
- `region_at(map_id, version, x, y) -> str | None`：先看命名区域，再看防区（设计 §11 第 9 条）；报警标题里的「在哪」用它（告警 context 带 `where`）。

- [ ] 测试：`test_存显示设置读回来`、`test_转角范围`、`test_底图上传和大小限制`、`test_区域命名_报警写在车库`、`test_没命名用防区名`、`test_楼梯默认放慢_生成限速区`、`test_草地不放慢`、`test_删特殊地面只删它生成的限速区`、`test_权限`
- [ ] 提交 `站点:地图显示设置、底图、区域命名、特殊地面(W44 第四期)`

---

### Task 3: 网页上传升级包、等都在充电时装（后端）

**Files:** `api.py`（`POST /api/releases/upload`（`manage` + 再输密码；收 `.tar.gz` ≤ 500 MB，存临时目录、解包、`ReleaseCatalog.add`）、`POST /api/releases/<名>/install {robots, when: "now" | "charging"}`）；Create `release_queue.py`（`when=charging` 的排队：机器人在充电（`charging` 状态）且没任务时才发现有的装命令）；Test `test_site_release_upload.py`

**Interfaces:**
- 上传失败的说法：签名不对 →「升级包的签名对不上，不是公司发的」；指纹不对 →「升级包坏了（文件跟清单对不上）」；名字重了 →「这个版本已经有了」。
- 在线下载（站点联网时）这一期不做（W45），接口回 501「站点还没联网下载」。
- 退回：现有 `release` 命令带上一版。

- [ ] 测试：`test_上传登记`、`test_签名不对拒`、`test_要再输密码`、`test_充电时才装`（不在充电不发；充电了发一次；装完出队）、`test_在线下载没开`
- [ ] 提交 `站点:网页上传升级包、等都在充电时装(W44 第四期)`

---

### Task 4: 高级 → 地图（网页）

**Files:** Create `advmaps.tsx`（从 `mapmgmt.tsx` 挪：列表、版本、在用的哪张、哪几台在用、发布、退回、对比、删、清噪点）；Delete `mapmgmt.tsx`；Test `advmaps.test.tsx`（`mapmgmt.test.tsx` 挪过来）

- 开头：「什么时候用：家具挪动很多，或者机器人老是迷路。」
- 每一版一行：在用 / 哪几台在用 / 备注；按钮：编辑（打开 Task 7 的编辑器）、发布到机器人（再输密码）、对比、删。
- 「重新建图」向导（步骤卡片，不自动动狗）：1 把机器人放到起点 → 2 用手机遥控走一圈（这一步在手机上，网页写怎么做）→ 3 站点生成新地图（等新版本出现）→ 4 新旧叠在一起看，防区、禁行区、路线、待命点按 R2「带过来要人核」→ 5 换成新图（发布，再输密码）。

- [ ] 测试：`列出版本和在用`、`发布要再输密码`、`重新建图五步`、原 mapmgmt 的测试挪过来照过
- [ ] 提交 `网页:高级 → 地图(W44 第四期)`

### Task 5: 高级 → 定位（网页）

**Files:** Create `advloc.tsx`；`locpage.tsx` 的历史表挪进「详细」（收起来）；Test `advloc.test.tsx`

- 开头：「什么时候用：机器人说找不到自己在哪。」每台一行：绿「知道自己在哪」/ 黄「不太确定」/ 红「迷路了」（按 `robot.loc` 的 `source`、`anchored`、`sigma_m`，跟值班页同一个判法）；「帮它找位置」→ 地图上拖到实际站的地方、拖箭头指朝向（`usePosePick`，`Relocalize.human`）。数字、历史收在「详细」里。

- [ ] 测试：`三种颜色`、`帮它找位置发出去的就是拖的`、`详细默认收着`
- [ ] 提交 `网页:高级 → 定位(W44 第四期)`

### Task 6: 高级 → 诊断、软件升级、系统（网页）

**Files:** Create `advdiag.tsx`、`advupgrade.tsx`、`advsystem.tsx`（`admin.tsx` 的 `SystemPage`、`PairPhone` 挪进系统）；Delete `admin.tsx` 里挪走的；Test 三个 `*.test.tsx`

- **诊断**：「什么时候用：有东西不正常，或者工程师让你发日志。」站点一张检查单（`/api/health` 各项，大白话，每项通过 / 不通过 + `detail`）；每台机器人一张（设备健康 `devices`：前摄像头有画面、激光雷达在转、网络稳定……认不得的原样）；「打包日志」下载 `/api/support-bundle`。
- **软件升级**：现在的版本（站点、每台机器人）；「上传升级包」（再输密码）；「在线下载」灰着写「站点联网以后开放（W45）」；每台机器人「现在装」/「等都在充电时装」；「退回上一版」。
- **系统**：站点名、时间、存储、证书到期、证据加密、手机配对（现有）；「外网和 VPN」一块写「还没装 VPN（W45）」；备份：最近一次备份、最近一次校验、备份盘还剩多少，超过 7 天写红（`/api/health` 的备份项）。

- [ ] 测试：`检查单说人话`、`打包日志是下载`、`上传要再输密码`、`在线下载灰着`、`备份超过7天写红`、`VPN还没装`
- [ ] 提交 `网页:高级 → 诊断、软件升级、系统(W44 第四期)`

### Task 7: 地图编辑器（网页）

**Files:** Create `mapedit.tsx`（全屏一页 `#/admin/mapedit?map=…&version=…`）、`mapeditops.ts`（不碰 DOM 的操作栈：加、撤销、重做、统计）；Test `mapedit.test.tsx`、`mapeditops.test.ts`

- 左边工具：清噪点（现有候选，点一处留着）、橡皮擦、画障碍（画笔 / 直线 / 方框，粗细 0.1–1 m）、区域命名（圈一块填名字）、特殊地面（圈一块选种类，楼梯台阶斜坡默认放慢 0.3 m/s 可改）、摆正（拖一个转盘，只转显示）、底图（上传、拖、转、缩放、透明度）。虚拟墙、禁行区、限速区那几样去「布置 → 禁行区」（链接过去）。
- 撤销、重做；右上「另存成新的一版」：先列出这一次擦了多少、画了多少，确认后 `POST edit`（栅格的操作）+ `POST display`（显示设置那几样）。
- 页面上写：「橡皮擦和画障碍改的是机器人哪里能走的平面图，不改它认位置用的立体地图。擦掉一辆停着的车，机器人会从那里走，认位置照旧靠周围的墙。」

- [ ] 测试：`操作栈撤销重做`、`橡皮擦一笔发出去的点和粗细`、`另存前列出面积要确认`、`特殊地面楼梯默认放慢`、`摆正只改显示`、`栅格换了说有人改过`
- [ ] 提交 `网页:地图编辑器第一版(W44 第四期)`

### Task 8: 删旧页、全量、截图、文档、四期一起的外审包

- 删 `mapmgmt.tsx`、`locpage.tsx`（挪完）、`zonesadmin.tsx`（第三期拆完）、`camadmin.tsx` 里不用的。
- 构建产物；ruff；Python 四组（约 20 分钟）；vitest；flutter。
- 截图：高级五页、地图编辑器。
- `docs/W44-第四期-完工报告.md`；真机项 §3ad（地图编辑后的新版本发布到机器人、擦掉的地方机器人真的走、楼梯放慢、上传升级包装到机器人）；台账 W44 改成「四期完成，待外审」；外审包说明 `docs/W44-外审说明.md`（四期的范围、各期完工报告、Rulings 汇总、没做的、真机项）。
- PR、九项检查、合并。
