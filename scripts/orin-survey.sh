#!/usr/bin/env bash
# =====================================================================
# orin-survey.sh —— 上狗前的勘察：把 Orin NX 上我们需要知道的一次量完
#
#   为什么要有这个脚本：**摸到真机的时间是不可再生的**。把「脑子搬上狗」
#   这件事的每一个未知数（磁盘够不够、Python 什么版本、有没有 Docker、
#   厂商占了哪些端口、Orin 自己有没有无线网卡），现场一条条手敲去问，
#   半天就没了；而漏问一条就要等下一次机会。所以先写好，插上就跑，一次拿全。
#
#   用法：
#     bash scripts/orin-survey.sh robot@192.168.168.100   # 从笔记本 ssh 进去采
#     bash scripts/orin-survey.sh --local                 # 已经在 Orin 上了，直接采
#
#   密码见《SDK 开发指南》—— 会提示输入。嫌烦就先 ssh-copy-id 做免密。
#
#   输出：runs/survey/<时间戳>-orin.txt（同时打屏）。**这个文件要带回来。**
#
#   ⚠ 本脚本【只读】：不装任何东西、不改任何配置、不停任何进程、不在狗上落盘。
#     采集全部走 stdout，由发起端 tee 到本地文件。
#
#   有一条它测不了，得你拿手机测 —— 见末尾「手机侧要单独测的」。
# =====================================================================

# 故意不用 set -e：勘察脚本里任何一条探测失败都不该中断后面的。
# 少问一条的代价是再跑一趟，中途 abort 的代价是整份报告都没了。
set -u

# --------------------------------------------------------------- 发起端
# 带主机名参数时，把脚本自己喂给远端的 bash，远端什么都不用装、不用拷。

if [ "${1:---local}" != "--local" ]; then
  target=$1
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  out="runs/survey/${stamp}-orin.txt"
  mkdir -p runs/survey
  echo "ssh 到 $target 采集，输出同时存到 $out"
  echo "（会提示输密码。Orin NX 默认是 1）"
  echo
  ssh -o ConnectTimeout=10 "$target" 'bash -s -- --local' < "$0" 2>&1 | tee "$out"
  echo
  echo "================================================================"
  echo "采完了。把这个文件带回来：$out"
  echo "================================================================"
  exit 0
fi

# --------------------------------------------------------------- 采集端

say()  { printf '%s\n' "$*"; }
sect() { say ""; say "=================== $* ==================="; }

# run "标题" "shell 命令" —— 命令挂了也照常往下走，输出统一缩进四格。
run() {
  say ""
  say "--- $1"
  local out
  out=$(eval "$2" 2>&1)
  if [ -z "$out" ]; then
    say "    (没有输出)"
  else
    printf '%s\n' "$out" | sed 's/^/    /'
  fi
}

say "================================================================"
say " D1 Max · Orin NX 勘察报告"
say " 采集时间(UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say " 采集时间(本机): $(date 2>/dev/null)"
say "================================================================"

# ------------------------------------------------------------------ 身份
sect "1. 这是台什么机器"

run "主机名"            "hostname; hostname -I 2>/dev/null"
run "内核与架构"        "uname -a"
run "发行版"            "cat /etc/os-release 2>/dev/null | head -8"
run "L4T / JetPack 版本" "cat /etc/nv_tegra_release 2>/dev/null; dpkg -l 2>/dev/null | grep -i nvidia-l4t-core"
run "厂商版本文件"      "cat /opt/release/version.yaml 2>/dev/null"
run "开机多久了"        "uptime"

# ------------------------------------------------------------------ 磁盘
# 录包是 GB 级的。这一段如果不够，建图就只能留在笔记本上，产品形态要改。
sect "2. 磁盘（决定建图能不能在狗上做）"

run "各挂载点"          "df -h"
run "根分区 inode"      "df -i / 2>/dev/null"
run "根分区是不是只读"  "grep ' / ' /proc/mounts"
run "有没有额外的盘"    "lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null"
run "/tmp 是不是内存盘" "grep ' /tmp ' /proc/mounts"
# du 走整棵目录树，堆了录制包的机器上能数好几分钟。掐 20 秒 —— 数不完
# 这件事本身就是个结论（家目录很大），不值得为它耗着现场的时间。
run "家目录占用"        'timeout 20 du -sh "$HOME" 2>/dev/null || echo "(20 秒没数完 —— 家目录很大,多半堆着录的包)"'

# ------------------------------------------------------------------ 算力
sect "3. 算力与内存余量"

