# dist —— 带到现场的安装包

这个目录里的东西**是二进制,不是源码**。放进仓库只有一个理由:
现场那台 Ubuntu 笔记本未必装了 Flutter 和 Android SDK,而
`mobile/build/` 是 git 忽略的,`git clone` 拉不到包。把包放这儿,
现场 `git pull` 就有,不用再找 U 盘。

## app-release.apk

| 项 | 值 |
|---|---|
| 大小 | 51 331 578 B（49.0 MB） |
| sha256 | `a484e9dc07b056f99a41cd0521b4409cb8f2c090fd671ae9d3d82dc394c9577f` |
| 编出来的分支/提交 | `feat/交付主线` @ `cf3fa50` |
| 编译命令 | `cd mobile && flutter build apk --release` |
| 编译机 | 开发机（Windows），不是现场笔记本 |
| 编译时间 | 2026-09-11 |

**通用包,三个 ABI（arm64-v8a / armeabi-v7a / x86_64）都在里面**,
不确定手机是什么架构就装它。装法见 `docs/真机现场执行单.md` 的 1.1b。

**签名**:`mobile/android/app/build.gradle` 的 release 配置回落到 debug key
（那几行本来就带着 Flutter 模板的 TODO）。自测够用,**上应用商店不够用**——
真要发客户得另配 keystore,这条记在第 9 卷。

## 不在这儿的东西

- 分 ABI 的三个包（`app-arm64-v8a-release.apk` 等)。要的话按上面的命令
  加 `--split-per-abi` 重编,或者问开发机要。
- 10.13 要的那个**带解码埋点的包**。执行单 1.1b 第 4 条说了,
  那是另一个包,这里没有。
