# W01 持久数据目录 Implementation Plan

> **执行后订正（2026-09-22）**：本计划已执行完毕。执行中发现并改掉的三处计划错误，
> 为免误导后来者，在原文处以 ⚠️ 标出：(1) Task 3 的 `continue` 应为 `break`；
> (2) Task 3 的保险只看 `runs` 是漏洞，应看四样；(3) Task 7 的 `migrate → restart`
> 是竞态，应为 `stop → migrate → start`；(4) Task 4 说 `missions/` 不搬是错的——它装的是
> `PUT /api/missions` 手写的 `*.yaml`，包里不带，跟 runs 一样是运行时数据，实际实现搬且受保险保护。
> 以代码与 `docs/装机清单.md` 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 巡检数据（runs、上传队列、基线、导出、任务文件）搬出版本槽，落到一个升级和回滚都不碰的持久目录 `/var/lib/d1max`，并把老机器槽里的数据一次性迁过来；`prune()` 加一道保险，绝不删还装着数据的槽。

**Architecture:** 新增 `engine/datadir.py`（数据根解析 + 槽内数据迁移，纯函数，根目录可注入）。`app/server.py` 的 `_build_parser()` 默认值改为从 `D1MAX_DATA_ROOT` 推导（没设就维持今天的相对路径，开发机行为不变）。systemd 单元设这个环境变量；`install.sh` 建目录并在重启服务前调 `release migrate-data`；`uninstall.sh` 的对账声明块跟上。`release.prune()` 对含 `runs/` 数据的槽跳过。

**Tech Stack:** Python 3.10、pytest、bash、systemd。测试命令一律用 `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest`（ROS 的 pytest 插件会经 PYTHONPATH 混进来，见 W07）。

**背景（为什么）：** `--runs-root` 默认相对路径 `runs`，服务的 `WorkingDirectory=/opt/d1max/current` 是版本槽的符号链接，所以证据落在 `releases/<版本>/runs`。切到新版后旧槽不再是当前目录（旧槽里待传的队列就此停传）；第三版 `commit()` 调 `prune()` 把最老的槽整个 `rmtree`，证据一起没了。已用真实函数复现。

**数据根布局（定下来，别再各写各的）：**

```
/var/lib/d1max/                 ← D1MAX_DATA_ROOT
  runs/                         ← --runs-root
    slam/                       ← --maps-dir
    bags/                       ← --bags-dir
    logs/                       ← --log-dir 默认
  missions/                     ← --missions-dir
  queue.jsonl                   ← 上传队列（server.py 里是 runs_root.parent / QUEUE_FILE_NAME，自动落对）
  baselines/                    ← 同上，runs_root.parent / BASELINE_DIR_NAME
  exports/                      ← 同上，runs_root.parent / EXPORTS_DIR_NAME
```

`runs_root.parent` 这层约定不改，所以队列、基线、导出三处**不用动代码**，只要 `runs_root` 落对。

---

## File map

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/d1max_patrol/engine/datadir.py` | 新建 | `DATA_ROOT_ENV`、`DataPaths`、`resolve_paths()`、`migrate_slot_data()` |
| `tests/engine/test_datadir.py` | 新建 | 上面两个函数的测试 |
| `src/d1max_patrol/app/server.py:4348-4350` | 改 | `--maps-dir/--bags-dir/--missions-dir/--runs-root` 默认值走 `resolve_paths()` |
| `tests/app/test_data_root.py` | 新建 | 解析器默认值随环境变量变化 |
| `src/d1max_patrol/engine/release.py:661-683` | 改 | `prune()` 跳过含数据的槽 |
| `tests/engine/test_release_switch.py` | 加测试 | 含数据的槽不删 |
| `src/d1max_patrol/cli.py:133-171, 515-580` | 改 | 新子命令 `release migrate-data` |
| `tests/test_cli_release.py` | 加测试 | 子命令跑通 |
| `deploy/d1max-patrol.service` | 改 | `Environment=D1MAX_DATA_ROOT=/var/lib/d1max` |
| `deploy/install.sh` | 改 | 建目录、声明块、重启前迁移 |
| `deploy/uninstall.sh` | 改 | 声明块、keep-data 分支、默认分支 |
| `tests/test_deploy_files.py` | 加测试 | 单元与脚本的约定 |
| `docs/装机清单.md` | 改 | 两处"数据在槽里"的说法 |
| `docs/庄园安防-差距核查与工单.md` | 改 | W01 打勾 |

---

### Task 1: `datadir.resolve_paths()` —— 数据根解析

**Files:**
- Create: `src/d1max_patrol/engine/datadir.py`
- Test: `tests/engine/test_datadir.py`

- [ ] **Step 1: 写失败的测试**

```python
"""数据根:巡检数据搬出版本槽之后落在哪。"""