run "CPU"               "nproc; grep -m1 'model name' /proc/cpuinfo 2>/dev/null; cat /proc/device-tree/model 2>/dev/null"
run "内存"              "free -h"
run "当前负载"          "uptime; top -bn1 2>/dev/null | head -12"
run "功耗档位"          "nvpmodel -q 2>/dev/null"
run "GPU"               "nvidia-smi 2>/dev/null | head -12"

# ------------------------------------------------------------------ Python
sect "4. Python（我们整个 app 就靠它）"

run "python3"           "which python3; python3 --version"
run "装了哪几个 python" "ls -1 /usr/bin/python3.* 2>/dev/null"
run "venv 能不能用"     "python3 -c 'import venv; print(\"venv ok\")'"
run "pip"               "python3 -m pip --version 2>/dev/null"
run "已装的关键包"      "python3 -m pip list 2>/dev/null | grep -iE 'websockets|pyyaml|numpy|pytest'"

# ------------------------------------------------------------------ 网络
# 手机要够到这台机器，全看这一段。
sect "5. 网络（决定手机连不连得上）"

run "网卡与地址"        "ip -br addr 2>/dev/null || ifconfig -a"
run "路由表"            "ip route 2>/dev/null || route -n"
run "有没有无线网卡"    "ls -d /sys/class/net/*/wireless 2>/dev/null; iw dev 2>/dev/null"
run "无线固件/驱动"     "lsmod 2>/dev/null | grep -iE 'cfg80211|mac80211|wl|iwl|rtw|mt7'"
run "USB 设备"          "lsusb 2>/dev/null"
run "在听哪些端口"      "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null"
run "到 RK3588 通不通"  "ping -c2 -W2 192.168.168.168 2>&1 | tail -3"
run "能不能出外网"      "curl -sS -m 8 -o /dev/null -w 'pypi 返回 %{http_code}\n' https://pypi.org/simple/ 2>&1"
run "DNS"               "cat /etc/resolv.conf 2>/dev/null | grep -v '^#'"

# ------------------------------------------------------------------ 厂商栈
sect "6. 厂商自己在跑什么（别踩到它）"

run "厂商相关进程"      "ps aux 2>/dev/null | grep -iE 'ros|nav|slam|robot|zenoh|lidar|camera' | grep -v grep"
run "systemd 服务"      "systemctl list-units --type=service --state=running --no-pager 2>/dev/null | head -40"
run "自定义 unit"       "ls -1 /etc/systemd/system/*.service 2>/dev/null"
run "robot-launch 在哪" "which robot-launch 2>/dev/null; ls -d /opt/robot* /opt/agibot* 2>/dev/null"

# ------------------------------------------------------------------ ROS2
sect "7. ROS2"

run "装了哪些发行版"    "ls -1 /opt/ros 2>/dev/null"
run "环境变量"          "env | grep -E '^ROS|^RMW|^ZENOH' | sort"
run "rmw 实现"          "ls -1 /opt/ros/humble/lib 2>/dev/null | grep -i rmw | head"
run "话题数（10 秒超时）" "timeout 10 bash -lc 'source /opt/ros/humble/setup.bash 2>/dev/null; ros2 topic list 2>/dev/null | wc -l'"
run "建图相关包在不在"  "ls -d /opt/ros/humble/share/slam_toolbox /opt/ros/humble/share/nav2_map_server /opt/ros/humble/share/pointcloud_to_laserscan 2>/dev/null"

# ------------------------------------------------------------------ 容器
sect "8. Docker（OTA 刷掉之后，容器是最快的重装方式）"

run "docker"            "docker --version 2>/dev/null; docker info 2>/dev/null | grep -E 'Storage Driver|Docker Root|Total Memory'"
run "在跑的容器"        "docker ps 2>/dev/null"
run "当前用户在 docker 组吗" "id"
run "有没有 podman"     "podman --version 2>/dev/null"

# ------------------------------------------------------------------ 权限
sect "9. 权限与自启（我们的东西要开机就在）"

run "我是谁"            "id; groups"
run "sudo 要不要密码"   "sudo -n true 2>&1 && echo '免密 sudo 可用' || echo '需要密码'"
run "能写哪些地方"      'for d in /opt /usr/local /etc/systemd/system "$HOME"; do printf "%s: " "$d"; if [ -w "$d" ]; then echo 可写; else echo 不可写; fi; done'

# ------------------------------------------------------------------ SDK
sect "10. 厂商 SDK 与相机"

