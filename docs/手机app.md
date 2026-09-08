# 手机 app（`mobile/`）

最终形态是**不带笔记本**：脑子跑在狗自己的 Orin NX 上，手机当遥控器。
这个目录是那个遥控器。

只做 Android。Flutter 3.47.2 / Dart 3.13.2。

---

## 现在做到哪儿了

**只有名册这一屏。** 登记我手上有哪几只狗、各自的 SN、热点名、PIN。

**操作界面那一层故意还没做**，因为有个岔路口要现场才知道走哪边：

机器人身上已经跑着一个功能完整的网页版 app（`src/d1max_patrol/app/`，建图、
标点、编任务、跑任务、判读、出报告全在里面，二十多个接口）。问题只是
**手机浏览器打不打得开、好不好用**：

| 现场答案 | 这个 app 要做的 |
|---|---|
| 好用 | 名册 + 连热点 + 存 PIN + 把那个页面嵌进 WebView。接口一个不用重写 |
| 不好用 | 才需要在这里原生重做那二十几个接口的界面 |

答案在 `START-HERE.md` 的"摸到机器的头两件事"里，现场滑三十秒就有。
**在那之前不猜，先把无论哪边都跑不掉的那部分做扎实 —— 就是名册。**

---

## 为什么名册必须在手机这边

每只 D1 Max 的内网地址**都一模一样**：RK3588 `192.168.168.168`、
Orin `192.168.168.100`、WiFi 侧 `192.168.234.1`。这是机器里写死的，不是我们
配的。所以：

- **地址分不出谁是谁。** 三只狗，三个一模一样的网址。
- **一次只能连一只。** 手机连的是狗自己开的热点，同一时刻只在一个热点上。
  所以名册不是"同时监控 N 只"的面板，是"我要去哪只"的选单。
- **只有手机知道全局。** 每只狗只知道自己是谁（`GET /api/identity`），
  它不认识别的狗，也没道理认识 —— 客户买三只，三只之间没有网络往来。

服务端那边对应的实现在 `src/d1max_patrol/app/identity.py`，同一套道理。

## 名册里存什么

以 **SN 为主键**。每条：

| 字段 | 是什么 |
|---|---|
| `sn` | 机身序列号。机器上跑 app 时用 `D1MAX_SN` 填的那个。不能为空 |
| `name` | 现场喊的名字，比如"三号"。可以不填 |
| `ssid` | 热点名，形如 `XG2WIFI_C40221` |
| `bssid` | 热点的 MAC。**连上之前就看得见**，所以能先认出是谁再决定拿哪个 PIN |
| `host` / `port` | 服务端在哪，默认 `192.168.168.100:8095` |

**PIN 不在这张表里。** 它走系统密钥库（`SecurePinVault`，Android Keystore
后面的 `EncryptedSharedPreferences`），名册文件里从头到尾看不见它 ——
名册会被导出、会被贴进工单、会跟着备份走，钥匙跟进去一次就收不回来。
`test/store_test.dart` 里有一条测试直接去读落到盘上的那串字节，确认搜不到
PIN。

几条刻意做出来的选择：

- **重复登记会被拦住，不静默覆盖。** 覆盖掉的是已经调好的那条，而界面上
  看起来一模一样，人不会发现。改已有的要点那一条进去改。
- **一个 SSID 对上两只狗时不许随便挑一只**，直接把歧义顶到界面上。挑错了，
  操作员就在错的狗上按了"跑"，而他不会知道。
- **删一只，PIN 跟着忘掉。** 条目没了钥匙还留着，是一份界面上看不见、
  因此谁也不会想起来删的残留。改 SN 也算换了一只，同理。
- **名册文件坏了要明说，不能装成一本空的。** 装成空的，人就会以为自己从来
  没登记过，然后重新填一遍，而坏掉的文件还躺在盘上。
- **写是先写临时文件再改名。** 手机随时没电，写到一半的 JSON 下次打开就是
  "我的狗全没了"。

---

## 在这台机器上怎么跑

Flutter 装在 `D:\toolchain\flutter`（不在 PATH 里，每次显式加）。
Android SDK 和 JDK 用 Android Studio 自带的那套。

```powershell
$env:PATH = "D:\toolchain\flutter\bin;$env:PATH"
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio1\jbr"
cd "D:\Projects\D1 Max\d1max-patrol\mobile"

flutter analyze     # 期望：No issues found!
flutter test        # 期望：56 条全过，一秒出头
flutter build apk --debug
```