from __future__ import annotations

from pathlib import Path

from d1max_patrol.engine.datadir import DataPaths, resolve_paths


def test_没设数据根就是今天的相对路径():
    """开发机上在仓库里跑,行为一个字不变。"""
    assert resolve_paths(None) == DataPaths(
        runs_root=Path("runs"), maps_dir=Path("runs/slam"),
        bags_dir=Path("runs/bags"), missions_dir=Path("missions"))


def test_设了数据根全部落到它底下():
    assert resolve_paths("/var/lib/d1max") == DataPaths(
        runs_root=Path("/var/lib/d1max/runs"),
        maps_dir=Path("/var/lib/d1max/runs/slam"),
        bags_dir=Path("/var/lib/d1max/runs/bags"),
        missions_dir=Path("/var/lib/d1max/missions"))


def test_空串和空白当没设():
    assert resolve_paths("   ") == resolve_paths(None)
```

- [ ] **Step 2: 跑,确认失败**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/engine/test_datadir.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'd1max_patrol.engine.datadir'`

- [ ] **Step 3: 写最小实现**

```python
"""数据根:巡检数据摆在哪。**在版本槽外面。**

``--runs-root`` 原来默认相对路径 ``runs``,而服务的 ``WorkingDirectory`` 是
``/opt/d1max/current`` —— 一条指着版本槽的符号链接。于是证据落在
``releases/<版本>/runs``:切版本之后旧槽不再是当前目录(旧槽里待传的队列就此
停传),第三版 ``commit()`` 调 ``prune()`` 把最老的槽整个删掉,证据一起没了。

这里只做两件事:

* :func:`resolve_paths` —— 给定数据根(来自 ``$D1MAX_DATA_ROOT``),算出
  ``runs_root / maps_dir / bags_dir / missions_dir`` 四个默认值。**没给就退回
  今天的相对路径**,开发机在仓库里跑的行为一个字不变。
* :func:`migrate_slot_data` —— 老机器槽里已经有的数据,一次性搬到数据根。

上传队列、基线、导出三处在 ``app/server.py`` 里是 ``runs_root.parent / <名字>``,
所以 ``runs_root`` 落对了它们就跟着落对,这里不另算。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: 服务单元设它;开发机不设。
DATA_ROOT_ENV = "D1MAX_DATA_ROOT"
#: 真机上的数据根。**装机脚本、服务单元、这里三处要一致。**
DEFAULT_DATA_ROOT = Path("/var/lib/d1max")


@dataclass(frozen=True)
class DataPaths:
    runs_root: Path
    maps_dir: Path
    bags_dir: Path
    missions_dir: Path


def resolve_paths(data_root: str | Path | None) -> DataPaths:
    """数据根 → 四个目录。``None``/空白 = 没设 = 今天的相对路径。"""
    if data_root is None or not str(data_root).strip():
        base = Path(".")
    else:
        base = Path(str(data_root).strip())
    runs = base / "runs"
    return DataPaths(runs_root=runs, maps_dir=runs / "slam",
                     bags_dir=runs / "bags", missions_dir=base / "missions")
```

注意:`Path(".") / "runs"` 得到的是 `Path("runs")`,和今天的默认值相等。

- [ ] **Step 4: 跑,确认通过**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/engine/test_datadir.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/engine/datadir.py tests/engine/test_datadir.py
git commit -m "engine/datadir: resolve data-root paths (W01)"
```

---

### Task 2: `_build_parser()` 默认值走数据根

**Files:**
- Modify: `src/d1max_patrol/app/server.py:4348-4350`(`--maps-dir`、`--bags-dir`、`--missions-dir`、`--runs-root` 四行)
- Test: `tests/app/test_data_root.py`

- [ ] **Step 1: 写失败的测试**

```python
"""``--runs-root`` 这几个默认值要跟着 ``$D1MAX_DATA_ROOT`` 走。"""

from __future__ import annotations

from pathlib import Path

from d1max_patrol.app.server import _build_parser
from d1max_patrol.engine.datadir import DATA_ROOT_ENV


def test_没设环境变量默认值不变(monkeypatch):
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    a = _build_parser().parse_args([])
    assert (a.runs_root, a.maps_dir, a.bags_dir, a.missions_dir) == (
        "runs", "runs/slam", "runs/bags", "missions")


def test_设了环境变量四个目录都落到数据根(monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, "/var/lib/d1max")
    a = _build_parser().parse_args([])
    assert Path(a.runs_root) == Path("/var/lib/d1max/runs")
    assert Path(a.maps_dir) == Path("/var/lib/d1max/runs/slam")
    assert Path(a.bags_dir) == Path("/var/lib/d1max/runs/bags")
    assert Path(a.missions_dir) == Path("/var/lib/d1max/missions")


