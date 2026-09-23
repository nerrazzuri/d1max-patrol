# W01b · OTA 携带并安装单元文件 —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OTA 升级（`release install` + `activate`，HTTP 或 CLI）把机器上的 systemd 单元更新到随包发布的那一份，并让以 `robot` 跑的服务真的能发出 restart/reboot。

**Architecture:** 包里带 `deploy/`；`install.sh` 装一个 root 拥有的小助手 `/usr/local/sbin/d1max-privileged` 和一条 sudoers 白名单；app 与 CLI 通过 `engine/privileged.py` 调它：先装单元、再切链、再重启（重启也走助手，先探 `check`，发不出去就报错而不是假装成功）。设计见 `docs/superpowers/specs/2026-09-23-W01b-ota-unit-and-privileges-design.md`。

**Tech Stack:** Python 3.10、bash、systemd、sudoers（`visudo -cf`）、pytest。

**测试命令：** `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest -p no:cacheprovider -q`

---

## 文件清单

| 文件 | 责任 |
|---|---|
| `src/d1max_patrol/engine/release.py` | `_PACK_INCLUDE` 加 `deploy` |
| `deploy/d1max-privileged`（新） | root 助手：`check` / `install-unit <版本名>` / `restart` / `reboot`；单元内容白名单校验 |
| `deploy/sudoers-d1max`（新） | 一行白名单，装到 `/etc/sudoers.d/d1max` |
| `deploy/install.sh` | 5/7 装助手与 sudoers；单元来源优先包内 `deploy/`；声明块两行 `@写盘` |
| `deploy/uninstall.sh` | `rm_sys` 范围加两处；两行 `@删除` |
| `deploy/footprint.sh` | 勘察范围加 `/usr/local/sbin:1`、`/etc/sudoers.d:1` |
| `src/d1max_patrol/engine/privileged.py`（新） | `Privileged`（`available` / `install_unit` / `restart_argv` / `ensure_can_restart`）、`PrivilegedError` |
| `src/d1max_patrol/app/server.py` | `AppContext.privileged`；`_spawn_restart` 走助手并先探权限；activate / rollback / 开机回滚调 `install_unit` |
| `src/d1max_patrol/cli.py` | `release activate` / `rollback` 调 `install_unit` |
| `tests/engine/test_release_pack.py`、`tests/test_deploy_files.py`、`tests/test_deploy_privileged.py`（新）、`tests/engine/test_privileged.py`（新）、`tests/app/test_api_release.py`、`tests/test_cli_release.py` | 各自的测试 |
| `docs/装机清单.md`、`docs/真机待验证清单.md`、`docs/W01b-完工报告.md`、工单表 | 文档 |

---

### Task 1：包里带 `deploy/`

**Files:** Modify `src/d1max_patrol/engine/release.py:94`；Test `tests/engine/test_release_pack.py`

- [ ] 测试：`_源码树()` 里加 `deploy/d1max-patrol.service`、`deploy/install.sh`、`deploy/d1max-privileged`；`test_包里只有白名单那三样加一份自述` 的集合加 `"deploy"`；新增 `test_单元文件和特权助手随包走`：`(dest/"deploy"/"d1max-patrol.service").is_file()` 且 `(dest/"deploy"/"d1max-privileged").is_file()`；`test_源目录里缺白名单里的目录就报错` 若按目录参数化则加 `deploy`。
- [ ] 跑：红（集合差 `deploy`）。
- [ ] 实现：`_PACK_INCLUDE = ("pyproject.toml", "src", "config", "deploy")`，注释写 W01b 理由。
- [ ] 跑 `tests/engine/test_release_pack.py tests/engine/test_release.py tests/test_cli_release.py`：绿。
- [ ] Commit `release: pack ships deploy/ so OTA can install the unit (W01b)`.

### Task 2：特权助手脚本

**Files:** Create `deploy/d1max-privileged`；Test `tests/test_deploy_privileged.py`（新）

- [ ] 测试（跑真 bash，非 root 模式必须给 `--root/--dest/--no-systemctl`）：

