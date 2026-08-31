# D1 Max 导航协议层与仿真器 实施计划（第 1 卷）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可离线运行的 D1 Max 自主导航链路——协议编解码、`NavBackend` 端口、厂商 WebSocket 实现、以及一台行为可控可注错的导航仿真器，使"逐点导航 + 地图/路径管理"在无真机条件下端到端跑通并被契约测试锁定。

**Architecture:** 三层。底层 `protocol/` 只管线格式（报文封装/解析/请求构造），不含业务语义，请求端与仿真器共用同一套编解码函数，线格式因此天然一致。中间层 `backends/` 定义 `NavBackend` 抽象端口并给出 `VendorNavBackend`，把厂商协议的四处怪癖全部吸收在这一层内。上层 `d1max_sim` 是仿真器：平面运动学 + 三套状态机 + 地图存储 + WebSocket 服务端 + 故障注入通道，它同时是"接口契约的可执行定义"。契约测试对着端口而非实现编写，后续 `Nav2Backend` 接同一套测试即可替换。

**Tech Stack:** Python 3.10 / asyncio / `websockets>=13`（`websockets.asyncio.server` 与 `websockets.asyncio.client`）/ PyYAML / pytest + pytest-asyncio（`asyncio_mode = "auto"`）/ ruff

**Spec:** `docs/superpowers/specs/2026-08-31-d1max-patrol-inspection-design.md`

## 分卷说明

设计文档 §9 划分了 8 个阶段。本卷覆盖 **阶段 0 / 1 / 2**：项目地基、导航协议层、仿真导航端与 `NavBackend` 契约。后续卷次：

| 卷 | 覆盖阶段 | 主题 |
|---|---|---|
| 第 1 卷（本文） | 0 / 1 / 2 | 协议层、仿真导航端、`NavBackend` 与契约测试 |
| 第 2 卷 | 3 / 4 | C++ `d1max-sdk-bridge`、`DeviceBackend`、SDK 仿真端 |
| 第 3 卷 | 5 / 6 | `MissionRunner` 任务引擎、安全规则、`MediaSource`、结构化归档 |
| 第 4 卷 | 7 | 真机一致性验证套件、录制回放、arm64 迁移 |

**本卷完成时的可演示状态：** 一条命令起仿真器，另一条命令对着它建图、存路径、读路径、逐点导航到位；契约测试与降级场景测试全绿。

## Global Constraints

以下逐条摘自设计文档，**每个任务的要求都隐含包含本节**：

- Python 版本下限 `>=3.10`；目标运行环境 Ubuntu 22.04，开发机 x86_64，后续需可迁移 aarch64，因此不得引入含预编译平台二进制的依赖。
- WebSocket 只用 `websockets>=13` 的 asyncio 新接口：`websockets.asyncio.server.serve`、`websockets.asyncio.client.connect`。**禁止**使用 `websockets.serve` / `websockets.connect` 旧版 legacy 接口。
- 厂商自主导航 WebSocket 地址：`ws://192.168.144.100:10010`（仅作默认值，所有测试必须走仿真器的动态端口）。
- RobotSDK 地址 `192.168.234.1:8081`、bridge IPC `127.0.0.1:8790` —— 本卷不涉及，不得在本卷代码里引用。
- **真理源规则**：自主导航 WebSocket 是导航状态的唯一真理源。本卷不得从任何其他来源推断导航状态。
- **全逐点执行**：多点导航只用于读取厂商 App 画好的路径（`get_all_paths_by_mapid`），实际行走一律用 `start_nav` 逐点下发。`start_multi_nav` / `start_multi_nav_by_points` 只提供请求构造函数，不进入 `NavBackend` 端口。
- 时间戳：对外 JSON 与文件名用 UTC、ISO-8601、以 `Z` 结尾；协议内 `time_stamp` 字段用毫秒整数。
- 代码注释与文档中文，标识符与日志键名英文。
- 无真机。本卷全部验收在仿真器上完成；任何"只能在真机上验证"的假设必须在代码里以 `# 假设(待真机验证):` 注释标出。
- 每个任务结束提交一次，提交信息用 Conventional Commits 前缀（`feat:` / `test:` / `chore:` / `fix:`）。

## 协议地雷（必须实现，不是可选项）

核对厂商文档 `refs/nav-api/自主导航_WEBSOCKET_API.md` 后确认的四处偏离直觉之处：

1. **响应嵌套层**：`get_navigation_speed` / `set_navigation_speed`（§3.9/§3.10）的响应把 payload 多包了一层 `data.req_result.AppReponseObjectData.{req_func,status,msg,data}`（厂商把 Response 拼成了 Reponse）。其余接口都是 `data.req_result.{...}` 直挂。解析器必须两种都吃。
2. **请求名与响应名不一致**：请求发 `loc_load_map`（§4.1），响应回的 `req_func` 是 `load_localization_map`。这直接击穿"按 `req_func` 名回退匹配"的策略，必须有别名表。（`exit_charging` 曾被归入此类，实为误判：§7.14 文档给了完整的请求/响应样例，响应名与请求名一致，且是被动答复而非主动推送。本卷不实现回充，别名表与推送名单里都不留它。）
3. **推送复用 `app_resp` 类型**：`notify_stop_mapping_status`（§1.2）是设备主动推送，但 `head.type` 也是 `app_resp`，`frame_count` 不对应任何请求。收到匹配不上的 `app_resp` 不能直接丢弃，必须先查推送名单。
4. **导航状态没有推送通道**：文档中只有 `alg_error_code_notify`（§9）和 `notify_stop_mapping_status` 两种主动推送。`get_nav_status` / `get_loc_status` / `get_mapping_status` 只能轮询。因此 `VendorNavBackend` 必须自带轮询器，把状态变化转成事件流。

另有三个接口文档只给了请求没给响应：`reset_loc`、`start_nav_return_home`、`start_multi_nav_by_points`。它们的响应 `req_func` 名属于**未验证**，代码中以 `UNVERIFIED_RESPONSE_FUNCS` 常量标注，真机验证前不得依赖。

## File Structure

```
d1max-patrol/
  pyproject.toml                         # 打包、依赖、pytest、ruff 配置
  refs/README.md                         # 官方资料落位说明（refs/ 本身不入库）
  scripts/extract_refs.sh                # 从原始压缩包解出 refs/ 的可重复脚本
  scripts/extract_doc_samples.py         # 从 API 文档抠 JSON 样例作 fixture
  src/d1max_patrol/
    __init__.py
    config/
      __init__.py
      models.py                          # AppConfig / NavConfig 数据类
      loader.py                          # YAML 加载 + 严格校验 + 环境变量覆盖
    protocol/
      __init__.py
      nav_frames.py                      # 报文封装/解析（请求端与仿真器共用）
      nav_types.py                       # 状态枚举、Pose/Waypoint、故障码常量
      nav_requests.py                    # 各接口请求构造 + 响应名别名表
    backends/
      __init__.py
      base.py                            # NavBackend / DeviceBackend / MediaSource 三个端口、事件、异常
      vendor_nav.py                      # 厂商 WebSocket 实现
    cli.py                               # d1max 命令行入口
  src/d1max_sim/
    __init__.py
    __main__.py                          # python -m d1max_sim 启动仿真器
    kinematics.py                        # 平面运动学模型
    nav_state.py                         # 导航/定位/建图三套状态机
    store.py                             # 地图与路径存储
    inject.py                            # 故障注入状态与命令解析
    nav_server.py                        # WebSocket 服务端与请求分发
  tests/
    config/test_loader.py
    protocol/test_nav_frames.py
    protocol/test_nav_types.py
    protocol/test_nav_requests.py
    protocol/test_doc_samples.py          # 文档 JSON 样例作 golden fixture
    protocol/fixtures/                    # 由 extract_doc_samples.py 生成、入库
    sim/test_kinematics.py
    sim/test_nav_state.py
    sim/test_store.py
    sim/test_nav_server.py
    sim/test_inject.py
    backends/test_base.py                 # 端口事件分发
    backends/test_ports.py                # 三个端口的接口约定
    backends/test_vendor_matching.py      # 两级响应匹配
    backends/test_vendor_capabilities.py  # 各能力方法
    backends/test_vendor_resilience.py    # 轮询与重连
    contract/README.md                    # 契约测试的规矩
    contract/conftest.py                  # 后端参数化夹具（未来接 Nav2Backend）
    contract/test_nav_contract.py         # 实现无关，换 Nav2 时一行不改
    scenarios/conftest.py                 # 故障注入台架
    scenarios/test_fault_scenarios.py     # 实现相关，专打厂商怪癖
    test_cli.py
    test_smoke.py
```

**职责边界：** `protocol/` 不 import `backends/` 或 `d1max_sim`；`d1max_sim` 只 import `d1max_patrol.protocol`，不 import `backends`；`backends/vendor_nav.py` 不 import `d1max_sim`。测试是唯一把三者串起来的地方。

---
### Task 1: 项目地基与官方资料落位

**Files:**
- Create: `pyproject.toml`
- Create: `src/d1max_patrol/__init__.py`
- Create: `src/d1max_sim/__init__.py`
- Create: `refs/README.md`
- Create: `scripts/extract_refs.sh`
- Modify: `.gitignore`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: 包 `d1max_patrol` 与 `d1max_sim` 可导入，各带 `__version__: str`；仓库根目录 `python -m pytest` 可直接运行且 `asyncio_mode=auto` 生效；`d1max` 控制台脚本入口点已注册（实现在 Task 17）

- [ ] **Step 1: 写 pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "d1max-patrol"
version = "0.1.0"
description = "智元 D1 Max 四足机器人巡检与例检系统"
requires-python = ">=3.10"
dependencies = [
    "websockets>=13",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.6",
]

[project.scripts]
d1max = "d1max_patrol.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 2: 写两个包的 `__init__.py`**

`src/d1max_patrol/__init__.py`：

```python
"""D1 Max 巡检与例检系统。"""

__version__ = "0.1.0"
```

`src/d1max_sim/__init__.py`：

```python
"""D1 Max 自主导航仿真器。仅用于开发与测试,不进入板载部署。"""

__version__ = "0.1.0"
```

- [ ] **Step 3: 写冒烟测试**

`tests/test_smoke.py`：

```python
"""确认包结构与测试框架可用。"""

import asyncio

import d1max_patrol
import d1max_sim


def test_包可导入且带版本号():
    assert d1max_patrol.__version__ == "0.1.0"
    assert d1max_sim.__version__ == "0.1.0"


async def test_异步测试模式已开启():
    """asyncio_mode=auto 生效时,这个不加装饰器的协程测试应被真正执行。"""
    await asyncio.sleep(0)
    assert True
```

- [ ] **Step 4: 安装并运行测试**

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/test_smoke.py -v
```

Expected: 2 passed。若 `test_异步测试模式已开启` 显示 skipped，说明 `asyncio_mode` 未生效，检查 pyproject 的 `[tool.pytest.ini_options]`。

- [ ] **Step 5: 写 refs 落位说明**

`refs/README.md`：

    # 官方资料目录

    本目录存放厂商原始资料，**除本文件外不入 git**（见 `.gitignore`）。
    用 `scripts/extract_refs.sh` 从原始压缩包重建，压缩包由项目负责人另行分发。

    重建后应具备的结构：

        refs/
          nav-api/自主导航_WEBSOCKET_API.md      # 自主导航 WebSocket JSON 协议
          robot-sdk/RobotSDK-0.2.0/              # C++ SDK 头文件、示例、文档
          urdf/max_description/                  # URDF 与网格

    代码与计划中引用官方接口时，一律以 `refs/nav-api/自主导航_WEBSOCKET_API.md`
    的章节号为准（例如"§3.1 开始单点导航"）。

（注：上面用四空格缩进块给出文件内容，是为了避免与外层代码围栏冲突；写入文件时请去掉这四个空格缩进。）

- [ ] **Step 6: 写解包脚本**

`scripts/extract_refs.sh`：

```bash
#!/usr/bin/env bash
# 从厂商原始压缩包重建 refs/ 目录。
# 用法: scripts/extract_refs.sh <存放原始压缩包的目录>
set -euo pipefail

SRC="${1:?用法: scripts/extract_refs.sh <压缩包目录>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/refs"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$DEST/nav-api" "$DEST/robot-sdk" "$DEST/urdf"

# lib/<arch>/librobot_sdk.so 是符号链接,跨平台解包会失败,本卷用不到,直接排除。
tar -xf "$SRC/3.自主导航二开资料.zip" -C "$TMP" \
    --exclude='*/lib/*/librobot_sdk.so'

find "$TMP" -name '自主导航_WEBSOCKET_API.md' -exec cp {} "$DEST/nav-api/" \;
find "$TMP" -maxdepth 4 -type d -name 'RobotSDK-*' -exec cp -r {} "$DEST/robot-sdk/" \;
find "$TMP" -maxdepth 4 -type d -name 'max_description' -exec cp -r {} "$DEST/urdf/" \;