def test_命令行显式给的赢过环境变量(monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, "/var/lib/d1max")
    a = _build_parser().parse_args(["--runs-root", "/mnt/x/runs"])
    assert a.runs_root == "/mnt/x/runs"
    assert Path(a.maps_dir) == Path("/var/lib/d1max/runs/slam")
```

- [ ] **Step 2: 跑,确认失败**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/app/test_data_root.py -v`
Expected: `test_设了环境变量四个目录都落到数据根` FAIL(`runs` != `/var/lib/d1max/runs`),其余通过

- [ ] **Step 3: 改解析器**

在 `src/d1max_patrol/app/server.py` 顶部 import 区加:

```python
from d1max_patrol.engine.datadir import DATA_ROOT_ENV, resolve_paths
```

在 `_build_parser()` 里,`--maps-dir` 那行之前加一句,然后改四行默认值:

```python
    # **数据根。** 服务单元设 D1MAX_DATA_ROOT=/var/lib/d1max,四个数据目录就都
    # 落到版本槽外面;开发机不设,还是仓库里的相对路径。理由见 engine/datadir.py。
    paths = resolve_paths(os.environ.get(DATA_ROOT_ENV))
    p.add_argument("--maps-dir", default=str(paths.maps_dir), help="自建地图目录")
    p.add_argument("--bags-dir", default=str(paths.bags_dir), help="录包落盘目录")
```

`--missions-dir` 与 `--runs-root` 两行改成:

```python
    p.add_argument("--missions-dir", default=str(paths.missions_dir))
    p.add_argument("--runs-root", default=str(paths.runs_root),
                   help=f"巡检数据根目录。默认按环境变量 {DATA_ROOT_ENV} 推:"
                        f"<数据根>/runs;没设就是相对路径 runs")
```

`--params-file` 那行(在 `--maps-dir`、`--bags-dir` 之间)保持原位不动。

- [ ] **Step 4: 跑,确认通过;再跑现有解析器测试**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/app/test_data_root.py tests/app/test_auth.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/app/server.py tests/app/test_data_root.py
git commit -m "app/server: data dirs default from D1MAX_DATA_ROOT (W01)"
```

---

### Task 3: `prune()` 不删还装着数据的槽

**Files:**
- Modify: `src/d1max_patrol/engine/release.py:661-683`(`prune()`)
- Test: `tests/engine/test_release_switch.py`(追加)

- [ ] **Step 1: 写失败的测试**(追加到文件末尾;`_layout`、`_pkg`、`stage`、`activate`、`commit`、`installed`、`NOW` 该文件里已有)

```python
def test_槽里还有巡检数据就不删(tmp_path, caplog):
    """W01 的保险:数据本该在 /var/lib/d1max,但万一老机器没迁干净,
    宁可多占盘也不删证据。"""
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    old_runs = layout.releases / "2026-09-01-aaaaaa" / "runs" / "20260901-0800"
    old_runs.mkdir(parents=True)
    (old_runs / "manifest.json").write_text("{}", encoding="utf-8")
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    dropped = commit(layout)
    assert dropped == ()
    assert "2026-09-01-aaaaaa" in installed(layout)
    assert (old_runs / "manifest.json").exists()
    assert "还有巡检数据" in caplog.text


def test_槽里runs目录是空的照删(tmp_path):
    layout = _layout(tmp_path)
    for name in ("2026-09-01-aaaaaa", "2026-09-06-a3f9c1", "2026-09-20-77b2de"):
        stage(layout, _pkg(tmp_path / name, name), now_ms=NOW)
    (layout.releases / "2026-09-01-aaaaaa" / "runs").mkdir()
    activate(layout, "2026-09-20-77b2de", now_ms=NOW)
    assert commit(layout) == ("2026-09-01-aaaaaa",)
```

- [ ] **Step 2: 跑,确认失败**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/engine/test_release_switch.py -k "巡检数据 or 照删" -v`
Expected: `test_槽里还有巡检数据就不删` FAIL(`dropped == ('2026-09-01-aaaaaa',)`),另一条通过

- [ ] **Step 3: 改 `prune()`**

在 `release.py` 里 `KEEP_RELEASES` 常量下面加:

```python
#: 槽里这个目录非空就说明还有巡检数据 —— W01 之前的机器数据就落在槽里。
#: prune 见到它不删。名字跟 datadir.resolve_paths 里 ``runs`` 一致。
SLOT_DATA_DIR = "runs"
```

`prune()` 的循环改成:

```python
    names = list(installed(layout))
    dropped: list[str] = []
    for name in names:
        if len(names) - len(dropped) <= keep:
            break
        if name in protected:
            continue
        if _has_slot_data(layout.releases / name):
            log.warning("版本 %s 的槽里还有巡检数据,不删 —— 先跑 "
                        "release migrate-data 把数据搬到数据根", name)
            break   # ⚠️ 原计划写 continue —— 那会跳过这一槽去删下一槽凑数;实际实现是 break
        shutil.rmtree(layout.releases / name, ignore_errors=True)
        dropped.append(name)
    return tuple(dropped)


def _has_slot_data(slot: Path) -> bool:
    """``<槽>/runs`` 底下还有任何文件吗。空目录不算。
    ⚠️ 原计划只看 runs;评审指出 queue.jsonl/baselines/exports 也会被一起 rmtree。
    实际实现看 release.SLOT_DATA_ITEMS 四样(非空文件或含文件的目录)。"""
    runs = slot / SLOT_DATA_DIR
    if not runs.is_dir():
        return False
    return any(p.is_file() for p in runs.rglob("*"))
```

`release.py` 目前**没有** logger。在文件顶部 import 区加 `import logging`,并在常量区(`KEEP_RELEASES` 上方)加 `log = logging.getLogger(__name__)`。