```python
HELPER = ROOT / "deploy" / "d1max-privileged"
UNIT = (ROOT / "deploy" / "d1max-patrol.service").read_text(encoding="utf-8")

def _机器(tmp_path, name="2026-09-20-77b2de", unit=UNIT):
    root = tmp_path / "opt"; (root/"releases"/name/"deploy").mkdir(parents=True)
    (root/"releases"/name/"deploy"/"d1max-patrol.service").write_text(unit, encoding="utf-8")
    return root, tmp_path / "etc" / "d1max-patrol.service"

def _跑(*args, root, dest):
    return subprocess.run(["bash", str(HELPER), "--root", str(root), "--dest", str(dest),
                           "--no-systemctl", *args], capture_output=True, text=True)

def test_语法(): bash -n
def test_非root不给测试参数就拒绝(): rc != 0, "root" in stderr
def test_check退0()
def test_合法单元第一次installed第二次unchanged(): dest 内容 == 源；dest 模式 0644
def test_版本名不合规就拒绝并且不落盘(): "../x", "2026-09-20-77b2de; rm -rf /"
def test_源不存在就拒绝()
@pytest.mark.parametrize 违规行 → rc!=0, dest 不存在:
    "User=root", "Group=root", "ExecStartPre=+/bin/sh -c id", "ExecStart=/bin/sh",
    "EnvironmentFile=/etc/passwd", "Environment=LD_PRELOAD=/x.so", "ExecStartPost=/bin/true",
    "AmbientCapabilities=CAP_SYS_ADMIN", "[Socket]"
def test_少了User=robot也拒绝()
def test_sudoers文件只有那一行(): deploy/sudoers-d1max 内容精确 == "robot ALL=(root) NOPASSWD: /usr/local/sbin/d1max-privileged\n"；本机有 visudo 就 `visudo -cf` 校验
```

- [ ] 跑：红（文件不存在）。
- [ ] 实现 `deploy/d1max-privileged`（要点）：`set -euo pipefail`；常量 `UNIT_NAME`、`RELEASE_ROOT=/opt/d1max`、`DEST=/etc/systemd/system/$UNIT_NAME`、`RUN_USER=robot`、`SYSTEMCTL=systemctl`；解析 `--root/--dest/--no-systemctl`（记 `TEST_MODE=1`）；`EUID==0 && TEST_MODE` → 拒；`EUID!=0 && !TEST_MODE` → 拒；`validate_unit <file>`：逐行，空/注释跳过，段头只许三种，`键=值`，键白名单 `Description After Wants StartLimitIntervalSec Type User ExecStart ExecStartPre Restart RestartSec Environment EnvironmentFile WorkingDirectory WantedBy`；`User` 必须 `robot` 且出现过；`ExecStart*` 值去掉一个前导 `-` 后必须 `^/opt/d1max/`，首字符不许 `+ ! : @`；`EnvironmentFile` 只许 `-/etc/d1max/env`；`Environment` 只许 `^D1MAX_[A-Z0-9_]+=` 或 `PYTHONUNBUFFERED=1`；`WorkingDirectory` 去 `-` 后 `^/opt/d1max/`；`install-unit <name>`：`^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9a-f]{6,12}$`，源 `$RELEASE_ROOT/releases/$name/deploy/$UNIT_NAME` 是普通文件且非链接、≤ 65536 字节，校验，`cmp -s` 相同 → `unchanged`，否则 `install -m 0644 src dest.tmp && mv -f dest.tmp dest && $SYSTEMCTL daemon-reload && echo installed`；`restart`：`stop || true` → 未 active 则 `runuser -u $RUN_USER -- env D1MAX_RELEASE_ROOT=… D1MAX_DATA_ROOT=/var/lib/d1max $RELEASE_ROOT/bin/python -m d1max_patrol.cli release migrate-data || warn` → `start`；`reboot`：`$SYSTEMCTL reboot`；测试模式下 restart/reboot 只打印。
- [ ] 实现 `deploy/sudoers-d1max`。
- [ ] 跑：绿。`chmod +x deploy/d1max-privileged`。
- [ ] Commit `deploy: root helper d1max-privileged + sudoers whitelist (W01b)`.

### Task 3：装机 / 卸载 / 足迹脚本

**Files:** Modify `deploy/install.sh`（声明块 + 5/7）、`deploy/uninstall.sh`（声明块、`rm_sys` case、5 步里真删）、`deploy/footprint.sh:36`；Test `tests/test_deploy_files.py`

- [ ] 测试：`test_装机脚本装特权助手到usr_local_sbin`（`install -m 0755 … /usr/local/sbin/d1max-privileged`，且没有任何行把它装进 `/opt/d1max/bin`）；`test_sudoers先visudo校验再落盘`（`visudo -cf` 的 index < `install -m 0440 … /etc/sudoers.d/d1max` 的 index）；`test_单元优先取包内deploy`（`"$PKG/deploy/d1max-patrol.service"` 出现，且退回 `$(dirname "$0")`）；`test_卸载脚本删助手和sudoers`（`rm_sys "/usr/local/sbin/d1max-privileged"`、`rm_sys "/etc/sudoers.d/d1max"`、case 里两条形状）；`test_足迹勘察覆盖两处`。`tests/test_deploy_coexist.py` 的对账会自动要求声明块两边一致。
- [ ] 跑：红。
- [ ] 实现：install.sh 声明块加 `# @写盘 /usr/local/sbin/d1max-privileged` 与 `# @写盘 /etc/sudoers.d/d1max`；5/7：

