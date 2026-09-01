#!/usr/bin/env bash
# =====================================================================
# field-stand.sh —— 让 D1 Max 站起来（StandUp）
#
#   ★ 只做 StandUp：原地站立、腿会动、【不行走、不平移】。★
#
# 固件坑：SDK 会话一次开机只能连一次，连过再断就锁死，必须重启才能再连。
# 所以本脚本先【重启 RK3588】清锁，再用重启后的第一条连接站起来。
#
# 前提（跑之前务必做到）：
#   1) 厂商 App 彻底关闭、且不会自动重连；遥控器关机。
#   2) 周围（含机器【下方】）清空，人站安全侧，【急停在手】。
#
# 用法：bash field-stand.sh    会问：SSH 密码 bot、sudo 密码 bot、两次回车确认。
# =====================================================================
set -u
cd "$(dirname "$0")"
DOG=192.168.234.1
SSID=XG2WIFI_C40221; PASS=12345678
BIN=./motion/stand2
mkdir -p runs/field-logs
LOG=runs/field-logs/stand-$(date -u +%Y%m%dT%H%M%SZ).log
exec > >(tee -a "$LOG") 2>&1
say(){ echo; echo "===== $* ====="; }

cat <<'B'
============================================================
 让 D1 Max 站起来（StandUp）—— 腿会动、原地站立、不行走。
 本脚本会【先重启机器狗】清掉锁死的控制会话，再站起。
 确认：App 已关且不会自动重连 / 遥控器关机 /
       周围含机器下方已清空 / 人在安全侧 / 急停在手。
============================================================
B
read -r -p ">> 以上都确认了？回车继续，Ctrl-C 退出... " _
[ -x "$BIN" ] || { echo "!! 缺 $BIN，需要先编译 stand2"; exit 1; }

say "1. 确保 WiFi 连在机器狗上（以便 SSH 进去重启）"
for i in $(seq 1 10); do
  cur=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2}')
  [ "$cur" = "$SSID" ] && { echo "已在 $SSID"; break; }
  nmcli dev wifi rescan 2>/dev/null; sleep 3
  nmcli dev wifi connect "$SSID" password "$PASS" 2>&1 && break
done
ping -c1 -W2 $DOG >/dev/null 2>&1 && echo "机器狗可达 ✓" || echo "机器狗暂不可达"

say "2. 重启 RK3588 清锁（会问 SSH 密码=bot，随后 sudo 密码也=bot）"
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
[ "$up" = 1 ] || { echo "  !! 等超时，机器可能还没起来，稍后重跑。"; exit 1; }
echo "  给运控服务 8 秒启动时间..."; sleep 8

say "4. 最后确认：马上要站起来了"
read -r -p ">> 周围仍清空、急停在手？回车=站起，Ctrl-C=放弃... " _

say "5. 单会话站起（连上→拿控制权→SwitchRemoteState 激活电机→StandUp→观察→交还）"
timeout 60 "$BIN" $DOG 8082
echo "(stand2 退出码: $?)"

say "完成"
echo "日志：$LOG —— 把上面输出整段贴给 Claude。"
echo "若仍报 Controlled denial of service：说明重启后有别的客户端(多半是 App)先连了；"
echo "彻底关掉 App 再重跑本脚本。"
