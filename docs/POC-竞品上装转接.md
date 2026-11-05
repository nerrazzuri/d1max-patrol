# POC：借竞品上装的警灯、喇叭（anbot_ros1 转接）

来源：用户 11-05 批准，这次放开「这周不接外部设备」。V2 上装到之前，POC 先借竞品上装的爆闪和播音，网页、手机上原来的「开警灯 / 喊话」照用。

代码：`packages/adapter-d1max/src/nexwar_adapter_d1max/anbot.py`。

## 怎么接

- 竞品主控跑 ROS1，master 在 `http://192.168.113.203:11311`。
- Orin 在 enP8p1s0 上的地址是 192.168.113.159，跟竞品主控同一网段，网线直连。
- Orin 上不装 ROS1：代理里用标准库实现最小的 ROS1 发布端（XML-RPC 注册 + TCPROS 握手）。

| 我们的 | 竞品话题 | 类型 | 竞品订阅端 |
|---|---|---|---|
| 警灯 | `/sensor/strobeLightSwitch` | std_msgs/Bool | led_control（两边爆闪一起） |
| 喊话 | `/anbot/voice/play` | std_msgs/String | anbotControl（讯飞 TTS 念文字） |

Orin 上 `/etc/nexwar/payload.json`：

```json
{"kind": "anbot_ros1", "master": "http://192.168.113.203:11311", "host": "192.168.113.159"}
```

可选项：`secs_per_char`（缺省 0.25）、`republish_s`（缺省 1）、`reregister_s`（缺省 10）、`strobe_topic`、`voice_topic`、`clips_dir`（缺省 `/var/lib/nexwar/clips`）、`aplay_device`（缺省 `dmix_oneway_play`）。

### 英文话术（录好的）

竞品自带的讯飞只有中文音库，英文用录好的 wav：代理 SSH 到竞品主控（192.168.113.101，账号 firefly），跑 `aplay -q -D dmix_oneway_play -`，wav 从管道送过去，文件不用拷到竞品上。这条跟竞品自己的讯飞 TTS 走同一个 dmix。

- **登录用密码**（用户 11-05 定：不改竞品任何设置）。竞品 `/home/firefly` 是 777，sshd 不认钥匙，改它就等于改竞品。
  - 密码放在 Orin 的 `/etc/nexwar/env`（600 root），三项：`NEXWAR_ANBOT_SSH_HOST`、`NEXWAR_ANBOT_SSH_USER`、`NEXWAR_ANBOT_SSH_PASSWORD`。systemd 起代理时交给它。
  - ssh 通过 `SSH_ASKPASS` 从环境变量取密码：不进命令行、不落盘、不进日志、不进仓库。
  - 不需要 sshpass、paramiko。三项不齐就报错；一项都没配，就只念文字。
- 话术放在 Orin 的 `/var/lib/nexwar/clips/<名>.wav`（属主 robot）。用户选定男声 Ryan，4 段：

| 名 | 内容 | 什么时候放 |
|---|---|---|
| blocked | Excuse me, my path is blocked. Please make way. Thank you. | 被挡住走不过去，自动放 |
| passing | Excuse me, security patrol robot passing through. | 走着时看到 3 个人以上，自动放 |
| distance | Please keep a safe distance from the robot. Thank you. | 有人进 2 米，自动放 |
| restricted | Attention. This is a restricted area. Please leave immediately. | 禁区驱离第 2 级（另存一份 `warn-en.wav`，站点驱离自动轮放） |

- 合成：Piper `en_US-ryan-medium`（rhasspy/piper-voices v1.0.0），参数 `--length-scale 1.1 --sentence-silence 0.3`。再用 ffmpeg 转成 48 kHz 双声道 s16，前后各加 0.3 秒静音：`-af "adelay=300|300,apad=pad_dur=0.3" -ar 48000 -ac 2 -sample_fmt s16`。

### 网页、App

- **网页**：机器人详情 → 控制 →「警灯与喊话」：
  - 警灯：时长选 30/60/120/300 秒，开、关；
  - 打字喊中文；
  - 4 个英文话术按钮；
  - 停止。
- **App**：单机器人页 →「上装…」，同样的功能（1.0.0-rc.1+4 起）。
- 保安、管理员能用；业主看不到。

### 特定时候自动放（代理自己判，断网也放）

