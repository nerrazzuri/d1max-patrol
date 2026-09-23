# W01b · OTA 升级携带并安装单元文件 —— 设计

日期：2026-09-23 · 工单：`docs/庄园安防-差距核查与工单.md` W01b（M，依赖 W01）

## 问题

1. `release pack` 只打 `pyproject.toml`/`src`/`config`，`deploy/` 不进包。OTA（`POST /api/release/install` + `activate`）升上来的机器，`/etc/systemd/system/d1max-patrol.service` 永远是装机那天的版本：没有 `Environment=D1MAX_DATA_ROOT`（W01）、`StartLimitIntervalSec` 在错的段（W01c）……巡检数据继续落在版本槽里，W01 只在启动时打一句警告。
2. 服务以 `User=robot` 跑，写 `/etc/systemd/system/` 和 `systemctl daemon-reload` 都要 root。
3. 同一个进程还要发 `systemctl restart`/`reboot`（升级、回滚、自检回滚）。`真机待验证清单.md` 第 6 条一直悬着：默认 polkit 下 `robot` 很可能发不出去，而 `_spawn_restart` 只 `Popen` 不看结果——**失败是静默的**，界面显示「已重启」而机器一动没动。
4. 即便单元更新了，OTA 路径上没有人做 W01 的槽内数据迁移（装机脚本 7/7 做的 stop → migrate → start 只在 `install.sh` 里）。

## 方案比较

| | 做法 | 能解决 | 不能解决 / 代价 |
|---|---|---|---|
| **A（采用）** | root 拥有的小助手脚本 `/usr/local/sbin/d1max-privileged` + `/etc/sudoers.d/d1max` 白名单（`robot` 免密只能跑这一个脚本）。子命令：`check`、`install-unit <版本名>`、`restart`、`reboot`。由 `install.sh` 安装（基础件，同 `/opt/d1max/bin/python`），OTA **不**更新助手自身 | 1、2、3 全部；一处 sudoers、一个脚本、可审计 | 助手改了要重跑 `install.sh`（所以它必须小而稳）；助手是提权面，必须校验输入 |
| B | polkit 规则允许 `robot` 管理 `d1max-patrol.service` | 只解决 3 | 写单元、`daemon-reload` 仍要 root，1 没解 |
| C | 单元里 `ExecStartPre=+…` 以 root 同步单元文件 | 1 | 单元在自己启动过程中被替换、`daemon-reload` 套在 ExecStartPre 里，systemd 行为不可靠；3 没解 |
| D | 服务改 root 跑 | 全部 | 不可接受 |

## 设计（方案 A）

### 1. 包里带 `deploy/`

`_PACK_INCLUDE` 加 `deploy`。`verify_package` 不变（树哈希覆盖它）。装机脚本从 `$(dirname "$0")` 取单元，改为优先取包内 `deploy/`（同一份）。

### 2. 特权助手 `deploy/d1max-privileged`（bash）

- 安装位置 `/usr/local/sbin/d1max-privileged`，`root:root 0755`。**不能放 `/opt/d1max/bin`**——那棵树归 `robot`，sudo 白名单指向 robot 可写的脚本等于把 root 送给 robot。
- sudoers：`/etc/sudoers.d/d1max`，`0440`，内容一行 `robot ALL=(root) NOPASSWD: /usr/local/sbin/d1max-privileged`。`install.sh` 先 `visudo -cf` 校验再落盘（坏 sudoers 会锁死整机 sudo）。默认 `env_reset` 保证 robot 传不进环境变量。
- 子命令：
  - `check`：`exit 0`。给 app 探「sudo 通不通」。
  - `install-unit <版本名>`：源 = `/opt/d1max/releases/<版本名>/deploy/d1max-patrol.service`；目标 = `/etc/systemd/system/d1max-patrol.service`。版本名过 `^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9a-f]{6,12}$`；源必须是普通文件、≤ 64 KiB；**内容白名单校验**（见下）；与目标 `cmp` 相同则打印 `unchanged` 直接退出；否则 `install -m 0644` 到临时名再 `mv`，`systemctl daemon-reload`，打印 `installed`。
  - `restart`：`systemctl stop d1max-patrol.service`（`|| true`）→ 若已停则以 `robot` 跑 `release migrate-data`（`|| true`，与 `install.sh` 7/7 相同）→ `systemctl start d1max-patrol.service`。
  - `reboot`：`systemctl reboot`。