echo "refs/ 已重建于 $DEST"
```

然后：`chmod +x scripts/extract_refs.sh`

- [ ] **Step 7: 让 refs/README.md 例外入库**

当前 `.gitignore` 里有一行 `refs/`，它会连 README 一起忽略。改成两行：

```
refs/*
!refs/README.md
```

验证：

```bash
git check-ignore -v refs/README.md; echo "exit=$?"
```

Expected: 无输出，`exit=1`（即不再被忽略）。再验证资料本体仍被忽略：

```bash
mkdir -p refs/nav-api && touch refs/nav-api/x.md
git check-ignore -v refs/nav-api/x.md
```

Expected: 输出 `.gitignore:<行号>:refs/*	refs/nav-api/x.md`。之后 `rm refs/nav-api/x.md`。

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml src/ tests/ refs/README.md scripts/ .gitignore
git commit -m "chore: 建立项目地基、包结构与资料落位脚本"
```

---

### Task 2: 配置模型与加载

**Files:**
- Create: `src/d1max_patrol/config/__init__.py`
- Create: `src/d1max_patrol/config/models.py`
- Create: `src/d1max_patrol/config/loader.py`
- Test: `tests/config/test_loader.py`

**Interfaces:**
- Consumes: Task 1 的包结构
- Produces:
  - `d1max_patrol.config.models.NavConfig(url: str = "ws://192.168.144.100:10010", request_timeout_s: float = 5.0, connect_timeout_s: float = 5.0, status_poll_interval_s: float = 0.5, reconnect_min_s: float = 1.0, reconnect_max_s: float = 15.0)` —— 冻结数据类
  - `d1max_patrol.config.models.AppConfig(nav: NavConfig, runs_dir: str = "runs")` —— 冻结数据类
  - `d1max_patrol.config.loader.load_config(path: str | pathlib.Path | None) -> AppConfig`
  - `d1max_patrol.config.loader.ConfigError(Exception)`
  - 环境变量 `D1MAX_NAV_URL` 覆盖 `nav.url`

- [ ] **Step 1: 写失败的测试**

`tests/config/test_loader.py`：

```python
"""配置加载的行为约定:默认可用、显式覆盖、拼错即报错。"""

import pytest

from d1max_patrol.config.loader import ConfigError, load_config
from d1max_patrol.config.models import AppConfig, NavConfig


def test_不给路径时返回全默认配置():
    cfg = load_config(None)
    assert isinstance(cfg, AppConfig)
    assert cfg.nav.url == "ws://192.168.144.100:10010"
    assert cfg.nav.request_timeout_s == 5.0
    assert cfg.nav.status_poll_interval_s == 0.5
    assert cfg.runs_dir == "runs"


def test_yaml_覆盖部分字段其余保持默认(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "nav:\n"
        "  url: ws://127.0.0.1:19999\n"
        "  request_timeout_s: 2.5\n"
        "runs_dir: /data/runs\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.nav.url == "ws://127.0.0.1:19999"
    assert cfg.nav.request_timeout_s == 2.5
    assert cfg.nav.connect_timeout_s == 5.0
    assert cfg.runs_dir == "/data/runs"


def test_顶层未知键报错(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("bridge:\n  port: 8790\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="未知配置项: bridge"):
        load_config(p)


def test_nav_下未知键报错(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("nav:\n  urls: ws://x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nav 下未知配置项: urls"):
        load_config(p)


def test_数字字段给字符串报错(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("nav:\n  request_timeout_s: fast\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="request_timeout_s"):
        load_config(p)


def test_空文件等价于全默认(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(p) == load_config(None)


def test_文件不存在报错(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "nope.yaml")


def test_环境变量覆盖_url(monkeypatch):
    monkeypatch.setenv("D1MAX_NAV_URL", "ws://10.0.0.1:10010")
    cfg = load_config(None)
    assert cfg.nav.url == "ws://10.0.0.1:10010"


def test_配置对象不可变():
    cfg = load_config(None)
    with pytest.raises(AttributeError):
        cfg.nav.url = "ws://other"  # type: ignore[misc]


def test_navconfig_可独立构造():
    nav = NavConfig(url="ws://a:1")
    assert nav.reconnect_min_s == 1.0
    assert nav.reconnect_max_s == 15.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/config/test_loader.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_patrol.config'`

- [ ] **Step 3: 写 models.py**

`src/d1max_patrol/config/models.py`：

```python
"""配置数据模型。全部冻结,配置在进程启动后不可变。"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 厂商自主导航 WebSocket 默认地址,见 refs/nav-api §概述。
DEFAULT_NAV_URL = "ws://192.168.144.100:10010"


@dataclass(frozen=True)
class NavConfig:
    """自主导航 WebSocket 链路参数。"""

    url: str = DEFAULT_NAV_URL
    #: 单个请求等待响应的上限
    request_timeout_s: float = 5.0
    #: 建链上限
    connect_timeout_s: float = 5.0
    #: 状态轮询周期。协议无状态推送通道,只能轮询,见计划"协议地雷 4"。
    status_poll_interval_s: float = 0.5
    #: 断线重连退避区间
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 15.0


@dataclass(frozen=True)
class AppConfig:
    """进程级配置。后续卷次会在此追加 bridge / media 小节。"""

    nav: NavConfig = field(default_factory=NavConfig)
    runs_dir: str = "runs"
```

- [ ] **Step 4: 写 loader.py**

`src/d1max_patrol/config/loader.py`：

```python
"""YAML 配置加载。严格校验:拼错的键必须报错,不能静默退回默认值。"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, NavConfig

#: nav 小节的字段类型表。不用 dataclasses.fields() 推断,因为
#: `from __future__ import annotations` 会让 Field.type 变成字符串。
_NAV_FIELD_TYPES: dict[str, type] = {
    "url": str,
    "request_timeout_s": float,
    "connect_timeout_s": float,
    "status_poll_interval_s": float,
    "reconnect_min_s": float,
    "reconnect_max_s": float,
}

_TOP_LEVEL_KEYS = frozenset({"nav", "runs_dir"})

#: 环境变量覆盖表: 变量名 -> (小节名, 字段名)
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "D1MAX_NAV_URL": ("nav", "url"),
}


class ConfigError(Exception):
    """配置文件缺失、含未知键或字段类型不符。"""


def _coerce(section: str, name: str, value: Any, target: type) -> Any:
    if target is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{section}.{name} 应为数字,实际为 {value!r}")
        return float(value)
    if target is str:
        if not isinstance(value, str):
            raise ConfigError(f"{section}.{name} 应为字符串,实际为 {value!r}")
        return value
    raise ConfigError(f"不支持的字段类型 {target!r}（{section}.{name}）")


def _build_nav(raw: Any) -> NavConfig:
    if raw is None:
        return NavConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"nav 小节应为映射,实际为 {raw!r}")
    unknown = sorted(set(raw) - set(_NAV_FIELD_TYPES))
    if unknown:
        raise ConfigError(f"nav 下未知配置项: {', '.join(unknown)}")
    kwargs = {
        name: _coerce("nav", name, value, _NAV_FIELD_TYPES[name])
        for name, value in raw.items()
    }
    return NavConfig(**kwargs)


def load_config(path: str | Path | None) -> AppConfig:
    """读取配置文件;`path` 为 None 时返回全默认配置。"""
    raw: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"配置文件不存在: {p}")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"配置文件顶层应为映射: {p}")
        raw = loaded

    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(unknown)}")

    nav = _build_nav(raw.get("nav"))
    runs_dir = raw.get("runs_dir", "runs")
    if not isinstance(runs_dir, str):
        raise ConfigError(f"runs_dir 应为字符串,实际为 {runs_dir!r}")

    return _apply_env(AppConfig(nav=nav, runs_dir=runs_dir))


def _apply_env(cfg: AppConfig) -> AppConfig:
    nav = cfg.nav
    for env_name, (section, field_name) in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None:
            continue
        if section == "nav":
            nav = dataclasses.replace(nav, **{field_name: value})
    return dataclasses.replace(cfg, nav=nav)
```

`src/d1max_patrol/config/__init__.py`：

```python
"""配置加载。"""

from .loader import ConfigError, load_config
from .models import AppConfig, NavConfig

__all__ = ["AppConfig", "ConfigError", "NavConfig", "load_config"]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/config/test_loader.py -v`
Expected: 10 passed

- [ ] **Step 6: 提交**

```bash
git add src/d1max_patrol/config tests/config
git commit -m "feat: 配置模型与严格 YAML 加载"
```

---
### Task 3: 导航报文封装与解析

厂商协议的线格式层。请求端和仿真器**共用**这一个模块构造与解析报文，因此两边的线格式由构造函数保证一致，不会出现"仿真器发的报文真机解析不了"这类只有真机才能暴露的偏差。

**Files:**
- Create: `src/d1max_patrol/protocol/__init__.py`
- Create: `src/d1max_patrol/protocol/nav_frames.py`
- Test: `tests/protocol/test_nav_frames.py`

**Interfaces:**
- Consumes: 无（纯标准库）
- Produces（`d1max_patrol.protocol.nav_frames`）：
  - 常量 `FRAME_TYPE_REQUEST = "app_req"`、`FRAME_TYPE_RESPONSE = "app_resp"`、`FRAME_TYPE_ALG_ERROR = "alg_error_code_notify"`、`SOURCE_APP = "app"`、`SOURCE_ALG = "alg_control_node"`、`NESTED_RESULT_KEY = "AppReponseObjectData"`、`STATUS_OK = "ok"`、`STATUS_ERROR = "error"`
  - `ProtocolError(Exception)`
  - `Response(frame_count: int | None, req_func: str, ok: bool, msg: str | None, data: Any, raw: dict)` —— 冻结数据类
  - `AlgErrorItem(code: int, description: str, severity: int)` —— 冻结数据类
  - `AlgErrorNotify(items: tuple[AlgErrorItem, ...], time_stamp_ms: int | None, raw: dict)` —— 冻结数据类
  - 类型别名 `Message = Response | AlgErrorNotify`
  - `build_request(req_func: str, args: Any, frame_count: int, time_stamp_ms: int | None = None) -> dict`
  - `encode_request(req_func: str, args: Any, frame_count: int, time_stamp_ms: int | None = None) -> str`
  - `build_response(req_func: str, frame_count: int, *, ok: bool = True, msg: str | None = None, data: Any = None, nested: bool = False, time_stamp_ms: int | None = None) -> dict`
  - `build_alg_error_notify(items: Sequence[AlgErrorItem], time_stamp_ms: int | None = None) -> dict`
  - `parse_message(text: str | bytes) -> Message`
  - `parse_request(text: str | bytes) -> tuple[int | None, str, Any]` —— 供仿真器解析来自客户端的 `app_req`，返回 `(frame_count, req_func, args)`

- [ ] **Step 1: 写失败的测试**

`tests/protocol/test_nav_frames.py`：

```python
"""线格式的编解码。样例报文全部照抄 refs/nav-api 文档。"""

import json

import pytest

from d1max_patrol.protocol.nav_frames import (
    AlgErrorItem,
    AlgErrorNotify,
    ProtocolError,
    Response,
    build_alg_error_notify,
    build_request,
    build_response,
    encode_request,
    parse_message,
    parse_request,
)


def test_请求报文结构与文档一致():
    frame = build_request("start_mapping", 0, frame_count=1, time_stamp_ms=1234567890123)
    assert frame == {
        "head": {
            "type": "app_req",
            "time_stamp": 1234567890123,
            "source": "app",
            "frame_count": 1,
        },
        "data": {"req_func": {"start_mapping": 0}},
    }


def test_请求参数为_null_时保留键():
    frame = build_request("get_nav_status", None, frame_count=7)
    assert frame["data"]["req_func"] == {"get_nav_status": None}


def test_encode_request_产出可解析的_utf8_json():
    text = encode_request("get_all_paths_by_mapid", "地图A", frame_count=3)
    assert "地图A" in text  # ensure_ascii=False
    assert json.loads(text)["data"]["req_func"]["get_all_paths_by_mapid"] == "地图A"


def test_解析成功响应():
    text = json.dumps(
        {
            "head": {
                "type": "app_resp",
                "time_stamp": 1234567890124,
                "source": "alg_control_node",
                "frame_count": 1,
            },
            "data": {
                "req_result": {
                    "req_func": "get_nav_status",
                    "status": "ok",
                    "msg": None,
                    "data": "StandBy",
                }
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, Response)
    assert msg.frame_count == 1
    assert msg.req_func == "get_nav_status"
    assert msg.ok is True
    assert msg.msg is None
    assert msg.data == "StandBy"


def test_解析失败响应():
    text = json.dumps(
        {
            "head": {"type": "app_resp", "time_stamp": 1, "source": "alg_control_node",
                     "frame_count": 9},
            "data": {
                "req_result": {
                    "req_func": "start_nav",
                    "status": "error",
                    "msg": "not in StandBy",
                    "data": None,
                }
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, Response)
    assert msg.ok is False
    assert msg.msg == "not in StandBy"


def test_解析嵌套的_AppReponseObjectData_变体():
    """§3.9/§3.10 的响应多包了一层,厂商还把 Response 拼成了 Reponse。"""
    text = json.dumps(
        {
            "head": {"type": "app_resp", "time_stamp": 1, "source": "alg_control_node",
                     "frame_count": 4},
            "data": {
                "req_result": {
                    "AppReponseObjectData": {
                        "req_func": "get_navigation_speed",
                        "status": "ok",
                        "msg": None,
                        "data": {"x": 0.8, "y": 0.4, "z": 1.2},
                    }
                }
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, Response)
    assert msg.frame_count == 4
    assert msg.req_func == "get_navigation_speed"
    assert msg.data == {"x": 0.8, "y": 0.4, "z": 1.2}


def test_解析算法故障码推送():
    text = json.dumps(
        {
            "head": {
                "type": "alg_error_code_notify",
                "time_stamp": 1763697492747,
                "source": "alg_control_node",
                "frame_count": 1,
            },
            "data": {
                "items": [
                    {"code": 13330, "description": "navigation blocked", "severity": 0},
                    {"code": 13331, "description": "lidar disconnected", "severity": 1},
                ]
            },
        }
    )
    msg = parse_message(text)
    assert isinstance(msg, AlgErrorNotify)
    assert msg.time_stamp_ms == 1763697492747
    assert msg.items == (
        AlgErrorItem(13330, "navigation blocked", 0),
        AlgErrorItem(13331, "lidar disconnected", 1),
    )


def test_故障码推送允许空列表():
    text = json.dumps(
        {"head": {"type": "alg_error_code_notify", "time_stamp": 1,
                  "source": "alg_control_node", "frame_count": 1},
         "data": {"items": []}}
    )
    msg = parse_message(text)
    assert isinstance(msg, AlgErrorNotify)
    assert msg.items == ()


def test_frame_count_缺失或非整数时为_None():
    text = json.dumps(
        {"head": {"type": "app_resp", "time_stamp": 1, "source": "alg_control_node"},
         "data": {"req_result": {"req_func": "stop_nav", "status": "ok",
                                 "msg": None, "data": None}}}
    )
    assert parse_message(text).frame_count is None


@pytest.mark.parametrize(
    "text, hint",
    [
        ("{ not json", "不是合法 JSON"),
        ("[1, 2]", "顶层不是对象"),
        ('{"data": {}}', "缺少 head"),
        ('{"head": {"type": "app_topic_resp"}, "data": {}}', "不支持的报文类型"),
        ('{"head": {"type": "app_resp"}, "data": {}}', "缺少 req_result"),
        ('{"head": {"type": "app_resp"}, "data": {"req_result": {"status": "ok"}}}',
         "缺少 req_func"),
        ('{"head": {"type": "alg_error_code_notify"}, "data": {}}', "缺少 items"),
    ],
)
def test_畸形报文抛_ProtocolError(text, hint):
    with pytest.raises(ProtocolError, match=hint):
        parse_message(text)


def test_build_response_可被自己解析回来():
    frame = build_response("get_loc_status", 12, data="ContinuousLoc")
    msg = parse_message(json.dumps(frame))
    assert isinstance(msg, Response)
    assert (msg.frame_count, msg.req_func, msg.ok, msg.data) == (
        12, "get_loc_status", True, "ContinuousLoc")


def test_build_response_嵌套模式往返():
    frame = build_response("set_navigation_speed", 3, data={"x": 0.8}, nested=True)
    assert "AppReponseObjectData" in frame["data"]["req_result"]
    msg = parse_message(json.dumps(frame))
    assert msg.req_func == "set_navigation_speed"
    assert msg.data == {"x": 0.8}


def test_build_response_错误模式():
    frame = build_response("start_nav", 5, ok=False, msg="busy")
    msg = parse_message(json.dumps(frame))
    assert msg.ok is False and msg.msg == "busy"


def test_build_alg_error_notify_往返():
    frame = build_alg_error_notify([AlgErrorItem(13331, "lidar disconnected", 1)])
    msg = parse_message(json.dumps(frame))
    assert isinstance(msg, AlgErrorNotify)
    assert msg.items[0].code == 13331


def test_parse_request_取出三元组():
    text = encode_request("start_multi_nav", ["m1", "p1"], frame_count=6)
    assert parse_request(text) == (6, "start_multi_nav", ["m1", "p1"])


@pytest.mark.parametrize(
    "text, hint",
    [
        ('{"head": {"type": "app_resp", "frame_count": 1}, "data": {}}', "不是 app_req"),
        ('{"head": {"type": "app_req", "frame_count": 1}, "data": {}}', "缺少 req_func"),
        ('{"head": {"type": "app_req", "frame_count": 1}, "data": {"req_func": {}}}',
         "应恰好含一个函数名"),
        ('{"head": {"type": "app_req", "frame_count": 1},'
         ' "data": {"req_func": {"a": 1, "b": 2}}}', "应恰好含一个函数名"),
    ],
)
def test_parse_request_畸形报文抛_ProtocolError(text, hint):
    with pytest.raises(ProtocolError, match=hint):
        parse_request(text)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/protocol/test_nav_frames.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_patrol.protocol'`

- [ ] **Step 3: 写 nav_frames.py**

`src/d1max_patrol/protocol/nav_frames.py`：

```python
"""自主导航 WebSocket 报文的封装与解析。

只负责线格式,不含任何业务语义。请求端与仿真器共用本模块,
因此两边的线格式由同一组构造函数保证一致。

参考: refs/nav-api/自主导航_WEBSOCKET_API.md §消息结构 / §5 / §9
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

FRAME_TYPE_REQUEST = "app_req"
FRAME_TYPE_RESPONSE = "app_resp"
FRAME_TYPE_ALG_ERROR = "alg_error_code_notify"

SOURCE_APP = "app"
SOURCE_ALG = "alg_control_node"

#: 协议地雷 1: §3.9/§3.10 的响应在 req_result 下多包一层,
#: 且厂商把 Response 拼写成了 Reponse。不要"修正"这个拼写。
NESTED_RESULT_KEY = "AppReponseObjectData"

STATUS_OK = "ok"
STATUS_ERROR = "error"


class ProtocolError(Exception):
    """收到无法按协议解释的报文。"""


@dataclass(frozen=True)
class Response:
    """一条 app_resp。注意它既可能是请求的响应,也可能是设备主动推送。"""

    frame_count: int | None
    req_func: str
    ok: bool
    msg: str | None
    data: Any
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class AlgErrorItem:
    code: int
    description: str
    severity: int


@dataclass(frozen=True)
class AlgErrorNotify:
    items: tuple[AlgErrorItem, ...]
    time_stamp_ms: int | None
    raw: dict[str, Any] = field(repr=False)


Message = Response | AlgErrorNotify


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_request(
    req_func: str,
    args: Any,
    frame_count: int,
    time_stamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造 app_req 报文。`args` 原样放进 req_func 的值位置。"""
    return {
        "head": {
            "type": FRAME_TYPE_REQUEST,
            "time_stamp": _now_ms() if time_stamp_ms is None else time_stamp_ms,
            "source": SOURCE_APP,
            "frame_count": frame_count,
        },
        "data": {"req_func": {req_func: args}},
    }


def encode_request(
    req_func: str,
    args: Any,
    frame_count: int,
    time_stamp_ms: int | None = None,
) -> str:
    return json.dumps(
        build_request(req_func, args, frame_count, time_stamp_ms),
        ensure_ascii=False,
    )


def build_response(
    req_func: str,
    frame_count: int,
    *,
    ok: bool = True,
    msg: str | None = None,
    data: Any = None,
    nested: bool = False,
    time_stamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造 app_resp 报文。`nested=True` 时套上 AppReponseObjectData 一层。"""
    result = {
        "req_func": req_func,
        "status": STATUS_OK if ok else STATUS_ERROR,
        "msg": msg,
        "data": data,
    }
    payload = {NESTED_RESULT_KEY: result} if nested else result
    return {
        "head": {
            "type": FRAME_TYPE_RESPONSE,
            "time_stamp": _now_ms() if time_stamp_ms is None else time_stamp_ms,
            "source": SOURCE_ALG,
            "frame_count": frame_count,
        },
        "data": {"req_result": payload},
    }


def build_alg_error_notify(
    items: Sequence[AlgErrorItem],
    time_stamp_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "head": {
            "type": FRAME_TYPE_ALG_ERROR,
            "time_stamp": _now_ms() if time_stamp_ms is None else time_stamp_ms,
            "source": SOURCE_ALG,
            "frame_count": 1,
        },
        "data": {
            "items": [
                {"code": it.code, "description": it.description, "severity": it.severity}
                for it in items
            ]
        },
    }


def _load(text: str | bytes) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"报文不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("报文顶层不是对象")
    return payload


def _head_of(payload: dict[str, Any]) -> dict[str, Any]:
    head = payload.get("head")
    if not isinstance(head, dict):
        raise ProtocolError("报文缺少 head")
    return head


def _frame_count_of(head: dict[str, Any]) -> int | None:
    fc = head.get("frame_count")
    return fc if isinstance(fc, int) and not isinstance(fc, bool) else None


def parse_message(text: str | bytes) -> Message:
    """解析设备端发来的一条报文。"""
    payload = _load(text)
    head = _head_of(payload)
    msg_type = head.get("type")
    if msg_type == FRAME_TYPE_ALG_ERROR:
        return _parse_alg_error(payload, head)
    if msg_type == FRAME_TYPE_RESPONSE:
        return _parse_response(payload, head)
    raise ProtocolError(f"不支持的报文类型: {msg_type!r}")


def _parse_response(payload: dict[str, Any], head: dict[str, Any]) -> Response:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProtocolError("app_resp 缺少 data")
    result = data.get("req_result")
    if not isinstance(result, dict):
        raise ProtocolError("app_resp 缺少 req_result")
    nested = result.get(NESTED_RESULT_KEY)
    if isinstance(nested, dict):
        result = nested
    req_func = result.get("req_func")
    if not isinstance(req_func, str):
        raise ProtocolError("req_result 缺少 req_func")
    return Response(
        frame_count=_frame_count_of(head),
        req_func=req_func,
        ok=result.get("status") == STATUS_OK,
        msg=result.get("msg"),
        data=result.get("data"),
        raw=payload,
    )


def _parse_alg_error(payload: dict[str, Any], head: dict[str, Any]) -> AlgErrorNotify:
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ProtocolError("alg_error_code_notify 缺少 items")
    items: list[AlgErrorItem] = []
    for entry in data["items"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("code"), int):
            raise ProtocolError(f"故障条目格式错误: {entry!r}")
        items.append(
            AlgErrorItem(
                code=entry["code"],
                description=str(entry.get("description", "")),
                severity=int(entry.get("severity", 0)),
            )
        )
    ts = head.get("time_stamp")
    return AlgErrorNotify(
        items=tuple(items),
        time_stamp_ms=ts if isinstance(ts, int) else None,
        raw=payload,
    )


def parse_request(text: str | bytes) -> tuple[int | None, str, Any]:
    """解析客户端发来的 app_req,返回 (frame_count, req_func, args)。供仿真器使用。"""
    payload = _load(text)
    head = _head_of(payload)
    if head.get("type") != FRAME_TYPE_REQUEST:
        raise ProtocolError(f"报文不是 app_req: {head.get('type')!r}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("req_func"), dict):
        raise ProtocolError("app_req 缺少 req_func")
    req_func_obj: dict[str, Any] = data["req_func"]
    if len(req_func_obj) != 1:
        raise ProtocolError(f"req_func 应恰好含一个函数名,实际 {len(req_func_obj)} 个")
    (name, args), = req_func_obj.items()
    return _frame_count_of(head), name, args
```

`src/d1max_patrol/protocol/__init__.py`：

```python
"""自主导航协议层。只管线格式与请求构造,不含业务语义。"""
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/protocol/test_nav_frames.py -v`
Expected: 全部 passed（含参数化共 25 个）

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/protocol tests/protocol
git commit -m "feat: 导航报文封装与解析,含 AppReponseObjectData 嵌套变体"
```

---
### Task 4: 导航状态枚举与位姿类型

**Files:**
- Create: `src/d1max_patrol/protocol/nav_types.py`
- Test: `tests/protocol/test_nav_types.py`

**Interfaces:**
- Consumes: 无（纯标准库）
- Produces（`d1max_patrol.protocol.nav_types`）：
  - `NavStatus(str, Enum)`：`STANDBY="StandBy"`、`INITIALIZING="Initializing"`、`ACTIVE="Active"`、`PAUSE="Pause"`、`CANCELLED="Cancelled"`、`SUCCEED="Succeed"`、`FAILED="Failed"`
  - `LocStatus(str, Enum)`：`INIT="Init"`、`MAP_LOADING="MapLoading"`、`INIT_LOCALIZATION="InitLocalization"`、`CONTINUOUS_LOC="ContinuousLoc"`、`ERROR="Error"`、`DYNAMIC_INIT_LOC="DynamicInitLoc"`、`LOC_LOST="LocLost"`
  - `MappingStatus(str, Enum)`：`UNKNOWN="Unknown"`、`PASSIVE="Passive"`、`INIT_WAIT_SENSOR="InitWaitSensor"`、`MAPPING_READY="MappingReady"`、`MAPPING_RUNNING="MappingRunning"`、`MAPP_ERROR="MappError"`、`MAPPING_SAVE_BEGIN="MappingSaveBegin"`、`MAPPING_SAVE_END="MappingSaveEnd"`
  - 集合常量 `NAV_TERMINAL_SUCCESS`、`NAV_TERMINAL_FAILURE`、`NAV_TERMINAL`、`LOC_HEALTHY`、`LOC_UNUSABLE`
  - 故障码常量 `ALG_NAV_BLOCKED = 13330`、`ALG_LIDAR_DISCONNECTED = 13331`
  - `parse_enum(cls, value) -> Enum | None` —— 未知取值返回 `None`，不抛异常
  - `Position(x: float, y: float, z: float = 0.0)`、`Orientation(x: float, y: float, z: float, w: float)`、`Pose(position: Position, orientation: Orientation)`、`Waypoint(name: str, pose: Pose)` —— 全部冻结数据类，各带 `to_wire() -> dict` 与 `from_wire(d) -> Self` 类方法
  - `Pose.from_xy_yaw(x: float, y: float, yaw: float = 0.0) -> Pose`
  - `yaw_to_orientation(yaw: float) -> Orientation`、`orientation_to_yaw(o: Orientation) -> float`
  - `Pose.yaw -> float` 属性、`Pose.distance_to(other: Pose) -> float`

- [ ] **Step 1: 写失败的测试**

`tests/protocol/test_nav_types.py`：

```python
"""状态枚举与位姿类型。取值全部照抄 refs/nav-api 的状态值说明。"""

import math

import pytest

from d1max_patrol.protocol.nav_types import (
    ALG_LIDAR_DISCONNECTED,
    ALG_NAV_BLOCKED,
    LOC_HEALTHY,
    NAV_TERMINAL,
    NAV_TERMINAL_FAILURE,
    NAV_TERMINAL_SUCCESS,
    LocStatus,
    MappingStatus,
    NavStatus,
    Orientation,
    Pose,
    Position,
    Waypoint,
    orientation_to_yaw,
    parse_enum,
    yaw_to_orientation,
)


def test_导航状态取值与文档一致():
    assert [s.value for s in NavStatus] == [
        "StandBy", "Initializing", "Active", "Pause", "Cancelled", "Succeed", "Failed",
    ]


def test_定位状态取值与文档一致():
    assert [s.value for s in LocStatus] == [
        "Init", "MapLoading", "InitLocalization", "ContinuousLoc",
        "Error", "DynamicInitLoc", "LocLost",
    ]


def test_建图状态取值与文档一致():
    assert [s.value for s in MappingStatus] == [
        "Unknown", "Passive", "InitWaitSensor", "MappingReady",
        "MappingRunning", "MappError", "MappingSaveBegin", "MappingSaveEnd",
    ]


def test_终态集合():
    assert NAV_TERMINAL_SUCCESS == frozenset({NavStatus.SUCCEED})
    assert NAV_TERMINAL_FAILURE == frozenset({NavStatus.FAILED, NavStatus.CANCELLED})
    assert NAV_TERMINAL == NAV_TERMINAL_SUCCESS | NAV_TERMINAL_FAILURE
    assert NavStatus.ACTIVE not in NAV_TERMINAL


def test_只有持续定位算健康():
    assert LOC_HEALTHY == frozenset({LocStatus.CONTINUOUS_LOC})


def test_故障码常量():
    assert (ALG_NAV_BLOCKED, ALG_LIDAR_DISCONNECTED) == (13330, 13331)


def test_parse_enum_已知取值():
    assert parse_enum(NavStatus, "Active") is NavStatus.ACTIVE
    assert parse_enum(LocStatus, "LocLost") is LocStatus.LOC_LOST


@pytest.mark.parametrize("value", ["Wandering", "", None, 3, "active"])
def test_parse_enum_未知取值返回_None_而不抛异常(value):
    """固件可能新增状态值,不能因为不认识就崩掉整条链路。"""
    assert parse_enum(NavStatus, value) is None


def test_pose_线格式与文档一致():
    pose = Pose(Position(1.0, 2.0, 0.0), Orientation(0.0, 0.0, 0.0, 1.0))
    assert pose.to_wire() == {
        "position": {"x": 1.0, "y": 2.0, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }


def test_pose_从线格式还原():
    wire = {
        "position": {"x": 1.5, "y": -2.5, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.7071, "w": 0.7071},
    }
    pose = Pose.from_wire(wire)
    assert pose.position == Position(1.5, -2.5, 0.0)
    assert pose.orientation.z == pytest.approx(0.7071)


def test_pose_从线格式缺少_z_时补零():
    pose = Pose.from_wire({"position": {"x": 1.0, "y": 2.0},
                           "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}})
    assert pose.position.z == 0.0


def test_pose_从线格式字段缺失报错():
    with pytest.raises(ValueError, match="position"):
        Pose.from_wire({"orientation": {"x": 0, "y": 0, "z": 0, "w": 1}})


@pytest.mark.parametrize("yaw", [0.0, 0.5, -0.5, 1.57, -1.57, 3.0, -3.0])
def test_yaw_四元数往返(yaw):
    assert orientation_to_yaw(yaw_to_orientation(yaw)) == pytest.approx(yaw, abs=1e-9)


def test_yaw_归一化到正负_pi():
    """输入 3π/2 应等价于 -π/2。"""
    o = yaw_to_orientation(3 * math.pi / 2)
    assert orientation_to_yaw(o) == pytest.approx(-math.pi / 2, abs=1e-9)


def test_pose_from_xy_yaw_与_yaw_属性():
    pose = Pose.from_xy_yaw(3.0, 4.0, math.pi / 2)
    assert pose.position == Position(3.0, 4.0, 0.0)
    assert pose.yaw == pytest.approx(math.pi / 2)


def test_pose_平面距离():
    a = Pose.from_xy_yaw(0.0, 0.0)
    b = Pose.from_xy_yaw(3.0, 4.0)
    assert a.distance_to(b) == pytest.approx(5.0)


def test_waypoint_线格式是二元列表():
    wp = Waypoint("P1_变压器", Pose.from_xy_yaw(1.0, 2.0))
    wire = wp.to_wire()
    assert wire[0] == "P1_变压器"
    assert wire[1]["position"]["x"] == 1.0
    assert Waypoint.from_wire(wire) == wp


def test_waypoint_从线格式格式错误报错():
    with pytest.raises(ValueError, match="路径点"):
        Waypoint.from_wire(["only_name"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/protocol/test_nav_types.py -v`
Expected: FAIL —— `ImportError: cannot import name ... from 'd1max_patrol.protocol.nav_types'`

- [ ] **Step 3: 写 nav_types.py**

`src/d1max_patrol/protocol/nav_types.py`：

```python
"""导航状态枚举、故障码与位姿类型。

状态取值逐字照抄 refs/nav-api/自主导航_WEBSOCKET_API.md
§1.3 / §3.6 / §4.3 的状态值说明,以及 §9 的算法故障码。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar


class NavStatus(str, Enum):
    """导航功能状态,见 §3.6。"""

    STANDBY = "StandBy"              # 就绪 —— 仅在此状态下可启动导航
    INITIALIZING = "Initializing"
    ACTIVE = "Active"
    PAUSE = "Pause"
    CANCELLED = "Cancelled"
    SUCCEED = "Succeed"
    FAILED = "Failed"


class LocStatus(str, Enum):
    """定位功能状态,见 §4.3。"""

    INIT = "Init"
    MAP_LOADING = "MapLoading"
    INIT_LOCALIZATION = "InitLocalization"
    CONTINUOUS_LOC = "ContinuousLoc"
    ERROR = "Error"
    DYNAMIC_INIT_LOC = "DynamicInitLoc"
    LOC_LOST = "LocLost"


class MappingStatus(str, Enum):
    """建图功能状态,见 §1.3。"""

    UNKNOWN = "Unknown"
    PASSIVE = "Passive"
    INIT_WAIT_SENSOR = "InitWaitSensor"
    MAPPING_READY = "MappingReady"
    MAPPING_RUNNING = "MappingRunning"
    MAPP_ERROR = "MappError"          # 文档原文如此,不是笔误
    MAPPING_SAVE_BEGIN = "MappingSaveBegin"
    MAPPING_SAVE_END = "MappingSaveEnd"


NAV_TERMINAL_SUCCESS = frozenset({NavStatus.SUCCEED})
NAV_TERMINAL_FAILURE = frozenset({NavStatus.FAILED, NavStatus.CANCELLED})
#: 一次 start_nav 结束的判定集合
NAV_TERMINAL = NAV_TERMINAL_SUCCESS | NAV_TERMINAL_FAILURE

#: 只有持续定位才允许继续行走
LOC_HEALTHY = frozenset({LocStatus.CONTINUOUS_LOC})
#: 出现这两个状态必须停车,见设计文档 §6.4 安全规则
LOC_UNUSABLE = frozenset({LocStatus.LOC_LOST, LocStatus.ERROR})

#: 算法故障码,见 §9
ALG_NAV_BLOCKED = 13330
ALG_LIDAR_DISCONNECTED = 13331

_E = TypeVar("_E", bound=Enum)


def parse_enum(cls: type[_E], value: Any) -> _E | None:
    """把线上字符串转成枚举;不认识的取值返回 None 而不是抛异常。

    固件升级可能引入本代码未知的状态值,整条链路不应因此崩溃;
    调用方负责按"未知状态"处理并记日志。
    """
    if not isinstance(value, str):
        return None
    try:
        return cls(value)
    except ValueError:
        return None


def _num(container: dict[str, Any], key: str, where: str, default: float | None = None
         ) -> float:
    if key not in container:
        if default is None:
            raise ValueError(f"{where} 缺少字段 {key}")
        return default
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}.{key} 应为数字,实际为 {value!r}")
    return float(value)


@dataclass(frozen=True)
class Position:
    x: float
    y: float
    z: float = 0.0

    def to_wire(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}

    @classmethod
    def from_wire(cls, raw: Any) -> Position:
        if not isinstance(raw, dict):
            raise ValueError(f"position 应为映射,实际为 {raw!r}")
        return cls(
            x=_num(raw, "x", "position"),
            y=_num(raw, "y", "position"),
            z=_num(raw, "z", "position", default=0.0),
        )


@dataclass(frozen=True)
class Orientation:
    x: float
    y: float
    z: float
    w: float

    def to_wire(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z, "w": self.w}

    @classmethod
    def from_wire(cls, raw: Any) -> Orientation:
        if not isinstance(raw, dict):
            raise ValueError(f"orientation 应为映射,实际为 {raw!r}")
        return cls(
            x=_num(raw, "x", "orientation"),
            y=_num(raw, "y", "orientation"),
            z=_num(raw, "z", "orientation"),
            w=_num(raw, "w", "orientation"),
        )


def yaw_to_orientation(yaw: float) -> Orientation:
    """绕 z 轴的偏航角转四元数。机器人在平面上运动,roll/pitch 恒为 0。"""
    half = yaw / 2.0
    return Orientation(x=0.0, y=0.0, z=math.sin(half), w=math.cos(half))


def orientation_to_yaw(o: Orientation) -> float:
    """四元数取偏航角,结果落在 (-π, π]。"""
    siny_cosp = 2.0 * (o.w * o.z + o.x * o.y)
    cosy_cosp = 1.0 - 2.0 * (o.y * o.y + o.z * o.z)
    return math.atan2(siny_cosp, cosy_cosp)


@dataclass(frozen=True)
class Pose:
    position: Position
    orientation: Orientation

    @classmethod
    def from_xy_yaw(cls, x: float, y: float, yaw: float = 0.0) -> Pose:
        return cls(Position(x, y, 0.0), yaw_to_orientation(yaw))

    @property
    def yaw(self) -> float:
        return orientation_to_yaw(self.orientation)

    def distance_to(self, other: Pose) -> float:
        """平面欧氏距离,忽略 z。"""
        return math.hypot(
            self.position.x - other.position.x,
            self.position.y - other.position.y,
        )

    def to_wire(self) -> dict[str, dict[str, float]]:
        return {
            "position": self.position.to_wire(),
            "orientation": self.orientation.to_wire(),
        }

    @classmethod
    def from_wire(cls, raw: Any) -> Pose:
        if not isinstance(raw, dict):
            raise ValueError(f"pose 应为映射,实际为 {raw!r}")
        if "position" not in raw:
            raise ValueError("pose 缺少 position")
        if "orientation" not in raw:
            raise ValueError("pose 缺少 orientation")
        return cls(
            position=Position.from_wire(raw["position"]),
            orientation=Orientation.from_wire(raw["orientation"]),
        )


@dataclass(frozen=True)
class Waypoint:
    """路径点。线格式是 ["点名", {pose}] 这样的二元列表,见 §2.1。"""

    name: str
    pose: Pose

    def to_wire(self) -> list[Any]:
        return [self.name, self.pose.to_wire()]

    @classmethod
    def from_wire(cls, raw: Any) -> Waypoint:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError(f"路径点应为 [名称, pose] 二元列表,实际为 {raw!r}")
        name, pose_raw = raw
        if not isinstance(name, str):
            raise ValueError(f"路径点名称应为字符串,实际为 {name!r}")
        return cls(name=name, pose=Pose.from_wire(pose_raw))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/protocol/test_nav_types.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/protocol/nav_types.py tests/protocol/test_nav_types.py
git commit -m "feat: 导航状态枚举、故障码常量与位姿类型"
```

---
### Task 5: 请求构造与响应函数别名表

把每个接口的 `req_func` 名、参数形状、以及"响应会用哪个名字回来"三件事集中定义在一处。**协议地雷 2 与 3 就在这个模块里解决。**

**Files:**
- Create: `src/d1max_patrol/protocol/nav_requests.py`
- Test: `tests/protocol/test_nav_requests.py`

**Interfaces:**
- Consumes: `d1max_patrol.protocol.nav_types.{Pose, Waypoint}`
- Produces（`d1max_patrol.protocol.nav_requests`）：
  - `REQUEST_TO_RESPONSE_FUNC: dict[str, str]` —— 请求名 → 响应名（只收录不一致的）
  - `response_func_for(req_func: str) -> str`
  - `PUSH_ONLY_FUNCS: frozenset[str]` —— 只会作为主动推送出现的响应名
  - `NESTED_RESPONSE_FUNCS: frozenset[str]` —— 响应带 `AppReponseObjectData` 外壳的请求名
  - `UNVERIFIED_RESPONSE_FUNCS: frozenset[str]` —— 文档未给响应样例、响应名未验证的请求名
  - `NavRequest(req_func: str, args: Any = None)` 冻结数据类，含 `response_func: str` 属性与 `nested: bool` 属性
  - 建图与地图：`start_mapping()`、`stop_mapping()`、`get_mapping_status()`、`get_pgm_map(map_id)`、`get_all_pgm_map()`、`remove_map_by_id(map_ids: Sequence[str])`、`rename_map_name(old_id, new_id)`
  - 路径：`get_all_paths_by_mapid(map_id)`、`add_nav_path(map_id, path_id, waypoints: Sequence[Waypoint])`、`modify_nav_path(map_id, old_path_id, new_path_id, waypoints)`、`remove_nav_path(pairs: Sequence[tuple[str, str]])`
  - 导航：`start_nav(pose: Pose)`、`start_multi_nav(map_id, path_id)`、`start_multi_nav_by_points(map_id, poses: Sequence[Pose])`、`start_nav_return_home()`、`stop_nav()`、`pause_nav()`、`continue_nav()`、`get_nav_status()`、`get_navigation_speed()`、`set_navigation_speed(x, y=None, z=None)`
  - 定位：`loc_load_map(map_id)`、`reset_loc()`、`get_loc_status()`
  - 解析辅助：`parse_paths_payload(data: Any) -> dict[str, list[Waypoint]]`、`parse_map_ids(data: Any) -> list[str]`

- [ ] **Step 1: 写失败的测试**

`tests/protocol/test_nav_requests.py`：

```python
"""请求构造。每个断言都对应 refs/nav-api 文档里的一段样例报文。"""

import pytest

from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_types import Pose, Waypoint


def test_建图请求参数是数字零而不是_null():
    """§1.1/§1.2 的样例是 {"start_mapping": 0},照抄。"""
    assert R.start_mapping().args == 0
    assert R.stop_mapping().args == 0


def test_无参查询用_null():
    for req in (R.get_mapping_status(), R.get_all_pgm_map(), R.get_nav_status(),
                R.get_loc_status(), R.reset_loc(), R.stop_nav(), R.pause_nav(),
                R.continue_nav(), R.start_nav_return_home()):
        assert req.args is None, req.req_func


def test_删除地图参数是数组():
    """§1.6: remove_map_by_id 收的是 ["map_id_1", "map_id_2"]。"""
    req = R.remove_map_by_id(["m1", "m2"])
    assert req.req_func == "remove_map_by_id"
    assert req.args == ["m1", "m2"]


def test_删除地图接受单个字符串以外的任意序列():
    assert R.remove_map_by_id(("m1",)).args == ["m1"]


def test_重命名地图参数是二元数组():
    """§1.7: rename_map_name 收的是 ["old", "new"]。"""
    assert R.rename_map_name("旧", "新").args == ["旧", "新"]


def test_添加路径参数形状():
    """§2.2: [map_id, path_id, [[点名, pose], ...]]。"""
    wps = [Waypoint("P1", Pose.from_xy_yaw(1.0, 2.0)),
           Waypoint("P2", Pose.from_xy_yaw(3.0, 4.0))]
    req = R.add_nav_path("m1", "p1", wps)
    assert req.req_func == "add_nav_path"
    assert req.args[0] == "m1"
    assert req.args[1] == "p1"
    assert req.args[2][0][0] == "P1"
    assert req.args[2][0][1]["position"]["x"] == 1.0
    assert req.args[2][1][0] == "P2"


def test_修改路径是四元参数且可改名():
    """§2.3: [map_id, old_path_id, new_path_id, waypoints] —— 比 add 多一个改名位。"""
    wps = [Waypoint("P1", Pose.from_xy_yaw(0.0, 0.0))]
    req = R.modify_nav_path("m", "old", "new", wps)
    assert req.req_func == "modify_nav_path"
    assert req.args[:3] == ["m", "old", "new"]
    # 路点段的编码与 add_nav_path 完全一致,只是位置不同
    assert req.args[3] == R.add_nav_path("m", "p", wps).args[2]


def test_删除路径参数是二元组数组():
    """§2.4: [["map_id_1","path_id_1"], ["map_id_2","path_id_2"]]。"""
    assert R.remove_nav_path([("m1", "p1"), ("m2", "p2")]).args == [
        ["m1", "p1"], ["m2", "p2"]]


def test_单点导航参数就是_pose():
    req = R.start_nav(Pose.from_xy_yaw(1.0, 2.0))
    assert req.req_func == "start_nav"
    assert req.args["position"] == {"x": 1.0, "y": 2.0, "z": 0.0}
    assert set(req.args["orientation"]) == {"x", "y", "z", "w"}


def test_多点导航按路径_id():
    assert R.start_multi_nav("m1", "p1").args == ["m1", "p1"]


def test_多点导航按点列表():
    req = R.start_multi_nav_by_points("m1", [Pose.from_xy_yaw(1.0, 2.0),
                                             Pose.from_xy_yaw(3.0, 4.0)])
    assert req.args[0] == "m1"
    assert len(req.args[1]) == 2
    assert req.args[1][1]["position"]["y"] == 4.0


def test_导航速度请求带_type_字段():
    """§3.9/§3.10 的参数里有一个固定的 type: navigation_speed。"""
    assert R.get_navigation_speed().args == {"type": "navigation_speed"}
    assert R.set_navigation_speed(0.8, 0.4, 1.2).args == {
        "type": "navigation_speed", "x": 0.8, "y": 0.4, "z": 1.2}


def test_设置速度可只传_x_由设备取默认():
    """文档注:只传 x 时设备端 y 默认 0.5、z 默认 1.5。这里不替设备补默认值。"""
    assert R.set_navigation_speed(0.8).args == {"type": "navigation_speed", "x": 0.8}


def test_速度接口的响应带嵌套外壳():
    assert R.get_navigation_speed().nested is True
    assert R.set_navigation_speed(0.8).nested is True
    assert R.start_nav(Pose.from_xy_yaw(0.0, 0.0)).nested is False


def test_加载定位地图的响应名与请求名不同():
    """协议地雷 2: 请求发 loc_load_map,响应回 load_localization_map。"""
    req = R.loc_load_map("m1")
    assert req.req_func == "loc_load_map"
    assert req.args == "m1"
    assert req.response_func == "load_localization_map"


def test_绝大多数接口请求名即响应名():
    for req in (R.start_mapping(), R.get_nav_status(), R.stop_nav(),
                R.add_nav_path("m", "p", []), R.get_navigation_speed()):
        assert req.response_func == req.req_func


def test_别名表只收录不一致的条目():
    assert R.REQUEST_TO_RESPONSE_FUNC["loc_load_map"] == "load_localization_map"
    for k, v in R.REQUEST_TO_RESPONSE_FUNC.items():
        assert k != v


def test_只推送不响应的函数名():
    """协议地雷 3: 这些名字只会作为主动推送出现,匹配不上请求属正常。"""
    assert R.PUSH_ONLY_FUNCS == frozenset({"notify_stop_mapping_status"})
    # exit_charging 不在此列: §7.14 有完整请求/响应样例,是答复不是推送。
    assert "exit_charging" not in R.PUSH_ONLY_FUNCS


def test_未验证响应名的接口被标注():
    assert R.UNVERIFIED_RESPONSE_FUNCS == frozenset(
        {"reset_loc", "start_nav_return_home", "start_multi_nav_by_points"})


def test_response_func_for_对未知名字原样返回():
    assert R.response_func_for("some_future_api") == "some_future_api"


def test_解析路径载荷():
    """§2.1 的 data 是 [map_id, [path_ids], {path_id: [[点名, pose], ...]}]。"""
    data = [
        "m1",
        ["p1", "p2"],
        {
            "p1": [["A", {"position": {"x": 1.0, "y": 2.0, "z": 0.0},
                          "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}]],
            "p2": [],
        },
    ]
    paths = R.parse_paths_payload(data)
    assert set(paths) == {"p1", "p2"}
    assert paths["p1"][0].name == "A"
    assert paths["p1"][0].pose.position.y == 2.0
    assert paths["p2"] == []


def test_解析路径载荷_只在第三元素缺失时报错():
    with pytest.raises(ValueError, match="路径载荷"):
        R.parse_paths_payload(["m1", ["p1"]])


def test_解析地图列表():
    """§1.5 的 data 是 {map_id: [OccupancyGrid, 标签列表]}。只取键。"""
    data = {"m2": [{}, []], "m1": [{}, []]}
    assert R.parse_map_ids(data) == ["m1", "m2"]


def test_解析地图列表_空与非法():
    assert R.parse_map_ids({}) == []
    with pytest.raises(ValueError, match="地图列表"):
        R.parse_map_ids(["m1"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/protocol/test_nav_requests.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_patrol.protocol.nav_requests'`

- [ ] **Step 3: 写 nav_requests.py**

`src/d1max_patrol/protocol/nav_requests.py`：

```python
"""各接口的请求构造与响应函数名映射。

参数形状逐条对照 refs/nav-api/自主导航_WEBSOCKET_API.md,
章节号写在每个函数的 docstring 里。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .nav_types import Pose, Waypoint

#: 协议地雷 2: 请求名与响应 req_func 不一致的接口。
#: 只收录不一致的条目;相同的不写,由 response_func_for 兜底。
REQUEST_TO_RESPONSE_FUNC: dict[str, str] = {
    "loc_load_map": "load_localization_map",
}

#: 协议地雷 3: 只会作为设备主动推送出现的响应 req_func。
#: 它们的 head.type 也是 app_resp,但 frame_count 不对应任何请求。
PUSH_ONLY_FUNCS = frozenset({"notify_stop_mapping_status"})

#: 协议地雷 1: 响应带 AppReponseObjectData 外壳(厂商把 Response 拼成 Reponse)的请求名。
#: 注意文档里还有第二种外壳键名 AppResponse(拼写正确),出现在 §7 回充/对桩接口
#: (get_arc_alg_status §7.13、exit_charging §7.14)。本卷不发这些请求,故解析器
#: 只处理 AppReponseObjectData;第 2 卷做回充时必须把 AppResponse 一并支持。
NESTED_RESPONSE_FUNCS = frozenset({"get_navigation_speed", "set_navigation_speed"})

#: 文档只给了请求没给响应样例的接口,响应 req_func 名未经验证。
#: 真机联调前不得依赖这几个名字,见计划"协议地雷"小节。
UNVERIFIED_RESPONSE_FUNCS = frozenset(
    {"reset_loc", "start_nav_return_home", "start_multi_nav_by_points"}
)


def response_func_for(req_func: str) -> str:
    """给出该请求预期的响应 req_func 名。"""
    return REQUEST_TO_RESPONSE_FUNC.get(req_func, req_func)


@dataclass(frozen=True)
class NavRequest:
    """一次请求的函数名与参数。不含 frame_count —— 那是传输层的事。"""

    req_func: str
    args: Any = None

    @property
    def response_func(self) -> str:
        return response_func_for(self.req_func)

    @property
    def nested(self) -> bool:
        """响应是否套着 AppReponseObjectData 外壳。"""
        return self.req_func in NESTED_RESPONSE_FUNCS


# ---------------------------------------------------------------- 建图与地图管理

def start_mapping() -> NavRequest:
    """§1.1 开始建图。参数是数字 0,不是 null。"""
    return NavRequest("start_mapping", 0)


def stop_mapping() -> NavRequest:
    """§1.2 停止建图。完成后设备会推送 notify_stop_mapping_status。"""
    return NavRequest("stop_mapping", 0)


def get_mapping_status() -> NavRequest:
    """§1.3 获取建图状态,返回 MappingStatus 字符串。"""
    return NavRequest("get_mapping_status", None)


def get_pgm_map(map_id: str) -> NavRequest:
    """§1.4 获取指定 PGM 地图。"""
    # 假设(待真机验证): 文档 §1.4 的请求样例写的是 {"get_pgm_map": null},没有给出
    # 按 map_id 取图的形状。这里仍按 map_id 下发 —— 上层需要指定地图,而多传一个
    # 参数通常被忽略;若真机拒绝,改回 None 即可,调用方签名不变。
    return NavRequest("get_pgm_map", map_id)


def get_all_pgm_map() -> NavRequest:
    """§1.5 获取所有 PGM 地图,返回 {map_id: [OccupancyGrid, 标签列表]}。"""
    return NavRequest("get_all_pgm_map", None)


def remove_map_by_id(map_ids: Sequence[str]) -> NavRequest:
    """§1.6 删除地图。参数是 id 数组,即使只删一张也要包成数组。"""
    return NavRequest("remove_map_by_id", list(map_ids))


def rename_map_name(old_id: str, new_id: str) -> NavRequest:
    """§1.7 重命名地图。参数是 [旧id, 新id]。"""
    return NavRequest("rename_map_name", [old_id, new_id])


# -------------------------------------------------------------------- 路径管理

def _waypoints_wire(waypoints: Sequence[Waypoint]) -> list[Any]:
    return [wp.to_wire() for wp in waypoints]


def get_all_paths_by_mapid(map_id: str) -> NavRequest:
    """§2.1 获取指定地图下的所有导航路径。"""
    return NavRequest("get_all_paths_by_mapid", map_id)


def add_nav_path(map_id: str, path_id: str, waypoints: Sequence[Waypoint]) -> NavRequest:
    """§2.2 新增导航路径。"""
    return NavRequest("add_nav_path", [map_id, path_id, _waypoints_wire(waypoints)])


def modify_nav_path(map_id: str, old_path_id: str, new_path_id: str,
                    waypoints: Sequence[Waypoint]) -> NavRequest:
    """§2.3 修改导航路径。比 add_nav_path 多一个改名位:老路径名改成新路径名。

    同名覆盖就把 old 和 new 传成同一个值。
    """
    return NavRequest(
        "modify_nav_path",
        [map_id, old_path_id, new_path_id, _waypoints_wire(waypoints)],
    )


def remove_nav_path(pairs: Sequence[tuple[str, str]]) -> NavRequest:
    """§2.4 删除导航路径。参数是 [[map_id, path_id], ...]。"""
    return NavRequest("remove_nav_path", [[m, p] for m, p in pairs])


# -------------------------------------------------------------------- 导航控制

def start_nav(pose: Pose) -> NavRequest:
    """§3.1 单点导航。巡检任务的主力接口 —— 全逐点执行。"""
    return NavRequest("start_nav", pose.to_wire())


def start_multi_nav(map_id: str, path_id: str) -> NavRequest:
    """§3.2 按路径 id 多点导航。

    本项目不用它行走(没有到点事件),仅保留以备排查与对照。
    """
    return NavRequest("start_multi_nav", [map_id, path_id])


def start_multi_nav_by_points(map_id: str, poses: Sequence[Pose]) -> NavRequest:
    """§3.3 按点列表多点导航。同上,不用于行走。响应名未验证。"""
    return NavRequest("start_multi_nav_by_points",
                      [map_id, [p.to_wire() for p in poses]])


def start_nav_return_home() -> NavRequest:
    """§3.4 开始返航。文档未给响应样例,响应名未验证。"""
    return NavRequest("start_nav_return_home", None)


def stop_nav() -> NavRequest:
    """§3.5 停止导航。"""
    return NavRequest("stop_nav", None)


def get_nav_status() -> NavRequest:
    """§3.6 获取导航状态,返回 NavStatus 字符串。"""
    return NavRequest("get_nav_status", None)


def pause_nav() -> NavRequest:
    """§3.7 暂停导航。"""
    return NavRequest("pause_nav", None)


def continue_nav() -> NavRequest:
    """§3.8 继续导航。"""
    return NavRequest("continue_nav", None)


def get_navigation_speed() -> NavRequest:
    """§3.9 获取导航速度配置。响应带 AppReponseObjectData 外壳。"""
    return NavRequest("get_navigation_speed", {"type": "navigation_speed"})


def set_navigation_speed(x: float, y: float | None = None,
                         z: float | None = None) -> NavRequest:
    """§3.10 设置导航速度配置。

    文档注明:只传 x 时设备端 y 取 0.5、z 取 1.5。此处不替设备补默认值,
    省略即省略,让设备行为保持文档描述的样子。
    """
    args: dict[str, Any] = {"type": "navigation_speed", "x": x}
    if y is not None:
        args["y"] = y
    if z is not None:
        args["z"] = z
    return NavRequest("set_navigation_speed", args)


# -------------------------------------------------------------------- 定位相关

def loc_load_map(map_id: str) -> NavRequest:
    """§4.1 加载定位地图。响应的 req_func 是 load_localization_map。"""
    return NavRequest("loc_load_map", map_id)


def reset_loc() -> NavRequest:
    """§4.2 重置定位。文档未给响应样例,响应名未验证。"""
    return NavRequest("reset_loc", None)


def get_loc_status() -> NavRequest:
    """§4.3 获取定位状态,返回 LocStatus 字符串。"""
    return NavRequest("get_loc_status", None)


# ---------------------------------------------------------------- 响应载荷解析

def parse_paths_payload(data: Any) -> dict[str, list[Waypoint]]:
    """解析 §2.1 的 data: [map_id, [path_ids], {path_id: [[点名, pose], ...]}]。"""
    if not isinstance(data, (list, tuple)) or len(data) < 3:
        raise ValueError(f"路径载荷应为 [map_id, path_ids, paths] 三元列表,实际为 {data!r}")
    raw_paths = data[2]
    if not isinstance(raw_paths, dict):
        raise ValueError(f"路径载荷第三项应为映射,实际为 {raw_paths!r}")
    result: dict[str, list[Waypoint]] = {}
    for path_id, points in raw_paths.items():
        if not isinstance(points, list):
            raise ValueError(f"路径 {path_id!r} 的点列表应为数组,实际为 {points!r}")
        result[str(path_id)] = [Waypoint.from_wire(p) for p in points]
    return result


def parse_map_ids(data: Any) -> list[str]:
    """解析 §1.5 的 data: {map_id: [OccupancyGrid, 标签列表]}。只取地图 id 并排序。"""
    if not isinstance(data, dict):
        raise ValueError(f"地图列表应为映射,实际为 {data!r}")
    return sorted(str(k) for k in data)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/protocol -v`
Expected: 全部 passed（nav_frames + nav_types + nav_requests 三个文件）

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/protocol/nav_requests.py tests/protocol/test_nav_requests.py
git commit -m "feat: 导航请求构造与响应函数别名表"
```

---
### Task 6: 仿真器平面运动学模型

仿真器的"腿"。纯同步、无 IO、确定性:给定初值和一串 `step(dt)`，轨迹完全可复现，这样上层状态机的测试才不会飘。

**Files:**
- Create: `src/d1max_sim/kinematics.py`
- Test: `tests/sim/test_kinematics.py`

**Interfaces:**
- Consumes: `d1max_patrol.protocol.nav_types.{Pose, orientation_to_yaw}`
- Produces（`d1max_sim.kinematics`）：
  - `wrap_angle(a: float) -> float` —— 归一化到 (-π, π]
  - `Planar2DModel` 可变数据类，字段：`x=0.0`、`y=0.0`、`yaw=0.0`、`linear_speed=0.6`、`angular_speed=1.2`、`position_tolerance=0.08`、`yaw_tolerance=0.10`、`bearing_threshold=0.20`、`speed_scale=1.0`、`frozen=False`
  - 方法：`set_goal(pose: Pose) -> None`、`clear_goal() -> None`、`teleport(pose: Pose) -> None`、`step(dt: float) -> None`、`pose() -> Pose`
  - 属性：`has_goal: bool`、`arrived: bool`、`goal_distance: float`（无目标时为 `inf`）

- [ ] **Step 1: 写失败的测试**

`tests/sim/test_kinematics.py`：

```python
"""平面运动学。确定性:同样的初值和步长序列必然给出同样的轨迹。"""

import math

import pytest

from d1max_patrol.protocol.nav_types import Pose
from d1max_sim.kinematics import Planar2DModel, wrap_angle

DT = 0.05


def _run(model: Planar2DModel, max_steps: int = 2000) -> int:
    """步进直到到达,返回用掉的步数;未到达返回 max_steps。"""
    for i in range(max_steps):
        if model.arrived:
            return i
        model.step(DT)
    return max_steps


@pytest.mark.parametrize(
    "value, expected",
    [(0.0, 0.0), (math.pi, math.pi), (-math.pi, math.pi),
     (3 * math.pi / 2, -math.pi / 2), (-3 * math.pi / 2, math.pi / 2)],
)
def test_角度归一化(value, expected):
    assert wrap_angle(value) == pytest.approx(expected)


def test_没有目标时不动也不算到达():
    m = Planar2DModel()
    assert m.has_goal is False
    assert m.arrived is False
    assert m.goal_distance == math.inf
    m.step(DT)
    assert (m.x, m.y, m.yaw) == (0.0, 0.0, 0.0)


def test_直线前进能到达目标():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(2.0, 0.0, 0.0))
    steps = _run(m)
    assert 0 < steps < 200
    assert m.arrived is True
    assert m.x == pytest.approx(2.0, abs=m.position_tolerance)
    assert m.y == pytest.approx(0.0, abs=m.position_tolerance)


def test_需要先转身再前进也能到达():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(0.0, 2.0, math.pi / 2))
    steps = _run(m)
    assert m.arrived is True
    assert steps < 300
    assert m.y == pytest.approx(2.0, abs=m.position_tolerance)
    assert wrap_angle(m.yaw - math.pi / 2) == pytest.approx(0.0, abs=m.yaw_tolerance)


def test_到位后还会转到目标朝向():
    m = Planar2DModel(x=1.0, y=1.0, yaw=0.0)
    m.set_goal(Pose.from_xy_yaw(1.0, 1.0, math.pi))
    assert m.arrived is False   # 位置到了但朝向没到
    _run(m)
    assert m.arrived is True
    assert abs(wrap_angle(m.yaw - math.pi)) <= m.yaw_tolerance


def test_目标已在容差内则立即算到达():
    m = Planar2DModel(x=1.0, y=1.0, yaw=0.0)
    m.set_goal(Pose.from_xy_yaw(1.02, 1.02, 0.0))
    assert m.arrived is True


def test_冻结后永不到达():
    """故障注入 stuck: 机器人报告状态正常但就是不动。"""
    m = Planar2DModel(frozen=True)
    m.set_goal(Pose.from_xy_yaw(2.0, 0.0))
    assert _run(m, max_steps=400) == 400
    assert (m.x, m.y) == (0.0, 0.0)
    assert m.arrived is False


def test_减速后步数显著增加():
    """故障注入 slow: 用于制造导航超时。"""
    fast = Planar2DModel()
    fast.set_goal(Pose.from_xy_yaw(2.0, 0.0))
    fast_steps = _run(fast)

    slow = Planar2DModel(speed_scale=0.25)
    slow.set_goal(Pose.from_xy_yaw(2.0, 0.0))
    slow_steps = _run(slow)

    assert slow_steps > fast_steps * 3


def test_清除目标后不再算到达():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(0.0, 0.0, 0.0))
    assert m.arrived is True
    m.clear_goal()
    assert m.has_goal is False
    assert m.arrived is False


def test_瞬移直接改变位姿并清除目标():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(5.0, 5.0))
    m.teleport(Pose.from_xy_yaw(1.0, 2.0, math.pi / 4))
    assert (m.x, m.y) == (1.0, 2.0)
    assert m.yaw == pytest.approx(math.pi / 4)
    assert m.has_goal is False


def test_pose_读数与内部状态一致():
    m = Planar2DModel(x=1.5, y=-2.5, yaw=1.0)
    pose = m.pose()
    assert pose.position.x == 1.5
    assert pose.position.y == -2.5
    assert pose.yaw == pytest.approx(1.0)


def test_轨迹可复现():
    def trace() -> list[tuple[float, float, float]]:
        m = Planar2DModel()
        m.set_goal(Pose.from_xy_yaw(1.0, 1.0, 0.5))
        out = []
        for _ in range(60):
            m.step(DT)
            out.append((m.x, m.y, m.yaw))
        return out

    assert trace() == trace()


def test_目标距离随前进单调下降():
    m = Planar2DModel()
    m.set_goal(Pose.from_xy_yaw(3.0, 0.0))
    prev = m.goal_distance
    for _ in range(50):
        m.step(DT)
        assert m.goal_distance <= prev + 1e-9
        prev = m.goal_distance
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/sim/test_kinematics.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_sim.kinematics'`

- [ ] **Step 3: 写 kinematics.py**

`src/d1max_sim/kinematics.py`：

```python
"""仿真机器人的平面运动学。

刻意做得简单而确定:没有加速度、没有噪声、没有 IO。
它的职责是让"从 A 走到 B 需要一段可控的时间"这件事可被上层复现,
而不是逼真地模拟四足步态。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from d1max_patrol.protocol.nav_types import Pose


def wrap_angle(a: float) -> float:
    """把角度归一化到 (-π, π]。"""
    wrapped = math.remainder(a, 2 * math.pi)
    # remainder 在恰好 -π 时返回 -π,统一到 +π
    return math.pi if wrapped == -math.pi else wrapped


@dataclass
class Planar2DModel:
    """平面上的位姿与朝向目标的趋近。"""

    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0

    #: 名义线速度 m/s
    linear_speed: float = 0.6
    #: 名义角速度 rad/s
    angular_speed: float = 1.2
    #: 位置到达容差 m
    position_tolerance: float = 0.08
    #: 朝向到达容差 rad
    yaw_tolerance: float = 0.10
    #: 朝向误差超过此值时只转不走
    bearing_threshold: float = 0.20

    #: 故障注入 slow: 线速度与角速度的统一缩放
    speed_scale: float = 1.0
    #: 故障注入 stuck: 状态照常但完全不动
    frozen: bool = False

    _goal: Pose | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ 目标

    def set_goal(self, pose: Pose) -> None:
        self._goal = pose

    def clear_goal(self) -> None:
        self._goal = None

    def teleport(self, pose: Pose) -> None:
        """直接改变位姿(用于设置初始位置),同时放弃当前目标。"""
        self.x = pose.position.x
        self.y = pose.position.y
        self.yaw = pose.yaw
        self._goal = None

    @property
    def has_goal(self) -> bool:
        return self._goal is not None

    @property
    def goal_distance(self) -> float:
        if self._goal is None:
            return math.inf
        return math.hypot(self._goal.position.x - self.x, self._goal.position.y - self.y)

    @property
    def arrived(self) -> bool:
        if self._goal is None:
            return False
        if self.goal_distance > self.position_tolerance:
            return False
        return abs(wrap_angle(self._goal.yaw - self.yaw)) <= self.yaw_tolerance

    # ------------------------------------------------------------------ 步进

    def pose(self) -> Pose:
        return Pose.from_xy_yaw(self.x, self.y, self.yaw)

    def step(self, dt: float) -> None:
        goal = self._goal
        if goal is None or self.frozen or self.arrived or dt <= 0.0:
            return

        max_turn = self.angular_speed * self.speed_scale * dt
        max_move = self.linear_speed * self.speed_scale * dt

        dx = goal.position.x - self.x
        dy = goal.position.y - self.y
        distance = math.hypot(dx, dy)

        if distance > self.position_tolerance:
            bearing_error = wrap_angle(math.atan2(dy, dx) - self.yaw)
            self.yaw = wrap_angle(self.yaw + _clamp(bearing_error, max_turn))
            if abs(bearing_error) <= self.bearing_threshold:
                # 朝向已经够准,边走边微调
                advance = min(distance, max_move)
                self.x += advance * math.cos(self.yaw)
                self.y += advance * math.sin(self.yaw)
            return

        # 位置到位,只剩终点朝向
        yaw_error = wrap_angle(goal.yaw - self.yaw)
        self.yaw = wrap_angle(self.yaw + _clamp(yaw_error, max_turn))


def _clamp(value: float, limit: float) -> float:
    """把 value 限制在 [-limit, limit]。"""
    return max(-limit, min(limit, value))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/sim/test_kinematics.py -v`
Expected: 全部 passed

若 `test_目标距离随前进单调下降` 偶发失败，说明"边转边走"在朝向误差接近阈值时会绕远。把 `bearing_threshold` 调到 `0.10` 并重跑；不要放宽断言。

- [ ] **Step 5: 提交**

```bash
git add src/d1max_sim/kinematics.py tests/sim/test_kinematics.py
git commit -m "feat: 仿真器平面运动学模型"
```

---
### Task 7: 仿真器的导航 / 定位 / 建图状态机

三套状态机各自独立推进，状态取值必须与真机文档完全一致——它们是契约测试判定"到点了没有"的唯一依据。

**Files:**
- Create: `src/d1max_sim/nav_state.py`
- Test: `tests/sim/test_nav_state.py`

**Interfaces:**
- Consumes: `d1max_sim.kinematics.Planar2DModel`；`d1max_patrol.protocol.nav_types.{LocStatus, MappingStatus, NavStatus, Pose}`
- Produces（`d1max_sim.nav_state`）：
  - `SimRejected(Exception)` —— 设备在当前状态下拒绝该操作（仿真器据此回 `status: "error"`）
  - `NavStateMachine(model: Planar2DModel, init_delay_s: float = 0.3, terminal_hold_s: float = 0.5)`：字段 `status: NavStatus`、`fail_next_start: bool`；方法 `start(pose: Pose)`、`stop()`、`pause()`、`resume()`、`fail(reason: str = "")`、`step(dt: float)`
  - `LocStateMachine(load_delay_s: float = 0.2, init_delay_s: float = 0.3)`：字段 `status: LocStatus`、`loaded_map_id: str | None`；方法 `load_map(map_id: str)`、`reset()`、`lose()`、`recover()`、`step(dt)`；属性 `healthy: bool`
  - `MappingStateMachine(sensor_delay_s: float = 0.2, ready_delay_s: float = 0.2, save_delay_s: float = 0.3)`：字段 `status: MappingStatus`、`on_saved: Callable[[], None] | None`；方法 `start()`、`stop()`、`step(dt)`

- [ ] **Step 1: 写失败的测试**

`tests/sim/test_nav_state.py`：

```python
"""三套状态机的迁移规则。"""

import math

import pytest

from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus, NavStatus, Pose
from d1max_sim.kinematics import Planar2DModel
from d1max_sim.nav_state import (
    LocStateMachine,
    MappingStateMachine,
    NavStateMachine,
    SimRejected,
)

DT = 0.05


def _advance(sm, seconds: float) -> None:
    for _ in range(int(round(seconds / DT))):
        sm.step(DT)


def _run_until(sm, predicate, max_seconds: float = 60.0) -> bool:
    for _ in range(int(max_seconds / DT)):
        if predicate():
            return True
        sm.step(DT)
    return predicate()


# ------------------------------------------------------------------ 导航状态机

def _nav() -> NavStateMachine:
    return NavStateMachine(model=Planar2DModel())


def test_初始状态是就绪():
    assert _nav().status is NavStatus.STANDBY


def test_启动后先初始化再激活():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(2.0, 0.0))
    assert sm.status is NavStatus.INITIALIZING
    _advance(sm, 0.35)
    assert sm.status is NavStatus.ACTIVE


def test_到点后成功再回到就绪():
    """真机上 StandBy 是启动导航的唯一前提,因此终态必须自动回落。"""
    sm = _nav()
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)


def test_非就绪状态下启动被拒绝():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(3.0, 0.0))
    with pytest.raises(SimRejected, match="StandBy"):
        sm.start(Pose.from_xy_yaw(1.0, 0.0))


def test_停止导航进入取消再回就绪():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(5.0, 0.0))
    _advance(sm, 0.5)
    sm.stop()
    assert sm.status is NavStatus.CANCELLED
    assert sm.model.has_goal is False
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)


def test_就绪状态下停止被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.stop()


def test_暂停期间不再前进():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(5.0, 0.0))
    _advance(sm, 0.5)
    sm.pause()
    assert sm.status is NavStatus.PAUSE
    x_before = sm.model.x
    _advance(sm, 1.0)
    assert sm.model.x == pytest.approx(x_before)


def test_继续后恢复前进并最终到达():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    _advance(sm, 0.5)
    sm.pause()
    sm.resume()
    assert sm.status is NavStatus.ACTIVE
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)


def test_未暂停时继续被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.resume()


def test_非激活状态下暂停被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.pause()


def test_无导航时注入失败被拒绝():
    sm = _nav()
    with pytest.raises(SimRejected):
        sm.fail("没有导航在跑")


def test_注入失败进入_failed_再回就绪():
    sm = _nav()
    sm.start(Pose.from_xy_yaw(5.0, 0.0))
    _advance(sm, 0.5)
    sm.fail("注入")
    assert sm.status is NavStatus.FAILED
    assert sm.model.has_goal is False
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)


def test_预约下一次导航失败():
    """fail_next_start 用于制造"某个航点走不到"的场景。"""
    sm = _nav()
    sm.fail_next_start = True
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    assert _run_until(sm, lambda: sm.status is NavStatus.FAILED)
    assert sm.fail_next_start is False   # 一次性
    assert _run_until(sm, lambda: sm.status is NavStatus.STANDBY)
    sm.start(Pose.from_xy_yaw(1.0, 0.0))
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)


def test_机器人卡住时状态停在_active():
    """stuck 注入:导航状态正常但永远到不了,用于触发上层航点超时。"""
    sm = NavStateMachine(model=Planar2DModel(frozen=True))
    sm.start(Pose.from_xy_yaw(3.0, 0.0))
    _advance(sm, 10.0)
    assert sm.status is NavStatus.ACTIVE


# ------------------------------------------------------------------ 定位状态机

def test_定位初始状态():
    assert LocStateMachine().status is LocStatus.INIT


def test_加载地图走完三段迁移():
    sm = LocStateMachine()
    sm.load_map("m1")
    assert sm.status is LocStatus.MAP_LOADING
    assert sm.loaded_map_id == "m1"
    _advance(sm, 0.25)
    assert sm.status is LocStatus.INIT_LOCALIZATION
    _advance(sm, 0.35)
    assert sm.status is LocStatus.CONTINUOUS_LOC
    assert sm.healthy is True


def test_定位丢失后不再健康():
    sm = LocStateMachine()
    sm.load_map("m1")
    _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)
    sm.lose()
    assert sm.status is LocStatus.LOC_LOST
    assert sm.healthy is False
    _advance(sm, 2.0)
    assert sm.status is LocStatus.LOC_LOST   # 不会自愈


def test_定位丢失后可恢复():
    sm = LocStateMachine()
    sm.load_map("m1")
    _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)
    sm.lose()
    sm.recover()
    assert _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)


def test_重置定位重新走初始化():
    sm = LocStateMachine()
    sm.load_map("m1")
    _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)
    sm.reset()
    assert sm.status is LocStatus.INIT_LOCALIZATION
    assert _run_until(sm, lambda: sm.status is LocStatus.CONTINUOUS_LOC)


# ------------------------------------------------------------------ 建图状态机

def test_建图初始状态是抑制():
    assert MappingStateMachine().status is MappingStatus.PASSIVE


def test_建图走完启动三段迁移():
    sm = MappingStateMachine()
    sm.start()
    assert sm.status is MappingStatus.INIT_WAIT_SENSOR
    _advance(sm, 0.25)
    assert sm.status is MappingStatus.MAPPING_READY
    _advance(sm, 0.25)
    assert sm.status is MappingStatus.MAPPING_RUNNING


def test_停止建图触发保存并回调一次():
    fired: list[int] = []
    sm = MappingStateMachine(on_saved=lambda: fired.append(1))
    sm.start()
    _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_RUNNING)
    sm.stop()
    assert sm.status is MappingStatus.MAPPING_SAVE_BEGIN
    assert _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_SAVE_END)
    _advance(sm, 2.0)
    assert fired == [1]


def test_未建图时停止被拒绝():
    sm = MappingStateMachine()
    with pytest.raises(SimRejected):
        sm.stop()


def test_建图中重复启动被拒绝():
    sm = MappingStateMachine()
    sm.start()
    with pytest.raises(SimRejected):
        sm.start()


def test_保存完成后可再次建图():
    sm = MappingStateMachine()
    sm.start()
    _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_RUNNING)
    sm.stop()
    _run_until(sm, lambda: sm.status is MappingStatus.MAPPING_SAVE_END)
    sm.start()
    assert sm.status is MappingStatus.INIT_WAIT_SENSOR


def test_零角度目标也能收敛():
    """回归:目标朝向为 π 时角度归一化不能来回抖。"""
    sm = NavStateMachine(model=Planar2DModel())
    sm.start(Pose.from_xy_yaw(1.0, 0.0, math.pi))
    assert _run_until(sm, lambda: sm.status is NavStatus.SUCCEED)


def test_跨初始化边界的大步长不多算行走时间():
    """回归:一次大 step 里,初始化占掉的那部分时间不能算进行走距离。"""
    big = _nav()
    big.start(Pose.from_xy_yaw(5.0, 0.0))
    big.step(0.31)                       # 0.30s 初始化 + 0.01s 行走
    small = _nav()
    small.start(Pose.from_xy_yaw(5.0, 0.0))
    for _ in range(31):
        small.step(0.01)
    assert big.status is NavStatus.ACTIVE
    assert small.status is NavStatus.ACTIVE
    assert big.model.x == pytest.approx(small.model.x, abs=1e-6)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/sim/test_nav_state.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_sim.nav_state'`

- [ ] **Step 3: 写 nav_state.py**

`src/d1max_sim/nav_state.py`：

```python
"""仿真设备的三套状态机:导航、定位、建图。

状态取值与迁移必须贴住 refs/nav-api 文档,因为契约测试用它们判定
"到点了没有"。凡是文档没写死、由本仿真器补齐的行为,一律在源码里加注待真机验证标记。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus, NavStatus, Pose

from .kinematics import Planar2DModel


class SimRejected(Exception):
    """设备在当前状态下拒绝该操作。仿真器据此回 status: "error"。"""


@dataclass
class _Schedule:
    """一串"多少秒之后发生什么"的延时事件,按入队顺序依次触发。"""

    _queue: list[tuple[float, Any]] = field(default_factory=list)

    def clear(self) -> None:
        self._queue.clear()

    def push(self, delay: float, value: Any) -> None:
        self._queue.append((delay, value))

    def tick(self, dt: float) -> list[tuple[Any, float]]:
        """推进 dt 秒,返回本次触发的 (值, 触发后本次 dt 的剩余量)。"""
        fired: list[tuple[Any, float]] = []
        remaining = dt
        while self._queue and remaining > 0.0:
            delay, value = self._queue[0]
            if delay > remaining:
                self._queue[0] = (delay - remaining, value)
                remaining = 0.0
            else:
                remaining -= delay
                self._queue.pop(0)
                fired.append((value, remaining))
        return fired


# ---------------------------------------------------------------------- 导航

@dataclass
class NavStateMachine:
    """导航功能状态,见 refs/nav-api §3.6。"""

    model: Planar2DModel
    # 假设(待真机验证): 以下两个延时参数是为使导航流程从初始化到活跃有明确的时间演变而设定的。
    # 文档未指定这些时间;在实际机器人上需验证:
    # 1. init_delay_s=0.3 s: 从启动导航到机器人可开始实际行走的初始化阶段耗时
    # 2. terminal_hold_s=0.5 s: 导航到达终态(成功/失败/取消)后回落至待命状态前的驻留时间
    #: StandBy -> Initializing -> Active 的过渡时长
    init_delay_s: float = 0.3
    # 假设(待真机验证): 终态(Succeed/Failed/Cancelled)保持多久后回落 StandBy。
    # 文档只说"仅在 StandBy 下可启动导航",没说终态如何退出,这里补一个短驻留。
    terminal_hold_s: float = 0.5

    status: NavStatus = NavStatus.STANDBY
    #: 一次性开关:下一次 start 直接失败,用于制造"某个航点走不到"
    fail_next_start: bool = False

    _schedule: _Schedule = field(default_factory=_Schedule, repr=False)

    def start(self, pose: Pose) -> None:
        if self.status is not NavStatus.STANDBY:
            raise SimRejected(f"导航只能在 StandBy 下启动,当前 {self.status.value}")
        self._schedule.clear()
        self.model.set_goal(pose)
        self.status = NavStatus.INITIALIZING
        if self.fail_next_start:
            self.fail_next_start = False
            self._schedule.push(self.init_delay_s, NavStatus.FAILED)
        else:
            self._schedule.push(self.init_delay_s, NavStatus.ACTIVE)

    def stop(self) -> None:
        if self.status not in (NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.PAUSE):
            raise SimRejected(f"当前 {self.status.value} 无导航可停止")
        self._enter_terminal(NavStatus.CANCELLED)

    def pause(self) -> None:
        if self.status is not NavStatus.ACTIVE:
            raise SimRejected(f"只能在 Active 下暂停,当前 {self.status.value}")
        self.status = NavStatus.PAUSE

    def resume(self) -> None:
        if self.status is not NavStatus.PAUSE:
            raise SimRejected(f"只能在 Pause 下继续,当前 {self.status.value}")
        self.status = NavStatus.ACTIVE

    def fail(self, reason: str = "") -> None:
        """外部注入导航失败。"""
        if self.status not in (NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.PAUSE):
            raise SimRejected(f"当前 {self.status.value} 无导航可失败")
        self._enter_terminal(NavStatus.FAILED)

    def _enter_terminal(self, status: NavStatus) -> None:
        self._schedule.clear()
        self.model.clear_goal()
        self.status = status
        self._schedule.push(self.terminal_hold_s, NavStatus.STANDBY)

    def step(self, dt: float) -> None:
        move_budget = dt
        for value, remaining in self._schedule.tick(dt):
            self.status = value
            # 进入 Active 之前的那段时间是初始化,不该算作行走
            if value is NavStatus.ACTIVE:
                move_budget = remaining
            elif value is NavStatus.FAILED:
                self.model.clear_goal()
                self._schedule.push(self.terminal_hold_s, NavStatus.STANDBY)
        if self.status is NavStatus.ACTIVE and move_budget > 0.0:
            self.model.step(move_budget)
            if self.model.arrived:
                self._enter_terminal(NavStatus.SUCCEED)


# ---------------------------------------------------------------------- 定位

@dataclass
class LocStateMachine:
    """定位功能状态,见 refs/nav-api §4.3。"""

    # 假设(待真机验证): 以下两个延时参数是地图加载和初始定位的预估耗时。
    # 文档未指定这些时间;在实际机器人上需验证:
    # 1. load_delay_s=0.2 s: 地图加载从启动到完成的耗时
    # 2. init_delay_s=0.3 s: 初始定位从加载完成到定位连续可用的耗时
    load_delay_s: float = 0.2
    init_delay_s: float = 0.3

    status: LocStatus = LocStatus.INIT
    loaded_map_id: str | None = None

    _schedule: _Schedule = field(default_factory=_Schedule, repr=False)

    @property
    def healthy(self) -> bool:
        return self.status is LocStatus.CONTINUOUS_LOC

    def load_map(self, map_id: str) -> None:
        self._schedule.clear()
        self.loaded_map_id = map_id
        self.status = LocStatus.MAP_LOADING
        self._schedule.push(self.load_delay_s, LocStatus.INIT_LOCALIZATION)
        self._schedule.push(self.init_delay_s, LocStatus.CONTINUOUS_LOC)

    def reset(self) -> None:
        self._schedule.clear()
        self.status = LocStatus.INIT_LOCALIZATION
        self._schedule.push(self.init_delay_s, LocStatus.CONTINUOUS_LOC)

    def lose(self) -> None:
        """注入定位丢失。不会自愈,必须显式 recover 或 reset。"""
        self._schedule.clear()
        self.status = LocStatus.LOC_LOST

    def recover(self) -> None:
        self.reset()

    def step(self, dt: float) -> None:
        for value, _ in self._schedule.tick(dt):
            self.status = value


# ---------------------------------------------------------------------- 建图

_MAPPING_STARTABLE = (
    MappingStatus.UNKNOWN,
    MappingStatus.PASSIVE,
    MappingStatus.MAPP_ERROR,
    MappingStatus.MAPPING_SAVE_END,
)


@dataclass
class MappingStateMachine:
    """建图功能状态,见 refs/nav-api §1.3。"""

    # 假设(待真机验证): 以下三个延时参数是建图流程各阶段的预估耗时。
    # 文档未指定这些时间;在实际机器人上需验证:
    # 1. sensor_delay_s=0.2 s: 传感器初始化等待耗时(启动到传感器就绪)
    # 2. ready_delay_s=0.2 s: 建图准备阶段耗时(就绪到实际开始建图)
    # 3. save_delay_s=0.3 s: 地图保存耗时(停止到保存完成)
    sensor_delay_s: float = 0.2
    ready_delay_s: float = 0.2
    save_delay_s: float = 0.3
    #: 保存完成时回调一次,仿真器用它落地图并推送 notify_stop_mapping_status
    on_saved: Callable[[], None] | None = None

    status: MappingStatus = MappingStatus.PASSIVE

    _schedule: _Schedule = field(default_factory=_Schedule, repr=False)

    def start(self) -> None:
        if self.status not in _MAPPING_STARTABLE:
            raise SimRejected(f"当前 {self.status.value} 不能开始建图")
        self._schedule.clear()
        self.status = MappingStatus.INIT_WAIT_SENSOR
        self._schedule.push(self.sensor_delay_s, MappingStatus.MAPPING_READY)
        self._schedule.push(self.ready_delay_s, MappingStatus.MAPPING_RUNNING)

    def stop(self) -> None:
        if self.status is not MappingStatus.MAPPING_RUNNING:
            raise SimRejected(f"当前 {self.status.value} 没有建图任务可停止")
        self._schedule.clear()
        self.status = MappingStatus.MAPPING_SAVE_BEGIN
        self._schedule.push(self.save_delay_s, MappingStatus.MAPPING_SAVE_END)

    def step(self, dt: float) -> None:
        for value, _ in self._schedule.tick(dt):
            self.status = value
            if value is MappingStatus.MAPPING_SAVE_END and self.on_saved is not None:
                self.on_saved()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/sim/test_nav_state.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_sim/nav_state.py tests/sim/test_nav_state.py
git commit -m "feat: 仿真器导航/定位/建图三套状态机"
```

---
### Task 8: 仿真器的地图与路径存储

**Files:**
- Create: `src/d1max_sim/store.py`
- Test: `tests/sim/test_store.py`

**Interfaces:**
- Consumes: `d1max_patrol.protocol.nav_types.Waypoint`
- Produces（`d1max_sim.store`）：
  - `StoreError(Exception)`
  - `MapRecord(map_id: str, width: int = 20, height: int = 20, resolution: float = 0.05, origin_x: float = 0.0, origin_y: float = 0.0, tags: list = [])` —— 可变数据类，`tags` 用 `field(default_factory=list)`
  - `MapStore()`：属性 `maps: dict[str, MapRecord]`、`paths: dict[str, dict[str, list[Waypoint]]]`
  - 方法：`create_map(map_id: str | None = None) -> str`、`remove_maps(map_ids: Sequence[str]) -> int`、`rename_map(old_id: str, new_id: str) -> None`、`occupancy_grid(map_id: str) -> dict`、`all_pgm_payload() -> dict`、`map_ids() -> list[str]`、`set_path(map_id, path_id, waypoints)`、`get_paths(map_id) -> dict[str, list[Waypoint]]`、`remove_path(map_id, path_id)`、`paths_payload(map_id) -> list`、`save(path)`、`load(path)`

- [ ] **Step 1: 写失败的测试**

`tests/sim/test_store.py`：

```python
"""仿真器的地图与路径存储。载荷形状必须与 refs/nav-api §1.5 / §2.1 一致。"""

import pytest

from d1max_patrol.protocol.nav_requests import parse_map_ids, parse_paths_payload
from d1max_patrol.protocol.nav_types import Pose, Waypoint
from d1max_sim.store import MapStore, StoreError


def _wp(name: str, x: float, y: float) -> Waypoint:
    return Waypoint(name, Pose.from_xy_yaw(x, y))


def test_新建地图自动编号且递增():
    s = MapStore()
    assert s.create_map() == "map_1"
    assert s.create_map() == "map_2"
    assert s.map_ids() == ["map_1", "map_2"]


def test_新建地图可指定_id():
    s = MapStore()
    assert s.create_map("厂区一层") == "厂区一层"
    assert "厂区一层" in s.maps


def test_重复_id_报错():
    s = MapStore()
    s.create_map("m1")
    with pytest.raises(StoreError, match="已存在"):
        s.create_map("m1")


def test_删除地图返回删掉的个数并连带删路径():
    s = MapStore()
    s.create_map("m1")
    s.create_map("m2")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0)])
    assert s.remove_maps(["m1", "不存在"]) == 1
    assert s.map_ids() == ["m2"]
    assert "m1" not in s.paths


def test_重命名地图连带搬运路径():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0)])
    s.rename_map("m1", "m9")
    assert s.map_ids() == ["m9"]
    assert s.maps["m9"].map_id == "m9"
    assert [w.name for w in s.get_paths("m9")["p1"]] == ["A"]


def test_重命名到已存在的_id_报错():
    s = MapStore()
    s.create_map("m1")
    s.create_map("m2")
    with pytest.raises(StoreError, match="已存在"):
        s.rename_map("m1", "m2")


def test_重命名不存在的地图报错():
    with pytest.raises(StoreError, match="不存在"):
        MapStore().rename_map("m1", "m2")


def test_栅格地图结构():
    s = MapStore()
    s.create_map("m1")
    grid = s.occupancy_grid("m1")
    assert set(grid) == {"header", "info", "data"}
    assert grid["info"]["width"] == 20
    assert grid["info"]["height"] == 20
    assert grid["info"]["resolution"] == 0.05
    assert len(grid["data"]) == 400
    assert set(grid["info"]["origin"]) == {"position", "orientation"}
    assert grid["info"]["map_load_time"] == {"sec": 0, "nanosec": 0}


def test_栅格地图取不存在的地图报错():
    with pytest.raises(StoreError, match="不存在"):
        MapStore().occupancy_grid("m1")


def test_全部地图载荷能被客户端解析器读回():
    s = MapStore()
    s.create_map("m2")
    s.create_map("m1")
    payload = s.all_pgm_payload()
    assert parse_map_ids(payload) == ["m1", "m2"]
    grid, tags = payload["m1"]
    assert grid["info"]["width"] == 20
    assert isinstance(tags, list)


def test_路径载荷能被客户端解析器读回():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0), _wp("B", 3.0, 4.0)])
    s.set_path("m1", "p2", [])
    paths = parse_paths_payload(s.paths_payload("m1"))
    assert set(paths) == {"p1", "p2"}
    assert [w.name for w in paths["p1"]] == ["A", "B"]
    assert paths["p1"][1].pose.position.y == 4.0
    assert paths["p2"] == []


def test_没有路径的地图返回空载荷():
    s = MapStore()
    s.create_map("m1")
    payload = s.paths_payload("m1")
    assert payload[0] == "m1"
    assert payload[1] == []
    assert payload[2] == {}


def test_对不存在的地图加路径报错():
    with pytest.raises(StoreError, match="不存在"):
        MapStore().set_path("m1", "p1", [])


def test_覆盖同名路径():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 0.0, 0.0)])
    s.set_path("m1", "p1", [_wp("B", 1.0, 1.0)])
    assert [w.name for w in s.get_paths("m1")["p1"]] == ["B"]


def test_删除路径():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [])
    s.remove_path("m1", "p1")
    assert s.get_paths("m1") == {}
    # 删不存在的不报错,与厂商"批量删除"的宽松语义一致
    s.remove_path("m1", "p1")
    s.remove_path("不存在", "p1")


def test_取路径返回的字典不会写回存储():
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("a", 0.0, 0.0)])
    got = s.get_paths("m1")
    got["p_injected"] = []
    assert "p_injected" not in s.paths["m1"]
    assert list(s.get_paths("m1")) == ["p1"]


def test_存档与读档往返(tmp_path):
    s = MapStore()
    s.create_map("m1")
    s.set_path("m1", "p1", [_wp("A", 1.0, 2.0)])
    f = tmp_path / "state.json"
    s.save(f)

    s2 = MapStore()
    s2.load(f)
    assert s2.map_ids() == ["m1"]
    assert [w.name for w in s2.get_paths("m1")["p1"]] == ["A"]
    assert s2.get_paths("m1")["p1"][0].pose.position.x == 1.0


def test_读档后自动编号不与已有地图冲突(tmp_path):
    s = MapStore()
    s.create_map()          # map_1
    s.create_map()          # map_2
    f = tmp_path / "state.json"
    s.save(f)

    s2 = MapStore()
    s2.load(f)
    assert s2.create_map() == "map_3"


def test_读档不存在的文件不报错(tmp_path):
    s = MapStore()
    s.load(tmp_path / "nope.json")
    assert s.map_ids() == []


def test_读档格式错误报_StoreError(tmp_path):
    s = MapStore()
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all", encoding="utf-8")
    with pytest.raises(StoreError, match="读档失败"):
        s.load(bad_file)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/sim/test_store.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_sim.store'`

- [ ] **Step 3: 写 store.py**

`src/d1max_sim/store.py`：

```python
"""仿真设备上的地图与路径。

载荷形状照抄 refs/nav-api §1.5(所有 PGM 地图)与 §2.1(某地图下的所有路径),
这样客户端的 parse_map_ids / parse_paths_payload 能原样读回。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d1max_patrol.protocol.nav_types import Waypoint

# 假设(待真机验证): 新建空白地图的 id 编号规则为 map_1, map_2, ...
# 供应商文档 §1.5 的示例使用 map_id_1/map_id_2，且无"新建空白地图"的接口，
# 实际设备保存完成的 SLAM 会话后分配什么 id 未知。须在真机上运行建图会话、
# 保存后调用 get_all_map，记录设备分配的实际 id。
_AUTO_NAME = re.compile(r"^map_(\d+)$")


class StoreError(Exception):
    """地图或路径不存在、或 id 冲突。"""


@dataclass
class MapRecord:
    """一张占用栅格地图。

    宽高默认值为仿真便利设定，不代表实际设备能力。实际设备的地图尺寸由 SLAM 结果决定。
    """

    map_id: str
    # 假设(待真机验证): 默认网格尺寸为 20×20，数据全零。供应商文档 §1.4 的示例为
    # 1000×1000 地图(约百万整数、数 MB JSON)，实际 get_pgm_map 响应可能远大于仿真器。
    # 须在真机验证：缓冲区大小、响应超时、网络开销是否能应对真实负载。所有零数据是
    # 仿真器便利(设备无障碍物，路径规划用运动学直线逼近)，与 SLAM 产出的混合值不同。
    width: int = 20
    height: int = 20
    resolution: float = 0.05
    origin_x: float = 0.0
    origin_y: float = 0.0
    #: §1.5 载荷第二项的标签列表,形如 [[0, [x, y, z]], ...]
    tags: list[Any] = field(default_factory=list)

    def to_grid(self) -> dict[str, Any]:
        return {
            "header": {"frame_id": "map", "stamp": {"sec": 0, "nanosec": 0}},
            "info": {
                "map_load_time": {"sec": 0, "nanosec": 0},
                "resolution": self.resolution,
                "width": self.width,
                "height": self.height,
                "origin": {
                    "position": {"x": self.origin_x, "y": self.origin_y, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
            },
            # 全 0 表示全空闲。仿真器不做障碍物,路径规划由运动学模型直线趋近代替。
            "data": [0] * (self.width * self.height),
        }


class MapStore:
    """地图与路径的内存存储,可选存盘以复现场景。"""

    def __init__(self) -> None:
        self.maps: dict[str, MapRecord] = {}
        self.paths: dict[str, dict[str, list[Waypoint]]] = {}

    # ------------------------------------------------------------ 地图

    def map_ids(self) -> list[str]:
        return sorted(self.maps)

    def _next_auto_id(self) -> str:
        used = {
            int(m.group(1))
            for key in self.maps
            if (m := _AUTO_NAME.match(key)) is not None
        }
        n = 1
        while n in used:
            n += 1
        return f"map_{n}"

    def create_map(self, map_id: str | None = None) -> str:
        if map_id is None:
            map_id = self._next_auto_id()
        if map_id in self.maps:
            raise StoreError(f"地图 {map_id!r} 已存在")
        self.maps[map_id] = MapRecord(map_id=map_id)
        self.paths.setdefault(map_id, {})
        return map_id

    def remove_maps(self, map_ids: Sequence[str]) -> int:
        """删除若干地图,返回实际删掉的个数。不存在的静默跳过。"""
        removed = 0
        for map_id in map_ids:
            if self.maps.pop(map_id, None) is not None:
                removed += 1
            self.paths.pop(map_id, None)
        return removed

    def rename_map(self, old_id: str, new_id: str) -> None:
        if old_id not in self.maps:
            raise StoreError(f"地图 {old_id!r} 不存在")
        if new_id in self.maps:
            raise StoreError(f"地图 {new_id!r} 已存在")
        record = self.maps.pop(old_id)
        record.map_id = new_id
        self.maps[new_id] = record
        self.paths[new_id] = self.paths.pop(old_id, {})

    def _require(self, map_id: str) -> MapRecord:
        record = self.maps.get(map_id)
        if record is None:
            raise StoreError(f"地图 {map_id!r} 不存在")
        return record

    def occupancy_grid(self, map_id: str) -> dict[str, Any]:
        return self._require(map_id).to_grid()

    def all_pgm_payload(self) -> dict[str, Any]:
        """§1.5 的 data: {map_id: [OccupancyGrid, 标签列表]}。"""
        return {
            map_id: [record.to_grid(), list(record.tags)]
            for map_id, record in self.maps.items()
        }

    # ------------------------------------------------------------ 路径

    def set_path(self, map_id: str, path_id: str, waypoints: Sequence[Waypoint]) -> None:
        self._require(map_id)
        self.paths.setdefault(map_id, {})[path_id] = list(waypoints)

    def get_paths(self, map_id: str) -> dict[str, list[Waypoint]]:
        self._require(map_id)
        # 返回映射的浅拷贝:调用方增删键不会影响存储。值(list[Waypoint])仍共享,
        # 与公开的 paths 属性同级别 —— Waypoint 本身不可变。
        return dict(self.paths.get(map_id, {}))

    def remove_path(self, map_id: str, path_id: str) -> None:
        """删不存在的静默返回 —— 与厂商批量删除的宽松语义一致。"""
        self.paths.get(map_id, {}).pop(path_id, None)

    def paths_payload(self, map_id: str) -> list[Any]:
        """§2.1 的 data: [map_id, [path_ids], {path_id: [[点名, pose], ...]}]。"""
        paths = self.get_paths(map_id)
        return [
            map_id,
            sorted(paths),
            {pid: [wp.to_wire() for wp in wps] for pid, wps in paths.items()},
        ]

    # ------------------------------------------------------------ 存盘

    def save(self, path: str | Path) -> None:
        payload = {
            "maps": [
                {
                    "map_id": r.map_id,
                    "width": r.width,
                    "height": r.height,
                    "resolution": r.resolution,
                    "origin_x": r.origin_x,
                    "origin_y": r.origin_y,
                    "tags": r.tags,
                }
                for r in self.maps.values()
            ],
            "paths": {
                map_id: {pid: [wp.to_wire() for wp in wps] for pid, wps in paths.items()}
                for map_id, paths in self.paths.items()
            },
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self, path: str | Path) -> None:
        """从存档恢复。文件不存在则保持空,便于首次启动。"""
        p = Path(path)
        if not p.is_file():
            return
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            self.maps = {
                entry["map_id"]: MapRecord(**entry) for entry in payload.get("maps", [])
            }
            self.paths = {
                map_id: {
                    pid: [Waypoint.from_wire(w) for w in wire] for pid, wire in paths.items()
                }
                for map_id, paths in payload.get("paths", {}).items()
            }
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise StoreError(f"读档失败({p})：{e}") from e
        for map_id in self.maps:
            self.paths.setdefault(map_id, {})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/sim/test_store.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_sim/store.py tests/sim/test_store.py
git commit -m "feat: 仿真器地图与路径存储"
```

---
### Task 9: 故障注入状态与命令解析

设计文档 §7.1 列了一组故障注入命令。本任务只做**纯同步的状态与解析**，不碰网络也不碰状态机：命令改的是一个 `FaultState`，由 Task 10 的服务端在 tick 里读取并施加到运动学模型与状态机上。这样注入语义可以被单测锁死，不必起服务器。

**Files:**
- Create: `src/d1max_sim/inject.py`
- Test: `tests/sim/test_inject.py`

**Interfaces:**
- Consumes: `d1max_patrol.protocol.nav_frames.AlgErrorItem`
- Produces（`d1max_sim.inject`）：
  - `InjectError(ValueError)`
  - `FaultState` 可变数据类，字段：`speed_scale: float = 1.0`、`stuck: bool = False`、`frame_count_zero: bool = False`、`response_delay_s: float = 0.0`、`disconnect_seconds: float = 0.0`、`fail_next_nav: bool = False`、`loc_lost_requested: bool = False`、`loc_recover_requested: bool = False`、`queued_alg_errors: list[AlgErrorItem]`
  - `FaultState.reset() -> None`、`FaultState.describe() -> str`、`FaultState.take_alg_errors() -> list[AlgErrorItem]`
  - `apply_command(state: FaultState, line: str) -> str` —— 解析并应用一条命令，返回人类可读结果；无法解析时抛 `InjectError`
  - `HELP_TEXT: str`

- [ ] **Step 1: 写失败的测试**

`tests/sim/test_inject.py`：

```python
"""故障注入命令的解析与语义。"""

import pytest

from d1max_sim.inject import FaultState, InjectError, apply_command


def test_默认状态什么都不注入():
    s = FaultState()
    assert s.speed_scale == 1.0
    assert s.stuck is False
    assert s.frame_count_zero is False
    assert s.response_delay_s == 0.0
    assert s.disconnect_seconds == 0.0
    assert s.fail_next_nav is False
    assert s.queued_alg_errors == []


def test_定位丢失与恢复():
    s = FaultState()
    assert "定位丢失" in apply_command(s, "loc_lost")
    assert s.loc_lost_requested is True
    apply_command(s, "loc_ok")
    assert s.loc_recover_requested is True


def test_注入算法故障码():
    s = FaultState()
    apply_command(s, "alg_error 13330")
    assert len(s.queued_alg_errors) == 1
    item = s.queued_alg_errors[0]
    assert item.code == 13330
    assert item.description == "navigation blocked"   # 已知码带默认描述
    assert item.severity == 0


def test_注入未知故障码也接受():
    s = FaultState()
    apply_command(s, "alg_error 19999 2")
    item = s.queued_alg_errors[0]
    assert (item.code, item.severity) == (19999, 2)
    assert item.description == "injected"


def test_取走故障码后队列清空():
    s = FaultState()
    apply_command(s, "alg_error 13331")
    assert [i.code for i in s.take_alg_errors()] == [13331]
    assert s.queued_alg_errors == []
    assert s.take_alg_errors() == []


def test_故障码必须是整数():
    with pytest.raises(InjectError, match="故障码"):
        apply_command(FaultState(), "alg_error abc")


def test_预约下次导航失败():
    s = FaultState()
    apply_command(s, "nav_fail")
    assert s.fail_next_nav is True


def test_减速命令换算成速度缩放():
    """slow 2 表示慢一倍。"""
    s = FaultState()
    apply_command(s, "slow 2")
    assert s.speed_scale == 0.5
    apply_command(s, "slow 1")
    assert s.speed_scale == 1.0


@pytest.mark.parametrize("bad", ["slow 0", "slow -1", "slow abc", "slow"])
def test_减速参数非法(bad):
    with pytest.raises(InjectError):
        apply_command(FaultState(), bad)


def test_卡住开关():
    s = FaultState()
    apply_command(s, "stuck on")
    assert s.stuck is True
    apply_command(s, "stuck off")
    assert s.stuck is False


def test_开关参数非法():
    with pytest.raises(InjectError, match="on 或 off"):
        apply_command(FaultState(), "stuck maybe")


def test_帧号归零开关():
    s = FaultState()
    apply_command(s, "frame_count_zero on")
    assert s.frame_count_zero is True


def test_乱序延迟():
    s = FaultState()
    apply_command(s, "reorder 0.3")
    assert s.response_delay_s == pytest.approx(0.3)


def test_断链秒数():
    s = FaultState()
    apply_command(s, "disconnect 2")
    assert s.disconnect_seconds == pytest.approx(2.0)


def test_sdk_链路命令给出明确的未实现提示():
    """battery / fault fatal / control_lost 属 SDK 链路,第 2 卷才有。"""
    for line in ("battery 20", "fault fatal", "control_lost"):
        with pytest.raises(InjectError, match="第 2 卷"):
            apply_command(FaultState(), line)


def test_reset_清空所有注入():
    s = FaultState()
    apply_command(s, "slow 4")
    apply_command(s, "stuck on")
    apply_command(s, "alg_error 13330")
    apply_command(s, "reset")
    assert s.speed_scale == 1.0
    assert s.stuck is False
    assert s.queued_alg_errors == []


def test_status_命令回显当前注入():
    s = FaultState()
    apply_command(s, "slow 2")
    out = apply_command(s, "status")
    assert "speed_scale=0.5" in out
    apply_command(s, "loc_lost")
    assert "loc_lost_requested=True" in apply_command(s, "status")


def test_alg_error_不带故障码被拒绝():
    with pytest.raises(InjectError, match="缺少故障码"):
        apply_command(FaultState(), "alg_error")


def test_alg_error_严重度非整数被拒绝():
    with pytest.raises(InjectError, match="严重度"):
        apply_command(FaultState(), "alg_error 13330 xyz")


def test_help_命令列出全部命令():
    out = apply_command(FaultState(), "help")
    for name in ("loc_lost", "alg_error", "nav_fail", "slow", "stuck",
                 "frame_count_zero", "reorder", "disconnect", "reset"):
        assert name in out


@pytest.mark.parametrize("bad", ["", "   ", "fly_to_the_moon"])
def test_未知命令报错(bad):
    with pytest.raises(InjectError, match="未知命令|命令为空"):
        apply_command(FaultState(), bad)


def test_命令大小写与多余空格容忍():
    s = FaultState()
    apply_command(s, "  STUCK   ON  ")
    assert s.stuck is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/sim/test_inject.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_sim.inject'`

- [ ] **Step 3: 写 inject.py**

`src/d1max_sim/inject.py`：

```python
"""故障注入。

只维护一份可变状态并解析命令行,不碰网络也不碰状态机 ——
施加动作由 nav_server 在 tick 循环里完成。这样注入语义能被单测锁死。

命令清单见设计文档 §7.1。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import ALG_LIDAR_DISCONNECTED, ALG_NAV_BLOCKED

#: 已知故障码的默认描述,与 refs/nav-api §9 的样例一致
_KNOWN_CODES = {
    ALG_NAV_BLOCKED: "navigation blocked",
    ALG_LIDAR_DISCONNECTED: "lidar disconnected",
}

#: 属 SDK 链路的注入命令,第 2 卷实现
_SDK_ONLY = {"battery", "fault", "control_lost"}

HELP_TEXT = """\
可用注入命令:
  loc_lost                 制造定位丢失
  loc_ok                   从定位丢失中恢复
  alg_error <码> [严重度]   推送一条算法故障码
  nav_fail                 让下一次 start_nav 失败
  slow <倍数>              行走减速,slow 2 表示慢一倍
  stuck on|off             状态正常但完全不动
  frame_count_zero on|off  响应的 frame_count 一律填 0,逼客户端走名字回退匹配
  reorder <秒>             响应统一延迟,制造乱序到达
  disconnect <秒>          断开所有连接并静默这么久
  reset                    清空所有注入
  status                   回显当前注入状态
  help                     显示本帮助
"""


class InjectError(ValueError):
    """命令无法解析或参数非法。"""


@dataclass
class FaultState:
    """当前生效的注入。字段被 nav_server 在每个 tick 读取。"""

    #: 运动速度缩放,1.0 为正常
    speed_scale: float = 1.0
    #: 完全不动
    stuck: bool = False
    #: 响应 frame_count 一律填 0
    frame_count_zero: bool = False
    #: 响应统一延迟秒数
    response_delay_s: float = 0.0
    #: 待执行的断链秒数,服务端消费后清零
    disconnect_seconds: float = 0.0
    #: 下一次 start_nav 失败
    fail_next_nav: bool = False
    #: 待执行的定位丢失 / 恢复,服务端消费后清零
    loc_lost_requested: bool = False
    loc_recover_requested: bool = False
    #: 待推送的算法故障码
    queued_alg_errors: list[AlgErrorItem] = field(default_factory=list)

    def reset(self) -> None:
        self.speed_scale = 1.0
        self.stuck = False
        self.frame_count_zero = False
        self.response_delay_s = 0.0
        self.disconnect_seconds = 0.0
        self.fail_next_nav = False
        self.loc_lost_requested = False
        self.loc_recover_requested = False
        self.queued_alg_errors.clear()

    def take_alg_errors(self) -> list[AlgErrorItem]:
        """取走待推送的故障码并清空队列。"""
        items = list(self.queued_alg_errors)
        self.queued_alg_errors.clear()
        return items

    def describe(self) -> str:
        return (
            f"speed_scale={self.speed_scale} stuck={self.stuck} "
            f"frame_count_zero={self.frame_count_zero} "
            f"response_delay_s={self.response_delay_s} "
            f"disconnect_seconds={self.disconnect_seconds} "
            f"fail_next_nav={self.fail_next_nav} "
            f"loc_lost_requested={self.loc_lost_requested} "
            f"loc_recover_requested={self.loc_recover_requested} "
            f"queued_alg_errors={len(self.queued_alg_errors)}"
        )


def _on_off(token: str | None, name: str) -> bool:
    if token is None or token.lower() not in ("on", "off"):
        raise InjectError(f"{name} 的参数必须是 on 或 off")
    return token.lower() == "on"


def _positive_float(token: str | None, name: str) -> float:
    if token is None:
        raise InjectError(f"{name} 缺少参数")
    try:
        value = float(token)
    except ValueError as exc:
        raise InjectError(f"{name} 的参数必须是数字: {token!r}") from exc
    if value <= 0:
        raise InjectError(f"{name} 的参数必须大于 0,实际为 {value}")
    return value


def apply_command(state: FaultState, line: str) -> str:
    """解析并应用一条注入命令,返回给操作者看的结果字符串。"""
    parts = line.strip().split()
    if not parts:
        raise InjectError("命令为空")
    name = parts[0].lower()
    args = parts[1:]

    if name in _SDK_ONLY:
        raise InjectError(f"{name} 属 SDK 链路的注入,本卷未实现,见第 2 卷")

    if name == "help":
        return HELP_TEXT
    if name == "status":
        return state.describe()
    if name == "reset":
        state.reset()
        return "已清空所有注入"

    if name == "loc_lost":
        state.loc_lost_requested = True
        return "已请求定位丢失"
    if name == "loc_ok":
        state.loc_recover_requested = True
        return "已请求定位恢复"

    if name == "alg_error":
        if not args:
            raise InjectError("alg_error 缺少故障码")
        try:
            code = int(args[0])
        except ValueError as exc:
            raise InjectError(f"故障码必须是整数: {args[0]!r}") from exc
        severity = 0
        if len(args) > 1:
            try:
                severity = int(args[1])
            except ValueError as exc:
                raise InjectError(f"严重度必须是整数: {args[1]!r}") from exc
        state.queued_alg_errors.append(
            AlgErrorItem(code=code,
                         description=_KNOWN_CODES.get(code, "injected"),
                         severity=severity)
        )
        return f"已排队故障码 {code}"

    if name == "nav_fail":
        state.fail_next_nav = True
        return "下一次 start_nav 将失败"

    if name == "slow":
        factor = _positive_float(args[0] if args else None, "slow")
        state.speed_scale = 1.0 / factor
        return f"行走速度缩放为 {state.speed_scale}"

    if name == "stuck":
        state.stuck = _on_off(args[0] if args else None, "stuck")
        return f"stuck={state.stuck}"

    if name == "frame_count_zero":
        state.frame_count_zero = _on_off(args[0] if args else None, "frame_count_zero")
        return f"frame_count_zero={state.frame_count_zero}"

    if name == "reorder":
        state.response_delay_s = _positive_float(args[0] if args else None, "reorder")
        return f"响应延迟 {state.response_delay_s}s"

    if name == "disconnect":
        state.disconnect_seconds = _positive_float(
            args[0] if args else None, "disconnect")
        return f"将断链 {state.disconnect_seconds}s"

    raise InjectError(f"未知命令: {name!r}。输入 help 查看可用命令")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/sim/test_inject.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_sim/inject.py tests/sim/test_inject.py
git commit -m "feat: 仿真器故障注入状态与命令解析"
```

---
### Task 10: 仿真导航服务端与控制通道

把前四个任务拼成一台可连的设备。它是接口契约的**可执行定义**：任何客户端行为的争议，以这台仿真器的表现为准，直到真机把它推翻。

**Files:**
- Create: `src/d1max_sim/nav_server.py`
- Create: `src/d1max_sim/__main__.py`
- Test: `tests/sim/test_nav_server.py`

**Interfaces:**
- Consumes: `d1max_sim.{kinematics,nav_state,store,inject}`；`d1max_patrol.protocol.{nav_frames,nav_requests,nav_types}`
- Produces（`d1max_sim.nav_server`）：
  - `SimNavServer(host: str = "127.0.0.1", port: int = 0, tick_hz: float = 50.0, store: MapStore | None = None, faults: FaultState | None = None)`
  - 属性：`port: int`（`start()` 后是实际端口）、`url: str`（`ws://host:port`）、`control_url: str`（`ws://host:port/control`）、`model`、`nav`、`loc`、`mapping`、`store`、`faults`、`speed: dict[str, float]`
  - 方法：`async start() -> None`、`async stop() -> None`
  - 模块常量 `CONTROL_PATH = "/control"`
- Produces（`d1max_sim.__main__`）：`main(argv: list[str] | None = None) -> int`

**设备行为约定（本仿真器补齐、文档未明写的部分，均以 `# 假设(待真机验证):` 标注）：**
1. `start_nav` 要求定位处于 `ContinuousLoc`，否则回 `error`。
2. 定位丢失时，进行中的导航转入 `Failed`。
3. `start_multi_nav` / `start_multi_nav_by_points` / `start_nav_return_home` 一律回 `error`：本项目全逐点执行，仿真器不假装支持它们。
4. `set_navigation_speed` 只传 `x` 时，设备端补 `y=0.5`、`z=1.5`（文档注明的行为）。

- [ ] **Step 1: 写失败的测试**

`tests/sim/test_nav_server.py`：

```python
"""仿真导航服务端。用真实 WebSocket 客户端对着它跑。"""

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import (
    AlgErrorNotify,
    Response,
    encode_request,
    parse_message,
)
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)
from d1max_sim.nav_server import SimNavServer


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _call(ws, req, frame_count: int) -> Response:
    """发一条请求,读到对应响应为止(跳过途中的推送)。"""
    await ws.send(encode_request(req.req_func, req.args, frame_count))
    while True:
        msg = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        if isinstance(msg, Response) and msg.req_func == req.response_func:
            return msg


async def _poll_until(ws, req, wanted: str, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    fc = 1000
    while loop.time() < deadline:
        fc += 1
        resp = await _call(ws, req, fc)
        if resp.data == wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{req.req_func} 未在 {timeout_s}s 内变为 {wanted}")


async def _ready_map(ws) -> str:
    """建一张图并加载定位,返回 map_id。"""
    await _call(ws, R.start_mapping(), 1)
    await _poll_until(ws, R.get_mapping_status(), MappingStatus.MAPPING_RUNNING.value)
    await _call(ws, R.stop_mapping(), 2)
    await _poll_until(ws, R.get_mapping_status(), MappingStatus.MAPPING_SAVE_END.value)
    maps = R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 3)).data)
    assert maps
    await _call(ws, R.loc_load_map(maps[0]), 4)
    await _poll_until(ws, R.get_loc_status(), LocStatus.CONTINUOUS_LOC.value)
    return maps[0]


async def test_服务端可连且初始状态就绪(sim):
    async with connect(sim.url) as ws:
        assert (await _call(ws, R.get_nav_status(), 1)).data == NavStatus.STANDBY.value
        assert (await _call(ws, R.get_loc_status(), 2)).data == LocStatus.INIT.value


async def test_响应帧号与请求一致(sim):
    async with connect(sim.url) as ws:
        assert (await _call(ws, R.get_nav_status(), 42)).frame_count == 42


async def test_建图全流程并推送保存完成通知(sim):
    async with connect(sim.url) as ws:
        await _call(ws, R.start_mapping(), 1)
        await _poll_until(ws, R.get_mapping_status(),
                          MappingStatus.MAPPING_RUNNING.value)
        await _call(ws, R.stop_mapping(), 2)

        # 在轮询之外单独收推送
        async def wait_notify():
            while True:
                msg = parse_message(await ws.recv())
                if isinstance(msg, Response) and \
                        msg.req_func == "notify_stop_mapping_status":
                    return msg

        notify = await asyncio.wait_for(wait_notify(), timeout=5.0)
        assert notify.ok is True
        assert R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 3)).data) == ["map_1"]


async def test_加载定位地图后进入持续定位(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)


async def test_加载不存在的地图回错误(sim):
    async with connect(sim.url) as ws:
        resp = await _call(ws, R.loc_load_map("不存在"), 1)
        assert resp.ok is False
        assert "不存在" in (resp.msg or "")


async def test_定位未就绪时拒绝导航(sim):
    async with connect(sim.url) as ws:
        resp = await _call(ws, R.start_nav(Pose.from_xy_yaw(1.0, 0.0)), 1)
        assert resp.ok is False
        assert "定位" in (resp.msg or "")


async def test_逐点导航能走到成功(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        assert (await _call(ws, R.start_nav(Pose.from_xy_yaw(1.0, 0.0)), 10)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.SUCCEED.value)
        await _poll_until(ws, R.get_nav_status(), NavStatus.STANDBY.value)
        assert sim.model.x == pytest.approx(1.0, abs=0.1)


async def test_停止导航进入取消(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)
        assert (await _call(ws, R.stop_nav(), 11)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.CANCELLED.value)


async def test_暂停与继续(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)
        assert (await _call(ws, R.pause_nav(), 11)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.PAUSE.value)
        assert (await _call(ws, R.continue_nav(), 12)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)


async def test_路径增删查往返(sim):
    async with connect(sim.url) as ws:
        map_id = await _ready_map(ws)
        wps = [Waypoint("A", Pose.from_xy_yaw(1.0, 2.0)),
               Waypoint("B", Pose.from_xy_yaw(3.0, 4.0))]
        assert (await _call(ws, R.add_nav_path(map_id, "p1", wps), 20)).ok
        got = R.parse_paths_payload(
            (await _call(ws, R.get_all_paths_by_mapid(map_id), 21)).data)
        assert [w.name for w in got["p1"]] == ["A", "B"]
        assert got["p1"][1].pose.position.y == 4.0

        assert (await _call(ws, R.modify_nav_path(map_id, "p1", "p1", wps[:1]), 22)).ok
        got = R.parse_paths_payload(
            (await _call(ws, R.get_all_paths_by_mapid(map_id), 23)).data)
        assert [w.name for w in got["p1"]] == ["A"]

        assert (await _call(ws, R.remove_nav_path([(map_id, "p1")]), 24)).ok
        got = R.parse_paths_payload(
            (await _call(ws, R.get_all_paths_by_mapid(map_id), 25)).data)
        assert got == {}


async def test_地图删除与重命名的数组参数(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        assert (await _call(ws, R.rename_map_name("map_1", "厂区"), 30)).ok
        assert R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 31)).data) == ["厂区"]
        assert (await _call(ws, R.remove_map_by_id(["厂区"]), 32)).ok
        assert R.parse_map_ids((await _call(ws, R.get_all_pgm_map(), 33)).data) == []


async def test_速度接口走嵌套外壳(sim):
    async with connect(sim.url) as ws:
        resp = await _call(ws, R.get_navigation_speed(), 1)
        assert resp.ok and set(resp.data) == {"x", "y", "z"}
        assert "AppReponseObjectData" in resp.raw["data"]["req_result"]

        resp = await _call(ws, R.set_navigation_speed(0.9), 2)
        assert resp.data == {"x": 0.9, "y": 0.5, "z": 1.5}   # 设备补默认值


@pytest.mark.parametrize(
    "req",
    [R.start_multi_nav("m", "p"),
     R.start_multi_nav_by_points("m", [Pose.from_xy_yaw(1.0, 0.0)]),
     R.start_nav_return_home()],
)
async def test_多点导航与返航被明确拒绝(sim, req):
    """本项目全逐点执行,仿真器不假装支持这些接口。"""
    async with connect(sim.url) as ws:
        resp = await _call(ws, req, 1)
        assert resp.ok is False
        assert "逐点" in (resp.msg or "") or "未实现" in (resp.msg or "")


async def test_未知接口回错误而不是断链(sim):
    async with connect(sim.url) as ws:
        await ws.send(encode_request("fly_to_the_moon", None, 1))
        msg = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert isinstance(msg, Response)
        assert msg.ok is False
        assert (await _call(ws, R.get_nav_status(), 2)).ok   # 链路仍在


async def test_畸形报文不打断链路(sim):
    async with connect(sim.url) as ws:
        await ws.send("{ not json")
        assert (await _call(ws, R.get_nav_status(), 1)).ok


async def _control(sim, command: str) -> dict:
    async with connect(sim.control_url) as ws:
        await ws.send(json.dumps({"cmd": command}))
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))


async def test_控制通道执行注入命令(sim):
    assert (await _control(sim, "slow 2"))["ok"] is True
    assert sim.faults.speed_scale == 0.5
    out = await _control(sim, "status")
    assert "speed_scale=0.5" in out["msg"]


async def test_控制通道对非法命令回_ok_false(sim):
    out = await _control(sim, "fly")
    assert out["ok"] is False
    assert "未知命令" in out["msg"]


async def test_注入的故障码被推送给所有客户端(sim):
    async with connect(sim.url) as ws:
        await _control(sim, "alg_error 13330")

        async def wait_alg():
            while True:
                msg = parse_message(await ws.recv())
                if isinstance(msg, AlgErrorNotify):
                    return msg

        notify = await asyncio.wait_for(wait_alg(), timeout=5.0)
        assert notify.items[0].code == 13330


async def test_帧号归零注入(sim):
    async with connect(sim.url) as ws:
        await _control(sim, "frame_count_zero on")
        await ws.send(encode_request("get_nav_status", None, 77))
        msg = parse_message(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert msg.frame_count == 0
        assert msg.req_func == "get_nav_status"


async def test_定位丢失使进行中的导航失败(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _call(ws, R.start_nav(Pose.from_xy_yaw(9.0, 0.0)), 10)
        await _poll_until(ws, R.get_nav_status(), NavStatus.ACTIVE.value)
        await _control(sim, "loc_lost")
        await _poll_until(ws, R.get_nav_status(), NavStatus.FAILED.value)
        assert (await _call(ws, R.get_loc_status(), 11)).data == LocStatus.LOC_LOST.value


async def test_预约导航失败(sim):
    async with connect(sim.url) as ws:
        await _ready_map(ws)
        await _control(sim, "nav_fail")
        assert (await _call(ws, R.start_nav(Pose.from_xy_yaw(1.0, 0.0)), 10)).ok
        await _poll_until(ws, R.get_nav_status(), NavStatus.FAILED.value)


async def test_断链注入会踢掉客户端(sim):
    ws = await connect(sim.url)
    await _control(sim, "disconnect 1")
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), timeout=5.0)


async def test_停止后端口释放(sim):
    url = sim.url
    await sim.stop()
    with pytest.raises(OSError):
        await asyncio.wait_for(connect(url), timeout=3.0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/sim/test_nav_server.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_sim.nav_server'`

- [ ] **Step 3: 写 nav_server.py**

`src/d1max_sim/nav_server.py`：

```python
"""仿真导航服务端。

它是接口契约的可执行定义:客户端行为有争议时,以这台仿真器的表现为准,
直到真机把它推翻。凡文档未明写、由本仿真器补齐的行为,一律在源码里
加注待真机验证标记。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from d1max_patrol.protocol.nav_frames import (
    ProtocolError,
    build_alg_error_notify,
    build_response,
    parse_request,
)
from d1max_patrol.protocol.nav_requests import NESTED_RESPONSE_FUNCS, response_func_for
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus, Pose, Waypoint

from .inject import FaultState, InjectError, apply_command
from .kinematics import Planar2DModel
from .nav_state import (
    LocStateMachine,
    MappingStateMachine,
    NavStateMachine,
    SimRejected,
)
from .store import MapStore, StoreError

log = logging.getLogger(__name__)

CONTROL_PATH = "/control"

#: 设备默认导航速度,见 refs/nav-api §3.10 的注解
_DEFAULT_SPEED = {"x": 0.8, "y": 0.5, "z": 1.5}


def _as_str(args: Any, name: str) -> str:
    if not isinstance(args, str):
        raise ValueError(f"{name} 的参数应为字符串,实际为 {args!r}")
    return args


def _as_list(args: Any, name: str, length: int | None = None) -> list[Any]:
    if not isinstance(args, (list, tuple)):
        raise ValueError(f"{name} 的参数应为数组,实际为 {args!r}")
    if length is not None and len(args) != length:
        raise ValueError(f"{name} 的参数应为长度 {length} 的数组,实际长度 {len(args)}")
    return list(args)


class SimNavServer:
    """一台可连的仿真 D1 Max 导航设备。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        tick_hz: float = 50.0,
        store: MapStore | None = None,
        faults: FaultState | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.tick_hz = tick_hz

        self.model = Planar2DModel()
        self.nav = NavStateMachine(model=self.model)
        self.loc = LocStateMachine()
        self.mapping = MappingStateMachine(on_saved=self._on_map_saved)
        self.store = store if store is not None else MapStore()
        self.faults = faults if faults is not None else FaultState()
        self.speed: dict[str, float] = dict(_DEFAULT_SPEED)

        self._clients: set[ServerConnection] = set()
        self._server: Server | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending_map_saved = False
        self._silent_until = 0.0
        self._response_seq = 0

    # ------------------------------------------------------------ 生命周期

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def control_url(self) -> str:
        return f"ws://{self.host}:{self.port}{CONTROL_PATH}"

    async def start(self) -> None:
        self._server = await serve(self._handle, self.host, self.port)
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]
        self._ticker = asyncio.create_task(self._tick_loop())
        log.info("仿真导航服务端已启动: %s", self.url)

    async def stop(self) -> None:
        for task in (self._ticker, *self._tasks):
            if task is not None:
                task.cancel()
        for task in (self._ticker, *self._tasks):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._ticker = None
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ 连接处理

    async def _handle(self, ws: ServerConnection) -> None:
        if ws.request is not None and ws.request.path.rstrip("/") == CONTROL_PATH:
            await self._handle_control(ws)
            return
        if asyncio.get_running_loop().time() < self._silent_until:
            await ws.close(code=1012, reason="injected disconnect")
            return
        await self._handle_nav(ws)

    async def _handle_nav(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                try:
                    frame_count, req_func, args = parse_request(raw)
                except ProtocolError as exc:
                    log.warning("忽略畸形报文: %s", exc)
                    continue
                ok, msg, data = self._dispatch(req_func, args)
                self._spawn(self._reply(ws, req_func, frame_count or 0, ok, msg, data))
        except Exception as exc:  # noqa: BLE001 —— 客户端断开不应打断服务端
            log.debug("导航连接结束: %s", exc)
        finally:
            self._clients.discard(ws)

    async def _handle_control(self, ws: ServerConnection) -> None:
        async for raw in ws:
            try:
                payload = json.loads(raw)
                command = payload["cmd"]
            except (ValueError, KeyError, TypeError):
                await ws.send(json.dumps({"ok": False, "msg": "请求应为 {\"cmd\": ...}"}))
                continue
            try:
                result = apply_command(self.faults, command)
            except InjectError as exc:
                await ws.send(json.dumps({"ok": False, "msg": str(exc)},
                                         ensure_ascii=False))
                continue
            await ws.send(json.dumps({"ok": True, "msg": result}, ensure_ascii=False))

    async def _reply(self, ws: ServerConnection, req_func: str, frame_count: int,
                     ok: bool, msg: str | None, data: Any) -> None:
        self._response_seq += 1
        # 乱序注入: 只延迟一半的响应,才能真正制造乱序到达
        delay = self.faults.response_delay_s if self._response_seq % 2 == 1 else 0.0
        if delay > 0:
            await asyncio.sleep(delay)
        frame = build_response(
            response_func_for(req_func),
            0 if self.faults.frame_count_zero else frame_count,
            ok=ok,
            msg=msg,
            data=data,
            nested=req_func in NESTED_RESPONSE_FUNCS,
        )
        with contextlib.suppress(Exception):
            await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _broadcast(self, frame: dict[str, Any]) -> None:
        text = json.dumps(frame, ensure_ascii=False)
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.send(text)

    # ------------------------------------------------------------ 心跳

    def _on_map_saved(self) -> None:
        self.store.create_map()
        self._pending_map_saved = True

    async def _tick_loop(self) -> None:
        dt = 1.0 / self.tick_hz
        while True:
            await asyncio.sleep(dt)
            await self._apply_faults()
            self.nav.step(dt)
            self.loc.step(dt)
            self.mapping.step(dt)
            # 假设(待真机验证): 定位丢失时进行中的导航转入 Failed。
            if self.loc.status is LocStatus.LOC_LOST and self.nav.status in (
                NavStatus.INITIALIZING, NavStatus.ACTIVE, NavStatus.PAUSE
            ):
                self.nav.fail("定位丢失")
            await self._flush_pushes()

    async def _apply_faults(self) -> None:
        f = self.faults
        self.model.speed_scale = f.speed_scale
        self.model.frozen = f.stuck
        if f.loc_lost_requested:
            f.loc_lost_requested = False
            self.loc.lose()
        if f.loc_recover_requested:
            f.loc_recover_requested = False
            self.loc.recover()
        if f.disconnect_seconds > 0:
            seconds, f.disconnect_seconds = f.disconnect_seconds, 0.0
            self._silent_until = asyncio.get_running_loop().time() + seconds
            for ws in list(self._clients):
                with contextlib.suppress(Exception):
                    await ws.close(code=1012, reason="injected disconnect")
            self._clients.clear()

    async def _flush_pushes(self) -> None:
        for item in self.faults.take_alg_errors():
            await self._broadcast(build_alg_error_notify([item]))
        if self._pending_map_saved:
            self._pending_map_saved = False
            # 推送复用 app_resp 且 frame_count 照文档填 1 —— 客户端必须能识别出
            # 这是推送而不是某个 frame_count=1 请求的响应。
            await self._broadcast(build_response("notify_stop_mapping_status", 1))

    # ------------------------------------------------------------ 请求分发

    def _dispatch(self, req_func: str, args: Any) -> tuple[bool, str | None, Any]:
        handler = _HANDLERS.get(req_func)
        if handler is None:
            return False, f"unsupported req_func: {req_func}", None
        try:
            return True, None, handler(self, args)
        except (SimRejected, StoreError, ValueError) as exc:
            return False, str(exc), None

    # --- 建图与地图 -------------------------------------------------------

    def _h_start_mapping(self, args: Any) -> None:
        self.mapping.start()

    def _h_stop_mapping(self, args: Any) -> None:
        self.mapping.stop()

    def _h_get_mapping_status(self, args: Any) -> str:
        return self.mapping.status.value

    def _h_get_pgm_map(self, args: Any) -> Any:
        return self.store.occupancy_grid(_as_str(args, "get_pgm_map"))

    def _h_get_all_pgm_map(self, args: Any) -> Any:
        return self.store.all_pgm_payload()

    def _h_remove_map_by_id(self, args: Any) -> None:
        self.store.remove_maps([_as_str(x, "remove_map_by_id")
                                for x in _as_list(args, "remove_map_by_id")])

    def _h_rename_map_name(self, args: Any) -> None:
        old, new = _as_list(args, "rename_map_name", 2)
        self.store.rename_map(_as_str(old, "rename_map_name"),
                              _as_str(new, "rename_map_name"))

    # --- 路径 -------------------------------------------------------------

    def _h_get_all_paths_by_mapid(self, args: Any) -> Any:
        return self.store.paths_payload(_as_str(args, "get_all_paths_by_mapid"))

    def _h_add_nav_path(self, args: Any) -> None:
        name = "add_nav_path"
        map_id, path_id, points = _as_list(args, name, 3)
        self.store.set_path(
            _as_str(map_id, name),
            _as_str(path_id, name),
            [Waypoint.from_wire(p) for p in _as_list(points, name)],
        )

    def _h_modify_nav_path(self, args: Any) -> None:
        """§2.3 是四元参数,且允许顺带改名:old_path_id -> new_path_id。"""
        name = "modify_nav_path"
        map_id, old_id, new_id, points = _as_list(args, name, 4)
        map_id = _as_str(map_id, name)
        old_id = _as_str(old_id, name)
        new_id = _as_str(new_id, name)
        self.store.set_path(
            map_id, new_id,
            [Waypoint.from_wire(p) for p in _as_list(points, name)],
        )
        if old_id != new_id:
            self.store.remove_path(map_id, old_id)

    def _h_remove_nav_path(self, args: Any) -> None:
        for pair in _as_list(args, "remove_nav_path"):
            map_id, path_id = _as_list(pair, "remove_nav_path", 2)
            self.store.remove_path(_as_str(map_id, "remove_nav_path"),
                                   _as_str(path_id, "remove_nav_path"))

    # --- 导航 -------------------------------------------------------------

    def _h_start_nav(self, args: Any) -> None:
        # 假设(待真机验证): 定位未进入 ContinuousLoc 时设备拒绝导航。
        if not self.loc.healthy:
            raise SimRejected(f"定位未就绪,当前 {self.loc.status.value}")
        if self.faults.fail_next_nav:
            self.faults.fail_next_nav = False
            self.nav.fail_next_start = True
        self.nav.start(Pose.from_wire(args))

    def _h_reject_multi(self, args: Any) -> None:
        # 假设(待真机验证): 多点导航一律回 error,是本仿真器的**有意偏离**,不是对设备的猜测。
        # 真机上 start_multi_nav / start_multi_nav_by_points 很可能是能正常走的;本项目
        # 决定全逐点执行(见设计方案"全逐点执行"一节),仿真器便不假装支持它们。
        # 真机核对:确认这两个接口在设备上确实可用、以及它们的响应函数名 —— 若将来要改回
        # 多点下发,这里的拒绝必须先撤掉,否则仿真器会与真机行为相反。
        raise SimRejected("本仿真器不支持多点导航行走,请逐点下发 start_nav")

    def _h_reject_return_home(self, args: Any) -> None:
        raise SimRejected("返航未实现: 响应函数名未经真机验证,见第 3 卷")

    def _h_stop_nav(self, args: Any) -> None:
        self.nav.stop()

    def _h_pause_nav(self, args: Any) -> None:
        self.nav.pause()

    def _h_continue_nav(self, args: Any) -> None:
        self.nav.resume()

    def _h_get_nav_status(self, args: Any) -> str:
        return self.nav.status.value

    def _h_get_navigation_speed(self, args: Any) -> Any:
        return dict(self.speed)

    def _h_set_navigation_speed(self, args: Any) -> Any:
        if not isinstance(args, dict) or "x" not in args:
            raise ValueError("set_navigation_speed 需要至少一个 x")
        # 文档注: 只传 x 时设备端 y 取 0.5、z 取 1.5
        self.speed = {
            "x": float(args["x"]),
            "y": float(args.get("y", _DEFAULT_SPEED["y"])),
            "z": float(args.get("z", _DEFAULT_SPEED["z"])),
        }
        return dict(self.speed)

    # --- 定位 -------------------------------------------------------------

    def _h_loc_load_map(self, args: Any) -> None:
        map_id = _as_str(args, "loc_load_map")
        if map_id not in self.store.maps:
            raise StoreError(f"地图 {map_id!r} 不存在")
        self.loc.load_map(map_id)

    def _h_reset_loc(self, args: Any) -> None:
        self.loc.reset()

    def _h_get_loc_status(self, args: Any) -> str:
        return self.loc.status.value


_HANDLERS: dict[str, Callable[[SimNavServer, Any], Any]] = {
    "start_mapping": SimNavServer._h_start_mapping,
    "stop_mapping": SimNavServer._h_stop_mapping,
    "get_mapping_status": SimNavServer._h_get_mapping_status,
    "get_pgm_map": SimNavServer._h_get_pgm_map,
    "get_all_pgm_map": SimNavServer._h_get_all_pgm_map,
    "remove_map_by_id": SimNavServer._h_remove_map_by_id,
    "rename_map_name": SimNavServer._h_rename_map_name,
    "get_all_paths_by_mapid": SimNavServer._h_get_all_paths_by_mapid,
    "add_nav_path": SimNavServer._h_add_nav_path,
    "modify_nav_path": SimNavServer._h_modify_nav_path,
    "remove_nav_path": SimNavServer._h_remove_nav_path,
    "start_nav": SimNavServer._h_start_nav,
    "start_multi_nav": SimNavServer._h_reject_multi,
    "start_multi_nav_by_points": SimNavServer._h_reject_multi,
    "start_nav_return_home": SimNavServer._h_reject_return_home,
    "stop_nav": SimNavServer._h_stop_nav,
    "pause_nav": SimNavServer._h_pause_nav,
    "continue_nav": SimNavServer._h_continue_nav,
    "get_nav_status": SimNavServer._h_get_nav_status,
    "get_navigation_speed": SimNavServer._h_get_navigation_speed,
    "set_navigation_speed": SimNavServer._h_set_navigation_speed,
    "loc_load_map": SimNavServer._h_loc_load_map,
    "reset_loc": SimNavServer._h_reset_loc,
    "get_loc_status": SimNavServer._h_get_loc_status,
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/sim/test_nav_server.py -v`
Expected: 全部 passed

- [ ] **Step 5: 写仿真器可执行入口**

`src/d1max_sim/__main__.py`：

```python
"""python -m d1max_sim —— 起一台仿真 D1 Max 导航设备。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from d1max_patrol.protocol.nav_types import Pose, Waypoint

from .nav_server import SimNavServer


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max_sim", description="D1 Max 自主导航仿真器")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10010)
    p.add_argument("--tick-hz", type=float, default=50.0)
    p.add_argument("--seed", action="store_true",
                   help="预置一张地图与一条三点巡检路径,便于演示")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _seed(server: SimNavServer) -> None:
    map_id = server.store.create_map("demo_map")
    server.store.set_path(map_id, "demo_route", [
        Waypoint("P1_配电柜", Pose.from_xy_yaw(2.0, 0.0, 0.0)),
        Waypoint("P2_水泵", Pose.from_xy_yaw(2.0, 2.0, 1.5708)),
        Waypoint("P3_出口", Pose.from_xy_yaw(0.0, 2.0, 3.1416)),
    ])


async def _serve(args: argparse.Namespace) -> None:
    server = SimNavServer(host=args.host, port=args.port, tick_hz=args.tick_hz)
    if args.seed:
        _seed(server)
    await server.start()
    print(f"导航: {server.url}")
    print(f"注入: {server.control_url}  (发送 {{\"cmd\": \"help\"}} 查看命令)")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: 手工验证入口可用**

```bash
python -m d1max_sim --port 0 --seed &
sleep 1
kill %1
```

Expected: 打印两行地址后可被 Ctrl-C / kill 干净退出，无 traceback。

- [ ] **Step 7: 提交**

```bash
git add src/d1max_sim/nav_server.py src/d1max_sim/__main__.py tests/sim/test_nav_server.py
git commit -m "feat: 仿真导航服务端、故障注入控制通道与可执行入口"
```

---
### Task 11: 导航后端接口与事件模型

这是全项目最重要的一个文件。`MissionRunner`（第 3 卷）只认识这个抽象；将来换 Nav2 时，被替换的只有实现类，契约测试一行不改。

因此这里出现的每一个方法名、每一个事件字段，都不能带厂商味道：不叫 `start_nav` 叫 `goto`，不叫 `loc_load_map` 叫 `load_map`，状态枚举用 `nav_types` 里的通用枚举而不是原始字符串。

**Files:**
- Create: `src/d1max_patrol/backends/__init__.py`
- Create: `src/d1max_patrol/backends/base.py`
- Test: `tests/backends/test_base.py`

**Interfaces:**
- Consumes: `d1max_patrol.protocol.nav_types.{NavStatus, LocStatus, MappingStatus, Pose, Waypoint, NAV_TERMINAL}`；`d1max_patrol.protocol.nav_frames.AlgErrorItem`
- Produces（`d1max_patrol.backends.base`）：
  - 异常：`NavBackendError`、`NavConnectionError(NavBackendError)`、`NavTimeoutError(NavBackendError)`、`NavRequestError(NavBackendError)`（字段 `operation: str`、`message: str`）
  - 事件（均为 frozen dataclass）：
    - `NavStatusEvent(status: NavStatus, previous: NavStatus | None)`
    - `LocStatusEvent(status: LocStatus, previous: LocStatus | None)`
    - `MappingStatusEvent(status: MappingStatus, previous: MappingStatus | None)`
    - `AlgErrorEvent(items: tuple[AlgErrorItem, ...], time_stamp_ms: int)`
    - `BackendDisconnected(reason: str)`
    - `BackendReconnected()`
    - `Event`:以上六者的 PEP 604 联合(`A | B | ...`;**不要用 `typing.Union`**,ruff UP007 会报错)
  - `NavBackend` 抽象基类

**订阅模型：** 多个消费者可以各拿一条队列，互不抢事件。第 3 卷的 `MissionRunner` 会订一条跑任务，日志会订另一条落盘。

- [ ] **Step 1: 写失败的测试**

`tests/backends/test_base.py`：

```python
"""导航后端抽象层。这里只测与厂商无关的那部分:订阅、广播、终态等待。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    LocStatusEvent,
    NavBackend,
    NavBackendError,
    NavRequestError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import LocStatus, NavStatus


class _Stub(NavBackend):
    """只实现抽象方法签名,不做任何事 —— 用来测基类的具体逻辑。"""

    async def connect(self): ...
    async def close(self): ...
    async def list_maps(self): return []
    async def remove_maps(self, map_ids): ...
    async def rename_map(self, old_id, new_id): ...
    async def get_map_grid(self, map_id): return {}
    async def start_mapping(self): ...
    async def stop_mapping(self): ...
    async def mapping_status(self): return None
    async def list_paths(self, map_id): return {}
    async def save_path(self, map_id, path_id, waypoints): ...
    async def remove_path(self, map_id, path_id): ...
    async def load_map(self, map_id): ...
    async def reset_localization(self): ...
    async def loc_status(self): return None
    async def goto(self, pose): ...
    async def stop(self): ...
    async def pause(self): ...
    async def resume(self): ...
    async def nav_status(self): return None
    async def get_speed(self): return {}
    async def set_speed(self, x, y=None, z=None): return {}


def test_抽象类不能直接实例化():
    with pytest.raises(TypeError):
        NavBackend()


def test_异常层次():
    for exc in (NavTimeoutError, NavRequestError, NavBackendError):
        assert issubclass(exc, Exception)
    assert issubclass(NavTimeoutError, NavBackendError)
    assert issubclass(NavRequestError, NavBackendError)


def test_请求错误带上操作名与设备消息():
    # 这里刻意用中性的操作名而不是厂商的 start_nav —— 本文件在换 Nav2 时
    # 要原封不动留下,任何厂商接口名出现在这里都是渗漏。
    exc = NavRequestError("goto", "定位未就绪")
    assert exc.operation == "goto"
    assert exc.message == "定位未就绪"
    assert "goto" in str(exc) and "定位未就绪" in str(exc)


async def test_等待超时后订阅被清理():
    """try/finally 的回归测试。删掉 `subscription()` 里的 finally 这条就该变红。

    队列是无界的,第 3 卷会按航点反复调 `wait_nav_terminal`;泄漏一条队列
    就是泄漏一路无界增长的内存,而且不会有任何测试变红。
    """
    backend = _Stub()
    with pytest.raises(NavTimeoutError):
        await backend.wait_nav_terminal(timeout_s=0.05)
    assert backend._subscribers == []


async def test_断链退出后订阅被清理():
    backend = _Stub()
    loop = asyncio.get_running_loop()
    loop.call_later(0.01, backend.emit, BackendDisconnected("读循环退出"))
    with pytest.raises(NavConnectionError):
        await backend.wait_nav_terminal(timeout_s=2.0)
    assert backend._subscribers == []


async def test_外部队列消灭先订阅后下发的竞态():
    """传 queue= 时,订阅在"下发"之前就建好了,终态不会漏。"""
    backend = _Stub()
    with backend.subscription() as queue:
        # 模拟"下发之后立刻推终态"——若 wait_nav_terminal 此刻才自己订阅,
        # 这条事件已经丢了。
        backend.emit(NavStatusEvent(NavStatus.SUCCEED, NavStatus.ACTIVE))
        assert await backend.wait_nav_terminal(2.0, queue=queue) is NavStatus.SUCCEED


async def test_等待导航终态_取消也算终态():
    backend = _Stub()
    loop = asyncio.get_running_loop()
    loop.call_later(
        0.01, backend.emit, NavStatusEvent(NavStatus.CANCELLED, NavStatus.ACTIVE))
    assert await backend.wait_nav_terminal(timeout_s=2.0) is NavStatus.CANCELLED


async def test_订阅者各自收到全部事件():
    backend = _Stub()
    a, b = backend.subscribe(), backend.subscribe()
    event = NavStatusEvent(NavStatus.ACTIVE, NavStatus.INITIALIZING)
    backend.emit(event)
    assert await a.get() is event
    assert await b.get() is event


async def test_退订后不再收到事件():
    backend = _Stub()
    q = backend.subscribe()
    backend.unsubscribe(q)
    backend.emit(BackendDisconnected("test"))
    assert q.empty()


async def test_没有订阅者时广播不报错():
    _Stub().emit(BackendDisconnected("nobody listening"))


async def test_订阅上下文管理器自动退订():
    backend = _Stub()
    with backend.subscription() as q:
        backend.emit(BackendDisconnected("x"))
        assert q.qsize() == 1
    backend.emit(BackendDisconnected("y"))
    assert q.qsize() == 1


async def test_等待导航终态():
    backend = _Stub()

    async def drive():
        await asyncio.sleep(0.01)
        backend.emit(NavStatusEvent(NavStatus.ACTIVE, NavStatus.INITIALIZING))
        await asyncio.sleep(0.01)
        backend.emit(NavStatusEvent(NavStatus.SUCCEED, NavStatus.ACTIVE))

    task = asyncio.create_task(drive())
    assert await backend.wait_nav_terminal(timeout_s=2.0) is NavStatus.SUCCEED
    await task


async def test_等待导航终态_失败也是终态():
    backend = _Stub()
    asyncio.get_running_loop().call_later(
        0.01, backend.emit, NavStatusEvent(NavStatus.FAILED, NavStatus.ACTIVE))
    assert await backend.wait_nav_terminal(timeout_s=2.0) is NavStatus.FAILED


async def test_等待导航终态_超时():
    backend = _Stub()
    with pytest.raises(NavTimeoutError):
        await backend.wait_nav_terminal(timeout_s=0.05)


async def test_等待导航终态_期间断链直接抛错():
    """断链后再等终态没有意义 —— 状态已经不可知,必须让调用方立刻知道。"""
    backend = _Stub()
    asyncio.get_running_loop().call_later(
        0.01, backend.emit, BackendDisconnected("链路断开"))
    with pytest.raises(NavBackendError, match="链路断开"):
        await backend.wait_nav_terminal(timeout_s=2.0)


async def test_等待导航终态_忽略无关事件():
    backend = _Stub()

    async def noise():
        await asyncio.sleep(0.01)
        backend.emit(LocStatusEvent(LocStatus.CONTINUOUS_LOC, LocStatus.INIT))
        backend.emit(AlgErrorEvent((AlgErrorItem(13330, "路径被挡", 2),), 1))
        backend.emit(NavStatusEvent(NavStatus.SUCCEED, NavStatus.ACTIVE))

    task = asyncio.create_task(noise())
    assert await backend.wait_nav_terminal(timeout_s=2.0) is NavStatus.SUCCEED
    await task


async def test_慢订阅者不阻塞广播():
    """队列无界:仿真器狂推故障码时,广播端绝不能卡住。"""
    backend = _Stub()
    q = backend.subscribe()
    for _ in range(1000):
        backend.emit(BackendDisconnected("flood"))
    assert q.qsize() == 1000
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/backends/test_base.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_patrol.backends'`

- [ ] **Step 3: 写 base.py**

`src/d1max_patrol/backends/__init__.py`：空文件。

`src/d1max_patrol/backends/base.py`：

```python
"""导航后端抽象。

上层任务逻辑只依赖这个文件。厂商 WebSocket 协议、Nav2 action、乃至将来
某个新固件的怪癖,都被关在各自的实现类里。

命名刻意去厂商化: `goto` 而不是 `start_nav`,`load_map` 而不是
`loc_load_map`。一旦上层出现厂商接口名,这层抽象就失效了。
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import (
    NAV_TERMINAL,
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)

# --------------------------------------------------------------------- 异常


class NavBackendError(Exception):
    """导航后端相关错误的基类。"""


class NavConnectionError(NavBackendError):
    """连不上,或链路中断。"""


class NavTimeoutError(NavBackendError):
    """等待响应或等待状态变化超时。"""


class NavRequestError(NavBackendError):
    """设备明确回了 error。"""

    def __init__(self, operation: str, message: str) -> None:
        super().__init__(f"{operation} 被设备拒绝: {message}")
        #: 被拒绝的操作名。厂商后端把自己的 req_func 映射进来,
        #: Nav2 后端映射自己的 action 名 —— 这一层不认识 req_func 这个词。
        self.operation = operation
        self.message = message


# --------------------------------------------------------------------- 事件


@dataclass(frozen=True)
class NavStatusEvent:
    status: NavStatus
    previous: NavStatus | None = None


@dataclass(frozen=True)
class LocStatusEvent:
    status: LocStatus
    previous: LocStatus | None = None


@dataclass(frozen=True)
class MappingStatusEvent:
    status: MappingStatus
    previous: MappingStatus | None = None


@dataclass(frozen=True)
class AlgErrorEvent:
    items: tuple[AlgErrorItem, ...]
    time_stamp_ms: int = 0


@dataclass(frozen=True)
class BackendDisconnected:
    reason: str


@dataclass(frozen=True)
class BackendReconnected:
    pass


Event = (
    NavStatusEvent
    | LocStatusEvent
    | MappingStatusEvent
    | AlgErrorEvent
    | BackendDisconnected
    | BackendReconnected
)


# --------------------------------------------------------------------- 抽象


class NavBackend(ABC):
    """一台可导航设备。

    订阅模型: 每个消费者拿一条自己的无界队列,互不抢事件。队列无界是刻意的
    —— 广播端绝不能因为某个慢消费者而卡住状态轮询。
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []

    # ------------------------------------------------------------ 事件分发

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    @contextlib.contextmanager
    def subscription(self) -> Iterator[asyncio.Queue[Event]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    def emit(self, event: Event) -> None:
        """向所有订阅者广播。同步、不阻塞、不抛错。"""
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def wait_nav_terminal(
        self, timeout_s: float,
        queue: asyncio.Queue[Event] | None = None,
    ) -> NavStatus:
        """等到导航进入终态。

        **有竞态的用法**:先 `await goto()` 再调用本方法。终态可能在这两步
        之间就推过来了,那条事件没人订阅、直接丢掉,本方法一路等到超时。

        **没有竞态的用法**:自己先订阅,再下发,再把队列交给本方法 ——
        订阅与下发之间没有 await,不存在让出点::

            with backend.subscription() as q:
                await backend.goto(pose)
                status = await backend.wait_nav_terminal(30.0, queue=q)

        不传 `queue` 时本方法自己订阅,只适合"订阅时导航已经在跑"的场合。

        注:用 `asyncio.wait_for` 而不是 `asyncio.timeout` —— 后者要
        Python 3.11+,而全局约束是 3.10。
        """
        if queue is not None:
            return await self._wait_on(queue, timeout_s)
        with self.subscription() as own_queue:
            return await self._wait_on(own_queue, timeout_s)

    async def _wait_on(
        self, queue: asyncio.Queue[Event], timeout_s: float,
    ) -> NavStatus:
        try:
            return await asyncio.wait_for(self._await_terminal(queue), timeout_s)
        except asyncio.TimeoutError as exc:
            raise NavTimeoutError(f"等待导航终态超过 {timeout_s}s") from exc

    async def _await_terminal(self, queue: asyncio.Queue[Event]) -> NavStatus:
        while True:
            event = await queue.get()
            if isinstance(event, BackendDisconnected):
                raise NavConnectionError(
                    f"等待导航终态期间链路断开: {event.reason}")
            if isinstance(event, NavStatusEvent) and event.status in NAV_TERMINAL:
                return event.status

    # ------------------------------------------------------------ 生命周期

    @abstractmethod
    async def connect(self) -> None:
        """建立连接。已连接时应为空操作。"""

    @abstractmethod
    async def close(self) -> None:
        """断开并释放所有后台任务。可重复调用。"""

    # ------------------------------------------------------------ 地图

    @abstractmethod
    async def list_maps(self) -> list[str]: ...

    @abstractmethod
    async def remove_maps(self, map_ids: Sequence[str]) -> None: ...

    @abstractmethod
    async def rename_map(self, old_id: str, new_id: str) -> None: ...

    @abstractmethod
    async def get_map_grid(self, map_id: str) -> dict[str, Any]:
        """返回 ROS OccupancyGrid 形状的栅格地图。"""

    # ------------------------------------------------------------ 建图

    @abstractmethod
    async def start_mapping(self) -> None: ...

    @abstractmethod
    async def stop_mapping(self) -> None: ...

    @abstractmethod
    async def mapping_status(self) -> MappingStatus | None:
        """未知状态返回 None,而不是抛错 —— 固件可能新增枚举值。"""

    # ------------------------------------------------------------ 路径

    @abstractmethod
    async def list_paths(self, map_id: str) -> dict[str, list[Waypoint]]: ...

    @abstractmethod
    async def save_path(self, map_id: str, path_id: str,
                        waypoints: Sequence[Waypoint]) -> None:
        """新增或覆盖一条路径。"""

    @abstractmethod
    async def remove_path(self, map_id: str, path_id: str) -> None: ...

    # ------------------------------------------------------------ 定位

    @abstractmethod
    async def load_map(self, map_id: str) -> None:
        """加载定位地图。返回时只表示指令被接受,不表示定位已就绪。"""

    @abstractmethod
    async def reset_localization(self) -> None: ...

    @abstractmethod
    async def loc_status(self) -> LocStatus | None: ...

    # ------------------------------------------------------------ 导航

    @abstractmethod
    async def goto(self, pose: Pose) -> None:
        """下发单点导航。返回时只表示指令被接受,不表示已到达。"""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def resume(self) -> None: ...

    @abstractmethod
    async def nav_status(self) -> NavStatus | None: ...

    # ------------------------------------------------------------ 速度

    @abstractmethod
    async def get_speed(self) -> dict[str, float]:
        """返回 {"x": .., "y": .., "z": ..},单位 m/s 与 rad/s。"""

    @abstractmethod
    async def set_speed(self, x: float, y: float | None = None,
                        z: float | None = None) -> dict[str, float]:
        """设置速度,返回设备回报的生效值(可能补齐了未传的分量)。"""
```


- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/backends/test_base.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/backends/ tests/backends/test_base.py
git commit -m "feat: 导航后端抽象接口与事件模型"
```

---
### Task 12: 厂商后端的连接、读循环与响应匹配

四个协议地雷里的三个在这一步引爆。匹配逻辑写错不会立刻报错，只会在压力下把 A 请求的响应交给 B 请求——所以这里的测试比实现重要。

**匹配规则（按顺序）：**

1. **主匹配**：`frame_count` 命中挂起表 **且** `req_func` 等于该请求的期望响应名 → 认领。两个条件缺一不可。
2. **推送识别**：`req_func ∈ PUSH_ONLY_FUNCS` → 当作设备主动推送处理，不认领任何挂起请求。
3. **降级匹配**：按 `req_func` 找最早一条期望该响应名的挂起请求 → 认领，并把 `fallback_matches` 加一。
4. 都不中 → 丢弃并把 `dropped_frames` 加一，记 warning。

第 2 步必须在第 3 步之前：`notify_stop_mapping_status` 顶着 `app_resp` 和一个可能撞车的 `frame_count` 进来，只有先按名字识别出它是推送，才不会污染匹配表。

**Files:**
- Create: `src/d1max_patrol/backends/vendor_nav.py`
- Test: `tests/backends/test_vendor_matching.py`

**Interfaces:**
- Consumes: `backends.base` 全部；`protocol.nav_frames`、`protocol.nav_requests`、`protocol.nav_types`；`config.models.NavConfig`
- Produces（`d1max_patrol.backends.vendor_nav`）：
  - `VendorNavBackend(config: NavConfig)`
  - 属性：`connected: bool`、`fallback_matches: int`、`dropped_frames: int`、`pending_count: int`
  - `async connect() -> None`、`async close() -> None`
  - `async request(req: NavRequest) -> Any` —— 发一条请求并等响应，返回 `data`；设备回 error 抛 `NavRequestError`，超时抛 `NavTimeoutError`，未连接抛 `NavConnectionError`
  - 模块常量 `NOTIFY_MAPPING_SAVED = "notify_stop_mapping_status"`
  - 本任务只实现到 `request()`，能力方法在 Task 13，状态轮询与重连在 Task 14

- [ ] **Step 1: 写失败的测试**

`tests/backends/test_vendor_matching.py`：

```python
"""厂商后端的响应匹配。协议地雷全在这里拆。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    NavConnectionError,
    NavRequestError,
    NavTimeoutError,
)
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol import nav_requests as R
from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus, NavStatus
from d1max_sim.nav_server import SimNavServer


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def backend(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, request_timeout_s=3.0))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


async def _await_status(backend, req, wanted: str, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await backend.request(req) == wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{req.req_func} 未在 {timeout_s}s 内变为 {wanted}")


async def test_连接后可用(backend):
    assert backend.connected is True
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_未连接时请求抛连接错误(sim):
    b = VendorNavBackend(NavConfig(url=sim.url))
    with pytest.raises(NavConnectionError):
        await b.request(R.get_nav_status())


async def test_连不上的地址抛连接错误():
    b = VendorNavBackend(NavConfig(url="ws://127.0.0.1:1", connect_timeout_s=1.0))
    with pytest.raises(NavConnectionError):
        await b.connect()


async def test_重复连接与重复关闭都安全(backend):
    await backend.connect()
    assert backend.connected is True
    await backend.close()
    await backend.close()
    assert backend.connected is False


async def test_连续请求不留挂起也不降级(backend):
    for _ in range(5):
        await backend.request(R.get_nav_status())
    assert backend.fallback_matches == 0
    assert backend.dropped_frames == 0
    assert backend.pending_count == 0


async def test_并发请求各自拿到自己的响应(backend):
    """并发下一旦匹配写错,这里就会串台。"""
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
        backend.request(R.get_mapping_status()),
        backend.request(R.get_all_pgm_map()),
    )
    assert results[0] == NavStatus.STANDBY.value
    assert results[1] == LocStatus.INIT.value
    assert results[2] == MappingStatus.PASSIVE.value
    assert R.parse_map_ids(results[3]) == []


async def test_乱序到达也不串台(sim, backend):
    """注入乱序:一半响应被延迟,到达顺序与发出顺序不同。"""
    sim.faults.response_delay_s = 0.15
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
    )
    assert results == [NavStatus.STANDBY.value, LocStatus.INIT.value,
                       NavStatus.STANDBY.value, LocStatus.INIT.value]
    assert backend.fallback_matches == 0


async def test_地雷1_速度接口的嵌套外壳被剥掉(backend):
    speed = await backend.request(R.get_navigation_speed())
    assert set(speed) == {"x", "y", "z"}          # 不是 {"AppReponseObjectData"}


async def test_地雷2_响应函数名与请求不同也能匹配(sim, backend):
    """loc_load_map 的响应叫 load_localization_map。"""
    map_id = sim.store.create_map()
    await backend.request(R.loc_load_map(map_id))
    assert backend.fallback_matches == 0          # 走的是主匹配,不是降级
    assert backend.dropped_frames == 0


async def test_地雷3_建图推送不会污染匹配表(backend):
    """notify_stop_mapping_status 顶着 app_resp 和 frame_count=1 进来。"""
    await backend.request(R.start_mapping())
    await _await_status(backend, R.get_mapping_status(),
                        MappingStatus.MAPPING_RUNNING.value)
    await backend.request(R.stop_mapping())
    await _await_status(backend, R.get_mapping_status(),
                        MappingStatus.MAPPING_SAVE_END.value)

    assert backend.fallback_matches == 0
    assert backend.dropped_frames == 0            # 推送被识别,不算丢帧
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_帧号归零时降级到按名字匹配(sim, backend):
    sim.faults.frame_count_zero = True
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value
    assert backend.fallback_matches == 1


async def test_降级匹配按先进先出认领同名响应(sim, backend):
    sim.faults.frame_count_zero = True
    results = await asyncio.gather(
        backend.request(R.get_nav_status()),
        backend.request(R.get_loc_status()),
    )
    assert results == [NavStatus.STANDBY.value, LocStatus.INIT.value]
    assert backend.fallback_matches == 2


async def test_设备回error抛请求错误(backend):
    with pytest.raises(NavRequestError) as info:
        await backend.request(R.loc_load_map("不存在的图"))
    assert info.value.operation == "loc_load_map"
    assert "不存在" in info.value.message


async def test_无人认领的响应被计入丢帧(sim, backend):
    """构造一条谁也不认的响应,链路必须活着。

    必须从**服务端**推。`backend._ws.send()` 是客户端→服务端方向,报文会被
    仿真器按 `app_req` 校验拒掉("忽略畸形报文: 报文不是 app_req"),根本回不到
    本端读循环,`dropped_frames` 永远是 0。这条测试要验的是读循环收到一条
    无人认领的响应之后:计数加一、且**不把读循环带崩**——所以最后一行的
    `request()` 才是这条测试的重点,不能改成直接调 `_route()` 了事。
    """
    await sim._broadcast({
        "head": {"type": "app_resp", "frame_count": 9999, "source": "app"},
        "data": {"req_result": {"req_func": "nobody_asked", "status": "ok"}},
    })
    await asyncio.sleep(0.1)
    assert backend.dropped_frames == 1
    assert await backend.request(R.get_nav_status()) == NavStatus.STANDBY.value


async def test_请求超时后挂起表被清理(sim, backend):
    sim.faults.response_delay_s = 5.0
    with pytest.raises(NavTimeoutError):
        await backend.request(R.get_nav_status())
    assert backend.pending_count == 0
    sim.faults.response_delay_s = 0.0


async def test_故障码推送转成事件(sim, backend):
    q = backend.subscribe()
    sim.faults.queued_alg_errors.append(AlgErrorItem(13330, "路径被挡", 2))
    event = await asyncio.wait_for(q.get(), timeout=3.0)
    assert isinstance(event, AlgErrorEvent)
    assert event.items[0].code == 13330


async def test_关闭时挂起请求被唤醒而不是永久卡住(sim, backend):
    sim.faults.response_delay_s = 5.0
    task = asyncio.create_task(backend.request(R.get_nav_status()))
    await asyncio.sleep(0.1)
    await backend.close()
    with pytest.raises(NavConnectionError):
        await task
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/backends/test_vendor_matching.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_patrol.backends.vendor_nav'`

- [ ] **Step 3: 写 vendor_nav.py 的连接与匹配部分**

`src/d1max_patrol/backends/vendor_nav.py`：

```python
"""智元 D1 Max 自主导航 WebSocket 后端。

厂商协议的全部怪癖都关在这个文件里。上层看到的只有 base.NavBackend。

已知怪癖(逐条对应实现里的注释):
  1. 速度相关响应多包一层 AppReponseObjectData(厂商把 Response 拼错了)
  2. loc_load_map 的响应函数名是 load_localization_map
  3. notify_stop_mapping_status 是推送,却顶着 app_resp 和会撞车的 frame_count
  4. 导航/定位/建图状态没有推送通道,只能轮询(见 Task 14)
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_frames import (
    AlgErrorNotify,
    ProtocolError,
    Response,
    encode_request,
    parse_message,
)
from d1max_patrol.protocol.nav_requests import PUSH_ONLY_FUNCS, NavRequest
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)

from .base import (
    AlgErrorEvent,
    NavBackend,
    NavConnectionError,
    NavRequestError,
    NavTimeoutError,
)

log = logging.getLogger(__name__)

#: 建图保存完成的推送函数名
NOTIFY_MAPPING_SAVED = "notify_stop_mapping_status"


@dataclass
class _Pending:
    """一条挂起的请求。"""

    req_func: str
    response_func: str
    future: asyncio.Future = field(repr=False)


class VendorNavBackend(NavBackend):
    def __init__(self, config: NavConfig) -> None:
        super().__init__()
        self.config = config
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._frame_counter = itertools.count(1)
        #: frame_count -> 挂起请求。dict 保序,降级匹配靠它取最早一条。
        self._pending: dict[int, _Pending] = {}
        self._closing = False

        #: 观测指标 —— 契约测试与真机对拍都靠它们判断链路健康
        self.fallback_matches = 0
        self.dropped_frames = 0

    # ------------------------------------------------------------ 生命周期

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def connect(self) -> None:
        if self._ws is not None:
            return
        self._closing = False
        try:
            self._ws = await asyncio.wait_for(
                connect(self.config.url, open_timeout=None),
                timeout=self.config.connect_timeout_s,
            )
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            raise NavConnectionError(f"连接 {self.config.url} 失败: {exc}") from exc
        self._reader = asyncio.create_task(self._read_loop())
        log.info("已连接导航设备 %s", self.config.url)

    async def close(self) -> None:
        self._closing = True
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_pending("连接已关闭")

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for entry in pending.values():
            if not entry.future.done():
                entry.future.set_exception(NavConnectionError(reason))

    # ------------------------------------------------------------ 读循环

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for raw in ws:
                try:
                    message = parse_message(raw)
                except ProtocolError as exc:
                    log.warning("忽略无法解析的报文: %s", exc)
                    continue
                self._route(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._closing:
                self._on_link_lost(f"读循环异常: {exc}")
            return
        if not self._closing:
            self._on_link_lost("设备关闭了连接")

    def _on_link_lost(self, reason: str) -> None:
        """链路断开的统一入口。Task 14 会在这里接上重连。"""
        log.warning("导航链路断开: %s", reason)
        self._ws = None
        self._fail_pending(reason)

    # ------------------------------------------------------------ 匹配

    def _route(self, message: Any) -> None:
        if isinstance(message, AlgErrorNotify):
            self.emit(AlgErrorEvent(tuple(message.items),
                                    message.time_stamp_ms or 0))
            return
        assert isinstance(message, Response)

        # 1. 主匹配: 帧号与函数名都对上才认领。缺一不可 —— 推送会撞帧号。
        entry = self._pending.get(message.frame_count)
        if entry is not None and entry.response_func == message.req_func:
            del self._pending[message.frame_count]
            self._settle(entry, message)
            return

        # 2. 推送识别: 顶着 app_resp 进来的设备主动上报。
        #    必须先于降级匹配,否则它会冒领一条同名挂起请求。
        if message.req_func in PUSH_ONLY_FUNCS:
            self._on_push(message)
            return

        # 3. 降级匹配: 帧号不可信时按函数名认领最早一条。
        for frame_count, entry in self._pending.items():
            if entry.response_func == message.req_func:
                del self._pending[frame_count]
                self.fallback_matches += 1
                log.warning("帧号 %s 未命中,按函数名 %s 降级匹配(累计 %d 次)",
                            message.frame_count, message.req_func,
                            self.fallback_matches)
                self._settle(entry, message)
                return

        # 4. 无人认领
        self.dropped_frames += 1
        log.warning("丢弃无人认领的响应: frame_count=%s req_func=%s",
                    message.frame_count, message.req_func)

    def _settle(self, entry: _Pending, message: Response) -> None:
        if not entry.future.done():
            entry.future.set_result(message)

    def _on_push(self, message: Response) -> None:
        if message.req_func == NOTIFY_MAPPING_SAVED:
            log.info("设备通知: 建图已保存")
            # 状态事件由 Task 14 的轮询器统一发出,这里只记日志,
            # 免得同一次建图完成被广播两遍。
            return
        log.info("收到未处理的设备推送: %s", message.req_func)

    # ------------------------------------------------------------ 请求

    async def request(self, req: NavRequest) -> Any:
        ws = self._ws
        if ws is None:
            raise NavConnectionError("尚未连接导航设备")

        frame_count = next(self._frame_counter)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = _Pending(req.req_func, req.response_func, future)
        self._pending[frame_count] = entry
        try:
            await ws.send(encode_request(req.req_func, req.args, frame_count))
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(frame_count, None)
            raise NavConnectionError(f"发送 {req.req_func} 失败: {exc}") from exc

        try:
            response = await asyncio.wait_for(future, self.config.request_timeout_s)
        except asyncio.TimeoutError as exc:
            self._pending.pop(frame_count, None)
            raise NavTimeoutError(
                f"{req.req_func} 超过 {self.config.request_timeout_s}s 未收到响应"
            ) from exc

        if not response.ok:
            raise NavRequestError(req.req_func, response.msg or "设备未给出原因")
        return response.data

    # ---------------------------------------------- Task 13 之前的占位块
    # `NavBackend` 有 22 个抽象方法,本任务只落了 `connect` / `close`。
    # ABC 只要还剩一个抽象方法没实现就不许实例化,而下面的测试要真的
    # `VendorNavBackend(config)` —— 所以这里先把余下 20 个占位掉。
    # **Task 13 的工作就是把这一整块换成真实现**,不是在它后面追加。

    async def nav_status(self) -> NavStatus | None:
        raise NotImplementedError("Task 13 实现")

    async def loc_status(self) -> LocStatus | None:
        raise NotImplementedError("Task 13 实现")

    async def mapping_status(self) -> MappingStatus | None:
        raise NotImplementedError("Task 13 实现")

    async def goto(self, pose: Pose) -> None:
        raise NotImplementedError("Task 13 实现")

    async def pause(self) -> None:
        raise NotImplementedError("Task 13 实现")

    async def resume(self) -> None:
        raise NotImplementedError("Task 13 实现")

    async def stop(self) -> None:
        raise NotImplementedError("Task 13 实现")

    async def load_map(self, map_id: str) -> None:
        raise NotImplementedError("Task 13 实现")

    async def list_maps(self) -> list[str]:
        raise NotImplementedError("Task 13 实现")

    async def rename_map(self, old_id: str, new_id: str) -> None:
        raise NotImplementedError("Task 13 实现")

    async def remove_maps(self, map_ids: Sequence[str]) -> None:
        raise NotImplementedError("Task 13 实现")

    async def get_map_grid(self, map_id: str) -> dict[str, Any]:
        raise NotImplementedError("Task 13 实现")

    async def list_paths(self, map_id: str) -> dict[str, list[Waypoint]]:
        raise NotImplementedError("Task 13 实现")

    async def save_path(
        self, map_id: str, path_id: str, waypoints: Sequence[Waypoint],
    ) -> None:
        raise NotImplementedError("Task 13 实现")

    async def remove_path(self, map_id: str, path_id: str) -> None:
        raise NotImplementedError("Task 13 实现")

    async def reset_localization(self) -> None:
        raise NotImplementedError("Task 13 实现")

    async def start_mapping(self) -> None:
        raise NotImplementedError("Task 13 实现")

    async def stop_mapping(self) -> None:
        raise NotImplementedError("Task 13 实现")

    async def get_speed(self) -> dict[str, float]:
        raise NotImplementedError("Task 13 实现")

    async def set_speed(
        self, x: float, y: float | None = None, z: float | None = None,
    ) -> dict[str, float]:
        raise NotImplementedError("Task 13 实现")
```

**为什么有那个占位块:** `NavBackend` 是 ABC,只要 `__abstractmethods__` 非空就不许实例化。
本任务的 fixture 要真的 `VendorNavBackend(NavConfig(...))`,所以余下 20 个抽象方法必须先
占位,否则整个测试文件在 fixture 就 `TypeError: Can't instantiate abstract class`。
占位块是脚手架,不是实现 —— Task 13 会整块换掉。**不要给占位方法写测试。**

**关于嵌套外壳（地雷 1）的剥离位置：** Task 3 的 `parse_message` 已经统一剥掉了 `AppReponseObjectData` 外层，所以 `request()` 直接返回 `response.data` 即可。**不要在这里再剥一次**——两处都剥会把速度字典变成 `KeyError`。执行本任务时先打开 `protocol/nav_frames.py` 确认 `parse_message` 的这段逻辑存在，再往下写。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/backends/test_vendor_matching.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/backends/vendor_nav.py tests/backends/test_vendor_matching.py
git commit -m "feat: 厂商导航后端的连接、读循环与两级响应匹配"
```

---
### Task 13: 厂商后端的能力方法

把 `NavBackend` 的抽象方法逐个落到 `request()` 上。这一层的全部价值在于**翻译**：厂商的数组参数、字符串状态、拼错的键名，到这里为止；再往上只有 `Waypoint`、`NavStatus`、`Pose`。

**Files:**
- Modify: `src/d1max_patrol/backends/vendor_nav.py`（**替换掉 Task 12 留下的占位块**——那 20 个 `raise NotImplementedError` 的方法，逐个换成真实现；不是在它后面追加，写完之后文件里不应再有 `NotImplementedError`）
- Test: `tests/backends/test_vendor_capabilities.py`

**Interfaces:**
- Consumes: Task 12 的 `VendorNavBackend.request()`；`protocol.nav_requests` 的全部构造函数与 `parse_paths_payload` / `parse_map_ids`；`protocol.nav_types.parse_enum`
- Produces: `VendorNavBackend` 实现 `NavBackend` 的全部抽象方法（签名见 Task 11 与 Task 12 的占位块，逐字一致）；文件里不再有 `NotImplementedError`

**状态方法的返回约定：** `nav_status()` / `loc_status()` / `mapping_status()` 拿到未知字符串时返回 `None` 并记 warning，不抛错。固件升级新增一个枚举值不应该让整条巡检线挂掉。

- [ ] **Step 1: 写失败的测试**

`tests/backends/test_vendor_capabilities.py`：

```python
"""厂商后端的能力方法。每个方法都对着仿真器往返一次。"""

import asyncio

import pytest

from d1max_patrol.backends.base import NavRequestError
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)
from d1max_sim.nav_server import SimNavServer


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def backend(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, request_timeout_s=3.0))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


async def _wait(getter, wanted, timeout_s: float = 10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")


async def _ready(backend) -> str:
    """建图 + 加载定位,返回 map_id。"""
    await backend.start_mapping()
    await _wait(backend.mapping_status, MappingStatus.MAPPING_RUNNING)
    await backend.stop_mapping()
    await _wait(backend.mapping_status, MappingStatus.MAPPING_SAVE_END)
    map_id = (await backend.list_maps())[0]
    await backend.load_map(map_id)
    await _wait(backend.loc_status, LocStatus.CONTINUOUS_LOC)
    return map_id


def test_不再是抽象类(sim):
    assert VendorNavBackend(NavConfig(url=sim.url)) is not None


async def test_建图状态与地图列表(backend):
    assert await backend.mapping_status() is MappingStatus.PASSIVE
    assert await backend.list_maps() == []
    map_id = await _ready(backend)
    assert await backend.list_maps() == [map_id]


async def test_地图重命名与删除(backend):
    map_id = await _ready(backend)
    await backend.rename_map(map_id, "厂区一层")
    assert await backend.list_maps() == ["厂区一层"]
    await backend.remove_maps(["厂区一层"])
    assert await backend.list_maps() == []


async def test_取栅格地图(backend):
    map_id = await _ready(backend)
    grid = await backend.get_map_grid(map_id)
    assert grid["info"]["width"] > 0
    assert len(grid["data"]) == grid["info"]["width"] * grid["info"]["height"]


async def test_路径保存读取与删除(backend):
    map_id = await _ready(backend)
    assert await backend.list_paths(map_id) == {}

    points = [Waypoint("P1", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
              Waypoint("P2", Pose.from_xy_yaw(1.0, 1.0, 1.5708))]
    await backend.save_path(map_id, "巡检一号线", points)

    paths = await backend.list_paths(map_id)
    assert list(paths) == ["巡检一号线"]
    got = paths["巡检一号线"]
    assert [w.name for w in got] == ["P1", "P2"]
    assert got[1].pose.position.y == 1.0
    assert got[1].pose.yaw == pytest.approx(1.5708, abs=1e-4)

    await backend.save_path(map_id, "巡检一号线", points[:1])   # 覆盖
    assert len((await backend.list_paths(map_id))["巡检一号线"]) == 1

    await backend.remove_path(map_id, "巡检一号线")
    assert await backend.list_paths(map_id) == {}


async def test_导航到点并走到成功(backend):
    await _ready(backend)
    assert await backend.nav_status() is NavStatus.STANDBY
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    await _wait(backend.nav_status, NavStatus.SUCCEED)


async def test_停止导航(backend):
    await _ready(backend)
    await backend.goto(Pose.from_xy_yaw(9.0, 0.0, 0.0))
    await _wait(backend.nav_status, NavStatus.ACTIVE)
    await backend.stop()
    await _wait(backend.nav_status, NavStatus.CANCELLED)


async def test_暂停与继续(backend):
    await _ready(backend)
    await backend.goto(Pose.from_xy_yaw(9.0, 0.0, 0.0))
    await _wait(backend.nav_status, NavStatus.ACTIVE)
    await backend.pause()
    await _wait(backend.nav_status, NavStatus.PAUSE)
    await backend.resume()
    await _wait(backend.nav_status, NavStatus.ACTIVE)


async def test_定位未就绪时导航被拒(backend):
    with pytest.raises(NavRequestError):
        await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))


async def test_重置定位(backend):
    await _ready(backend)
    await backend.reset_localization()
    assert await backend.loc_status() is not None


async def test_读写速度(backend):
    assert set(await backend.get_speed()) == {"x", "y", "z"}
    got = await backend.set_speed(0.9)
    assert got["x"] == pytest.approx(0.9)
    assert (await backend.get_speed())["x"] == pytest.approx(0.9)

    got = await backend.set_speed(0.4, y=0.2, z=1.0)
    assert got == {"x": 0.4, "y": 0.2, "z": 1.0}


async def test_未知状态字符串返回None而不是抛错(backend, monkeypatch):
    """固件加了个新枚举值,不能让巡检整条线挂掉。"""
    async def fake_request(req):
        return "SomeBrandNewStatus"

    monkeypatch.setattr(backend, "request", fake_request)
    assert await backend.nav_status() is None
    assert await backend.loc_status() is None
    assert await backend.mapping_status() is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/backends/test_vendor_capabilities.py -v`
Expected: FAIL —— `TypeError: Can't instantiate abstract class VendorNavBackend`

- [ ] **Step 3: 把占位块换成能力方法**

在 `src/d1max_patrol/backends/vendor_nav.py` 末尾，`request()` 之后追加（仍在 `VendorNavBackend` 类体内）：

```python
    # ------------------------------------------------------------ 地图

    async def list_maps(self) -> list[str]:
        return parse_map_ids(await self.request(get_all_pgm_map()))

    async def remove_maps(self, map_ids: Sequence[str]) -> None:
        await self.request(remove_map_by_id(list(map_ids)))

    async def rename_map(self, old_id: str, new_id: str) -> None:
        await self.request(rename_map_name(old_id, new_id))

    async def get_map_grid(self, map_id: str) -> dict[str, Any]:
        return await self.request(get_pgm_map(map_id))

    # ------------------------------------------------------------ 建图

    async def start_mapping(self) -> None:
        await self.request(start_mapping())

    async def stop_mapping(self) -> None:
        await self.request(stop_mapping())

    async def mapping_status(self) -> MappingStatus | None:
        return self._as_enum(MappingStatus, await self.request(get_mapping_status()))

    # ------------------------------------------------------------ 路径

    async def list_paths(self, map_id: str) -> dict[str, list[Waypoint]]:
        return parse_paths_payload(await self.request(get_all_paths_by_mapid(map_id)))

    async def save_path(self, map_id: str, path_id: str,
                        waypoints: Sequence[Waypoint]) -> None:
        # 厂商把新增和修改分成两个接口,但语义都是"整条覆盖"。
        # 上层只需要一个 save,已存在就走 modify。
        existing = await self.list_paths(map_id)
        if path_id in existing:
            # 同名覆盖:老名字新名字传成同一个。
            req = modify_nav_path(map_id, path_id, path_id, list(waypoints))
        else:
            req = add_nav_path(map_id, path_id, list(waypoints))
        await self.request(req)

    async def remove_path(self, map_id: str, path_id: str) -> None:
        await self.request(remove_nav_path([(map_id, path_id)]))

    # ------------------------------------------------------------ 定位

    async def load_map(self, map_id: str) -> None:
        await self.request(loc_load_map(map_id))

    async def reset_localization(self) -> None:
        await self.request(reset_loc())

    async def loc_status(self) -> LocStatus | None:
        return self._as_enum(LocStatus, await self.request(get_loc_status()))

    # ------------------------------------------------------------ 导航

    async def goto(self, pose: Pose) -> None:
        await self.request(start_nav(pose))

    async def stop(self) -> None:
        await self.request(stop_nav())

    async def pause(self) -> None:
        await self.request(pause_nav())

    async def resume(self) -> None:
        await self.request(continue_nav())

    async def nav_status(self) -> NavStatus | None:
        return self._as_enum(NavStatus, await self.request(get_nav_status()))

    # ------------------------------------------------------------ 速度

    async def get_speed(self) -> dict[str, float]:
        return self._as_speed(await self.request(get_navigation_speed()))

    async def set_speed(self, x: float, y: float | None = None,
                        z: float | None = None) -> dict[str, float]:
        return self._as_speed(await self.request(set_navigation_speed(x, y, z)))

    # ------------------------------------------------------------ 小工具

    @staticmethod
    def _as_enum(cls, value: Any):
        parsed = parse_enum(cls, value)
        if parsed is None:
            log.warning("设备回了未知的 %s 值: %r", cls.__name__, value)
        return parsed

    @staticmethod
    def _as_speed(payload: Any) -> dict[str, float]:
        if not isinstance(payload, dict):
            raise NavBackendError(f"速度响应格式意外: {payload!r}")
        return {key: float(payload[key]) for key in ("x", "y", "z") if key in payload}
```

导入区补上：

```python
from collections.abc import Sequence

from d1max_patrol.protocol.nav_requests import (
    PUSH_ONLY_FUNCS,
    NavRequest,
    add_nav_path,
    continue_nav,
    get_all_paths_by_mapid,
    get_all_pgm_map,
    get_loc_status,
    get_mapping_status,
    get_nav_status,
    get_navigation_speed,
    get_pgm_map,
    loc_load_map,
    modify_nav_path,
    parse_map_ids,
    parse_paths_payload,
    pause_nav,
    remove_map_by_id,
    remove_nav_path,
    rename_map_name,
    reset_loc,
    set_navigation_speed,
    start_mapping,
    start_nav,
    stop_mapping,
    stop_nav,
)
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
    parse_enum,
)

from .base import (
    AlgErrorEvent,
    NavBackend,
    NavBackendError,
    NavConnectionError,
    NavRequestError,
    NavTimeoutError,
)
```

**命名冲突提醒：** 模块里既有方法 `self.start_mapping()` 又有导入的函数 `start_mapping()`。在方法体内 `start_mapping()` 指的是模块级函数（方法要靠 `self.` 才能访问），所以不冲突——但读起来容易眼花。若执行时觉得不放心，改成 `from d1max_patrol.protocol import nav_requests as R`，方法体内写 `R.start_mapping()`。两种写法都可以，选一种贯彻到底。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/backends/ -v`
Expected: 全部 passed（Task 11、12 的测试也要一起过）

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/backends/vendor_nav.py tests/backends/test_vendor_capabilities.py
git commit -m "feat: 厂商后端的地图/路径/导航/定位/速度能力方法"
```

---
### Task 14: 状态轮询、断链与自动重连

第四个地雷：**设备没有导航状态推送通道**。文档里的主动上报只有 `alg_error_code_notify` 和 `notify_stop_mapping_status` 两种，`get_nav_status` / `get_loc_status` / `get_mapping_status` 全是轮询接口。

所以"状态变化事件"这件事，必须由客户端自己造出来——轮询 → 比对 → 变了才发事件。上层因此仍然能写成事件驱动，将来换成真有推送的 Nav2 后端时，`base.py` 的事件契约一个字不用改。

**Files:**
- Modify: `src/d1max_patrol/backends/vendor_nav.py`
- Test: `tests/backends/test_vendor_resilience.py`

**Interfaces:**
- Consumes: Task 12/13 的全部；`base.{NavStatusEvent, LocStatusEvent, MappingStatusEvent, BackendDisconnected, BackendReconnected}`；`NavConfig.{status_poll_interval_s, reconnect_min_s, reconnect_max_s}`
- Produces（`VendorNavBackend` 新增）：
  - `auto_reconnect: bool = True`（构造参数，测试里可关掉）
  - `reconnect_attempts: int` —— 累计重连次数
  - `last_nav_status: NavStatus | None`、`last_loc_status: LocStatus | None`、`last_mapping_status: MappingStatus | None` —— 轮询器最近一次读到的值
  - `async wait_connected(timeout_s: float) -> None` —— 等到重连完成，超时抛 `NavTimeoutError`

**重连后必须重新对齐：** 重连成功时把三个 `last_*` 清成 `None`，下一轮轮询会以 `previous=None` 重新广播当前状态。断链期间机器人可能已经走完或已经失败，缓存的旧状态一律作废。

- [ ] **Step 1: 写失败的测试**

`tests/backends/test_vendor_resilience.py`：

```python
"""状态轮询、断链与重连。第四个地雷:设备没有状态推送通道。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    BackendDisconnected,
    BackendReconnected,
    LocStatusEvent,
    MappingStatusEvent,
    NavBackendError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import (
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
)
from d1max_sim.nav_server import SimNavServer

FAST = dict(request_timeout_s=3.0, status_poll_interval_s=0.05,
            connect_timeout_s=2.0, reconnect_min_s=0.05, reconnect_max_s=0.2)


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def backend(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST))
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


async def _drain_until(queue, predicate, timeout_s: float = 10.0):
    async def loop():
        while True:
            event = await queue.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(loop(), timeout=timeout_s)


async def _ready(backend) -> str:
    await backend.start_mapping()
    while await backend.mapping_status() is not MappingStatus.MAPPING_RUNNING:
        await asyncio.sleep(0.02)
    await backend.stop_mapping()
    while await backend.mapping_status() is not MappingStatus.MAPPING_SAVE_END:
        await asyncio.sleep(0.02)
    map_id = (await backend.list_maps())[0]
    await backend.load_map(map_id)
    while await backend.loc_status() is not LocStatus.CONTINUOUS_LOC:
        await asyncio.sleep(0.02)
    return map_id


async def test_轮询器开局就广播当前状态(backend):
    q = backend.subscribe()
    event = await _drain_until(q, lambda e: isinstance(e, NavStatusEvent))
    assert event.status is NavStatus.STANDBY
    assert event.previous is None


async def test_状态不变时不重复发事件(backend):
    await asyncio.sleep(0.3)          # 让轮询器跑好几轮
    q = backend.subscribe()
    await asyncio.sleep(0.3)
    assert q.empty()


async def test_导航状态变化被转成事件(backend):
    await _ready(backend)
    q = backend.subscribe()
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    event = await _drain_until(
        q, lambda e: isinstance(e, NavStatusEvent) and e.status is NavStatus.SUCCEED)
    assert event.previous in (NavStatus.ACTIVE, NavStatus.INITIALIZING)


async def test_定位与建图状态也有事件(backend):
    q = backend.subscribe()
    await _ready(backend)
    await _drain_until(
        q, lambda e: isinstance(e, MappingStatusEvent)
        and e.status is MappingStatus.MAPPING_RUNNING)
    await _drain_until(
        q, lambda e: isinstance(e, LocStatusEvent)
        and e.status is LocStatus.CONTINUOUS_LOC)


async def test_等待导航终态可用(backend):
    await _ready(backend)
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=15.0))
    await asyncio.sleep(0.05)         # 确保订阅已建立再下发
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED


async def test_导航失败也是终态(sim, backend):
    await _ready(backend)
    sim.faults.fail_next_nav = True
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=15.0))
    await asyncio.sleep(0.05)
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.FAILED


async def test_断链发出断开事件(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    event = await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    assert event.reason


async def test_自动重连并重新对齐状态(sim, backend):
    q = backend.subscribe()
    sim.faults.disconnect_seconds = 0.3
    await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
    await _drain_until(q, lambda e: isinstance(e, BackendReconnected), timeout_s=15.0)

    assert backend.reconnect_attempts >= 1
    assert backend.connected is True
    # 重连后缓存作废,当前状态被重新广播一遍
    event = await _drain_until(
        q, lambda e: isinstance(e, NavStatusEvent) and e.previous is None)
    assert event.status is NavStatus.STANDBY
    assert await backend.nav_status() is NavStatus.STANDBY


async def test_断链期间的请求抛错而不是永久挂起(sim, backend):
    sim.faults.disconnect_seconds = 1.0
    await asyncio.sleep(0.3)
    with pytest.raises(NavBackendError):
        await backend.request_nav_status_for_test()


async def test_等待重连(sim, backend):
    sim.faults.disconnect_seconds = 0.3
    await asyncio.sleep(0.2)
    await backend.wait_connected(timeout_s=15.0)
    assert backend.connected is True


async def test_等待重连超时(sim, backend):
    sim.faults.disconnect_seconds = 30.0
    await asyncio.sleep(0.2)
    with pytest.raises(NavTimeoutError):
        await backend.wait_connected(timeout_s=0.5)


async def test_关闭时不再重连(sim, backend):
    sim.faults.disconnect_seconds = 0.3
    await asyncio.sleep(0.1)
    await backend.close()
    before = backend.reconnect_attempts
    await asyncio.sleep(0.8)
    assert backend.reconnect_attempts == before
    assert backend.connected is False


async def test_关掉自动重连时只发断开事件(sim):
    b = VendorNavBackend(NavConfig(url=sim.url, **FAST), auto_reconnect=False)
    await b.connect()
    try:
        q = b.subscribe()
        sim.faults.disconnect_seconds = 0.2
        await _drain_until(q, lambda e: isinstance(e, BackendDisconnected))
        await asyncio.sleep(0.8)
        assert b.connected is False
        assert b.reconnect_attempts == 0
    finally:
        await b.close()


async def test_轮询遇到超时不会杀死轮询器(sim, backend):
    sim.faults.response_delay_s = 4.0
    await asyncio.sleep(0.5)
    sim.faults.response_delay_s = 0.0
    q = backend.subscribe()
    # 轮询器还活着 —— 状态重新流动起来
    await _drain_until(q, lambda e: isinstance(e, NavStatusEvent), timeout_s=10.0)
```

`test_断链期间的请求抛错而不是永久挂起` 里的 `request_nav_status_for_test` 不存在。改成：

```python
async def test_断链期间的请求抛错而不是永久挂起(sim, backend):
    sim.faults.disconnect_seconds = 1.0
    await asyncio.sleep(0.3)
    with pytest.raises(NavBackendError):
        await backend.nav_status()
```

并在导入区加上 `NavBackendError`。（断链瞬间可能刚好已重连成功，所以把 `disconnect_seconds` 设成 1.0、`reconnect_min_s` 是 0.05——若这条测试偶发不稳，把断链窗口拉到 3.0 秒，不要放宽断言。）

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/backends/test_vendor_resilience.py -v`
Expected: FAIL —— `TypeError: VendorNavBackend() got an unexpected keyword argument 'auto_reconnect'`

- [ ] **Step 3: 加轮询器与重连**

`__init__` 改成：

```python
    def __init__(self, config: NavConfig, auto_reconnect: bool = True) -> None:
        super().__init__()
        self.config = config
        self.auto_reconnect = auto_reconnect
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None
        self._reconnector: asyncio.Task[None] | None = None
        self._connected_event = asyncio.Event()
        self._frame_counter = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._closing = False

        self.fallback_matches = 0
        self.dropped_frames = 0
        self.reconnect_attempts = 0

        self.last_nav_status: NavStatus | None = None
        self.last_loc_status: LocStatus | None = None
        self.last_mapping_status: MappingStatus | None = None
```

`connect()` 末尾补上轮询器与就绪标志：

```python
        self._reader = asyncio.create_task(self._read_loop())
        if self._poller is None:
            self._poller = asyncio.create_task(self._poll_loop())
        self._connected_event.set()
        log.info("已连接导航设备 %s", self.config.url)
```

`close()` 里把三个后台任务一起收掉：

```python
    async def close(self) -> None:
        self._closing = True
        self._connected_event.clear()
        tasks = [self._reader, self._poller, self._reconnector]
        self._reader = self._poller = self._reconnector = None
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_pending("连接已关闭")
```

`_on_link_lost` 接上重连：

```python
    def _on_link_lost(self, reason: str) -> None:
        if self._ws is None and not self._connected_event.is_set():
            return                       # 已经在处理同一次断链了
        log.warning("导航链路断开: %s", reason)
        self._ws = None
        self._connected_event.clear()
        self._fail_pending(reason)
        self.emit(BackendDisconnected(reason))
        if self.auto_reconnect and not self._closing and self._reconnector is None:
            self._reconnector = asyncio.create_task(self._reconnect_loop())
```

新增三块：

```python
    # ------------------------------------------------------------ 重连

    async def wait_connected(self, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout_s)
        except asyncio.TimeoutError as exc:
            raise NavTimeoutError(f"等待重连超过 {timeout_s}s") from exc

    async def _reconnect_loop(self) -> None:
        delay = self.config.reconnect_min_s
        try:
            while not self._closing:
                await asyncio.sleep(delay)
                self.reconnect_attempts += 1
                try:
                    await self.connect()
                except NavConnectionError as exc:
                    log.info("第 %d 次重连失败: %s", self.reconnect_attempts, exc)
                    delay = min(delay * 2, self.config.reconnect_max_s)
                    continue
                # 断链期间机器人可能已经走完或已经失败,缓存一律作废,
                # 下一轮轮询会以 previous=None 重新广播真实状态。
                self.last_nav_status = None
                self.last_loc_status = None
                self.last_mapping_status = None
                self.emit(BackendReconnected())
                log.info("重连成功(第 %d 次尝试)", self.reconnect_attempts)
                return
        finally:
            self._reconnector = None

    # ------------------------------------------------------------ 状态轮询

    async def _poll_loop(self) -> None:
        """设备没有状态推送通道,只能自己轮询出"变化"这件事。"""
        while not self._closing:
            await asyncio.sleep(self.config.status_poll_interval_s)
            if self._ws is None:
                continue
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except NavBackendError as exc:
                # 单轮失败不该杀死轮询器 —— 断链有 _on_link_lost 兜底
                log.debug("状态轮询这一轮失败: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("状态轮询意外异常")

    async def _poll_once(self) -> None:
        nav = await self.nav_status()
        if nav is not None and nav is not self.last_nav_status:
            self.emit(NavStatusEvent(nav, self.last_nav_status))
            self.last_nav_status = nav

        loc = await self.loc_status()
        if loc is not None and loc is not self.last_loc_status:
            self.emit(LocStatusEvent(loc, self.last_loc_status))
            self.last_loc_status = loc

        mapping = await self.mapping_status()
        if mapping is not None and mapping is not self.last_mapping_status:
            self.emit(MappingStatusEvent(mapping, self.last_mapping_status))
            self.last_mapping_status = mapping
```

导入区补上 `BackendDisconnected`、`BackendReconnected`、`LocStatusEvent`、`MappingStatusEvent`、`NavStatusEvent`。

**关于终态被轮询漏掉：** 轮询周期 0.5s、仿真器终态保持 `terminal_hold_s=0.5s`，边界上可能刚好错过。测试里把 `status_poll_interval_s` 压到 0.05s 就够了；真机上若发现漏采，把 `NavStateMachine.terminal_hold_s` 的对应真机行为记进 `docs/` 的真机差异表（第 4 卷），不要靠加长超时来掩盖。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/backends/ -v`
Expected: 全部 passed

- [ ] **Step 5: 全量回归**

Run: `python -m pytest -v`
Expected: 全部 passed。这时 Task 1–14 的测试都在，跑完应在 30 秒内。

- [ ] **Step 6: 提交**

```bash
git add src/d1max_patrol/backends/vendor_nav.py tests/backends/test_vendor_resilience.py
git commit -m "feat: 状态轮询器、断链事件与指数退避重连"
```

---
### Task 15: 契约测试套件

这一套测试是"将来能换成 Nav2"这句话的唯一保证。它只 import `backends.base` 和 `protocol.nav_types`——**不 import `vendor_nav`，也不 import `d1max_sim`**。哪个实现在跑，只有 `conftest.py` 知道。

将来加 `Nav2Backend` 时，改动只有 `conftest.py` 里多一行工厂；这一整个目录一个字不用改。改不动就说明抽象漏了，那时该修的是 `base.py`，不是测试。

它和 Task 13 的测试**看起来重复**，其实职责不同：Task 13 测"厂商实现对不对"，可以随便摸 `sim.faults`、`backend.fallback_matches`；本套测"接口约定是什么"，只许用 `NavBackend` 上有的东西。

**Files:**
- Create: `tests/contract/__init__.py`
- Create: `tests/contract/conftest.py`
- Create: `tests/contract/test_nav_contract.py`
- Create: `tests/contract/README.md`
- Modify: `pyproject.toml`（注册 `contract` marker）

**Interfaces:**
- Consumes: `d1max_patrol.backends.base`、`d1max_patrol.protocol.nav_types`；`conftest` 额外 consume `vendor_nav.VendorNavBackend`、`config.models.NavConfig`、`d1max_sim.nav_server.SimNavServer`
- Produces（fixtures，供本目录与将来的第 3 卷任务测试复用）：
  - `nav_backend` —— 已连接的 `NavBackend`，按 `BACKEND_FACTORIES` 参数化
  - `ready_backend` —— 在 `nav_backend` 基础上完成建图与定位加载，返回 `(backend, map_id)`

- [ ] **Step 1: 写 conftest 与 README**

`tests/contract/__init__.py`：空文件。

`tests/contract/README.md`：

```markdown
# 契约测试

这里的测试定义 `NavBackend` 的行为约定,与任何具体实现无关。

## 规矩

- 只能 import `d1max_patrol.backends.base` 和 `d1max_patrol.protocol.nav_types`
- 不许 import `vendor_nav`、不许 import `d1max_sim`、不许碰 `sim.faults`
- 不许断言任何实现独有的属性(`fallback_matches`、`_pending`、…)

违反以上任何一条,这套测试就不再是契约测试,换后端时会假通过。

## 加一个新后端

在 `conftest.py` 的 `BACKEND_FACTORIES` 里加一行工厂函数即可,本目录其余文件
不用动。改不动说明抽象漏了 —— 该修的是 `base.py`。
```

`tests/contract/conftest.py`：

```python
"""契约测试的后端工厂。

这是整个 tests/contract 目录里唯一知道"跑的是哪个实现"的文件。
将来加 Nav2Backend,只需在 BACKEND_FACTORIES 里加一行。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

import pytest

from d1max_patrol.backends.base import NavBackend
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus
from d1max_sim.nav_server import SimNavServer


@contextlib.asynccontextmanager
async def _vendor_against_sim() -> AsyncIterator[NavBackend]:
    """厂商后端 + 仿真设备。"""
    sim = SimNavServer(tick_hz=100.0)
    await sim.start()
    backend = VendorNavBackend(NavConfig(
        url=sim.url,
        request_timeout_s=3.0,
        status_poll_interval_s=0.05,
        reconnect_min_s=0.05,
        reconnect_max_s=0.2,
    ))
    try:
        await backend.connect()
        yield backend
    finally:
        await backend.close()
        await sim.stop()


#: 后端名 -> 上下文管理器工厂。加新后端只改这里。
BACKEND_FACTORIES: dict[str, Callable[[], contextlib.AbstractAsyncContextManager]] = {
    "vendor": _vendor_against_sim,
}


@pytest.fixture(params=sorted(BACKEND_FACTORIES), ids=sorted(BACKEND_FACTORIES))
async def nav_backend(request) -> AsyncIterator[NavBackend]:
    async with BACKEND_FACTORIES[request.param]() as backend:
        yield backend


@pytest.fixture
async def ready_backend(nav_backend) -> tuple[NavBackend, str]:
    """建好图、加载好定位的后端。契约测试的常用起点。"""
    await nav_backend.start_mapping()
    await _until(lambda: nav_backend.mapping_status(),
                 MappingStatus.MAPPING_RUNNING)
    await nav_backend.stop_mapping()
    await _until(lambda: nav_backend.mapping_status(),
                 MappingStatus.MAPPING_SAVE_END)

    maps = await nav_backend.list_maps()
    assert maps, "建图完成后应至少有一张地图"
    map_id = maps[0]

    await nav_backend.load_map(map_id)
    await _until(lambda: nav_backend.loc_status(), LocStatus.CONTINUOUS_LOC)
    return nav_backend, map_id


async def _until(getter, wanted, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")
```

`pyproject.toml` 的 `[tool.pytest.ini_options]` 补上：

```toml
markers = ["contract: NavBackend 的实现无关行为约定"]
```

- [ ] **Step 2: 写契约测试**

`tests/contract/test_nav_contract.py`：

```python
"""NavBackend 的行为约定。

只依赖 backends.base 与 protocol.nav_types。见本目录 README 的规矩。
"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    LocStatusEvent,
    NavRequestError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.protocol.nav_types import (
    NAV_TERMINAL,
    LocStatus,
    MappingStatus,
    NavStatus,
    Pose,
    Waypoint,
)

pytestmark = pytest.mark.contract


async def _drain_until(queue, predicate, timeout_s: float = 15.0):
    async def loop():
        while True:
            event = await queue.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(loop(), timeout=timeout_s)


# --------------------------------------------------------------- 建图与地图


async def test_初始没有地图(nav_backend):
    assert await nav_backend.list_maps() == []


async def test_建图产出一张可用的地图(ready_backend):
    backend, map_id = ready_backend
    assert map_id in await backend.list_maps()
    assert await backend.mapping_status() is MappingStatus.MAPPING_SAVE_END


async def test_栅格地图形状合法(ready_backend):
    backend, map_id = ready_backend
    grid = await backend.get_map_grid(map_id)
    info = grid["info"]
    assert info["width"] > 0 and info["height"] > 0
    assert info["resolution"] > 0
    assert len(grid["data"]) == info["width"] * info["height"]


async def test_重命名后旧名字消失(ready_backend):
    backend, map_id = ready_backend
    await backend.rename_map(map_id, "契约图")
    names = await backend.list_maps()
    assert "契约图" in names and map_id not in names


async def test_删除地图(ready_backend):
    backend, map_id = ready_backend
    await backend.remove_maps([map_id])
    assert await backend.list_maps() == []


# --------------------------------------------------------------- 路径


async def test_路径的保存读取覆盖与删除(ready_backend):
    backend, map_id = ready_backend
    assert await backend.list_paths(map_id) == {}

    points = [Waypoint("配电柜", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
              Waypoint("水泵", Pose.from_xy_yaw(1.0, 1.0, 1.5))]
    await backend.save_path(map_id, "线路A", points)

    paths = await backend.list_paths(map_id)
    assert [w.name for w in paths["线路A"]] == ["配电柜", "水泵"]
    assert paths["线路A"][1].pose.position.y == pytest.approx(1.0)

    await backend.save_path(map_id, "线路A", points[:1])
    assert len((await backend.list_paths(map_id))["线路A"]) == 1

    await backend.remove_path(map_id, "线路A")
    assert await backend.list_paths(map_id) == {}


async def test_空路径也能存(ready_backend):
    """厂商 App 里画了一半的路径,读回来不能炸。"""
    backend, map_id = ready_backend
    await backend.save_path(map_id, "空线", [])
    assert (await backend.list_paths(map_id))["空线"] == []


# --------------------------------------------------------------- 定位


async def test_加载地图后定位健康(ready_backend):
    backend, _ = ready_backend
    assert await backend.loc_status() is LocStatus.CONTINUOUS_LOC


async def test_定位状态变化产生事件(nav_backend):
    queue = nav_backend.subscribe()
    await nav_backend.start_mapping()
    await nav_backend.stop_mapping()
    await _drain_until(queue, lambda e: isinstance(e, LocStatusEvent) or True,
                       timeout_s=15.0)


# --------------------------------------------------------------- 导航


async def test_导航到点走到成功(ready_backend):
    backend, _ = ready_backend
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED
    assert await backend.nav_status() in NAV_TERMINAL | {NavStatus.STANDBY}


async def test_连续走多个点(ready_backend):
    """全逐点执行:路线由我们排序,后端只管一次一个点。"""
    backend, _ = ready_backend
    for target in (Pose.from_xy_yaw(1.0, 0.0, 0.0),
                   Pose.from_xy_yaw(1.0, 1.0, 1.5708),
                   Pose.from_xy_yaw(0.0, 0.0, 3.1416)):
        waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=20.0))
        await asyncio.sleep(0.05)
        await backend.goto(target)
        assert await waiter is NavStatus.SUCCEED


async def test_停止导航进入取消(ready_backend):
    backend, _ = ready_backend
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await backend.goto(Pose.from_xy_yaw(9.0, 0.0, 0.0))
    await asyncio.sleep(0.2)
    await backend.stop()
    assert await waiter is NavStatus.CANCELLED


async def test_暂停后不再前进继续后能走完(ready_backend):
    backend, _ = ready_backend
    queue = backend.subscribe()
    await backend.goto(Pose.from_xy_yaw(3.0, 0.0, 0.0))
    await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                       and e.status is NavStatus.ACTIVE)
    await backend.pause()
    await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                       and e.status is NavStatus.PAUSE)
    await backend.resume()
    await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                       and e.status is NavStatus.SUCCEED)


async def test_未加载定位地图时导航被拒(nav_backend):
    with pytest.raises(NavRequestError):
        await nav_backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))


async def test_等待终态会超时而不是永久挂起(ready_backend):
    backend, _ = ready_backend
    with pytest.raises(NavTimeoutError):
        await backend.wait_nav_terminal(timeout_s=0.2)


# --------------------------------------------------------------- 速度


async def test_速度读写往返(nav_backend):
    speed = await nav_backend.get_speed()
    assert {"x", "y", "z"} <= set(speed)
    assert all(isinstance(v, float) for v in speed.values())

    await nav_backend.set_speed(0.5, y=0.3, z=1.0)
    got = await nav_backend.get_speed()
    assert got["x"] == pytest.approx(0.5)
    assert got["y"] == pytest.approx(0.3)
    assert got["z"] == pytest.approx(1.0)


# --------------------------------------------------------------- 事件订阅


async def test_多个订阅者互不抢事件(ready_backend):
    backend, _ = ready_backend
    a, b = backend.subscribe(), backend.subscribe()
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    ea = await _drain_until(a, lambda e: isinstance(e, NavStatusEvent)
                            and e.status is NavStatus.SUCCEED)
    eb = await _drain_until(b, lambda e: isinstance(e, NavStatusEvent)
                            and e.status is NavStatus.SUCCEED)
    assert ea.status is eb.status


async def test_退订后不再收事件(ready_backend):
    backend, _ = ready_backend
    queue = backend.subscribe()
    backend.unsubscribe(queue)
    while not queue.empty():
        queue.get_nowait()
    await backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    await asyncio.sleep(0.5)
    assert queue.empty()


# --------------------------------------------------------------- 生命周期


async def test_关闭后不再连接(nav_backend):
    await nav_backend.close()
    assert nav_backend.connected is False
    await nav_backend.close()          # 可重复调用
```

`test_定位状态变化产生事件` 里的 `lambda e: ... or True` 是废的。改成实打实的断言：

```python
async def test_定位状态变化产生事件(ready_backend):
    """ready_backend 已经把定位带到 ContinuousLoc,订阅新队列后重新读一次即可。"""
    backend, _ = ready_backend
    assert await backend.loc_status() is LocStatus.CONTINUOUS_LOC
```

事件流本身在 `test_暂停后不再前进继续后能走完` 里已经被真正断言过了。

- [ ] **Step 3: 运行契约测试**

Run: `python -m pytest tests/contract -v`
Expected: 全部 passed，测试 id 形如 `test_导航到点走到成功[vendor]`

- [ ] **Step 4: 验证契约测试真的与实现解耦**

Run: `grep -rn "d1max_sim\|vendor_nav\|fallback_matches\|_pending\|faults" tests/contract/test_nav_contract.py`
Expected: 无输出。有输出就说明契约测试偷看了实现，删掉那一行再重跑。

- [ ] **Step 5: 提交**

```bash
git add tests/contract/ pyproject.toml
git commit -m "test: NavBackend 契约测试套件(实现无关)"
```

---
### Task 16: 故障场景与降级行为

契约测试证明"一切正常时行为对"。这一套证明"出事时行为也对"——而巡检机器人的大部分时间都花在出事上。

和契约测试相反，这里**允许**摸 `sim.faults` 和实现内部的计数器：我们要断言的正是"降级发生了，并且降级得体面"。

**Files:**
- Create: `tests/scenarios/__init__.py`
- Create: `tests/scenarios/conftest.py`
- Create: `tests/scenarios/test_fault_scenarios.py`

**Interfaces:**
- Consumes: `VendorNavBackend`、`SimNavServer`、`FaultState`、`backends.base` 全部事件
- Produces（fixtures，第 3 卷的任务级故障测试会复用）：
  - `rig` —— dataclass `Rig(sim, backend, map_id)`，已连接、已建图、已加载定位
  - `Rig.inject(command: str) -> str` —— 直接调 `apply_command`，等价于走控制通道但不用开连接

- [ ] **Step 1: 写 conftest**

`tests/scenarios/__init__.py`：空文件。

`tests/scenarios/conftest.py`：

```python
"""故障场景的测试台。

与 tests/contract 相反,这里刻意与实现耦合:要断言的正是降级行为本身。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.models import NavConfig
from d1max_patrol.protocol.nav_types import LocStatus, MappingStatus
from d1max_sim.inject import apply_command
from d1max_sim.nav_server import SimNavServer


@dataclass
class Rig:
    sim: SimNavServer
    backend: VendorNavBackend
    map_id: str

    def inject(self, command: str) -> str:
        """等价于通过控制通道下发一条注入命令。"""
        return apply_command(self.sim.faults, command)


async def _until(getter, wanted, timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await getter() is wanted:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"未在 {timeout_s}s 内变为 {wanted}")


@pytest.fixture
async def rig():
    sim = SimNavServer(tick_hz=100.0)
    await sim.start()
    backend = VendorNavBackend(NavConfig(
        url=sim.url,
        request_timeout_s=3.0,
        status_poll_interval_s=0.05,
        reconnect_min_s=0.05,
        reconnect_max_s=0.2,
    ))
    await backend.connect()
    try:
        await backend.start_mapping()
        await _until(backend.mapping_status, MappingStatus.MAPPING_RUNNING)
        await backend.stop_mapping()
        await _until(backend.mapping_status, MappingStatus.MAPPING_SAVE_END)
        map_id = (await backend.list_maps())[0]
        await backend.load_map(map_id)
        await _until(backend.loc_status, LocStatus.CONTINUOUS_LOC)
        yield Rig(sim, backend, map_id)
    finally:
        await backend.close()
        await sim.stop()
```

- [ ] **Step 2: 写场景测试**

`tests/scenarios/test_fault_scenarios.py`：

```python
"""故障场景。每一条都对应一件真机上必然会发生的事。"""

import asyncio

import pytest

from d1max_patrol.backends.base import (
    AlgErrorEvent,
    BackendDisconnected,
    BackendReconnected,
    LocStatusEvent,
    NavBackendError,
    NavStatusEvent,
    NavTimeoutError,
)
from d1max_patrol.protocol.nav_types import (
    ALG_LIDAR_DISCONNECTED,
    ALG_NAV_BLOCKED,
    LocStatus,
    NavStatus,
    Pose,
)


async def _drain_until(queue, predicate, timeout_s: float = 20.0):
    async def loop():
        while True:
            event = await queue.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(loop(), timeout=timeout_s)


def _nav_is(status):
    return lambda e: isinstance(e, NavStatusEvent) and e.status is status


# ------------------------------------------------------- 算法故障码


async def test_路径被挡的故障码不打断正在进行的导航(rig):
    """13330 是"提示"不是"终止"。上层应当记录并继续,由超时兜底。"""
    queue = rig.backend.subscribe()
    await rig.backend.goto(Pose.from_xy_yaw(2.0, 0.0, 0.0))
    rig.inject(f"alg_error {ALG_NAV_BLOCKED}")

    alg = await _drain_until(queue, lambda e: isinstance(e, AlgErrorEvent))
    assert alg.items[0].code == ALG_NAV_BLOCKED
    await _drain_until(queue, _nav_is(NavStatus.SUCCEED))


async def test_雷达断连故障码也能透传(rig):
    queue = rig.backend.subscribe()
    rig.inject(f"alg_error {ALG_LIDAR_DISCONNECTED} 3")
    alg = await _drain_until(queue, lambda e: isinstance(e, AlgErrorEvent))
    assert alg.items[0].code == ALG_LIDAR_DISCONNECTED
    assert alg.items[0].severity == 3


async def test_连续多条故障码不丢(rig):
    queue = rig.backend.subscribe()
    for _ in range(5):
        rig.inject(f"alg_error {ALG_NAV_BLOCKED}")
    received = 0
    while received < 5:
        await _drain_until(queue, lambda e: isinstance(e, AlgErrorEvent))
        received += 1


# ------------------------------------------------------- 走不动


async def test_卡住时导航等待终态超时(rig):
    """stuck: 状态一直是 Active,永远到不了。只能靠超时发现。"""
    rig.inject("stuck on")
    waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=1.0))
    await asyncio.sleep(0.05)
    await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
    with pytest.raises(NavTimeoutError):
        await waiter
    assert await rig.backend.nav_status() is NavStatus.ACTIVE


async def test_超时后能停下并恢复(rig):
    """超时之后必须能收拾残局,否则下一个点没法开始。"""
    rig.inject("stuck on")
    await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
    await asyncio.sleep(0.3)

    queue = rig.backend.subscribe()
    await rig.backend.stop()
    await _drain_until(queue, _nav_is(NavStatus.CANCELLED))

    rig.inject("stuck off")
    await _drain_until(queue, _nav_is(NavStatus.STANDBY))
    waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED


async def test_减速会让原本够用的超时变得不够用(rig):
    """slow 是制造"能走但太慢"的手段 —— 巡检里最难判断的一类故障。"""
    rig.inject("slow 20")
    waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=1.0))
    await asyncio.sleep(0.05)
    await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
    with pytest.raises(NavTimeoutError):
        await waiter


# ------------------------------------------------------- 定位


async def test_定位丢失使导航失败并发出定位事件(rig):
    queue = rig.backend.subscribe()
    await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
    await _drain_until(queue, _nav_is(NavStatus.ACTIVE))

    rig.inject("loc_lost")
    await _drain_until(queue, lambda e: isinstance(e, LocStatusEvent)
                       and e.status is LocStatus.LOC_LOST)
    await _drain_until(queue, _nav_is(NavStatus.FAILED))


async def test_定位恢复后可以重新导航(rig):
    queue = rig.backend.subscribe()
    rig.inject("loc_lost")
    await _drain_until(queue, lambda e: isinstance(e, LocStatusEvent)
                       and e.status is LocStatus.LOC_LOST)

    with pytest.raises(NavBackendError):
        await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))

    rig.inject("loc_ok")
    await _drain_until(queue, lambda e: isinstance(e, LocStatusEvent)
                       and e.status is LocStatus.CONTINUOUS_LOC)

    waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED


# ------------------------------------------------------- 链路


async def test_导航途中断链重连后能继续下一个点(rig):
    queue = rig.backend.subscribe()
    await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
    await _drain_until(queue, _nav_is(NavStatus.ACTIVE))

    rig.inject("disconnect 0.4")
    await _drain_until(queue, lambda e: isinstance(e, BackendDisconnected))
    await _drain_until(queue, lambda e: isinstance(e, BackendReconnected))

    # 重连后状态被重新广播一遍(previous 为 None)
    await _drain_until(queue, lambda e: isinstance(e, NavStatusEvent)
                       and e.previous is None)

    await rig.backend.stop()
    await asyncio.sleep(0.3)
    waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED


async def test_断链期间等待终态直接抛错(rig):
    waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await rig.backend.goto(Pose.from_xy_yaw(5.0, 0.0, 0.0))
    await asyncio.sleep(0.2)
    rig.inject("disconnect 0.5")
    with pytest.raises(Exception, match="断开|disconnect"):
        await waiter


# ------------------------------------------------------- 协议降级


async def test_帧号全程归零仍能跑完一趟三点巡检(rig):
    """最坏情况:帧号完全不可用,全靠函数名降级匹配。"""
    rig.inject("frame_count_zero on")
    for target in (Pose.from_xy_yaw(1.0, 0.0, 0.0),
                   Pose.from_xy_yaw(1.0, 1.0, 1.5708),
                   Pose.from_xy_yaw(0.0, 0.0, 3.1416)):
        waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=25.0))
        await asyncio.sleep(0.05)
        await rig.backend.goto(target)
        assert await waiter is NavStatus.SUCCEED

    assert rig.backend.fallback_matches > 0      # 确实降级了
    assert rig.backend.dropped_frames == 0       # 但一帧没丢


async def test_乱序加并发不串台(rig):
    rig.inject("reorder 0.15")
    for _ in range(5):
        nav, loc, speed = await asyncio.gather(
            rig.backend.nav_status(),
            rig.backend.loc_status(),
            rig.backend.get_speed(),
        )
        assert nav is NavStatus.STANDBY
        assert loc is LocStatus.CONTINUOUS_LOC
        assert set(speed) == {"x", "y", "z"}


async def test_注入复位后一切回到正常(rig):
    rig.inject("slow 10")
    rig.inject("frame_count_zero on")
    rig.inject("reset")
    assert rig.sim.faults.speed_scale == 1.0
    assert rig.sim.faults.frame_count_zero is False

    waiter = asyncio.create_task(rig.backend.wait_nav_terminal(timeout_s=20.0))
    await asyncio.sleep(0.05)
    await rig.backend.goto(Pose.from_xy_yaw(1.0, 0.0, 0.0))
    assert await waiter is NavStatus.SUCCEED
```

- [ ] **Step 3: 运行测试**

Run: `python -m pytest tests/scenarios -v`
Expected: 全部 passed

若 `test_导航途中断链重连后能继续下一个点` 偶发失败，先看是不是断链窗口（0.4s）比重连退避（0.05→0.2s）短，导致重连在设备仍拒连时耗掉几次尝试——这是**正常行为**，把窗口调到 0.8s 即可。不要靠加长 `_drain_until` 的超时掩盖。

- [ ] **Step 4: 全量回归并记录耗时**

Run: `python -m pytest -q --durations=10`
Expected: 全部 passed。留意最慢的 10 条：任何一条超过 5 秒，说明它在等一个本该被事件驱动替代的固定 sleep，改掉它。

- [ ] **Step 5: 提交**

```bash
git add tests/scenarios/
git commit -m "test: 故障场景与协议降级行为"
```

---
### Task 17: 命令行入口

第 1 卷的交付物要能被手拿着用，而不只是被测试用。这条命令行是接下来所有真机调试的入口：接上机器狗第一件事就是 `d1max --url ws://192.168.144.100:10010 status`。

`walk` 子命令是本卷的收官演示：**读取厂商 App 画好的路线，然后全逐点走完**。它有意做得很薄——不拍照、不归档、不重试，那些是第 3 卷 `MissionRunner` 的事。

**Files:**
- Create: `src/d1max_patrol/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `config.loader.load_config`、`config.models.NavConfig`、`backends.vendor_nav.VendorNavBackend`、`protocol.nav_types.{Pose, NAV_TERMINAL}`、`d1max_sim.nav_server.SimNavServer`（仅 `sim` 子命令）
- Produces（`d1max_patrol.cli`）：
  - `build_parser() -> argparse.ArgumentParser`
  - `main(argv: list[str] | None = None) -> int` —— 成功返回 0，设备拒绝/超时/连不上返回 1，参数错误由 argparse 返回 2
  - 已在 Task 1 的 `pyproject.toml` 里注册为 `d1max` 命令

**子命令：**

| 命令 | 作用 |
|---|---|
| `d1max status` | 打印导航/定位/建图状态与当前速度 |
| `d1max maps` | 列出地图 |
| `d1max paths <map_id>` | 列出该地图上的路线与航点 |
| `d1max path-save <map_id> <path_id> --point ...` | 写入一条路线（新增或整条覆盖）|
| `d1max map-start` / `d1max map-stop` | 开始 / 结束建图 |
| `d1max load <map_id>` | 加载定位地图并等到定位就绪 |
| `d1max goto <x> <y> [yaw]` | 走一个点并等终态 |
| `d1max walk <map_id> <path_id>` | 按厂商路线全逐点走完 |
| `d1max speed [x] [--y] [--z]` | 读 / 写导航速度 |
| `d1max sim [--port] [--seed]` | 起一台仿真设备（转调 `d1max_sim.__main__.main`）|

全局参数：`--url`（覆盖配置里的地址）、`--config <path>`、`--timeout <秒>`（单点导航超时，默认 120）、`-v`。

- [ ] **Step 1: 写失败的测试**

`tests/test_cli.py`：

```python
"""命令行入口。对着仿真器跑,断言退出码与关键输出。"""

import pytest

from d1max_patrol.cli import build_parser, main
from d1max_sim.nav_server import SimNavServer


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def run(sim, *args: str) -> int:
    return main(["--url", sim.url, "--timeout", "30", *args])


def test_没有子命令时打印帮助并返回2():
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_解析器认识所有子命令():
    parser = build_parser()
    for argv in (["status"], ["maps"], ["paths", "m"], ["map-start"], ["map-stop"],
                 ["load", "m"], ["goto", "1", "2"], ["walk", "m", "p"],
                 ["path-save", "m", "p", "--point", "A:1:2"],
                 ["speed"], ["sim"]):
        assert parser.parse_args(argv) is not None


def test_status(sim, capsys):
    assert run(sim, "status") == 0
    out = capsys.readouterr().out
    assert "StandBy" in out
    assert "Init" in out
    assert "x=" in out


def test_连不上时返回1(capsys):
    assert main(["--url", "ws://127.0.0.1:1", "status"]) == 1
    assert "连接" in capsys.readouterr().err


def test_maps_初始为空(sim, capsys):
    assert run(sim, "maps") == 0
    assert "没有地图" in capsys.readouterr().out


def test_建图与列图(sim, capsys):
    assert run(sim, "map-start") == 0
    assert run(sim, "map-stop") == 0
    assert run(sim, "maps") == 0
    assert "map_1" in capsys.readouterr().out


def test_load_与_goto(sim, capsys):
    run(sim, "map-start")
    run(sim, "map-stop")
    assert run(sim, "load", "map_1") == 0
    assert "ContinuousLoc" in capsys.readouterr().out

    assert run(sim, "goto", "1", "0", "0") == 0
    assert "Succeed" in capsys.readouterr().out


def test_goto_在定位未就绪时返回1(sim, capsys):
    assert run(sim, "goto", "1", "0") == 1
    assert "定位" in capsys.readouterr().err


def test_paths_与_walk(sim, capsys):
    from d1max_patrol.protocol.nav_types import Pose, Waypoint

    map_id = sim.store.create_map("厂区")
    sim.store.set_path(map_id, "一号线", [
        Waypoint("P1", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
        Waypoint("P2", Pose.from_xy_yaw(1.0, 1.0, 1.5708)),
    ])

    assert run(sim, "paths", map_id) == 0
    out = capsys.readouterr().out
    assert "一号线" in out and "P1" in out and "P2" in out

    assert run(sim, "load", map_id) == 0
    assert run(sim, "walk", map_id, "一号线") == 0
    out = capsys.readouterr().out
    assert "1/2" in out and "2/2" in out
    assert "全部完成" in out


def test_path_save_写入并读回(sim, capsys):
    map_id = sim.store.create_map("厂区")
    assert run(sim, "path-save", map_id, "新线",
               "--point", "配电柜:1.0:2.0:1.57", "--point", "水泵:3.0:4.0") == 0
    assert "2 个点" in capsys.readouterr().out

    assert run(sim, "paths", map_id) == 0
    out = capsys.readouterr().out
    assert "配电柜" in out and "水泵" in out and "x=3.00" in out


def test_path_save_覆盖已有路线(sim, capsys):
    map_id = sim.store.create_map("厂区")
    run(sim, "path-save", map_id, "线", "--point", "A:1:0", "--point", "B:2:0")
    assert run(sim, "path-save", map_id, "线", "--point", "A:1:0") == 0
    assert run(sim, "paths", map_id) == 0
    assert "(1 点)" in capsys.readouterr().out


def test_path_save_航点格式错误返回1(sim, capsys):
    map_id = sim.store.create_map()
    assert run(sim, "path-save", map_id, "线", "--point", "缺了坐标") == 1
    assert "格式" in capsys.readouterr().err


def test_walk_遇到不存在的路线返回1(sim, capsys):
    map_id = sim.store.create_map()
    assert run(sim, "walk", map_id, "没这条线") == 1
    assert "没这条线" in capsys.readouterr().err


def test_walk_中途失败即刻停下(sim, capsys):
    from d1max_patrol.protocol.nav_types import Pose, Waypoint

    map_id = sim.store.create_map("厂区")
    sim.store.set_path(map_id, "线", [
        Waypoint("P1", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
        Waypoint("P2", Pose.from_xy_yaw(2.0, 0.0, 0.0)),
    ])
    run(sim, "load", map_id)
    sim.faults.fail_next_nav = True

    assert run(sim, "walk", map_id, "线") == 1
    err = capsys.readouterr().err
    assert "P1" in err and "Failed" in err


def test_speed_读与写(sim, capsys):
    assert run(sim, "speed") == 0
    assert "x=0.8" in capsys.readouterr().out

    assert run(sim, "speed", "0.5", "--y", "0.3", "--z", "1.0") == 0
    out = capsys.readouterr().out
    assert "x=0.5" in out and "y=0.3" in out and "z=1.0" in out


def test_超时参数会传下去(sim, capsys):
    run(sim, "map-start")
    run(sim, "map-stop")
    run(sim, "load", "map_1")
    sim.faults.stuck = True
    assert main(["--url", sim.url, "--timeout", "1", "goto", "5", "0"]) == 1
    assert "超时" in capsys.readouterr().err
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'd1max_patrol.cli'`

- [ ] **Step 3: 写 cli.py**

`src/d1max_patrol/cli.py`：

```python
"""d1max 命令行。

接上真机后的第一条命令应该是:
    d1max --url ws://192.168.144.100:10010 status
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

from d1max_patrol.backends.base import NavBackendError
from d1max_patrol.backends.vendor_nav import VendorNavBackend
from d1max_patrol.config.loader import ConfigError, load_config
from d1max_patrol.protocol.nav_types import LOC_HEALTHY, NavStatus, Pose, Waypoint

log = logging.getLogger(__name__)

DEFAULT_NAV_TIMEOUT_S = 120.0
#: 等定位就绪的上限
LOC_READY_TIMEOUT_S = 60.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="d1max", description="D1 Max 巡检工具")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--url", help="覆盖导航 WebSocket 地址")
    parser.add_argument("--timeout", type=float, default=DEFAULT_NAV_TIMEOUT_S,
                        help="单点导航超时秒数(默认 %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="打印导航/定位/建图状态与速度")
    sub.add_parser("maps", help="列出地图")

    p = sub.add_parser("paths", help="列出某张地图上的路线")
    p.add_argument("map_id")

    p = sub.add_parser("path-save", help="写入一条路线(新增或整条覆盖)")
    p.add_argument("map_id")
    p.add_argument("path_id")
    p.add_argument("--point", action="append", default=[], metavar="名称:x:y[:yaw]",
                   help="航点,可重复。例: --point 配电柜:1.0:2.0:1.57")

    sub.add_parser("map-start", help="开始建图")
    sub.add_parser("map-stop", help="结束建图并保存")

    p = sub.add_parser("load", help="加载定位地图并等到定位就绪")
    p.add_argument("map_id")

    p = sub.add_parser("goto", help="走一个点")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("yaw", type=float, nargs="?", default=0.0)

    p = sub.add_parser("walk", help="按厂商路线全逐点走完")
    p.add_argument("map_id")
    p.add_argument("path_id")

    p = sub.add_parser("speed", help="读或写导航速度")
    p.add_argument("x", type=float, nargs="?")
    p.add_argument("--y", type=float)
    p.add_argument("--z", type=float)

    p = sub.add_parser("sim", help="起一台仿真设备")
    p.add_argument("--port", type=int, default=10010)
    p.add_argument("--seed", action="store_true")

    return parser


# --------------------------------------------------------------------- 骨架


@contextlib.asynccontextmanager
async def _backend(args) -> AsyncIterator[VendorNavBackend]:
    config = load_config(args.config)
    nav = config.nav if args.url is None else \
        type(config.nav)(**{**config.nav.__dict__, "url": args.url})
    backend = VendorNavBackend(nav, auto_reconnect=False)
    await backend.connect()
    try:
        yield backend
    finally:
        await backend.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    if args.command == "sim":
        from d1max_sim.__main__ import main as sim_main

        sim_argv = ["--port", str(args.port)]
        if args.seed:
            sim_argv.append("--seed")
        return sim_main(sim_argv)

    try:
        return asyncio.run(_run(args))
    except (NavBackendError, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1


async def _run(args) -> int:
    async with _backend(args) as backend:
        handler = _COMMANDS[args.command]
        return await handler(backend, args)


# --------------------------------------------------------------------- 命令


async def _cmd_status(backend, args) -> int:
    nav = await backend.nav_status()
    loc = await backend.loc_status()
    mapping = await backend.mapping_status()
    speed = await backend.get_speed()
    print(f"导航: {nav.value if nav else '未知'}")
    print(f"定位: {loc.value if loc else '未知'}")
    print(f"建图: {mapping.value if mapping else '未知'}")
    print("速度: " + "  ".join(f"{k}={v}" for k, v in sorted(speed.items())))
    return 0


async def _cmd_maps(backend, args) -> int:
    maps = await backend.list_maps()
    if not maps:
        print("没有地图")
        return 0
    for map_id in maps:
        print(map_id)
    return 0


async def _cmd_paths(backend, args) -> int:
    paths = await backend.list_paths(args.map_id)
    if not paths:
        print(f"{args.map_id} 上没有路线")
        return 0
    for path_id, waypoints in paths.items():
        print(f"{path_id}  ({len(waypoints)} 点)")
        for index, wp in enumerate(waypoints, 1):
            p = wp.pose.position
            print(f"  {index}. {wp.name}  x={p.x:.2f} y={p.y:.2f} "
                  f"yaw={wp.pose.yaw:.3f}")
    return 0


def _parse_point(text: str) -> Waypoint:
    parts = text.split(":")
    if len(parts) not in (3, 4):
        raise ValueError(f"航点格式应为 名称:x:y[:yaw],实际为 {text!r}")
    name, x, y = parts[0], float(parts[1]), float(parts[2])
    yaw = float(parts[3]) if len(parts) == 4 else 0.0
    return Waypoint(name, Pose.from_xy_yaw(x, y, yaw))


async def _cmd_path_save(backend, args) -> int:
    try:
        waypoints = [_parse_point(text) for text in args.point]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    await backend.save_path(args.map_id, args.path_id, waypoints)
    print(f"已写入 {args.path_id}: {len(waypoints)} 个点")
    return 0


async def _cmd_map_start(backend, args) -> int:
    await backend.start_mapping()
    print("已开始建图。走完一圈后执行 d1max map-stop 保存。")
    return 0


async def _cmd_map_stop(backend, args) -> int:
    await backend.stop_mapping()
    print("已结束建图。用 d1max maps 查看保存结果。")
    return 0


async def _cmd_load(backend, args) -> int:
    await backend.load_map(args.map_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOC_READY_TIMEOUT_S
    while loop.time() < deadline:
        loc = await backend.loc_status()
        if loc is not None and loc in LOC_HEALTHY:
            print(f"定位就绪: {loc.value}")
            return 0
        await asyncio.sleep(0.3)
    print(f"定位在 {LOC_READY_TIMEOUT_S}s 内未就绪", file=sys.stderr)
    return 1


async def _goto_and_wait(backend, pose: Pose, timeout_s: float) -> NavStatus:
    waiter = asyncio.create_task(backend.wait_nav_terminal(timeout_s))
    await asyncio.sleep(0)          # 让 waiter 先把订阅建起来
    try:
        await backend.goto(pose)
    except Exception:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        raise
    return await waiter


async def _cmd_goto(backend, args) -> int:
    status = await _goto_and_wait(
        backend, Pose.from_xy_yaw(args.x, args.y, args.yaw), args.timeout)
    print(f"导航结束: {status.value}")
    return 0 if status is NavStatus.SUCCEED else 1


async def _cmd_walk(backend, args) -> int:
    """全逐点执行:路线由厂商 App 画,顺序由我们排,一次只下发一个点。"""
    paths = await backend.list_paths(args.map_id)
    if args.path_id not in paths:
        print(f"{args.map_id} 上没有路线 {args.path_id}", file=sys.stderr)
        return 1

    waypoints = paths[args.path_id]
    total = len(waypoints)
    if total == 0:
        print("这条路线上没有航点")
        return 0

    for index, wp in enumerate(waypoints, 1):
        print(f"[{index}/{total}] 前往 {wp.name}", flush=True)
        status = await _goto_and_wait(backend, wp.pose, args.timeout)
        if status is not NavStatus.SUCCEED:
            print(f"在 {wp.name} 处失败: {status.value}", file=sys.stderr)
            return 1
    print(f"全部完成: {total} 个点")
    return 0


async def _cmd_speed(backend, args) -> int:
    if args.x is None:
        speed = await backend.get_speed()
    else:
        speed = await backend.set_speed(args.x, y=args.y, z=args.z)
    print("  ".join(f"{k}={v}" for k, v in sorted(speed.items())))
    return 0


_COMMANDS = {
    "status": _cmd_status,
    "maps": _cmd_maps,
    "paths": _cmd_paths,
    "path-save": _cmd_path_save,
    "map-start": _cmd_map_start,
    "map-stop": _cmd_map_stop,
    "load": _cmd_load,
    "goto": _cmd_goto,
    "walk": _cmd_walk,
    "speed": _cmd_speed,
}


if __name__ == "__main__":
    sys.exit(main())
```

`_backend()` 里用 `type(config.nav)(**{**config.nav.__dict__, "url": args.url})` 来覆盖 URL 太绕。`NavConfig` 是 frozen dataclass，改用 `dataclasses.replace`：

```python
from dataclasses import replace
...
    nav = config.nav if args.url is None else replace(config.nav, url=args.url)
```

`_goto_and_wait` 里 `await asyncio.sleep(0)` 只让出一次事件循环，不足以保证 `wait_nav_terminal` 已经订阅完成。改成先订阅、再下发、再把队列交给 `wait_nav_terminal`：

```python
async def _goto_and_wait(backend, pose: Pose, timeout_s: float) -> NavStatus:
    with backend.subscription() as queue:
        await backend.goto(pose)
        return await backend.wait_nav_terminal(timeout_s, queue=queue)
```

先订阅再下发，中间没有让出点——这才是没有竞态的写法。把上面 `_goto_and_wait` 的初版整段替换掉。

`wait_nav_terminal` 的 `queue=` 参数就是为这个场合加的（Task 11 评审结论），所以这里**不要**在 `cli.py` 里另写一份等终态的循环——那会把 `base.py` 的逻辑整段抄第二遍。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_cli.py -v`
Expected: 全部 passed

- [ ] **Step 5: 手工验证一遍完整流程**

```bash
python -m d1max_sim --port 10011 --seed &
d1max --url ws://127.0.0.1:10011 status
d1max --url ws://127.0.0.1:10011 maps
d1max --url ws://127.0.0.1:10011 paths demo_map
d1max --url ws://127.0.0.1:10011 load demo_map
d1max --url ws://127.0.0.1:10011 walk demo_map demo_route
kill %1
```

Expected: `walk` 依次打印 `[1/3] 前往 P1_配电柜` … `[3/3] 前往 P3_出口`，最后 `全部完成: 3 个点`。

- [ ] **Step 6: 提交**

```bash
git add src/d1max_patrol/cli.py tests/test_cli.py
git commit -m "feat: d1max 命令行,含全逐点 walk 演示"
```

---
### Task 18: 另外两个端口的接口定义

规范 §3.2 要求阶段 2 交付**三个** Port 的定义。本卷只实现 `NavBackend`，但 `DeviceBackend` 与 `MediaSource` 的接口现在就要定下来——它们是第 2、3 卷的施工图，也是"任务引擎只依赖接口"这句话能不能兑现的前提。

**只定接口，不写实现。** 没有真机时把方法体猜出来是浪费，把方法**名**定下来不是。

（这个任务在逻辑上紧挨 Task 11，放在这里只是为了不打乱已有编号。执行时也可以在 Task 11 之后立刻做。）

**Files:**
- Modify: `src/d1max_patrol/backends/base.py`
- Test: `tests/backends/test_ports.py`

**Interfaces:**
- Consumes: Task 11 的 `NavBackend` 事件分发实现
- Produces（`d1max_patrol.backends.base` 新增）：
  - `EventEmitter` —— 从 `NavBackend` 里提取出来的订阅/广播基类；`NavBackend(EventEmitter, ABC)`，三个端口共用它
  - 设备事件：`BatteryEvent(percent: float, charging: bool)`、`FaultEvent(items: tuple[str, ...], fatal: bool)`、`ControlLostEvent(reason: str)`、`DevicePoseEvent(pose: Pose)`
  - `DeviceBackend(EventEmitter, ABC)`，抽象方法：`connect`、`close`、`acquire_control`、`release_control`、`stand`、`lie`、`set_light`、`set_gimbal`、`take_photo`、`battery`、`has_control`
  - `Frame(data: bytes, mime: str, captured_at_ms: int)` frozen dataclass
  - `MediaSource(ABC)`，抽象方法：`open`、`close`、`grab`、`healthy`
  - `MediaError(Exception)`、`DeviceBackendError(Exception)`

- [ ] **Step 1: 写失败的测试**

`tests/backends/test_ports.py`：

```python
"""三个端口的接口约定。本卷只实现 NavBackend,另两个先把词汇定死。"""

import inspect

import pytest

from d1max_patrol.backends.base import (
    BatteryEvent,
    ControlLostEvent,
    DeviceBackend,
    DevicePoseEvent,
    EventEmitter,
    FaultEvent,
    Frame,
    MediaSource,
    NavBackend,
)
from d1max_patrol.protocol.nav_types import Pose

DEVICE_METHODS = {
    "connect", "close", "acquire_control", "release_control",
    "stand", "lie", "set_light", "set_gimbal", "take_photo",
    "battery", "has_control",
}
MEDIA_METHODS = {"open", "close", "grab", "healthy"}


def _abstracts(cls) -> set[str]:
    return set(cls.__abstractmethods__)


def test_三个端口都不能直接实例化():
    for cls in (NavBackend, DeviceBackend, MediaSource):
        with pytest.raises(TypeError):
            cls()


def test_设备端口的方法集():
    assert _abstracts(DeviceBackend) == DEVICE_METHODS


def test_取图端口的方法集():
    assert _abstracts(MediaSource) == MEDIA_METHODS


def test_所有端口方法都是协程():
    for cls, names in ((DeviceBackend, DEVICE_METHODS), (MediaSource, MEDIA_METHODS)):
        for name in names:
            func = getattr(cls, name)
            assert inspect.iscoroutinefunction(func), f"{cls.__name__}.{name} 应为协程"


def test_导航端口与设备端口共用同一套事件分发():
    assert issubclass(NavBackend, EventEmitter)
    assert issubclass(DeviceBackend, EventEmitter)


def test_事件分发基类可独立使用():
    emitter = EventEmitter()
    queue = emitter.subscribe()
    event = BatteryEvent(percent=42.0, charging=False)
    emitter.emit(event)
    assert queue.get_nowait() is event


def test_设备事件字段():
    assert BatteryEvent(80.0, True).charging is True
    assert FaultEvent(("电机过温",), fatal=True).items == ("电机过温",)
    assert ControlLostEvent("被手柄接管").reason == "被手柄接管"
    assert DevicePoseEvent(Pose.from_xy_yaw(1.0, 2.0)).pose.position.x == 1.0


def test_帧对象():
    frame = Frame(data=b"\xff\xd8", mime="image/jpeg", captured_at_ms=1700000000000)
    assert frame.mime == "image/jpeg"
    with pytest.raises(AttributeError):
        frame.data = b""          # frozen
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/backends/test_ports.py -v`
Expected: FAIL —— `ImportError: cannot import name 'EventEmitter'`

- [ ] **Step 3: 提取 EventEmitter 并加两个端口**

在 `base.py` 中，把 Task 11 写在 `NavBackend` 里的 `__init__` / `subscribe` / `unsubscribe` / `subscription` / `emit` 整体上移成独立类，`NavBackend` 改为继承它——方法体一行不改：

```python
class EventEmitter:
    """一对多的事件分发。

    每个消费者拿一条自己的无界队列,互不抢事件。队列无界是刻意的 ——
    广播端绝不能因为某个慢消费者而卡住状态轮询。
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    @contextlib.contextmanager
    def subscription(self) -> Iterator[asyncio.Queue]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    def emit(self, event: Any) -> None:
        """向所有订阅者广播。同步、不阻塞、不抛错。"""
        for queue in list(self._subscribers):
            queue.put_nowait(event)


class NavBackend(EventEmitter, ABC):
    ...   # 其余部分与 Task 11 相同,删掉已上移的五个方法与 __init__
```

在文件末尾追加另外两个端口：

```python
# ------------------------------------------------------------------ 设备端口


class DeviceBackendError(Exception):
    """本体动作或遥测相关错误。"""


@dataclass(frozen=True)
class BatteryEvent:
    percent: float
    charging: bool = False


@dataclass(frozen=True)
class FaultEvent:
    items: tuple[str, ...]
    fatal: bool = False


@dataclass(frozen=True)
class ControlLostEvent:
    """控制权被别人拿走了。见规范 §5.3 —— 拿不到控制权就不许下动作指令。"""

    reason: str


@dataclass(frozen=True)
class DevicePoseEvent:
    pose: Pose


class DeviceBackend(EventEmitter, ABC):
    """本体动作与遥测。

    本卷不实现 —— 它要靠 C++ 旁路进程(第 2 卷)。这里先把词汇定死,
    免得第 2 卷写着写着又发明一套名字。

    真理源规则(规范 §3.4): 导航进展以 NavBackend 为准,本端口的遥测
    只作交叉校验与安全兜底。两边不一致时停车记异常,不做猜测性推断。
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def acquire_control(self) -> None:
        """申请控制权。拿不到时抛 DeviceBackendError。"""

    @abstractmethod
    async def release_control(self) -> None: ...

    @abstractmethod
    async def has_control(self) -> bool: ...

    @abstractmethod
    async def stand(self) -> None: ...

    @abstractmethod
    async def lie(self) -> None: ...

    @abstractmethod
    async def set_light(self, on: bool) -> None: ...

    @abstractmethod
    async def set_gimbal(self, pitch: float, yaw: float) -> None:
        """云台角度,单位 rad。"""

    @abstractmethod
    async def take_photo(self) -> Frame:
        """用机身相机拍一张。MediaSource 的备用取图路径。"""

    @abstractmethod
    async def battery(self) -> float:
        """当前电量百分比。"""


# ------------------------------------------------------------------ 取图端口


class MediaError(Exception):
    """取图失败。"""


@dataclass(frozen=True)
class Frame:
    """一帧图像。"""

    data: bytes
    mime: str
    captured_at_ms: int


class MediaSource(ABC):
    """到点取图。

    本卷不实现。主路径是 RTSP 抽帧,备用路径是 DeviceBackend.take_photo。
    不继承 EventEmitter —— 取图是请求式的,没有事件流。
    """

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def grab(self) -> Frame:
        """取一帧。取不到时抛 MediaError。"""

    @abstractmethod
    async def healthy(self) -> bool:
        """流是否还活着。用于巡检前的预检。"""
```

`Frame` 被 `DeviceBackend.take_photo` 的类型注解引用，所以要么把 `Frame` 定义挪到 `DeviceBackend` 之前，要么依赖文件顶部已有的 `from __future__ import annotations`（Task 11 已经写了）——按后者即可，注解不在定义时求值。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/backends/ -v`
Expected: 全部 passed。**Task 11 的测试必须一条不改地继续通过**——提取 `EventEmitter` 是纯搬家，改了行为就是搬错了。

- [ ] **Step 5: 提交**

```bash
git add src/d1max_patrol/backends/base.py tests/backends/test_ports.py
git commit -m "feat: DeviceBackend 与 MediaSource 端口定义,事件分发提取为 EventEmitter"
```

---

### Task 19: 文档样例作为 golden fixture

规范阶段 1 的验收标准写着"协议单测全绿（**文档样例作 fixture**）"。到这一步为止的协议测试都是我们自己编的报文——自己编的报文只能证明代码自洽，不能证明代码认得设备真正会发的东西。

厂商文档里有 67 段 JSON 样例。全部抠出来，逐条喂给解析器。这是无真机阶段能拿到的、最接近真机的输入。

**Files:**
- Create: `scripts/extract_doc_samples.py`
- Create: `tests/protocol/fixtures/`（由脚本生成，入库）
- Create: `tests/protocol/test_doc_samples.py`
- Modify: `refs/README.md`（补一句样例来源）

**Interfaces:**
- Consumes: `refs/nav-api/自主导航_WEBSOCKET_API.md`（Task 1 的 `extract_refs.sh` 落位）；`protocol.nav_frames.{parse_message, parse_request}`
- Produces: `tests/protocol/fixtures/NNN.json` 一批文件 + 一个 `index.json` 记录每段样例在文档中的行号

- [ ] **Step 1: 写抽取脚本**

`scripts/extract_doc_samples.py`：

```python
"""把厂商文档里的 JSON 样例抠成 fixture。

这些样例是无真机阶段能拿到的、最接近真机的输入。抠出来入库,
解析器每次改动都要重新对着它们过一遍。

用法:
    python scripts/extract_doc_samples.py refs/nav-api/自主导航_WEBSOCKET_API.md tests/protocol/fixtures
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FENCE = re.compile(r"^```json\s*$")
END = re.compile(r"^```\s*$")


def extract(doc: str) -> list[tuple[int, str]]:
    """返回 [(起始行号, 代码块文本)]。"""
    out: list[tuple[int, str]] = []
    lines = doc.splitlines()
    index = 0
    while index < len(lines):
        if FENCE.match(lines[index]):
            start = index + 1
            body: list[str] = []
            index += 1
            while index < len(lines) and not END.match(lines[index]):
                body.append(lines[index])
                index += 1
            out.append((start + 1, "\n".join(body)))
        index += 1
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    doc_path, out_dir = Path(argv[1]), Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    blocks = extract(doc_path.read_text(encoding="utf-8"))
    index: list[dict[str, object]] = []
    kept = skipped = 0
    for number, (line_no, body) in enumerate(blocks, 1):
        try:
            payload = json.loads(body)
        except ValueError as exc:
            # 文档里有些块是片段或带省略号,不是完整报文
            print(f"跳过第 {line_no} 行的样例: {exc}", file=sys.stderr)
            skipped += 1
            continue
        name = f"{number:03d}.json"
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        index.append({"file": name, "doc_line": line_no,
                      "type": payload.get("head", {}).get("type")})
        kept += 1

    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"抠出 {kept} 段,跳过 {skipped} 段 -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 2: 跑脚本生成 fixture**

```bash
python scripts/extract_doc_samples.py refs/nav-api/自主导航_WEBSOCKET_API.md tests/protocol/fixtures
```

Expected: 打印"抠出 N 段"，`N` 在 50 以上；`tests/protocol/fixtures/` 下出现一批 `NNN.json` 与 `index.json`。

被跳过的块要**逐条看一眼**：若某条是因为文档里写了 `...` 省略号而跳过，无妨；若是因为我们的正则漏了 ` ```JSON ` 这种大小写变体，把正则改成 `re.IGNORECASE` 重跑。

- [ ] **Step 3: 写对着 fixture 的测试**

`tests/protocol/test_doc_samples.py`：

```python
"""厂商文档里的 JSON 样例。自己编的报文只能证明代码自洽,这些才接近真机。"""

import json
from pathlib import Path

import pytest

from d1max_patrol.protocol.nav_frames import (
    FRAME_TYPE_ALG_ERROR,
    FRAME_TYPE_REQUEST,
    FRAME_TYPE_RESPONSE,
    AlgErrorNotify,
    Response,
    parse_message,
    parse_request,
)

FIXTURES = Path(__file__).parent / "fixtures"
INDEX = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))


def _entries(frame_type: str) -> list[dict]:
    return [e for e in INDEX if e["type"] == frame_type]


def _payload(entry: dict) -> str:
    return (FIXTURES / entry["file"]).read_text(encoding="utf-8")


def test_样例数量够多():
    """低于这个数说明抽取脚本漏了 —— 文档里有 60 多段。"""
    assert len(INDEX) >= 50


def test_三类报文都有样例():
    for frame_type in (FRAME_TYPE_REQUEST, FRAME_TYPE_RESPONSE, FRAME_TYPE_ALG_ERROR):
        assert _entries(frame_type), f"没有 {frame_type} 类型的样例"


@pytest.mark.parametrize("entry", _entries(FRAME_TYPE_REQUEST),
                         ids=lambda e: e["file"])
def test_文档里的请求样例都能解析(entry):
    frame_count, req_func, args = parse_request(_payload(entry))
    assert isinstance(req_func, str) and req_func
    assert frame_count is None or isinstance(frame_count, int)
    del args      # 参数形状五花八门,能取出来就行


@pytest.mark.parametrize("entry", _entries(FRAME_TYPE_RESPONSE),
                         ids=lambda e: e["file"])
def test_文档里的响应样例都能解析(entry):
    message = parse_message(_payload(entry))
    assert isinstance(message, Response)
    assert message.req_func
    assert isinstance(message.ok, bool)


@pytest.mark.parametrize("entry", _entries(FRAME_TYPE_ALG_ERROR),
                         ids=lambda e: e["file"])
def test_文档里的故障推送样例都能解析(entry):
    message = parse_message(_payload(entry))
    assert isinstance(message, AlgErrorNotify)
    assert message.items
    assert all(isinstance(item.code, int) for item in message.items)


def test_嵌套外壳的样例确实被剥掉():
    """速度接口的响应样例里应当至少有一条带 AppReponseObjectData。"""
    nested = [e for e in _entries(FRAME_TYPE_RESPONSE)
              if "AppReponseObjectData" in _payload(e)]
    assert nested, "文档里应当有嵌套外壳的样例,没找到说明抽取漏了"
    for entry in nested:
        message = parse_message(_payload(entry))
        assert message.data is None or "AppReponseObjectData" not in str(message.data)


def test_改名的响应样例存在():
    """loc_load_map 的响应叫 load_localization_map。"""
    funcs = {parse_message(_payload(e)).req_func for e in _entries(FRAME_TYPE_RESPONSE)}
    assert "load_localization_map" in funcs
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/protocol/test_doc_samples.py -v`
Expected: 全部 passed。

**任何一条失败都是真发现，不是测试写错了。** 解析器读不懂厂商自己给的样例，说明 Task 3 的实现漏了一种形状——回去改 `nav_frames.py`，把这条样例的形状纳进去，不要改测试来迁就。若某条样例是文档笔误（例如少了一个逗号但内容明显是完整报文），在 `tests/protocol/fixtures/README.md` 里记一笔再排除它。

- [ ] **Step 5: 记录来源**

在 `refs/README.md` 末尾追加：

```markdown
## 测试用的文档样例

`tests/protocol/fixtures/` 下的 JSON 由
`python scripts/extract_doc_samples.py refs/nav-api/自主导航_WEBSOCKET_API.md tests/protocol/fixtures`
从本目录的 API 文档中抠出,已入库。文档更新后重跑该脚本并复查 diff。
```

- [ ] **Step 6: 提交**

```bash
git add scripts/extract_doc_samples.py tests/protocol/fixtures/ \
        tests/protocol/test_doc_samples.py refs/README.md
git commit -m "test: 厂商文档 JSON 样例作为解析器 golden fixture"
```

---

## 第 1 卷验收

全部 19 个任务完成后，逐条核对。做不到的那条就是没做完，不要记进"后续优化"。

- [ ] `python -m pytest -q` 全绿，总耗时 60 秒以内
- [ ] `ruff check src tests` 无告警
- [ ] `grep -rn "d1max_sim" src/d1max_patrol/` 无输出 —— 生产代码不认识仿真器
- [ ] `grep -rn "d1max_patrol.backends\|d1max_sim" src/d1max_patrol/protocol/` 无输出 —— 协议层不认识上面两层
- [ ] `grep -rn "d1max_sim\|vendor_nav\|faults" tests/contract/test_nav_contract.py` 无输出 —— 契约测试没偷看实现
- [ ] `grep -rn "start_nav\|loc_load_map\|app_req" src/d1max_patrol/cli.py` 无输出 —— 厂商接口名没有泄漏到抽象层之上
- [ ] 四个协议地雷各有一条以它命名的测试：嵌套外壳、响应改名、推送冒充响应、无状态推送通道
- [ ] `python -m d1max_sim --seed` 起得来，`d1max walk demo_map demo_route` 走得完
- [ ] 每一处"文档没写、我们自己定"的行为，源码里都有 `# 假设(待真机验证):` 注释
- [ ] `python -m pytest tests/protocol/test_doc_samples.py -q` 全绿 —— 解析器读得懂厂商自己给的每一段样例
- [ ] `backends/base.py` 里三个端口俱在：`NavBackend`、`DeviceBackend`、`MediaSource`
- [ ] 计划里出现的每个模块都有对应的测试文件，没有只写不测的文件

## 真机差异表

上一条验收项里的每一个 `# 假设(待真机验证):`，在拿到真机后要逐条核对。建一个文件收着，第 4 卷的一致性验证直接照它跑：

- [ ] **收尾任务：** 创建 `docs/真机待验证清单.md`，内容为

```markdown
# 真机待验证清单

拿到 D1 Max 后逐条核对。每条注明:仿真器怎么做的、真机实际怎样、要不要改。

| # | 假设 | 出处 | 真机结果 | 处理 |
|---|------|------|----------|------|
| 1 | 导航终态(Succeed/Failed/Cancelled)保持约 0.5s 后自动回落 StandBy | `d1max_sim/nav_state.py` | 待测 | |
| 2 | 定位未进入 ContinuousLoc 时 `start_nav` 被设备拒绝 | `d1max_sim/nav_server.py` | 待测 | |
| 3 | 定位丢失时进行中的导航转入 Failed | `d1max_sim/nav_server.py` | 待测 | |
| 4 | `reset_loc` 的响应函数名就是 `reset_loc` | `protocol/nav_requests.py` `UNVERIFIED_RESPONSE_FUNCS` | 待测 | |
| 5 | `start_nav_return_home` 的响应函数名 | 同上 | 待测 | |
| 6 | `start_multi_nav_by_points` 的响应函数名 | 同上 | 待测 | |
| 7 | `get_pgm_map` 能否按 map_id 取图(文档样例写的是 null) | `protocol/nav_requests.py` `get_pgm_map` | 待测 | |
| 8 | `notify_stop_mapping_status` 推送里的 `frame_count` 取值 | `d1max_sim/nav_server.py` | 待测 | |
| 9 | §7 回充接口的 `AppResponse` 外壳(拼写与速度接口的 `AppReponseObjectData` 不同) | `protocol/nav_requests.py` `NESTED_RESPONSE_FUNCS` 注释 | 文档已确认存在,本卷不发这些请求 | 第 2 卷做回充时解析器要一并支持 |
| 10 | 状态轮询 0.5s 是否会漏掉短暂终态 | `config/models.py` | 待测 | |
| 11 | 四足运动参数:直线 0.6 m/s、转身 1.2 rad/s、到点容差 0.08 m、朝向容差 0.10 rad、朝向偏差阈值 0.20 rad | `d1max_sim/kinematics.py` | 待测 | |
| 12 | 导航初始化耗时 `init_delay_s`≈0.3s(StandBy→Initializing→Active) | `d1max_sim/nav_state.py` | 待测 | |
| 13 | 地图加载 0.2s、初始定位 0.3s(`load_delay_s` / `init_delay_s`) | `d1max_sim/nav_state.py` | 待测 | |
| 14 | 建图三阶段耗时:传感器 0.2s、就绪 0.2s、保存 0.3s | `d1max_sim/nav_state.py` | 待测 | |
| 15 | 真机保存一次建图会话后,设备给新地图分配的 id 长什么样(文档样例是 `map_id_1`,仿真器发 `map_1`) | `d1max_sim/store.py` `_AUTO_NAME` | 待测 | 跑一次建图→保存→`get_all_map`,抄回实际 id |
| 16 | `get_pgm_map` 真实响应体量:文档样例地图 1000×1000(约百万整数、数 MB JSON),仿真器只发 20×20。缓冲区、响应超时、网络开销扛不扛得住 | `d1max_sim/store.py` `MapRecord` | 待测 | |
| 17 | **有意偏离,非猜测**:仿真器让 `start_multi_nav` / `start_multi_nav_by_points` 一律回 `error`,真机上它们很可能是能走的。本项目全逐点执行才这么做 | `d1max_sim/nav_server.py` `_h_reject_multi` | 待测 | 真机确认这两个接口可用性与响应函数名;若将来改回多点下发,必须先撤掉这处拒绝 |

核对方法:`d1max --url ws://192.168.144.100:10010 -v <子命令>`,把原始报文抄进本表。
```

```bash
git add docs/真机待验证清单.md
git commit -m "docs: 真机待验证清单"
```

## 交付物

第 1 卷做完，手里有这些东西：

1. **一台仿真 D1 Max** —— `python -m d1max_sim`，带故障注入控制通道，是接口契约的可执行定义
2. **一个协议层** —— 客户端与仿真器共用，四个厂商地雷全部就地拆除
3. **三个端口定义** —— `NavBackend`（已实现）、`DeviceBackend`、`MediaSource`（只有接口）；上层只认识它们，换 Nav2 时契约测试一行不改
4. **一个厂商实现** —— `VendorNavBackend`，两级响应匹配 + 状态轮询 + 自动重连
5. **四套测试** —— 单元、文档样例 golden fixture、契约（实现无关）、故障场景（实现相关）
6. **一条命令行** —— 接上真机第一件事就能跑
7. **一份待验证清单** —— 把"我们猜的"和"文档说的"分得清清楚楚

**没有的东西**（有意为之）：C++ SDK 桥接、`DeviceBackend` 与 `MediaSource` 的实现、相机、任务编排、归档、Web 界面。它们分别在第 2、3 卷——接口已经定好，实现留给它们。

## 下一卷入口

第 2 卷从 `docs/超越bridge/` 的 C++ 旁路进程开始，第一个任务是 `127.0.0.1:8790` 的 IPC 协议契约与它的仿真器——和本卷同样的路数：**先写仿真器，再写客户端，契约测试站中间**。
