# fixtures 说明

本目录下的 `NNN.json` 与 `index.json` 全部由
`python scripts/extract_doc_samples.py refs/nav-api/自主导航_WEBSOCKET_API.md tests/protocol/fixtures`
从 `refs/nav-api/自主导航_WEBSOCKET_API.md` 抠出,**不要手工编辑**——手改过的
fixture 就不是"厂商给的样例",是我们自己编的报文。

文档里有 67 段 json 代码块,机械修复(补尾逗号、剥 `//` 行注释,见脚本 `repair()`)
后 61 段可解析,6 段跳过。跳过的 6 段成因不同,分开记:

| 文档行 | 内容 | 跳过原因 | 是不是缺陷 |
|---|------|----------|------------|
| 18 | 报文骨架模板(`"type": "消息类型"`、`"time_stamp": 时间戳(毫秒)`) | 本来就不是报文,是字段说明 | 否 |
| 252 | `get_pgm_map` 响应 | `"data": [0, 1, 2, ...]` 省略号 | 否,文档有意省略 |
| 324 | `get_all_pgm_map` 响应 | `{...}` / `[...]` 省略号 | 否 |
| 466 | `get_all_paths_by_mapid` 响应 | `{...}` / `[...]` 省略号 | 否 |
| 502 | `add_nav_path` 请求 | `{...}` 省略号 | 否 |
| 1011 | `set_navigation_speed` 请求 | 厂商漏了一个右花括号,报文被截断 | **是,厂商笔误** |

第 1011 段是唯一真正的损失(`set_navigation_speed` 的**请求**样例没了)。**没有**靠
补花括号把它救回来——补括号是猜,不是机械修复。损失可接受:这个接口的两段**响应**
样例(第 983、1037 行,对应 `041.json`/`043.json`)都在,嵌套外壳(`AppReponseObjectData`)
这颗地雷长在响应上,不影响回归覆盖——两段外壳都被正确剥掉,`data` 是
`{"x": 0.8, "y": 0.4, "z": 1.2}`。

机械修复用了两条规则,共命中 3 段(记在各自 `index.json` 条目的 `"repaired"` 字段里):

| 文档行 | fixture | 修复 |
|---|---------|------|
| 963 | `040.json` | 尾逗号 |
| 1181 | `049.json` | 行注释 |
| 1671 | `067.json` | 尾逗号 |

第 1671 行那段是全文档**唯一**一段 `alg_error_code_notify` 样例——不修就没有故障
推送链路的真机样例,所以这条修复不是"锦上添花",是让三类报文都有覆盖的必需项。

抽取结果实测:61 段,类型分布 `app_req` 36 / `app_resp` 24 / `alg_error_code_notify` 1。
