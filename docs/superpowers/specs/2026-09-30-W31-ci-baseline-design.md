# W31 CI 基线 · 设计稿

> 状态：**按推荐**（外审 2026-09-30 建议；用户 2026-09-30「三项都按你推荐的来」—— 决策 20：开了必过检查之后走「分支 → PR → CI 过了再合」，分支保护是仓库设置、
> 由用户开）。外审复查 W09 之后：「可以现在开始 W31 …… 先纳入确定性测试和 lint，端口、Mosquitto、ROS、MOLA、Flutter 等较慢或依赖环境的测试先分层运行，
> 不要一开始全部设成 required check」。性质：工程门禁，不阻断别的工单。

## 摸底（2026-09-30）

- 仓库在 GitHub（`nerrazzuri/d1max-patrol`，**公开** —— Actions 的标准运行器免费），没有 `.github/workflows`；一直直推 `master`。
- Python 3.10（Ubuntu 22.04 与狗上一致）；根包 + `packages/*` 六个包，开发机用 `.venv` 可编辑安装；测试入口 `pytest`（`pyproject.toml` 的 `testpaths`），
  约 3500 条、开发机约 13 分钟。依赖外部程序的测试（mosquitto、ffmpeg、g++、ROS、bash）没有这个程序就**自己跳过**。
- ruff 只管 `packages`、`src`、`tests`、`tools/w09f_live_preview_check.py`（`tools/` 下其余是老的现场脚本，不在 lint 范围）。
- 手机：Flutter 3.47.5，`flutter test --concurrency=1` 约 1–2.5 分钟。
- 见过的偶发失败：手机 `live_video_test`「页面被盖住之后不再占着看画面的位子」（全量时一次）、站点 `test_site_supervise::test_心跳不刷审计_只记开始和结束`（全量时一次）。

## 决定（都按推荐）

1. **一个工作流** `.github/workflows/ci.yml`，推 `master`、开 PR 时跑；同一分支的新提交把旧的那次取消掉。第三方 action 只用 GitHub 官方的
   `actions/checkout`、`actions/setup-python`，**按提交哈希钉死**；Flutter 不用第三方 action，按标签从官方仓库拉 SDK。
2. **分层**：
   - **必过候选**（确定性，运行器上不装 mosquitto / ffmpeg / ROS，依赖环境的测试自己跳过）：
     `lint`（ruff）、`py-contract`（契约、两个适配器、定位器）、`py-agent`（代理）、`py-site`（站点）、`py-core`（根目录 `tests/`：老应用、部署脚本静态检查、打包）。
   - **手机** `flutter`：`flutter analyze` + `flutter test --concurrency=1`。
   - **集成层** `integration`：装上 mosquitto、ffmpeg、g++ 跑全量 Python（真 broker、真推流、C++ 速度闸）—— **不设必过**（`continue-on-error`），先看稳不稳。
   - ROS / MOLA 相关的（建图、定位器真跑）不上 CI：运行器上没有 ROS，这些测试自己跳过；真链路留给开发机工具与真机项。
3. **偶发失败**：先跑，记录；要是在 CI 上复现，查根因修掉（不加重试把它藏起来）。这一版先把已见过的两条查一遍能修的修掉。
4. **开分支保护**（决策 20，仓库设置由用户开）：CI 在几次提交上都稳了之后，把 `lint`、`py-contract`、`py-agent`、`py-site`、`py-core` 设成 `master` 的必过检查；
   `flutter` 稳了再加；`integration` 不加。保留管理员紧急处置。开了之后我这边改走分支 → PR → 过了再合。
5. **不做**：覆盖率门槛、多 Python 版本矩阵、Windows / macOS、发布流水线、CI 上跑 ROS。

## 测试

CI 本身就是测试：第一次推上去看每个 job 过没过、各多久；在开发机上用干净的虚拟环境按工作流的装法装一遍、跑一遍（不带 ROS 环境变量），确认装法对。