见 `packages/robot-agent/src/nexwar_agent/autovoice.py`：
- 被挡：导航报 `nav_blocked` 时放 `blocked`，挡着每 20 秒再放一次；
- 有人进 2 米：放 `distance`，30 秒内不重复；
- 走着看到 3 个人以上：放 `passing`，60 秒内不重复。

规矩：
- 只放机器人上有的那段：没拷那个文件，就等于没开这一条。
- 优先级 40，比手动（60）、驱离（80）都低，正在放更要紧的就不插。
- 「有人」靠人员检测（W24/W74），没起来就只有「被挡」这一条。

## 行为

- **警灯**：开着每 1 秒重发一次「开」，防止竞品自己的程序发「关」把我们盖掉。到点、点关、代理退出或断开，都发一次「关」。关着就不再发，不跟竞品自己的用法抢。
- **打字喊话**（中文，竞品讯飞念）：
  - 念完没有回执，按字数估：每字 0.25 秒。
  - **念到一半停不下来**：没找到停播的话题。「关喇叭」只是不再发新的。
- **录好的话术**：放的时候点「停止」能掐断。放完、放坏了、到点都有回执。
- **竞品主控没开或没连上**：代理照常起来，不卡。
  - 喊话当场报错，站点那头看得到没喊出去。
  - 警灯开着却发不到竞品那头，报 `payload_strobe` 故障。
  - 设备健康里报 `link:anbot` 离线。
  - 竞品主控起来后 10 秒内自己重新注册。
- **没有**：警笛、聚光灯、云台、`/anbot/light/control`（取值没验过）。

## 风险（POC 说明里要写）

1. **竞品主控得一直开着。** 它的 dog_control 能抢机器人的运动控制，10-19 撞玻璃门就是它。
2. **音量**：用户定 70%（`amixer -c 1` 的 DACL、DACR 都是 70%，-6 dB），**没存盘**：竞品主控重启会恢复原样，要重新调。
   - 我们的代码不改竞品的音量，也不改它的任何设置。
3. **登录竞品主控**：POC 阶段用户同意，只用来放录好的话术。密码只在 Orin 的 `/etc/nexwar/env`。
4. **竞品在 Orin 上的桥（parameter_bridge，把前雷达转给竞品）**：11-05 用户同意停掉，省约 3–4% 的整机 CPU（单核 29%）。
   - 按 PID 停的，PID 记在 Orin `~/bridge-pids-1105.txt`；自启没改，Orin 重启它会回来。
   - 停了以后，警灯、喊话、`link:anbot` 都照常。
   - 竞品自己的定位、避障可能用不了。POC 期间本来就不让竞品带机器人跑。
5. **视频走 SRT（UDP）**：经不了 SSH 隧道，站点主机和机器人要在同一个网里。站点 `site.json` 里 `"video": {"srt_host": "<站点主机在这个网里的地址>"}`；换网（比如改连手机热点）要跟着改。

## 上装相机（11-05）

Orin 上 `/etc/nexwar/cameras.json`（root:robot 0640，里面有口令，不拷出 Orin）：

```json
{"thermal": {"url": "rtsp://<账号>:<口令>@192.168.113.113:554/ch1/main/av_stream", "record": false},
 "payload": {"url": "rtsp://<账号>:<口令>@192.168.113.103:554/ch1/sub/av_stream", "record": false}}
```

- 热像 384×288 用 main；可见光用 sub，经中继 Wi-Fi 推 1080p 太重。
- 网页画面上「热像」「上装」二选一，App 视频页能切 4 路。两路都不录。
- 11-05 现场：4 路出画面，用户说不太卡、能接受；看视频时 Orin 整机 CPU 约 54–57%。

## 现场试（笔记本，机器人不用动）

装上这个分支的代理，写好上面的 payload.json，重启代理。

| # | 做什么 | 看什么 |
|---|---|---|
| 1 | 代理起来 | 日志「上装配置 …:接了 strobe、speaker」；网页设备健康 `link:anbot` 在线 |
| 2 | 网页开警灯 30 秒 | 两边爆闪亮；30 秒后自己灭 |
| 3 | 开警灯 → 马上点关 | 灭 |
| 4 | 开警灯 → 停代理（systemctl stop） | 灭 |
| 5 | 网页、手机各喊一句 | 竞品念出来；手机上喊话状态约 字数×0.25 秒后结束 |
| 6 | 拔竞品网线（或竞品主控关机）后喊一句 | 网页当场说没喊出去；接回 10 秒内恢复 |
