# D1 Max 巡检系统

## 要去现场接真机？

**读 [`START-HERE.md`](START-HERE.md)** —— 里面有一段可以直接贴给笔记本上那个
Claude 的开场指令，贴完它就自己往下跑了。

细节在 [`docs/真机联调手册.md`](docs/真机联调手册.md)，那份文档是自足的：
怎么连（走 WiFi 热点最省事，实测够用）、配 IP、模拟器彩排、导航只读采集、
SDK 常驻会话、运动控制，一条不落。按 **Ubuntu 22.04** 写。

**2026-09-01 已经跑过一趟真机。** 结论回灌在手册和
[`docs/真机待验证清单.md`](docs/真机待验证清单.md) 的 #34–#49 —— 那份清单
现在是最新事实的所在地，和手册冲突时以清单为准。

厂商资料已经全部入库在 [`refs/`](refs/README.md)（`refs/urdf/` 除外），
**clone 就是全套，不用手工拷贝任何东西。**

## 开发

本仓库的 lint 指令是在仓库根执行不带路径的 `ruff check`；不要写成
`ruff check src tests`，那样新增目录会落在检查之外。

跑测试：`python -m pytest --tb=short`（`pyproject.toml` 里已经有 `-q`，
不要再加一个）。Windows 上先 `$env:PYTHONIOENCODING="utf-8"`。

起仿真器：`python -m d1max_patrol.cli sim --port 10011 --seed`