```bash
UNIT_SRC="$PKG/deploy/d1max-patrol.service"
[[ -f "$UNIT_SRC" ]] || UNIT_SRC="$(dirname "$0")/d1max-patrol.service"   # 老包没带 deploy/
install -m 0644 "$UNIT_SRC" /etc/systemd/system/
HELPER_SRC="$PKG/deploy/d1max-privileged"; [[ -f "$HELPER_SRC" ]] || HELPER_SRC="$(dirname "$0")/d1max-privileged"
install -m 0755 -o root -g root "$HELPER_SRC" /usr/local/sbin/d1max-privileged
SUDOERS_SRC=…/sudoers-d1max
visudo -cf "$SUDOERS_SRC" >/dev/null   # 坏 sudoers 会锁死整机 sudo,先验再落
install -m 0440 -o root -g root "$SUDOERS_SRC" /etc/sudoers.d/d1max
```
  uninstall.sh：声明块 `# @删除` 两行；`rm_sys` case 加 `/usr/local/sbin/d1max-privileged) ;;` 与 `/etc/sudoers.d/d1max) ;;`；在删主单元那一步旁边 `rm_sys "/usr/local/sbin/d1max-privileged" "特权助手"`、`rm_sys "/etc/sudoers.d/d1max" "sudo 白名单"`；footprint.sh `SURVEY_DIRS` 加 `"/usr/local/sbin:1"`、`"/etc/sudoers.d:1"`。
- [ ] 跑 `tests/test_deploy_files.py tests/test_deploy_coexist.py tests/test_deploy_guards.py`：绿。`bash -n` 三个脚本。
- [ ] Commit `deploy: install/uninstall/footprint carry the helper and sudoers (W01b)`.

### Task 4：`engine/privileged.py`

**Files:** Create `src/d1max_patrol/engine/privileged.py`；Test `tests/engine/test_privileged.py`

- [ ] 测试（注入 `runner`，记录 argv，按脚本返回 `CompletedProcess`）：`available` 三态（助手文件不在 → False 且不跑 runner；`check` 退 0 → True；退 1 / `OSError` / `TimeoutExpired` → False）；`install_unit`：助手不在 → 返回以 `skipped:` 开头且不跑 runner；退 0 stdout `installed` → `"installed"`；`unchanged` → `"unchanged"`；退 1 stderr `User 只能是 robot` → `PrivilegedError` 文本含之；argv 精确 `["sudo","-n",helper,"install-unit",name]`；`restart_argv`：助手不在 → `plan.argv` 原样；在 → `("sudo","-n",helper,"restart")` / `kind=="machine"` → `"reboot"`；`ensure_can_restart`：助手不在 → 不抛；在但 `check` 失败 → `PrivilegedError`。
- [ ] 跑：红（ImportError）。
- [ ] 实现：

```python
HELPER = Path("/usr/local/sbin/d1max-privileged")
class PrivilegedError(Exception): ...
@dataclass(frozen=True)
class Privileged:
    helper: Path = HELPER
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    def _argv(self, *args: str) -> list[str]: return ["sudo", "-n", str(self.helper), *args]
    def present(self) -> bool: return self.helper.is_file()
    def available(self) -> bool: ...    # present + check 退 0,5 s 超时
    def install_unit(self, name: str) -> str: ...   # 见设计 §3
    def restart_argv(self, plan: RestartPlan) -> tuple[str, ...]: ...
    def ensure_can_restart(self) -> None: ...
```
- [ ] 跑:绿。ruff。
- [ ] Commit `engine: Privileged — sudo helper client for unit install and restart (W01b)`.

### Task 5：app 接线

**Files:** Modify `src/d1max_patrol/app/server.py`（`_spawn_restart` ~676、`AppContext` ~724、`_release_activate` ~3901、`_release_rollback` ~3941、开机回滚 ~4408）；Test `tests/app/test_api_release.py`、`tests/app/test_postcheck_boot.py`