- [ ] **Step 4: 跑整个 release 测试组**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/engine/test_release*.py tests/test_cli_release.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/engine/release.py tests/engine/test_release_switch.py
git commit -m "release: prune never removes a slot that still holds run data (W01)"
```

---

### Task 4: `migrate_slot_data()` —— 老槽数据搬家

**Files:**
- Modify: `src/d1max_patrol/engine/datadir.py`
- Test: `tests/engine/test_datadir.py`(追加)

搬的四样(与 `runs_root.parent` 约定对应):`runs/`、`queue.jsonl`、`baselines/`、`exports/`。规则:**只增不覆盖**(目标已有同路径文件就跳过并记下),搬完把槽里的源改名加 `.migrated` 后缀,这样 Task 3 的保险不会因为已迁移的槽永远拦住 prune。~~`missions/` 不搬~~ ⚠️ 错:`missions/*.yaml` 是 `PUT /api/missions` 写出来的、包里不带(`release.py` 的打包注释明说),实际实现把 `missions` 加进了 `SLOT_DATA_ITEMS`,搬且保护。

- [ ] **Step 1: 写失败的测试**(追加)

```python
import json

from d1max_patrol.engine.datadir import migrate_slot_data
from d1max_patrol.engine.release import Layout


def _slot(root: Path, name: str) -> Path:
    slot = root / "releases" / name
    slot.mkdir(parents=True)
    (slot / "release.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return slot


def test_把每个槽里的数据搬到数据根并改名源目录(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    b = _slot(root, "2026-09-06-a3f9c1")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("a", encoding="utf-8")
    (a / "queue.jsonl").write_text('{"k":1}\n', encoding="utf-8")
    (b / "runs" / "r2").mkdir(parents=True)
    (b / "runs" / "r2" / "manifest.json").write_text("b", encoding="utf-8")
    (b / "baselines").mkdir()
    (b / "baselines" / "p1.jpg").write_bytes(b"jpg")

    report = migrate_slot_data(Layout(root=root), data)

    assert (data / "runs" / "r1" / "manifest.json").read_text(encoding="utf-8") == "a"
    assert (data / "runs" / "r2" / "manifest.json").read_text(encoding="utf-8") == "b"
    assert (data / "queue.jsonl").read_text(encoding="utf-8") == '{"k":1}\n'
    assert (data / "baselines" / "p1.jpg").read_bytes() == b"jpg"
    assert not (a / "runs").exists() and (a / "runs.migrated").is_dir()
    assert not (a / "queue.jsonl").exists() and (a / "queue.jsonl.migrated").is_file()
    assert not (b / "runs").exists() and (b / "runs.migrated").is_dir()
    assert report.copied == 4 and report.skipped == 0
    assert set(report.slots) == {"2026-09-01-aaaaaa", "2026-09-06-a3f9c1"}


def test_目标已有的文件不覆盖(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("old", encoding="utf-8")
    (data / "runs" / "r1").mkdir(parents=True)
    (data / "runs" / "r1" / "manifest.json").write_text("new", encoding="utf-8")

    report = migrate_slot_data(Layout(root=root), data)

    assert (data / "runs" / "r1" / "manifest.json").read_text(encoding="utf-8") == "new"
    assert report.copied == 0 and report.skipped == 1
    assert (a / "runs.migrated" / "r1" / "manifest.json").exists()


def test_没有槽或槽里没数据什么都不做(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    _slot(root, "2026-09-01-aaaaaa")
    report = migrate_slot_data(Layout(root=root), data)
    assert report.copied == 0 and report.slots == ()
    assert not data.exists()


def test_重跑是幂等的(tmp_path):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    a = _slot(root, "2026-09-01-aaaaaa")
    (a / "runs" / "r1").mkdir(parents=True)
    (a / "runs" / "r1" / "manifest.json").write_text("a", encoding="utf-8")
    migrate_slot_data(Layout(root=root), data)
    report = migrate_slot_data(Layout(root=root), data)
    assert report.copied == 0 and report.slots == ()
```

- [ ] **Step 2: 跑,确认失败**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/engine/test_datadir.py -v`
Expected: 4 条新测试 FAIL,`ImportError: cannot import name 'migrate_slot_data'`

- [ ] **Step 3: 实现**(追加到 `datadir.py`)

```python
import shutil

from .release import Layout, installed

#: 槽里要搬走的东西。跟 app/server.py 里 ``runs_root`` 及其兄弟位置的名字一致:
#: ``QUEUE_FILE_NAME``、``BASELINE_DIR_NAME``、``EXPORTS_DIR_NAME``。
SLOT_DATA_ITEMS = ("runs", "queue.jsonl", "baselines", "exports")
#: 搬完在槽里留下的名字。Task 3 的 ``_has_slot_data`` 只看 ``runs``,
#: 改了名它就不再拦 prune。
MIGRATED_SUFFIX = ".migrated"


@dataclass(frozen=True)
class MigrationReport:
    slots: tuple[str, ...]
    copied: int
    skipped: int


def migrate_slot_data(layout: Layout, data_root: Path) -> MigrationReport:
    """把每个版本槽里的巡检数据搬到 ``data_root``。**只增不覆盖,可重跑。**

    搬完把槽里的源改名加 :data:`MIGRATED_SUFFIX`,所以第二次跑什么都不做。
    目标已有同路径文件时跳过(算进 ``skipped``),源照样改名 —— 那份留在
    ``*.migrated`` 里,人想核对随时能看。
    """
    data_root = Path(data_root)
    touched: list[str] = []
    copied = skipped = 0
    for name in installed(layout):
        slot = layout.releases / name
        hit = False
        for item in SLOT_DATA_ITEMS:
            src = slot / item
            if not src.exists():
                continue
            hit = True
            c, s = _merge_into(src, data_root / item)
            copied += c
            skipped += s
            src.rename(slot / (item + MIGRATED_SUFFIX))
        if hit:
            touched.append(name)
    return MigrationReport(slots=tuple(touched), copied=copied, skipped=skipped)


def _merge_into(src: Path, dst: Path) -> tuple[int, int]:
    """``src``(文件或目录)并进 ``dst``:目标已有的文件不动。返回 (拷了, 跳过)。"""
    if src.is_file():
        if dst.exists():
            return 0, 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1, 0
    copied = skipped = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        target = dst / p.relative_to(src)
        if target.exists():
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        copied += 1
    return copied, skipped
```

把 `import shutil` 和 `from .release import Layout, installed` 移到文件顶部的 import 区。`installed()` 只列有 `release.json` 的槽,测试里的 `_slot` 写了这个文件。

- [ ] **Step 4: 跑,确认通过**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/engine/test_datadir.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/engine/datadir.py tests/engine/test_datadir.py
git commit -m "engine/datadir: migrate slot data into the data root (W01)"
```

---

### Task 5: CLI `release migrate-data`

**Files:**
- Modify: `src/d1max_patrol/cli.py:164-171`(子命令注册)、`:515-580`(`_cmd_release` 分派)
- Test: `tests/test_cli_release.py`(追加)

- [ ] **Step 1: 写失败的测试**(追加;`_pkg`、`main`、`stage`、`Layout`、`NOW` 该文件顶部已 import)

```python
def test_migrate_data把槽里的数据搬到数据根(tmp_path, capsys):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    layout = Layout(root=root)
    layout.releases.mkdir(parents=True)
    stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=NOW)
    slot = layout.releases / "2026-09-20-77b2de"
    (slot / "runs" / "r1").mkdir(parents=True)
    (slot / "runs" / "r1" / "manifest.json").write_text("{}", encoding="utf-8")

    assert main(["release", "migrate-data", "--root", str(root),
                 "--data-root", str(data)]) == 0

    out = capsys.readouterr().out
    assert (data / "runs" / "r1" / "manifest.json").exists()
    assert "2026-09-20-77b2de" in out and "1" in out


def test_migrate_data默认数据根读环境变量(tmp_path, monkeypatch):
    root = tmp_path / "opt"
    data = tmp_path / "var"
    layout = Layout(root=root)
    layout.releases.mkdir(parents=True)
    stage(layout, _pkg(tmp_path / "pkg", "2026-09-20-77b2de"), now_ms=NOW)
    (layout.releases / "2026-09-20-77b2de" / "queue.jsonl").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("D1MAX_DATA_ROOT", str(data))

    assert main(["release", "migrate-data", "--root", str(root)]) == 0
    assert (data / "queue.jsonl").read_text(encoding="utf-8") == "x\n"
```

该文件里**没有** `NOW`,在 import 之后加 `NOW = 1_700_000_000_000`;`Layout`、`stage` 已在顶部的 import 列表里。

- [ ] **Step 2: 跑,确认失败**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/test_cli_release.py -k migrate -v`
Expected: FAIL,argparse `invalid choice: 'migrate-data'`(退出码 2 → `SystemExit`)

- [ ] **Step 3: 注册子命令并分派**

`cli.py` 的 `build_parser()`,`boot-guard` 那条 `_root_arg(...)` 之后加:

```python
    p_mig = rel_sub.add_parser(
        "migrate-data",
        help="把版本槽里的巡检数据搬到数据根(W01;装机脚本在重启服务前调它)")
    _root_arg(p_mig)
    p_mig.add_argument("--data-root", default=None,
                       help=f"数据根,默认取 ${DATA_ROOT_ENV} 或 {DEFAULT_DATA_ROOT}")
```

文件顶部 import:

```python
from d1max_patrol.engine.datadir import DATA_ROOT_ENV, DEFAULT_DATA_ROOT, migrate_slot_data
```

`_cmd_release()` 里 `if args.release_command == "rollback":` 那段之前加:

```python
    if args.release_command == "migrate-data":
        env = os.environ.get(DATA_ROOT_ENV, "").strip()
        data_root = Path(args.data_root or env or DEFAULT_DATA_ROOT)
        report = migrate_slot_data(layout, data_root)
        print(f"数据根   : {data_root}")
        print(f"搬过的槽 : {', '.join(report.slots) or '(没有要搬的)'}")
        print(f"拷了 {report.copied} 个文件,跳过 {report.skipped} 个(目标已有)")
        return 0
```

- [ ] **Step 4: 跑**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/test_cli_release.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/cli.py tests/test_cli_release.py
git commit -m "cli: release migrate-data (W01)"
```

---

### Task 6: 服务单元设数据根

**Files:**
- Modify: `deploy/d1max-patrol.service`(`Environment=D1MAX_RELEASE_ROOT=/opt/d1max` 那行之后)
- Test: `tests/test_deploy_files.py`(追加)

- [ ] **Step 1: 写失败的测试**(追加;`服务单元` fixture 已有)

```python
def test_服务把巡检数据指到版本槽外面(服务单元):
    """W01:没有这一行,--runs-root 默认相对路径 runs,落在
    WorkingDirectory=/opt/d1max/current 也就是版本槽里,升两次级就被 prune 删掉。"""
    assert "Environment=D1MAX_DATA_ROOT=/var/lib/d1max" in 服务单元
```

- [ ] **Step 2: 跑,确认失败**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/test_deploy_files.py -k 版本槽外面 -v`
Expected: FAIL

- [ ] **Step 3: 改单元**

在 `Environment=D1MAX_RELEASE_ROOT=/opt/d1max` 之后插入:

```ini
# **巡检数据不在版本槽里。** 没有这一行,app/server.py 的 --runs-root 默认是
# 相对路径 runs,落在上面那个 WorkingDirectory —— 也就是 releases/<版本>/ ——
# 底下:切版本之后旧槽里待传的队列停传,第三版坐实时 prune 把最老的槽连同
# 证据一起删掉(W01,已复现)。这个目录由 install.sh 1/7 建、chown 给 robot,
# uninstall.sh 默认删、--keep-data 留。**三处路径要一致。**
Environment=D1MAX_DATA_ROOT=/var/lib/d1max
```

- [ ] **Step 4: 跑整个文件**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/test_deploy_files.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add deploy/d1max-patrol.service tests/test_deploy_files.py
git commit -m "deploy: service points run data at /var/lib/d1max (W01)"
```

---

### Task 7: 装机脚本建目录、迁移;卸载脚本对账

**Files:**
- Modify: `deploy/install.sh`(声明块 `:17-27`、`1/7` 建目录 `:137-138`、`7/7` 重启前 `:475`)
- Modify: `deploy/uninstall.sh`(声明块 `:43-62`、keep-data 分支 `:393-405`、默认分支 `:408-415`)
- Test: `tests/test_deploy_files.py`(追加);`tests/test_deploy_coexist.py`(现有,必须继续绿)

- [ ] **Step 1: 写失败的测试**(追加到 `test_deploy_files.py`;`装机脚本` fixture 已有)

```python
def test_装机脚本建数据根并在重启前把老槽的数据搬过去(装机脚本):
    """W01。目录归 robot,不然服务写不进去;迁移必须排在 systemctl restart
    之前,不然新版起来后 --runs-root 指着一个空目录,值守屏上历史全没了。"""
    assert re.search(r'mkdir -p .*"/var/lib/d1max"', 装机脚本)
    assert 'chown -R "$RUN_USER":"$RUN_USER" "/var/lib/d1max"' in 装机脚本
    mig = 装机脚本.index("release migrate-data")
    restart = 装机脚本.index("systemctl restart d1max-patrol.service")
    assert mig < restart


def test_卸载脚本默认删数据根而keep_data留着():
    text = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "# @删除 /var/lib/d1max" in text
    assert "# @保留 /var/lib/d1max" in text
    assert 'rm_sys "/var/lib/d1max"' in text
```

- [ ] **Step 2: 跑,确认失败**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/test_deploy_files.py -k "数据根" -v`
Expected: 2 FAIL

- [ ] **Step 3: 改 `install.sh`**

声明块:`# @写盘 /opt/d1max/releases` 那行的注释改为 `版本槽(只有代码和 venv;巡检数据在 /var/lib/d1max)`,并在 `# @写盘 /opt/d1max/pending.json` 之后加一行:

```bash
# @写盘 /var/lib/d1max                                                   巡检数据根(runs、上传队列、基线、导出),升级回滚都不碰它
```

`1/7` 那段改成:

```bash
say "1/7 建目录 $ROOT 和数据根 /var/lib/d1max"
mkdir -p "$ROOT/releases" "$ROOT/bin"
chown -R "$RUN_USER":"$RUN_USER" "$ROOT"
# **巡检数据根,在版本槽外面。** 服务单元的 Environment=D1MAX_DATA_ROOT 指着它,
# uninstall.sh 默认删、--keep-data 留。路径写死,理由跟 $ROOT 一样:单元里是字面量。
mkdir -p "/var/lib/d1max"
chown -R "$RUN_USER":"$RUN_USER" "/var/lib/d1max"
```

`7/7` 里 `systemctl restart d1max-patrol.service` 之前加:

> ⚠️ **原计划此处是竞态**:migrate 时老服务还在跑,可能正往槽里的 `queue.jsonl`/`runs/` 追加,
> 半截文件会被当成已完整拷走。实际实现是 **`systemctl stop || true` → migrate-data → `systemctl start`**,
> 且 `migrate_slot_data` 的 docstring 写明了"调用前提:没有任何进程还在往槽里写"。

```bash
# **老机器槽里的数据先搬到数据根,再重启。** W01 之前 --runs-root 落在槽里;
# 新版起来后读的是 /var/lib/d1max,不搬的话历史全"消失"、待传队列停传。
# 可重跑:搬过的源会改名 *.migrated,第二次什么都不做。失败不拦装机 ——
# 数据还在槽里,prune 见到槽里有 runs/ 也不会删(engine/release.py)。
sudo -u "$RUN_USER" env D1MAX_RELEASE_ROOT="$ROOT" D1MAX_DATA_ROOT=/var/lib/d1max \
  "$ROOT/bin/python" -m d1max_patrol.cli release migrate-data \
  || echo "  !! 槽内数据迁移没成功,数据还在原槽里没丢;装完后手工跑一次 release migrate-data" >&2
```

- [ ] **Step 4: 改 `uninstall.sh`**

声明块 `@删除` 段,`# @删除 /opt/d1max/releases` 注释改为 `版本槽(代码和 venv)`,并在 `# @删除 /opt/d1max/pending.json` 之后加:

```bash
# @删除 /var/lib/d1max                                                   巡检数据根
```

`@保留` 段,`# @保留 /opt/d1max/releases` 那行**删掉**(它不再装数据),加:

```bash
# @保留 /var/lib/d1max   巡检数据根:runs、上传队列、基线、导出
```

keep-data 分支(`:393-405`):把 `say "  留着:$ROOT/releases —— **巡检数据就在版本槽里**..."` 和它的续行换成:

```bash
    rm_sys "$ROOT/releases" "版本槽;数据不在里面了(W01 之后在 /var/lib/d1max)"
    say "  留着:/var/lib/d1max —— 巡检数据根:runs、上传队列、基线、导出。"
```

默认分支(`:408-415`):`rm_sys "$ROOT/releases" "版本槽,**巡检数据也在里面**"` 改为 `rm_sys "$ROOT/releases" "版本槽"`,并在 `rm_sys "$ROOT/bundles" "任务包"` 之后加:

```bash
    rm_sys "/var/lib/d1max" "巡检数据根"
```

`:390` 那句 `say "  巡检数据就在它底下的 runs/,任务包在 $ROOT/bundles。"` 改为 `say "  巡检数据在 /var/lib/d1max,任务包在 $ROOT/bundles。"`。

`rm_sys()`(`:155-170`)有一道护栏:`case "$target"` 只放行 `/opt/d1max*`、`/etc/d1max*` 和两种 systemd 单元路径,其他一律"拒绝删除"。**必须加一个分支**,否则默认模式下数据根删不掉、脚本还只是 warn:

```bash
    /opt/d1max|/opt/d1max/*) ;;
    /etc/d1max|/etc/d1max/*) ;;
    /var/lib/d1max|/var/lib/d1max/*) ;;
```

- [ ] **Step 5: 跑两个 deploy 测试文件**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest tests/test_deploy_files.py tests/test_deploy_coexist.py tests/test_deploy_guards.py -q`
Expected: 全部通过。若 `test_每一处声明在卸载脚本的代码里真的出现过` 红,说明 `rm_sys "/var/lib/d1max"` 没写成字面量;若 `test_保留清单是写盘清单的子集` 红,说明 `@保留 /opt/d1max/releases` 没删或 `@写盘 /var/lib/d1max` 没加。

- [ ] **Step 6: bash 语法检查**

Run: `bash -n deploy/install.sh && bash -n deploy/uninstall.sh && echo OK`
Expected: OK

- [ ] **Step 7: 提交**

```bash
git add deploy/install.sh deploy/uninstall.sh tests/test_deploy_files.py
git commit -m "deploy: create /var/lib/d1max, migrate slot data before restart, uninstall bookkeeping (W01)"
```

---

### Task 8: 文档与工单

**Files:**
- Modify: `docs/装机清单.md:428-438`、`:490-496`
- Modify: `docs/庄园安防-差距核查与工单.md`(W01 行)

- [ ] **Step 1: 改 `装机清单.md`**

`:433` `/opt/d1max/releases——版本槽，**巡检数据也落在槽里**` 改为 `/opt/d1max/releases——版本槽（只有代码和 venv）`,并在 `/opt/d1max/pending.json` 那行后加:

```markdown
- `/var/lib/d1max`——**巡检数据根**（runs、上传队列、基线、导出）。升级、回滚、`prune` 都不碰它；装机脚本在重启服务前把老机器槽里的数据搬过来一次（`release migrate-data`，可重跑）
```

`:494` 那段 `/opt/d1max/releases`（**巡检数据就在版本槽里**，`current/runs` 实际指的是 `<槽>/runs`） 改为 `/var/lib/d1max`（**巡检数据根**）,并把 "它留下五处" 的数量核对一遍——`@保留` 现在是 `/etc/d1max`、`/etc/d1max/env`、`/opt/d1max`、`/var/lib/d1max`、`/opt/d1max/bundles`,仍是五处。

- [ ] **Step 2: 工单打勾**

`docs/庄园安防-差距核查与工单.md` W01 那行末尾 `☐` 改为 `☑ 2026-09-22`,要点末尾追加:`已做:D1MAX_DATA_ROOT + engine/datadir + release migrate-data + prune 保险。真机项:老机器升级后核一次 /var/lib/d1max/runs 里历史齐不齐。`

- [ ] **Step 3: 全量测试**

Run: `env -u PYTHONPATH -u AMENT_PREFIX_PATH -u COLCON_PREFIX_PATH .venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -8`
Expected: 只有 W07 已记录的 5 项环境性失败(`test_static.py` 1、`test_store.py` 3、`test_packaging.py` 1),没有新的红。

- [ ] **Step 4: 提交**

```bash
git add docs/装机清单.md docs/庄园安防-差距核查与工单.md
git commit -m "docs: data root in install checklist; tick W01"
```

---

## 自查

- 规格覆盖:D1 的三个后果——数据在槽里(Task 2、6)、切槽停传(迁移 Task 4、7)、prune 删证据(Task 3 保险 + 数据已不在槽)——各有任务。卸载保留名单与共存测试(Task 7)。老机器迁移(Task 4、5、7)。
- 类型一致:`DataPaths` 字段 `runs_root/maps_dir/bags_dir/missions_dir` 在 Task 1、2 一致;`MigrationReport(slots, copied, skipped)` 在 Task 4、5 一致;`SLOT_DATA_DIR = "runs"` 与 `SLOT_DATA_ITEMS[0]` 与 `MIGRATED_SUFFIX` 的配合在 Task 3、4 一致。
- 未做(有意):`missions/` 不迁(任务包机制负责);`log_dir` 仍按 `runs_root / "logs"` 推,自动落到数据根。
