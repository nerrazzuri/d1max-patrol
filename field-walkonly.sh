#!/usr/bin/env bash
# =====================================================================
# field-walkonly.sh —— 只负责“走动”（假设机器【已经站着】）
#   不重启、不重新站起。连上→拿控制权→SwitchRemoteState→Gait→Move→停。
#
#   用法：
#     bash field-walkonly.sh                 # 前进 1.0s，量 0.11
#     bash field-walkonly.sh 1.7             # 前进 1.7s（约1米）
#     bash field-walkonly.sh 1.0 0.2         # 前进 1.0s，量 0.2（更快）
#     bash field-walkonly.sh 1.0 0 0 0.2     # 原地左转 1.0s（fwd=0,lat=0,yaw=0.2）
#     bash field-walkonly.sh 1.0 0 0.15 0    # 左平移
#   参数顺序：<秒> <前进fwd> <侧移lat> <转向yaw>  （符号同官方例子）
#
#   前提：机器已经站着；App 关掉；周围+前方按走动方向留足空地；急停在手。
# =====================================================================
set -u
cd "$(dirname "$0")"
DOG=192.168.234.1; SSID=XG2WIFI_C40221; PASS=12345678
BIN=./motion/walkonly
SEC="${1:-1.0}"; FWD="${2:-0.5}"; LAT="${3:-0.0}"; YAW="${4:-0.0}"   # fwd 默认 0.5（0.11 太慢几乎不动）
mkdir -p runs/field-logs
LOG="runs/field-logs/walkonly-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo " 只走动：Move(lat=$LAT, fwd=$FWD, yaw=$YAW) 共 ${SEC}s"
echo " 前提：机器已站着 / App 已关 / 按方向留足空地 / 急停在手"
echo "============================================================"
read -r -p ">> 确认？回车走，Ctrl-C 退出... " _
[ -x "$BIN" ] || { echo "!! 缺 $BIN"; exit 1; }

# 确保连在狗热点
cur=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2}')
if [ "$cur" != "$SSID" ]; then
  nmcli dev wifi rescan 2>/dev/null; sleep 3
  nmcli dev wifi connect "$SSID" password "$PASS" 2>&1 | tail -1; sleep 2
fi
ping -c1 -W2 $DOG >/dev/null 2>&1 || { echo "!! 机器狗不可达"; exit 1; }

timeout 60 "$BIN" $DOG 8082 "$SEC" "$FWD" "$LAT" "$YAW" 2>&1 | grep -vE "current_state"
echo "(walkonly exit: ${PIPESTATUS[0]})"
echo "日志：$LOG"
echo "走不动的话：优先把前进量调大（fwd 0.11 太慢几乎不动，0.3~0.5 才明显走）。"
echo "例如：bash field-walkonly.sh 1.0 0.5"
