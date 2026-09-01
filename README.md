# D1 Max 巡检系统

## 要去现场接真机？

**先读 [`docs/真机联调手册.md`](docs/真机联调手册.md)。** 那份文档是自足的：
接网线、配 IP、模拟器彩排、导航只读采集、SDK 侦察、运动测试、要带回来哪些文件，
一条不落。它是写给"带机器出门的那台电脑上的 Claude"看的，按
**Ubuntu 22.04** 写。

## 开发

本仓库的 lint 指令是在仓库根执行不带路径的 `ruff check`；不要写成
`ruff check src tests`，那样新增目录会落在检查之外。

跑测试：`python -m pytest --tb=short`（`pyproject.toml` 里已经有 `-q`，
不要再加一个）。Windows 上先 `$env:PYTHONIOENCODING="utf-8"`。

起仿真器：`python -m d1max_patrol.cli sim --port 10011 --seed`
