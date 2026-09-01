#!/usr/bin/env bash
# =====================================================================
# D1 Max 现场离线脚本 · S 阶段 SDK 侦察   (走 WiFi / 单网卡)
#
#   ★ 只订阅传感数据，机器【绝对不会动】。不发运动指令、不碰厂商 App。★
#
# 会做三件事：
#   1) 连上机器狗热点 + 加路由
#   2) SSH 抄两台主机的平台版本号（决定该信哪个版本 SDK 的输出）
#   3) 跑 SDK 的 data 例子（订阅：运控/20Hz速度/关节），两个版本都试
#
# 用法（真实终端里）：  bash field-sdk.sh
# 期间会问：sudo 密码、两次 SSH 密码（脚本里已提示是哪个）。
# 跑完自动连回 Always Robots_5G，把 ~/sdk-data.log 整段贴回给 Claude。
# =====================================================================
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"
DOG_SSID="XG2WIFI_C40221"; DOG_PASS="12345678"; HOME_SSID="Always Robots_5G"
LOG=~/sdk-data.log
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1
say(){ echo; echo "========== $* =========="; }

cat <<'BANNER'
============================================================
 S 阶段 · SDK 侦察 —— 机器【不会动】，只订阅数据
 安全前提：请保持厂商 App 连着。App 连着时 SDK 抢不到控制权，
 这是今天的安全垫。本脚本不发任何运动指令，也不会退出 App。
============================================================
BANNER
read -r -p ">> 厂商 App 确认还连着吗？连着按回车继续（否则 Ctrl-C 退出）... " _

say "0. 预热 sudo（连狗/加路由要用）"
sudo -v || { echo "!! sudo 认证失败"; exit 1; }

say "1. 临时关掉 $HOME_SSID 的自动连接"
sudo nmcli con mod "$HOME_SSID" connection.autoconnect no 2>/dev/null && echo "已临时关闭（收尾会恢复）"

say "2. 连接机器狗热点 $DOG_SSID"
CONNECTED=no
for i in $(seq 1 12); do
  nmcli dev wifi rescan 2>/dev/null; sleep 3
  if nmcli -f SSID dev wifi list 2>/dev/null | grep -q "$DOG_SSID"; then
    echo "第 $i 轮：看到热点，连接中..."
    nmcli dev wifi connect "$DOG_SSID" password "$DOG_PASS" 2>&1 && { CONNECTED=yes; break; }
  else
    echo "第 $i 轮：还没扫到，重试..."
  fi
done
[ "$CONNECTED" = yes ] || echo "!! 没连上 $DOG_SSID（离机器近点再重跑）"
sleep 3
ip -br addr show wlp59s0

say "3. 加路由（导航主机 Orin NX 在 192.168.168 网段）"
sudo ip route del 192.168.168.0/24 2>/dev/null
sudo ip route add 192.168.168.0/24 via 192.168.234.1 && echo "路由已加" || echo "!! 路由失败"

say "4. 抄两台主机平台版本号（清单 #19/#30，决定用哪版 SDK）"
echo ">>> 这里会问 SSH 密码。RK3588 (robot@192.168.234.1) 的密码是:  bot"
ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new robot@192.168.234.1 \
    'echo "[RK3588  /opt/release/version.yaml]"; cat /opt/release/version.yaml' 2>&1 \
    || echo "(RK3588 SSH 没连上或没这文件,跳过)"
echo
echo ">>> 接着问 Orin NX (robot@192.168.168.100) 的密码，是:  1"
ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new robot@192.168.168.100 \
    'echo "[Orin NX  /opt/release/version.yaml]"; cat /opt/release/version.yaml' 2>&1 \
    || echo "(Orin NX SSH 没连上或没这文件,跳过)"

say "5. 跑 SDK data 例子（端口 8081,WiFi 侧地址 192.168.234.1）——两个版本都试一遍"
echo "   data 例子会依次订阅：运控数据 / 20Hz 速度 / 关节状态，各约 2 秒后自动退出。"
echo "   哪个版本能连上并吐数据，就是机器该配的那版（另一版报错是正常的，也是答案）。"
for V in 0.2.0 0.1.1; do
  echo
  echo "-------------------- SDK $V : ./data 192.168.234.1 8081 --------------------"
  BIN="$ROOT/refs/robot-sdk/RobotSDK-$V/example/build/data"
  LIB="$ROOT/refs/robot-sdk/RobotSDK-$V/lib/x86_64"
  if [ -x "$BIN" ]; then
    LD_LIBRARY_PATH="$LIB" timeout 25 "$BIN" 192.168.234.1 8081 2>&1
    rc=$?
    echo "(SDK $V data 退出码: $rc  —— 0=正常跑完, 124=25s超时被杀, 其它=连不上/报错)"
  else
    echo "!! $V 的 data 二进制不存在（B4 没编好）"
  fi
done

say "6. 收尾：恢复上网"
sudo nmcli con mod "$HOME_SSID" connection.autoconnect yes 2>/dev/null
sudo nmcli con up "$HOME_SSID" 2>&1
echo
echo "===================================================================="
echo " 上网恢复后，请把这个文件整段贴回给 Claude："
echo "     $LOG"
echo "===================================================================="