**这套测试跟 Python 那套是两码事，各跑各的。** Python 的 `pytest` 不会碰
`mobile/`，`flutter test` 也不碰 `src/`。

### 踩过的坑

- **Dart 的标识符不认中文。** 测试名（字符串）可以是中文，变量名不行 ——
  写了会报 `illegal_character`。所以数据用 `_sanHao` 这种拼音名。
- **仓库路径里有空格**（`D1 Max`）**不是问题**，APK 照样编得出来，
  连 NDK r28c 装在带空格的 SDK 路径下也没事。不用搬。
- **`flutter_secure_storage` 卡在 v9。** v11 要求 `compileSdk = 37`，
  而 AGP 9.1 官方推荐上限是 36，本机也只有 android-37.1 这个预览版平台。
  交付给客户的东西不压在预览 SDK 上。等 AGP 正式支持 37 再升。
- **v9 的 `encryptedSharedPreferences: true` 必须显式打开。** 这个插件在
  Android 上的默认行为是普通 SharedPreferences，也就是**明文**。不写那一行，
  PIN 就是纯文本躺在私有目录里，整个 `SecurePinVault` 等于白写。

---

## 文件

| 文件 | 职责 |
|---|---|
| `lib/model/robot.dart` | 名册里的一条。JSON 往返、兜底 SN 判定、BSSID 归一 |
| `lib/model/registry.dart` | 名册本体。按 SN / SSID / BSSID 找狗，拒重，歧义上报 |
| `lib/store/registry_store.dart` | 名册怎么存（原子写的 JSON 文件 + 测试用内存实现） |
| `lib/store/pin_vault.dart` | PIN 存取的口子 + 测试用内存实现 |
| `lib/store/secure_pin_vault.dart` | 真机上的 PIN 库，落在 Android Keystore 后面 |
| `lib/store/paths.dart` | 名册文件在手机上的位置（app 私有 documents 目录） |
| `lib/ui/roster_page.dart` | 名册界面。存取全靠外面传进来，所以整屏能离线测 |
| `lib/main.dart` | 入口，把真实现接上去 |

---

## 接下来

1. **周一现场答那个岔路口的问题**（网页版在手机上好不好用）。
2. 按答案做操作界面：嵌 WebView，或者原生重做。
3. 连热点这一步现在还要手动去系统设置里点。Android 10 以后
   `WifiNetworkSpecifier` 可以让 app 自己发起连接，值得做 —— 现场一只只
   切热点，手动点是真的烦。
4. 服务端那两个接口的客户端还没写：`POST /api/auth`（PIN 换 token）和
   `GET /api/identity`（确认面前这只跟名册上选的是同一只）。契约见
   `src/d1max_patrol/app/auth.py`：token 走 `Authorization: Bearer`，
   闲置 12 小时过期（在狗自己的热点上是 30 分钟），只在内存里 ——
   狗一重启就要重新解锁。

---

## 解锁之后还有第二道闸：控制权

**拿到 token 只等于「能看」，不等于「能动」。** 一只狗同时只有一个人能改变
它正在做什么，那份权限叫 **L1 控制权**，要单独去取（`POST /api/control/acquire`），
30 秒到期，每 10 秒发一次 `POST /api/control/heartbeat` 续。

做操作界面时这三条会直接撞上来：

- **受控接口在没取控制权时回 HTTP 409**，错误文案是「先取控制权」。遥控走一
  拍、起任务、暂停／中止、换地图、设初始位姿都在这一档。**409 要照文案分**：
  闸门说的那句是「去取控制权」，业务说的那句（比如「没在跑」）是别的意思 ——
  两者状态码一样。
- **一条不存在的受控路径现在也回 409，不是 404。** 闸门排在路由匹配之前，跟
  「没解锁时一律先 401 而不是 404」是同一条立场：不给未授权的人当存在性探针。
  所以 app 别拿 404 来探「这台狗支不支持某个接口」，探不出来。
- **看和急停永远不要控制权。** 画面、状态、报告随便看；`POST /api/estop` 任何
  时候都按得下去，包括控制权在别人手上、包括只读凭证。

全部规矩（三个时间尺度、接管怎么走、热点上为什么只发只读凭证）见
`docs/鉴权与控制权.md`。
