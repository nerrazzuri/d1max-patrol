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

## 测试用的文档样例

`tests/protocol/fixtures/` 下的 JSON 由
`python scripts/extract_doc_samples.py refs/nav-api/自主导航_WEBSOCKET_API.md tests/protocol/fixtures`
从本目录的 API 文档中抠出,已入库。文档更新后重跑该脚本并复查 diff。
