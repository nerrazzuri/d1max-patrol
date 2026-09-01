#!/usr/bin/env bash
# =====================================================================
# field-walk.sh —— 让 D1 Max 站起来并【前进一小段】做标定
#
#   ★ 会站起 + 向前走一小段（默认 0.5 秒、gentle 0.11）。会平移！★
#   前方留出至少 1 米空地。
#
# 固件坑：SDK 一次开机只能连一次，所以先重启清锁，再在单会话里
#         激活→站起→前进→停，并测出实际走了多远。
#
# 前提：厂商 App 彻底关闭且不自动重连；遥控器关机；
#       前方及四周（含机器下方）清空；人在安全侧；急停在手。
#
# 用法：bash field-walk.sh [前进秒数=0.5]     会问 SSH 密码 bot、两次回车确认。
# =====================================================================
set -u
cd "$(dirname "$0")"
DOG=192.168.234.1
SSID=XG2WIFI_C40221; PASS=12345678
BIN=./motion/walk1
SEC="${1:-0.5}"
mkdir -p runs/field-logs
LOG=runs/field-logs/walk-$(date -u +%Y%m%dT%H%M%SZ).log
exec > >(tee -a "$LOG") 2>&1
say(){ echo; echo "===== $* ====="; }

cat <<B
============================================================
 D1 Max：站起 + 前进 ${SEC}s（会向前平移！前方留 ≥1 米空地）
 本脚本先重启机器狗清锁，再单会话 激活→站起→前进→停。
 确认：App 已关且不自动重连 / 遥控器关机 /
       前方及四周(含机器下方)清空 / 人在安全侧 / 急停在手。
============================================================
B
read -r -p ">> 都确认了？回车继续，Ctrl-C 退出... " _
[ -x "$BIN" ] || { echo "!! 缺 $BIN"; exit 1; }

say "1. 确保 WiFi 连在机器狗上"
for i in $(seq 1 10); do
  cur=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2}')
  [ "$cur" = "$SSID" ] && { echo "已在 $SSID"; break; }
  nmcli dev wifi rescan 2>/dev/null; sleep 3
  nmcli dev wifi connect "$SSID" password "$PASS" 2>&1 && break
done
ping -c1 -W2 $DOG >/dev/null 2>&1 && echo "机器狗可达 ✓" || echo "机器狗暂不可达"

say "2. 重启 RK3588 清锁（SSH 密码 bot，sudo 密码 bot）"
ssh -t -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 robot@$DOG 'sudo reboot' 2>&1 \
  || echo "(连接已断/命令已发，正常)"

say "3. 等机器狗重启（先掉线再上线，约 1-2 分钟）"
echo -n "  等待下线 "
for i in $(seq 1 40); do ping -c1 -W1 $DOG >/dev/null 2>&1 || { echo "已下线"; break; }; echo -n .; sleep 2; done
echo -n "  等待上线 "
up=0
for i in $(seq 1 90); do
  nmcli dev wifi rescan 2>/dev/null >/dev/null
  nmcli -t -f SSID dev wifi 2>/dev/null | grep -q "$SSID" \
    && nmcli dev wifi connect "$SSID" password "$PASS" >/dev/null 2>&1
  if ping -c1 -W1 $DOG >/dev/null 2>&1; then echo "已上线 ✓"; up=1; break; fi
  echo -n .; sleep 2
done
[ "$up" = 1 ] || { echo "  !! 等超时，稍后重跑。"; exit 1; }
echo "  给运控服务 8 秒启动时间..."; sleep 8

say "4. 最后确认：马上站起并前进 ${SEC}s"
read -r -p ">> 前方≥1米空地、周围清空、急停在手？回车=开始，Ctrl-C=放弃... " _

say "5. 单会话：激活→站起→前进 ${SEC}s→停→测距"
timeout 90 "$BIN" $DOG 8082 "$SEC"
echo "(walk1 退出码: $?)"

say "完成"
echo "日志：$LOG —— 把上面输出整段贴回给 Claude（尤其那行 位移/里程/峰值速度）。"
