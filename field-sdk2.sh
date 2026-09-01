#!/usr/bin/env bash
# =====================================================================
# D1 Max 现场离线脚本 · S 阶段 SDK 侦察 第二趟（退 App 后重试）
#
#   ★ 只订阅传感数据，机器【绝对不会动】。不发运动指令。★
#
# 前提：你已在【手机/平板上退出厂商 App】（本人已同意）。
# 本趟覆盖：SDK 0.1.1 与 0.2.0  ×  端口 8081 与 8082  = 四种组合全试。
#   （0.1.1 是版本表对上机器 RK3588=0.2.4 的那版，放前面先试）
#
# 用法（真实终端）：  bash field-sdk2.sh
# 会问：sudo 密码。跑完自动连回网，把 ~/sdk-data2.log 整段贴回给 Claude。
# 跑完请记得【把厂商 App 重新连回机器狗】，恢复安全垫。
# =====================================================================
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"
DOG_SSID="XG2WIFI_C40221"; DOG_PASS="12345678"; HOME_SSID="Always Robots_5G"
LOG=~/sdk-data2.log
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1
say(){ echo; echo "========== $* =========="; }

cat <<'BANNER'
============================================================
 S 阶段 · SDK 侦察 第二趟 —— 机器【不会动】，只订阅数据
 这一趟需要【厂商 App 已退出】。退出后机器只受 SDK 管，
 但 data 例子只订阅数据、不发任何运动指令，机器不会动。
 跑完记得把 App 重新连回去。
============================================================
BANNER
read -r -p ">> 厂商 App 确认【已经退出】了吗？退出了按回车继续（否则 Ctrl-C）... " _

say "0. 预热 sudo"
sudo -v || { echo "!! sudo 认证失败"; exit 1; }

say "1. 临时关掉 $HOME_SSID 自动连接"
sudo nmcli con mod "$HOME_SSID" connection.autoconnect no 2>/dev/null && echo "已临时关闭（收尾恢复）"

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
[ "$CONNECTED" = yes ] || echo "!! 没连上 $DOG_SSID"
sleep 3
ip -br addr show wlp59s0

say "3. 加路由"
sudo ip route del 192.168.168.0/24 2>/dev/null
sudo ip route add 192.168.168.0/24 via 192.168.234.1 && echo "路由已加" || echo "!! 路由失败"

say "4. 跑 data：两个版本 × 两个端口，全试一遍（App 已退出）"
echo "   data 会依次订阅 运控/20Hz速度/关节 各约 2 秒后自退。"
echo "   看到 [DATA]/传感字段刷出来 = 成功；ShakeHand failed = 仍被拒。"
for V in 0.1.1 0.2.0; do
  BIN="$ROOT/refs/robot-sdk/RobotSDK-$V/example/build/data"
  LIB="$ROOT/refs/robot-sdk/RobotSDK-$V/lib/x86_64"
  for PORT in 8081 8082; do
    echo
    echo "-------------------- SDK $V  ×  端口 $PORT  →  ./data 192.168.234.1 $PORT --------------------"
    if [ -x "$BIN" ]; then
      LD_LIBRARY_PATH="$LIB" timeout 25 "$BIN" 192.168.234.1 "$PORT" 2>&1
      echo "(SDK $V @ $PORT 退出码: $?  —— 0=正常跑完, 124=25s超时被杀, 其它=连不上/报错)"
    else
      echo "!! $V 的 data 不存在"
    fi
  done
done

say "5. 收尾：恢复上网"
sudo nmcli con mod "$HOME_SSID" connection.autoconnect yes 2>/dev/null
sudo nmcli con up "$HOME_SSID" 2>&1
echo
echo "===================================================================="
echo " 1) 别忘了把厂商 App 重新连回机器狗（恢复安全垫）"
echo " 2) 把这个文件整段贴回给 Claude：  $LOG"
echo "===================================================================="
