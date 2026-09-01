# 官方资料目录

本目录存放厂商原始资料。**除 `urdf/` 外全部入 git**，所以 clone 下来就是全套，
出差那台笔记本不需要手工拷贝任何东西。

    refs/
      nav-api/自主导航_WEBSOCKET_API.md      # 自主导航 WebSocket JSON 协议（28 KB，已入库）
      robot-sdk/RobotSDK-0.2.0/              # C++ SDK v0.2.0：头文件、示例、文档（12 MB，已入库）
      robot-sdk/RobotSDK-0.1.1/              # C++ SDK v0.1.1：老平台用（7.9 MB，已入库）
      hardware/                              # 《D1 Max SDK 开发指南 V0.1.0》PDF + 正文提取（3.8 MB，已入库）
      urdf/max_description/                  # URDF 与网格（252 MB，**不入库**）

`urdf/` 是唯一的例外：252 MB，其中 126 MB 是 meshes，另有一份完全重复的
install 拷贝；现场用不到。真要用就跑 `scripts/extract_refs.sh` 从原始压缩包
（`3.自主导航二开资料.zip` 等，由项目负责人分发）解出来。

## 为什么两个 SDK 版本都在

厂商的 SDK 与机器平台固件是**强绑定**的，配错了连不上（见
`robot-sdk/RobotSDK-0.2.0/README_zh.md` 的版本对照表）：

| 平台固件版本 | 能用的 SDK |
|---|---|
| `0.3.0(V4)` | 仅 0.2.0 |
| `0.1.0-C` … `0.2.4(V4)` | 仅 ≤0.1.1 |

到现场先查平台版本（`cat /opt/release/version.yaml`），再决定用哪个。
两个都入库了，省得现场缺哪个都得重新要人发。

## 硬件资料里最要紧的几条

`hardware/D1Max-SDK开发指南V0.1.0-正文提取.txt` 是 PDF 的纯文本提取
（PDF 本身也在，但很多机器上没装 poppler，Claude 读不了图，文本版更保险）。
接线、IP、RTSP 地址都在里面，**接机器前先读 `docs/真机联调手册.md` 的 C 阶段**，
那里已经把结论抄好了。

## 代码里怎么引用

代码与计划中引用官方导航接口时，一律以
`refs/nav-api/自主导航_WEBSOCKET_API.md` 的章节号为准（例如"§3.1 开始单点导航"）。

## 测试用的文档样例

`tests/protocol/fixtures/` 下的 JSON 由
`python scripts/extract_doc_samples.py refs/nav-api/自主导航_WEBSOCKET_API.md tests/protocol/fixtures`
从本目录的 API 文档中抠出，已入库。文档更新后重跑该脚本并复查 diff。