- **单元内容白名单**（提权面的护栏；robot 已经能以 robot 跑任意代码，护栏要挡的是「借单元变成 root」）：每个非空非注释行要么是段头（只许 `[Unit]`/`[Service]`/`[Install]`），要么是 `键=值`，键在允许表里：`Description After Wants StartLimitIntervalSec Type User ExecStart ExecStartPre Restart RestartSec Environment EnvironmentFile WorkingDirectory WantedBy`。硬约束：`User=robot` 必须出现且只能是这个值；`ExecStart` 与 `ExecStartPre` 的值去掉 `-` 前缀后必须以 `/opt/d1max/` 开头，**不许** `+`/`!`/`:` 前缀（那三种以 root 跑）；`EnvironmentFile` 只许 `-/etc/d1max/env`；`Environment` 只许 `D1MAX_*` 与 `PYTHONUNBUFFERED`；不许 `Group`/`ExecStartPost`/`ExecStop*`/`Capability*`/`AmbientCapabilities` 等（不在表里即拒绝）。任一违规 → 非零退出，一行说明，不落盘。
- 非 root 运行（开发机测试）：必须显式给 `--root <dir> --dest <file> --no-systemctl`，用于测校验逻辑；root 运行时这三个参数一律拒绝。

### 3. app 侧 `engine/privileged.py`

```python
@dataclass(frozen=True)
class Privileged:
    helper: Path = Path("/usr/local/sbin/d1max-privileged")
    runner: Callable[..., CompletedProcess] = subprocess.run   # 可注入
    def available(self) -> bool            # helper 存在且 `sudo -n helper check` 退 0
    def install_unit(self, name) -> str    # "installed" | "unchanged" | "skipped: <原因>"；校验失败抛 PrivilegedError
    def restart_argv(self, plan) -> tuple  # 助手在 → ("sudo","-n",helper,"restart"/"reboot")；不在 → plan.argv 原样
```
- **调用点**：`_release_activate`：precheck 通过 → `install_unit(name)`（抛错 → 409「新版单元文件没装上，没有切换」）→ `activate()` → `ctx.restart(plan)`。先装单元再切链：单元只引用 `current` 与稳定路径，新单元配老代码是安全的，反过来（切了链、单元没装上）才是 W01b 要消灭的状态。
- `_release_rollback` 与开机自检回滚：`rollback()` 之后 `install_unit(退回的版本)`，失败只记 WARNING（回到能跑的代码比单元一致更要紧），继续重启。
- `_spawn_restart(plan)`：先同步跑 `sudo -n helper check`（1 s 超时）；不通 → 抛 `PrivilegedError`，路由回 500「重启命令发不出去（sudo 白名单没装）」，**不再假装已重启**；通 → `Popen(restart_argv)`。开发机没有助手 → 退回现状（直接 `systemctl …`），日志一句。
- CLI `release activate`/`rollback` 同样调 `install_unit`（助手不在就跳过并打印一句）。

### 4. 单元与装机脚本

- `deploy/d1max-patrol.service`：**不加** `migrate-data` 的 `ExecStartPre`（`Restart=always` 每次重试都会跑它；迁移放在助手 `restart` 里，跟 `install.sh` 7/7 同一条路）。
- `install.sh` 5/7：安装助手与 sudoers（`visudo -cf` 校验），单元来源改为包内 `deploy/`。`uninstall.sh`：删助手与 sudoers。`footprint.sh`：脚印清单加这两处。
- boot-guard 回滚（开机前）不装单元：那时没有 app 进程，单元已被 systemd 读入；新单元向后兼容，留到下次 `activate` 再对齐。文档写明。

### 5. 测试

- `tests/engine/test_release_pack.py`：包里有 `deploy/d1max-patrol.service` 与 `deploy/d1max-privileged`。
- `tests/test_deploy_files.py`：助手存在、`bash -n`；sudoers 一行的精确形状；`install.sh` 用 `visudo -cf`、装到 `/usr/local/sbin`、不装进 `/opt/d1max/bin`；`uninstall.sh` 删两处。
- `tests/test_deploy_privileged.py`（新，跑真 bash，非 root 模式）：合法单元 → `installed`；再跑 → `unchanged`；`User=root`、`ExecStartPre=+…`、未知键、`Group=`、`EnvironmentFile=/etc/passwd`、坏版本名、源不存在 → 非零且不落盘；root 模式下 `--dest` 被拒（用 `fakeroot`? 不——只测「非 root 不给参数就拒」）。
- `tests/engine/test_privileged.py`：注入 runner，三种返回；助手不存在时 `available()=False`、`restart_argv` 原样。
- `tests/app/test_release_routes*.py`：activate 先装单元再切链、单元失败 409 且链未动；rollback 装回旧版单元；restart 权限探测失败 → 500 且未 Popen。
- 真机待验证：`sudo -u robot sudo -n /usr/local/sbin/d1max-privileged check`；OTA 一次后 `systemctl cat d1max-patrol` 含 `D1MAX_DATA_ROOT`；`/var/lib/d1max` 出现迁移数据。

## 不做

- 不更新助手自身（提权面，只随 `install.sh`）。
- 不做 polkit。
- 不动 boot-guard。