# 全盘 find 在盘大的机器上能跑上好几分钟。现场的时间比这条结果值钱，
# 所以掐 30 秒，并且先翻最可能的几个地方。
run "SDK 动态库"        "ls /opt/*/lib/*/librobot_sdk* /usr/local/lib/librobot_sdk* 2>/dev/null; timeout 30 find /opt /home /usr/local -maxdepth 6 -name 'librobot_sdk*' 2>/dev/null | head"
run "ffmpeg"            "ffmpeg -version 2>/dev/null | head -2"
run "RTSP 端口通不通"   "timeout 3 bash -c '</dev/tcp/192.168.234.1/8554' 2>&1 && echo '8554 通' || echo '8554 不通（可能要先连热点网段）'"

# ------------------------------------------------------------------ 结论
sect "关键结论（先看这几条，它们决定架构）"

# 中文一个字三字节，而 printf 的 %-22s 数的是字节 —— 拿它对齐只会更歪。
verdict() { printf '  %s：%s\n' "$1" "$2"; }

# 优先 --output=avail：df 第一列的设备名可能含空格，按列号取会整体错位，
# 而错位的后果是把「已用」当成「可用」报出去 —— 那比没量到还糟。
free_root=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')
[ -z "${free_root:-}" ] && free_root=$(df -BG / 2>/dev/null | awk 'END {print $(NF-2)}' | tr -dc '0-9')
if [ -n "${free_root:-}" ] && [ "$free_root" -ge 50 ] 2>/dev/null; then
  verdict "磁盘余量" "${free_root}G —— 够，建图可以搬上狗"
elif [ -n "${free_root:-}" ] && [ "$free_root" -ge 15 ] 2>/dev/null; then
  verdict "磁盘余量" "${free_root}G —— 偏紧，能跑 app 但录包要外接存储"
elif [ -n "${free_root:-}" ]; then
  verdict "磁盘余量" "${free_root}G —— ⚠ 不够，建图只能留在别处，产品形态要重想"
else
  verdict "磁盘余量" "没量出来，翻上面第 2 节"
fi

pyv=$(python3 --version 2>&1 | awk '{print $2}')
case "${pyv:-}" in
  3.1[0-9]*) verdict "Python" "$pyv —— 对得上，我们的代码直接跑" ;;
  3.9*|3.8*) verdict "Python" "$pyv —— ⚠ 太老，要么装新的要么改代码，这条影响工期" ;;
  "")        verdict "Python" "⚠ 没有 python3" ;;
  *)         verdict "Python" "$pyv —— 手工确认一下" ;;
esac

if command -v docker >/dev/null 2>&1; then
  verdict "Docker" "有 —— OTA 刷掉之后重装最省事，优先走容器"
else
  verdict "Docker" "没有 —— 部署走安装脚本 + systemd，得自己保证可重放"
fi

if ls -d /sys/class/net/*/wireless >/dev/null 2>&1; then
  verdict "Orin 无线网卡" "有 —— 万一手机够不到，可以让 Orin 自己开热点"
else
  verdict "Orin 无线网卡" "没有 —— 手机只能靠 RK3588 那个热点转发过来"
fi

for p in 8090 8092 8095; do
  if ss -tln 2>/dev/null | grep -q ":$p "; then
    verdict "端口 $p" "⚠ 已被占用，我们要换端口"
  else
    verdict "端口 $p" "空着，可以用"
  fi
done

if curl -sS -m 8 -o /dev/null https://pypi.org/simple/ 2>/dev/null; then
  verdict "出外网" "通 —— 可以直接 pip install"
else
  verdict "出外网" "不通 —— 我要提前准备 aarch64 的离线 wheel 包"
fi

say ""
say "--- 这个脚本测不了、要你拿手机单独测的"
say "    1. 手机连上狗热点 XG2WIFI_C40221（密码见手册），浏览器开"
say "       http://192.168.168.100:10010 —— 有任何反应就算通。"
say "       ★ 这条决定「脑子搬上狗、手机当遥控器」这个架构成不成立。"
say "       笔记本要手工加一条 ip route 才够得到 Orin，而手机加不了；"
say "       但手机的默认网关就是 RK3588，很可能本来就转发过去了。"
say "    2. 不通的话，在手机上 ping 192.168.234.1（RK3588 本身）确认热点是通的，"
say "       好把「热点没连上」和「转发不过去」这两件事分开。"
say ""
say "================================================================"
say " 采集结束"
say "================================================================"