- [ ] 测试：fixture `rel_server` 给 ctx 注入 `Privileged(helper=tmp_path/"helper", runner=假)`——默认助手文件存在、`check` 退 0、`install-unit` 退 0 `installed`，并记录调用：
  - `test_切版本先装单元再切链再重启`：调用顺序 `["install-unit <name>"]`，链已切，重启记录 1。
  - `test_单元装不上就不切链`：`install-unit` 退 1 → 409 文本含「单元」，`current_name == ""`，`read_pending is None`，重启记录空。
  - `test_重启命令发不出去就不切`：`check` 退 1 → 409「重启命令发不出去」，链未动。
  - `test_没有特权助手的开发机照旧`：`helper` 不存在 → activate 200，重启记录 1（现状不变）。
  - `test_回滚把上一版的单元装回去`：rollback 后 `install-unit` 的最后一次参数是退回的版本名；`install-unit` 失败时 rollback 仍 200、链已退。
  - `test_spawn_restart探到没权限就抛而不是Popen`：直接调 `_spawn_restart(plan, privileged=假)`，`check` 退 1 → `PrivilegedError`，monkeypatch 的 `subprocess.Popen` 未被调用；退 0 → Popen argv == `("sudo","-n",helper,"restart")`。
  - `tests/app/test_postcheck_boot.py`：开机自检没过回滚时也调了 `install-unit <退回版本>`。
- [ ] 跑：红。
- [ ] 实现：`AppContext.privileged: Privileged = field(default_factory=Privileged)`；`_spawn_restart(plan, privileged=Privileged())`：`privileged.ensure_can_restart(); Popen(list(privileged.restart_argv(plan)), start_new_session=True)`；`_release_activate`：precheck 之后 `try: ctx.privileged.ensure_can_restart(); note = ctx.privileged.install_unit(name) except PrivilegedError as exc: raise HttpError(409, "新版单元文件没装上,没有切换", str(exc))`；响应加 `"unit": note`；`_release_rollback` 与开机回滚：`rollback()` 后 `try: ctx.privileged.install_unit(back) except PrivilegedError as exc: log.warning("退回 %s 了,但它的单元没装回去: %s", back, exc)`；`ctx.restart(plan)` 抛 `PrivilegedError` → `HttpError(500, "版本切了,但重启命令发不出去", str(exc))`（只可能出现在 ensure 与 spawn 之间助手状态变了的窗口）。
- [ ] 跑 `tests/app/test_api_release.py tests/app/test_postcheck_boot.py tests/app`：绿。
- [ ] Commit `app: activate installs the shipped unit via the helper before switching; restart goes through sudo helper (W01b)`.

### Task 6：CLI

**Files:** Modify `src/d1max_patrol/cli.py`（`activate` / `rollback` 分支）；Test `tests/test_cli_release.py`

- [ ] 测试：`test_activate在有助手时装单元`：monkeypatch `d1max_patrol.cli.Privileged` 为返回假对象的工厂（记录 `install_unit` 调用），activate 后调用 == `[name]`；`test_activate没助手就提示并照常切`（stdout 含「单元没更新」）；`test_activate单元装不上退非零且不切链`；`test_rollback也装回上一版单元`。
- [ ] 跑：红。
- [ ] 实现：`activate`：`priv = Privileged()`；`if priv.present(): try: print(f"单元文件: {priv.install_unit(args.name)}") except PrivilegedError as exc: print(f"切不了: 新版单元文件没装上 —— {exc}", file=sys.stderr); return 2` **在 `activate()` 之前**；不在 → `print("提示: 没有特权助手,单元文件没更新;要更新单元请重跑 deploy/install.sh")`。`rollback`：之后 `install_unit(back)`，失败只打印一行。
- [ ] 跑:绿。
- [ ] Commit `cli: release activate/rollback install the slot's unit through the helper (W01b)`.

### Task 7：文档、报告、工单

- [ ] `docs/装机清单.md`：装机会多出两处文件；OTA 后如何核对 `systemctl cat d1max-patrol`。`docs/真机待验证清单.md` 第 6 条改写：现在有 sudoers 白名单，验证命令 `sudo -u robot sudo -n /usr/local/sbin/d1max-privileged check`。
- [ ] `docs/W01b-完工报告.md`（供审核）；工单表 W01b 打勾。
- [ ] 全量测试，失败集合 ⊆ 已知 5 条；突变检查；内部评审；push。

---

## 自查

- 设计 §1 → Task 1；§2 → Task 2、3；§3 → Task 4、5、6；§4 → Task 3、5（boot-guard 不动，文档说明）；§5 → 各任务测试。
- 名字一致：`Privileged.present/available/install_unit/restart_argv/ensure_can_restart`、`PrivilegedError`、`ctx.privileged`、助手子命令 `check/install-unit/restart/reboot`。
